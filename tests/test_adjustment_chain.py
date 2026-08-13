# -*- coding: utf-8 -*-
"""窗口复权链的行为测试。

这个模块的产出会被下游当作「这段价格序列复权一致」的凭证，所以错一个因子不会报错，
只会让某只股票的历史阻力位悄悄偏移。这里测的重点不是能不能跑通，而是几件容易出错
又不会自己暴露的事：

1. 累乘方向对不对（是 close(t-1)/pre_close(t)，不是反过来）
2. 有没有归一到窗口末日 D（末行因子必须恒为 1，否则入场价不等于真实可成交价）
3. 除权日之后因子有没有保持住（写成赋值而非累乘就会掉回 1）
4. volume 有没有反向缩放、amount 有没有被误伤
5. 越界判定是按方向而不是按幅度（按幅度收上界会拒绝合法的大比例送转拆股）
6. 断链是切段而不是整只作废，且断链点之后照常施加
7. 缺 pre_close 有没有被悄悄按 1.0 补上（部分复权的序列比拒绝更糟）
8. 窗口内现算与全历史现算是否给出同一个 g —— 整套「只看窗口」的承重假设

不变式用的是 ``f(t)*pre_close(t) == f(t-1)*close(t-1)``，等价于
``close_adj(t)/close_adj(t-1) == close(t)/pre_close(t)``。注意**不是**
``close(t-1) == pre_close(t)``：复权后前者带因子、后者是原始价，那个等式不成立。
"""

import numpy as np
import pandas as pd
import pytest

from src.services import adjustment_chain as chain_module
from src.services.adjustment_chain import (
    ANOMALOUS_SOURCE,
    BREAK_MISSING_PRE_CLOSE,
    BREAK_RATIO_ABOVE_CEILING,
    BREAK_RATIO_BELOW_FLOOR,
    RATIO_CORRUPTION_CEILING,
    TRUSTED_SOURCE,
    analyze_window,
    apply_adjustment,
    compute_window_factors,
)


def _frame(closes, pre_closes, *, volumes=None, code="600001", start=2):
    """造一个单只股票、按日期升序的窗口。

    OHL 由 close 按固定比例派生，只是为了验证它们和 close 走同一个系数；
    amount 由「原始价 × 原始量」给出，用来钉住价 × 量守恒。
    """
    count = len(closes)
    if volumes is None:
        volumes = [1_000_000.0] * count
    return pd.DataFrame(
        {
            "code": [code] * count,
            "date": [pd.Timestamp(f"2024-01-{start + i:02d}") for i in range(count)],
            "open": [None if c is None else c * 0.99 for c in closes],
            "high": [None if c is None else c * 1.02 for c in closes],
            "low": [None if c is None else c * 0.97 for c in closes],
            "close": closes,
            "pre_close": pre_closes,
            "volume": volumes,
            "amount": [
                None if c is None else c * v for c, v in zip(closes, volumes)
            ],
        }
    )


def _assert_invariant(raw, adjusted):
    """f(t)*pre_close(t) == f(t-1)*close(t-1)，逐行核对。

    断链处两侧有一侧的因子是 NaN，跳过——那正是「这一段与 D 之间没有可信换算关系」
    的表达，不是漏测。
    """
    factors = adjusted["adj_factor"].to_numpy(dtype=float)
    close = raw["close"].to_numpy(dtype=float)
    pre_close = raw["pre_close"].to_numpy(dtype=float)
    checked = 0
    for index in range(1, len(raw)):
        if np.isnan(factors[index]) or np.isnan(factors[index - 1]):
            continue
        assert factors[index] * pre_close[index] == pytest.approx(
            factors[index - 1] * close[index - 1], rel=1e-12
        ), f"不变式在第 {index} 行不成立"
        # 等价形式：复权后的日收益必须等于原始日收益。
        assert adjusted["close"].iloc[index] / adjusted["close"].iloc[
            index - 1
        ] == pytest.approx(close[index] / pre_close[index], rel=1e-12)
        checked += 1
    assert checked, "这条用例没有核对到任何一行，断言写空了"


# ── 1. 基线与归一 ────────────────────────────────────────────────────
def test_factor_stays_one_without_any_ex_dividend():
    """pre_close(t) 恒等于 close(t-1) 的窗口，因子必须全程为 1。

    基线：这条红了说明累乘方向或首行初值就是错的，后面所有断言都没有意义。
    """
    frame = _frame([10.0, 11.0, 12.0, 13.0], [9.9, 10.0, 11.0, 12.0])

    factors = compute_window_factors(frame)

    assert list(factors) == [1.0, 1.0, 1.0, 1.0]


def test_last_row_factor_is_exactly_one():
    """窗口末日因子恒为 1 —— 入场价必须等于当日真实可成交价。

    不归一时末行会带上整段累乘值，回测的 entry_close 就不再是那天真正能成交的价格，
    而前瞻 bar 取自原始表，两者尺度不同，收益直接算错。
    """
    frame = _frame(
        [10.0, 10.0, 9.2, 9.5, 4.8],
        [9.9, 10.0, 9.0, 9.2, 4.75],
    )

    factors = compute_window_factors(frame)

    assert factors.iloc[-1] == 1.0
    # 除权前的 bar 被缩到今天的价格尺度上，因此严格小于 1。
    assert all(value < 1.0 for value in factors.iloc[:-1])


def test_single_ex_dividend_lifts_earlier_bars_and_holds_the_level():
    """除权日之前的 bar 整体降到 1/ratio，且「降下去」要一直保持。

    「保持」是这条用例真正吃重的部分：把累乘写成赋值，跳变那一天照样正确，
    但再往前一天就会回到 1，等于除权前后的价格又被放到同一把尺子上比。
    """
    frame = _frame([10.0, 10.0, 9.2, 9.5], [9.9, 10.0, 9.0, 9.2])

    factors = compute_window_factors(frame)

    level = 9.0 / 10.0
    assert factors.iloc[0] == pytest.approx(level)
    assert factors.iloc[1] == pytest.approx(level)
    assert factors.iloc[2] == 1.0
    assert factors.iloc[3] == 1.0


def test_two_ex_dividends_compound():
    """两次除权必须相乘，而不是只留下最近一次。"""
    frame = _frame([10.0, 9.0, 9.0, 8.0], [10.0, 8.0, 9.0, 7.2])

    factors = compute_window_factors(frame)

    # f = [1, 1.25, 1.25, 1.5625]，除以 f(D)=1.5625
    assert list(factors) == pytest.approx([0.64, 0.8, 0.8, 1.0])


def test_invariant_holds_across_multiple_events():
    frame = _frame(
        [10.0, 10.0, 9.2, 9.5, 4.8, 4.9],
        [9.9, 10.0, 9.0, 9.2, 4.75, 4.8],
    )

    adjusted = apply_adjustment(frame)

    _assert_invariant(frame, adjusted)


# ── 2. 施加范围：价格、成交量、成交额 ─────────────────────────────────
def test_all_price_columns_share_one_factor():
    frame = _frame([10.0, 10.0, 9.2, 9.5], [9.9, 10.0, 9.0, 9.2])

    adjusted = apply_adjustment(frame)

    factors = adjusted["adj_factor"].to_numpy(dtype=float)
    for column in ("open", "high", "low", "close", "pre_close"):
        assert adjusted[column].to_numpy() == pytest.approx(
            frame[column].to_numpy() * factors
        ), f"{column} 没有跟着同一个因子走"


def test_volume_is_scaled_inversely_and_amount_is_untouched():
    """volume 除以 g，amount 原样保留。

    除权前的 bar 以「老股本」计量成交量，要放大到今天的股本口径才可比；送转日
    volume 台阶式跳变，不处理会让 volume_ratio（最新量 / 前 5 日均量）被虚增 2~3 倍。
    amount 不处理不是省事——价 × 量守恒（price*g 乘 volume/g），单独缩放反而破坏它。
    """
    frame = _frame(
        [20.0, 20.0, 10.5, 11.0],
        [19.5, 20.0, 10.0, 10.5],
        volumes=[1_000_000.0, 1_000_000.0, 2_000_000.0, 2_100_000.0],
    )

    adjusted = apply_adjustment(frame)

    # 10 送 10：除权前的 bar 因子为 0.5，成交量必须翻倍才与除权后可比。
    assert list(adjusted["adj_factor"]) == pytest.approx([0.5, 0.5, 1.0, 1.0])
    assert list(adjusted["volume"]) == pytest.approx(
        [2_000_000.0, 2_000_000.0, 2_000_000.0, 2_100_000.0]
    )
    assert list(adjusted["amount"]) == pytest.approx(list(frame["amount"]))
    # 守恒关系必须在复权后的表里仍然自洽。
    assert list(adjusted["close"] * adjusted["volume"]) == pytest.approx(
        list(adjusted["amount"])
    )


def test_volume_ratio_is_no_longer_inflated_on_a_bonus_share_day():
    """送转日的 volume_ratio 从 2 倍虚增回到真实水平。

    直接钉住 factor_service 里那个「最新量 / 前 5 日均量」的算式，因为这才是
    虚增真正落地的地方。
    """
    volumes = [1_000_000.0] * 5 + [2_100_000.0]
    frame = _frame(
        [20.0, 20.0, 20.0, 20.0, 20.0, 10.5],
        [19.5, 20.0, 20.0, 20.0, 20.0, 10.0],
        volumes=volumes,
    )

    raw_ratio = volumes[-1] / np.mean(volumes[:-1])
    adjusted = apply_adjustment(frame)
    adjusted_volume = adjusted["volume"].to_numpy(dtype=float)
    fixed_ratio = adjusted_volume[-1] / adjusted_volume[:-1].mean()

    assert raw_ratio == pytest.approx(2.1)
    assert fixed_ratio == pytest.approx(1.05)


def test_source_column_is_written_for_trusted_rows():
    frame = _frame([10.0, 11.0], [9.9, 10.0])

    adjusted = apply_adjustment(frame)

    assert list(adjusted["adj_factor_source"]) == [TRUSTED_SOURCE] * 2


def test_apply_adjustment_does_not_mutate_the_input():
    """调用方常把同一份窗口喂给多个消费者，就地改会污染其他人。"""
    frame = _frame([10.0, 10.0, 9.2, 9.5], [9.9, 10.0, 9.0, 9.2])
    before = frame.copy()

    apply_adjustment(frame)

    pd.testing.assert_frame_equal(frame, before)


def test_applying_twice_is_rejected():
    """二次施加会把因子平方，而结果依然「有因子、非空、为正」，必须当场拦下。"""
    frame = _frame([10.0, 10.0, 9.2, 9.5], [9.9, 10.0, 9.0, 9.2])

    once = apply_adjustment(frame)

    with pytest.raises(ValueError, match="二次施加"):
        apply_adjustment(once)


def test_the_adjusted_window_is_internally_consistent():
    """对复权后的窗口重新解链，比值恒为 1 —— 整张表自洽。

    这是「pre_close 也要跟着缩放」的直接收益。不缩放的话 close 已复权而 pre_close
    还是原始价，任何按 close(t)/pre_close(t) 复算当日收益的消费方都会拿到错数，
    而且这里的比值会变成除权比值本身，重复施加就成了静默平方。
    """
    frame = _frame([10.0, 10.0, 9.2, 9.5], [9.9, 10.0, 9.0, 9.2])

    adjusted = apply_adjustment(frame).drop(
        columns=["adj_factor", "adj_factor_source"]
    )

    assert list(compute_window_factors(adjusted)) == [1.0, 1.0, 1.0, 1.0]


# ── 3. 送转与拆股：向上的比值，无论多大都是合法公司行为 ────────────────
def test_bonus_share_split_ratio_two_is_accepted():
    """10 送 10（比值 2.0）必须算出因子而不是被当成异常。

    A 股每年上千个送转事件都落在这一带；按幅度收上界，先被拒掉的就是它们。
    """
    frame = _frame([20.0, 20.0, 10.5, 11.0], [19.5, 20.0, 10.0, 10.5])

    result = analyze_window(frame)

    assert result.trusted
    assert list(result.sources) == [TRUSTED_SOURCE] * 4


def test_byd_2025_split_and_dividend_is_trusted():
    """002594 在 2025-07-29 的真实数据：比值 3.0358，必须可信。

    10 送 8 转增 12 派 39.74 元，1 股变 3 股：337.00/3 = 112.33，减去每新股 1.32 元
    现金红利正好是 111.01。旧的 3.0 上界只差 1.2% 就把比亚迪整段序列判成不可信，而
    同期被接受的比值上沿本来就贴着 3.0000——两者之间不存在任何讲得清的原则性区别。
    这条用例钉住「上界不是业务规则」。
    """
    frame = _frame(
        [337.93, 337.00, 111.42, 108.70],
        [342.72, 337.93, 111.01, 111.42],
        code="002594",
    )

    result = analyze_window(frame)

    ratio = 337.00 / 111.01
    assert ratio > 3.0, "这条用例的前提是该比值确实超过旧上界 3.0"
    assert result.trusted
    assert list(result.factors) == pytest.approx(
        [1.0 / ratio, 1.0 / ratio, 1.0, 1.0]
    )


def test_ten_for_one_split_is_trusted():
    """920402 在 2022-09-28 的真实数据：约 10:1 拆股，比值 9.7173，必须可信。

    这是全表实测到的最大合法比值，也是上界为什么要设在 20 而不是贴着它的原因。
    """
    frame = _frame(
        [55.00, 55.00, 5.70, 5.80],
        [54.00, 55.00, 5.66, 5.70],
        code="920402",
    )

    result = analyze_window(frame)

    ratio = 55.00 / 5.66
    assert ratio == pytest.approx(9.7173, abs=1e-4)
    assert result.trusted
    assert list(result.factors) == pytest.approx(
        [1.0 / ratio, 1.0 / ratio, 1.0, 1.0]
    )


# ── 4. 越界判定：下界按方向、上界只兜脏数据 ───────────────────────────
def test_ratio_exactly_at_the_corruption_ceiling_is_trusted():
    """恰好落在上界上算可信：界是拒绝的起点，不是可信的终点。"""
    frame = _frame([100.0, 100.0, 5.0, 5.1], [99.0, 100.0, 5.0, 5.0])

    result = analyze_window(frame)

    assert result.trusted
    assert 100.0 / 5.0 == RATIO_CORRUPTION_CEILING


def test_ratio_beyond_the_corruption_ceiling_breaks_the_chain():
    """比值 27.5：超出任何观测到的合法公司行为量级，按脏数据切段。"""
    frame = _frame([55.0, 55.0, 2.1, 2.2], [54.0, 55.0, 2.0, 2.1])

    result = analyze_window(frame)

    assert not result.trusted
    assert [item.reason for item in result.breaks] == [BREAK_RATIO_ABOVE_CEILING]
    assert result.breaks[0].ratio == pytest.approx(27.5)


def test_ratio_just_below_one_is_rejected_as_directional():
    """比值 0.98：幅度很小，但方向错了。

    0.98 离 1 只差 2%，比大量被接受的分红比值（1.0~1.10）更靠近 1，可它意味着
    pre_close 高于前一日收盘，分红送转做不出这个方向。任何按幅度设的下界都会放它过去。
    """
    frame = _frame([10.0, 9.8, 10.1, 10.2], [9.9, 10.0, 10.0, 10.1], code="920089")

    result = analyze_window(frame)

    assert not result.trusted
    assert [item.reason for item in result.breaks] == [BREAK_RATIO_BELOW_FLOOR]


def test_the_floor_is_a_float_tolerance_not_a_business_margin():
    """比值 0.9954 必须被拒。

    上一版下界取 0.99，理由是「给上游格式变化留防御余量」。实测把这个理由推翻了：
    全表 962 万行里 pre_close 与前一日收盘要么逐位精确相等，要么差额来自真实公司
    行为，``0 < |差| < 0.005`` 的行数是 0。也就是说 0.99 守的是一个不存在的威胁，
    代价却是放进 4 个真实的方向性错误（920718 / 920242 / 920786 / 920946），它们会
    变成各自序列上 0.19%~0.46% 的永久因子水平误差——比交叉验证最大偏差 0.1145% 还
    大 1.5~4 倍。这条用例就是防止有人把下界当可调参数再放回去。
    """
    frame = _frame([199.08, 199.08, 200.10], [198.00, 200.00, 200.00], code="920718")

    result = analyze_window(frame)

    ratio = 199.08 / 200.00
    assert ratio == pytest.approx(0.9954)
    assert not result.trusted
    assert result.breaks[0].reason == BREAK_RATIO_BELOW_FLOOR


def test_a_ratio_of_exactly_one_is_trusted():
    """下界写成 1.0 - 1e-9 是浮点表示容差：数学上等于 1 的比值必须放行。"""
    frame = _frame([12.34, 12.34, 12.34], [12.00, 12.34, 12.34])

    result = analyze_window(frame)

    assert result.trusted
    assert list(result.factors) == [1.0, 1.0, 1.0]


# ── 5. 断链：切段而不是整只作废 ───────────────────────────────────────
def test_break_invalidates_only_the_prefix_and_keeps_applying_afterwards():
    """断链点之前作废，断链点及之后照常施加。

    依据是 g(t)=f(t)/f(D) 的约掉性质：断链点之后的比值不受它影响。整只作废会把一次
    转板重组的代价扩大到整段历史——而这段历史里的送转事件本来是算得准的。
    """
    frame = _frame(
        [10.0, 10.0, 12.0, 12.5, 6.4, 6.5],
        [9.9, 10.0, 11.0, 12.0, 6.25, 6.4],
        code="920089",
    )

    result = analyze_window(frame)
    adjusted = apply_adjustment(frame, chain=result)

    assert result.segment_start == 2
    assert [item.position for item in result.breaks] == [2]
    assert list(result.sources) == [ANOMALOUS_SOURCE] * 2 + [TRUSTED_SOURCE] * 4
    # 被作废的前缀：因子留空、价格原样，两件事都要成立。
    assert result.factors.iloc[:2].isna().all()
    assert list(adjusted["close"].iloc[:2]) == pytest.approx([10.0, 10.0])
    # 断链点之后的送转照常被施加，序列在除权日不再跳空。
    assert list(result.factors.iloc[2:]) == pytest.approx([0.5, 0.5, 1.0, 1.0])
    assert list(adjusted["close"].iloc[2:]) == pytest.approx([6.0, 6.25, 6.4, 6.5])
    _assert_invariant(frame, adjusted)


def test_only_the_last_break_starts_the_trusted_segment():
    """两处断链时，可信段从最后一处开始——中间那段同样与 D 没有可信换算关系。"""
    frame = _frame(
        [10.0, 12.0, 12.0, 15.0, 15.5],
        [9.9, 11.0, 12.0, 13.0, 15.0],
    )

    result = analyze_window(frame)

    assert [item.position for item in result.breaks] == [1, 3]
    assert result.segment_start == 3
    assert list(result.sources) == [ANOMALOUS_SOURCE] * 3 + [TRUSTED_SOURCE] * 2


def test_a_break_on_the_last_row_leaves_only_that_row_trusted():
    frame = _frame([10.0, 10.0, 12.1], [9.9, 10.0, 12.0])

    result = analyze_window(frame)

    assert result.segment_start == 2
    assert result.factors.iloc[-1] == 1.0
    assert result.factors.iloc[:2].isna().all()


# ── 6. 缺数据：fail-closed，绝不 ffill、绝不按 1.0 补 ──────────────────
def test_missing_pre_close_mid_window_breaks_the_chain():
    """缺 pre_close 的行无从判断当日有没有除权，只能切段。

    上一版在这里取比值 1「延续既有水平」，那是按 1.0 补：算出来的序列有因子、非空、
    为正，能顺利通过下游门禁，却是一段部分复权的价格。fail-closed 意味着宁可这只
    股票在这个回放日出不了信号。
    """
    frame = _frame(
        [10.0, 10.0, 9.2, 9.5, 9.6],
        [9.9, 10.0, 9.0, None, 9.5],
    )

    result = analyze_window(frame)

    assert not result.trusted
    assert [item.reason for item in result.breaks] == [BREAK_MISSING_PRE_CLOSE]
    assert result.segment_start == 3
    assert result.breaks[0].ratio is None
    # 断链之后的那一段照常算得出因子：缺失的比值是 NaN，一旦让它进累乘，整个
    # 可信段都会被染成 NaN，而来源标记还写着「可信」。
    assert list(result.factors.iloc[3:]) == [1.0, 1.0]


def test_missing_pre_close_on_the_first_row_is_not_a_break():
    """窗口首行的 pre_close 从来不参与计算，新股首个交易日不该因此被拒。"""
    frame = _frame([10.0, 11.0, 12.0], [None, 10.0, 11.0])

    result = analyze_window(frame)

    assert result.trusted
    assert list(result.factors) == [1.0, 1.0, 1.0]


def test_non_positive_pre_close_is_treated_as_missing():
    """0 或负的 pre_close 不能进除法，也不该被当成除权信号。"""
    frame = _frame([10.0, 11.0, 12.0], [9.9, 0.0, 11.0])

    result = analyze_window(frame)

    assert not result.trusted
    assert result.breaks[0].reason == BREAK_MISSING_PRE_CLOSE


def test_missing_previous_close_breaks_the_chain():
    """前一日 close 缺失时同样无从取比值，处理方式一致。"""
    frame = _frame([10.0, None, 12.0, 12.5], [9.9, 10.0, 11.0, 12.0])

    result = analyze_window(frame)

    assert not result.trusted
    assert result.segment_start == 2


def test_trusted_rows_always_carry_a_usable_factor():
    """标了可信来源的行，因子必须有限且为正。

    NaN 因子配可信来源是最坏的组合：apply_adjustment 会按 1.0 把价格原样放行，而
    adj_factor_source 却告诉下游「这段复权过了」——没复权的价格戴着可信标记进检测器，
    正是整个方案要避免的静默算错。缺数据的断链最容易踩到它：NaN 比值只要进了累乘，
    整个可信段都会被染成 NaN。
    """
    frames = {
        "缺 pre_close": _frame(
            [10.0, 10.0, 9.2, 9.5, 9.6], [9.9, 10.0, 9.0, None, 9.5]
        ),
        "缺前一日 close": _frame(
            [10.0, None, 12.0, 12.5], [9.9, 10.0, 11.0, 12.0]
        ),
        "方向性缺口": _frame(
            [10.0, 9.8, 10.1, 10.2], [9.9, 10.0, 10.0, 10.1]
        ),
        "无事件": _frame([10.0, 11.0, 12.0], [9.9, 10.0, 11.0]),
    }
    for label, frame in frames.items():
        result = analyze_window(frame)
        trusted = result.factors[result.sources == TRUSTED_SOURCE].to_numpy(
            dtype=float
        )
        assert len(trusted), f"{label}: 可信段是空的"
        assert np.isfinite(trusted).all(), f"{label}: 可信段里有非有限因子"
        assert (trusted > 0.0).all(), f"{label}: 可信段里有非正因子"


def test_anomalous_rows_are_not_adjusted_and_not_labelled_trusted():
    """两件事必须同时成立：不施加，且不打可信来源。

    只做前一件会留下一段没复权却带可信标记的价格，正是「静默算错」。
    """
    frame = _frame([10.0, 9.8, 10.1, 10.2], [9.9, 10.0, 10.0, 10.1])

    adjusted = apply_adjustment(frame)

    assert list(adjusted["adj_factor_source"].iloc[:1]) == [ANOMALOUS_SOURCE]
    assert adjusted["adj_factor"].iloc[0] != adjusted["adj_factor"].iloc[0]  # NaN
    assert adjusted["close"].iloc[0] == pytest.approx(10.0)
    assert adjusted["volume"].iloc[0] == pytest.approx(frame["volume"].iloc[0])


# ── 7. 窗口内现算 == 全历史现算（4.1 的承重测试）─────────────────────
def test_window_factors_match_full_history_factors():
    """同一个 D，窗口内现算与全历史现算必须给出同一个 g(t)。

    这是「只看窗口」这个决定的唯一依据：g(t)=f(t)/f(D) 只依赖 t 与 D 之间的事件，
    窗口起点之前的一切在相除时整体约掉。这条红了，意味着稀疏历史会顺着窗口边界
    渗进结果里，整套方案的风险敞口就不再受窗口限制。
    """
    closes = [10.0, 10.0, 9.2, 9.5, 9.6, 4.9, 5.0, 5.1, 2.6, 2.7]
    pre_closes = [9.9, 10.0, 9.0, 9.2, 9.5, 4.8, 4.9, 5.0, 2.55, 2.6]
    full = _frame(closes, pre_closes)
    window = full.iloc[4:].reset_index(drop=True)

    full_factors = compute_window_factors(full)
    window_factors = compute_window_factors(window)

    assert list(window_factors) == pytest.approx(
        list(full_factors.iloc[4:]), rel=1e-12
    )


def test_a_break_before_the_window_does_not_leak_into_the_window():
    """窗口起点之前的断链不该影响窗口内的因子——那段本来就被约掉了。"""
    closes = [10.0, 12.0, 12.0, 12.5, 6.4, 6.5]
    pre_closes = [9.9, 11.0, 12.0, 12.0, 6.25, 6.4]
    full = _frame(closes, pre_closes)
    window = full.iloc[2:].reset_index(drop=True)

    full_result = analyze_window(full)
    window_result = analyze_window(window)

    assert not full_result.trusted
    assert window_result.trusted
    assert list(window_result.factors) == pytest.approx(
        list(full_result.factors.iloc[2:]), rel=1e-12
    )


# ── 8. 退化输入与调用约定 ─────────────────────────────────────────────
def test_empty_window():
    result = analyze_window(_frame([], []))

    assert result.trusted
    assert result.factors.empty

    adjusted = apply_adjustment(pd.DataFrame())
    assert adjusted.empty
    assert "adj_factor" in adjusted.columns


def test_single_row_window():
    frame = _frame([10.0], [9.9])

    result = analyze_window(frame)

    assert result.trusted
    assert list(result.factors) == [1.0]


def test_missing_required_columns_raises():
    """缺列时报错而不是按 1.0 走过去。"""
    with pytest.raises(ValueError, match="pre_close"):
        analyze_window(pd.DataFrame({"close": [1.0, 2.0]}))


def test_unsorted_window_raises():
    """排序由调用方负责，但顺序错了算出来的因子是错的却看不出来，必须当场拦下。"""
    frame = _frame([10.0, 11.0], [9.9, 10.0]).iloc[::-1]

    with pytest.raises(ValueError, match="升序"):
        analyze_window(frame)


def test_multiple_codes_in_one_window_raises():
    """两只股票拼在一起算出的因子数值上说得通，但没有任何意义。"""
    frame = pd.concat(
        [_frame([10.0, 11.0], [9.9, 10.0], code="600001"),
         _frame([20.0, 21.0], [19.9, 20.0], code="600002", start=4)],
        ignore_index=True,
    )

    with pytest.raises(ValueError, match="单只股票"):
        analyze_window(frame)


def test_apply_adjustment_rejects_a_chain_of_the_wrong_length():
    frame = _frame([10.0, 11.0, 12.0], [9.9, 10.0, 11.0])
    stale = analyze_window(_frame([10.0, 11.0], [9.9, 10.0]))

    with pytest.raises(ValueError, match="行数不一致"):
        apply_adjustment(frame, chain=stale)


def test_optional_columns_are_not_required():
    """只有 close / pre_close 是必需的，取数路径少给一列不该炸。"""
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "close": [10.0, 5.2],
            "pre_close": [9.9, 5.0],
        }
    )

    adjusted = apply_adjustment(frame)

    assert list(adjusted["adj_factor"]) == pytest.approx([0.5, 1.0])
    assert list(adjusted["close"]) == pytest.approx([5.0, 5.2])


def test_source_constants_stay_distinguishable():
    """可信来源与被作废来源必须是两个字符串，且后者不能是前者的前缀式误配。

    下游白名单按精确匹配放行；两者写成同一个值等于把没复权的价格放进可信集合。
    """
    assert chain_module.TRUSTED_SOURCE != chain_module.ANOMALOUS_SOURCE
    assert chain_module.ANOMALOUS_SOURCE.startswith(chain_module.TRUSTED_SOURCE)
