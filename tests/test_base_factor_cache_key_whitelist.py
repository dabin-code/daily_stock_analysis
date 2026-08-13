# -*- coding: utf-8 -*-
"""白名单必须覆盖所有影响 base 因子输出的配置字段。

base 快照键改用白名单后，漏登记一个真正影响 base 的字段不会报错，
只会让缓存把旧参数的因子当成新参数的结果返回——静默算错。
本测试逐字段改值重算，凡是能改变输出的字段都必须已登记。

这条测试最危险的失败模式不是变红，而是**变绿却没有约束力**：
夹具若探不到任何检测器分支，所有变异都不会改变输出，测试照样全绿。
`_FIELDS_WITH_MEASURED_BITE` 是防这一点的哨兵——它钉住了今天实测有
约束力的那些字段，夹具一旦失去咬合就会变红，而不是安静地退化成安慰剂。
"""
from dataclasses import fields, replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.backtest.services.bottom_divergence_v2_performance import (
    BASE_FACTOR_CONFIG_FIELDS,
    ValidationFactorCache,
)
from src.config import Config
from src.services.factor_service import FactorService
from tests.test_low_123_trendline_detector import (
    _deep_retrace_breakout_ready,
    _downtrend_then_breakout_ready_low123,
    _late_breakout,
    _long_span_p1_to_p2,
    _make_df,
    _stale_late_p2_breakout,
    _zigzag_downtrend,
)

_TRADE_DATE = date(2026, 8, 5)

_BOTTOM_DIVERGENCE_CSV = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "001337_bottom_divergence_20251201_20260805.csv"
)

# universe 的 list_date 必须让 days_since_listed 落在 screening_min_list_days
# 默认值（120）附近，否则 `_build_risk_flags` 的 new_listing 判断恒为同一侧，
# 该字段就没有约束力了。124 天使默认值不触发、放宽到 127 天触发。
_DAYS_SINCE_LISTED = 124


# ---------------------------------------------------------------------------
# 夹具
#
# 单调斜坡对 8 个字段全部没有约束力（滚动最高价与窗口长度无关、检测器一律
# rejected），所以这里复用 `test_low_123_trendline_detector.py` 里那些真正能
# 触发检测器的形态，再补两个专门压在容差边界上的形态。单只股票在结构上不可能
# 同时满足全部分支，因此组成多股 universe。
# ---------------------------------------------------------------------------

def _low123_marginal_undercut() -> pd.DataFrame:
    """低位 123 成形后，P3 之后的最低价**略微**跌破 P1。

    `_is_structure_broken_after_p3` 的判据是 `low < P1 * (1 - break_tolerance)`。
    这里把跌破幅度做成 1%：默认容差 0.0 时结构判为破位失效（rejected），
    容差放宽到 1.7% 时结构重新成立（breakout_ready）。这是
    `low123_break_tolerance` 唯一可观测的翻转点。
    """
    n = 80
    prices = np.zeros(n)
    prices[:30] = _zigzag_downtrend(130, 90, 30, amplitude=5.0)
    prices[30] = 86                                   # P1
    prices[31:40] = np.linspace(86, 97, 9)
    prices[39] = 97                                   # P2
    prices[40:45] = np.linspace(97, 89, 5)
    prices[44] = 89                                   # P3
    prices[45:48] = [90.0, 89.5, 89.0]
    prices[48] = 86.5                                 # 回踩
    prices[49:52] = [88.0, 92.0, 95.0]
    prices[52] = 99                                   # 上破 P2
    prices[53:] = np.linspace(99, 102, n - 53)

    highs = prices * 1.005
    lows = prices * 0.995
    highs[39] = 97.5
    lows[30] = 85.5                                   # P1 最低价
    lows[44] = 88.5
    lows[48] = 85.5 * 0.99                            # 略微跌破 P1（1%）
    highs[52] = 100
    return _make_df(prices, highs=highs, lows=lows)


def _bottom_divergence_final_bar_undercut() -> pd.DataFrame:
    """底背离结构成立后，**末根** K 线的最低价略微跌破 min(A, B)。

    `_evaluate_candidate` 的护栏是 `post_b_low < min(A, B) * (1 - tolerance)`。
    跌破点必须落在末根 K 线上：`find_swing_lows`（`divergence_detector.py:41`）
    刻意放宽了右侧窗口，只要后面还有 1 根 K 线，任何更低的低点都会被认成
    新的摆动低点、进而被选为新的 B——此时 `post_b_low` 就不再低于 min(A, B)，
    护栏永远不触发。末根 K 线是全序列里唯一无法成为摆动低点的位置。

    默认容差 0.0 时该候选被护栏丢弃（rejected/no_valid_pattern），
    放宽到 1.7% 时候选存活（divergence_only）。
    """
    n = 80
    prices = np.zeros(n)
    prices[:30] = _zigzag_downtrend(140, 92, 30, amplitude=5.0)
    prices[30] = 90.5                                 # A
    prices[31:43] = np.linspace(92, 100, 12)
    prices[42] = 100.0                                # 反弹高点 H
    prices[43:59] = np.linspace(99, 88.5, 16)
    prices[58] = 88.5                                 # B
    prices[59:75] = np.linspace(89.5, 93.5, 16)
    prices[75:79] = [92.0, 91.0, 90.0, 89.0]
    prices[79] = 87.6

    highs = prices * 1.005
    lows = prices * 0.995
    lows[30] = 90.0                                   # A 最低价
    highs[42] = 100.5
    lows[58] = 88.0                                   # B 最低价
    lows[79] = 88.0 * 0.99                            # 末根略微跌破（1%）
    return _make_df(prices, highs=highs, lows=lows)


def _with_snapshot_columns(code: str, frame: pd.DataFrame) -> pd.DataFrame:
    """补齐 `build_factor_snapshot_from_groups` 需要而夹具没有的列。

    `_make_df` 只产 open/high/low/close/volume，CSV 夹具缺 amount；
    消费点分别在 `factor_service.py:174`（date）、`:181`（amount）、
    `:229`（pct_chg）。

    日期一律按交易日倒推到同一个 `_TRADE_DATE`：`factor_service.py:174-176`
    会把末根日期对不上的 group **静默丢弃**（无异常、无日志），夹具会就此失效。
    """
    frame = frame.copy().reset_index(drop=True)
    frame["code"] = code
    frame["date"] = pd.bdate_range(
        end=pd.Timestamp(_TRADE_DATE), periods=len(frame)
    )
    if "amount" not in frame.columns:
        frame["amount"] = frame["close"] * frame["volume"]
    if "pct_chg" not in frame.columns:
        frame["pct_chg"] = frame["close"].pct_change().fillna(0.0) * 100.0
    return frame


def _load_bottom_divergence_csv() -> pd.DataFrame:
    frame = pd.read_csv(_BOTTOM_DIVERGENCE_CSV)
    if "volume" not in frame.columns:
        frame["volume"] = 1_000_000.0
    return frame


_FIXTURE_GROUPS = {
    code: _with_snapshot_columns(code, builder())
    for code, builder in (
        ("000001", _downtrend_then_breakout_ready_low123),
        ("000002", _deep_retrace_breakout_ready),
        ("000003", _long_span_p1_to_p2),
        ("000004", _stale_late_p2_breakout),
        ("000005", _late_breakout),
        ("000006", _low123_marginal_undercut),
        ("000007", _bottom_divergence_final_bar_undercut),
        ("001337", _load_bottom_divergence_csv),
    )
}

_FIXTURE_UNIVERSE = pd.DataFrame([
    {
        "code": code,
        "name": f"FIX{code}",
        # 真实日期，不能用 NaN：`factor_service.py:184-186` 对 NaN 走
        # `bool(nan) is True` 分支，`pd.to_datetime(nan)` 得到 NaT，
        # 随后的日期相减直接 TypeError。
        "list_date": _TRADE_DATE - timedelta(days=_DAYS_SINCE_LISTED),
        "is_st": False,
        "circ_mv": 5_000_000_000.0,
    }
    for code in sorted(_FIXTURE_GROUPS)
])

# 无法机械变异的字段类型：字符串多为密钥/URL/模型名，改动可能触发校验；
# 容器与 None 无通用的"改一点点"语义。base 因子只消费数值与布尔，
# 因此跳过它们是安全的——但跳过集合本身要被断言，防止将来新增
# 一个影响 base 的非数值字段后被静默略过。
_UNMUTABLE_TYPES = (str, type(None), dict, list, tuple)

# 今天实测**确实**能改变 base 快照的白名单字段。它不是验收门槛，而是夹具
# 的活性哨兵：检测器或夹具将来若失去咬合，这条断言会变红，而不是让整条
# 变异测试安静地退化成一律全绿的安慰剂。
#
# 8 个白名单字段里唯一未列入的是 `screening_factor_lookback_days`：
# `build_factor_snapshot_from_groups` 根本不读它，它只决定喂进来的窗口长度，
# 因此改由 `test_factor_lookback_days_changes_both_the_window_and_the_base_key`
# 覆盖。
_FIELDS_WITH_MEASURED_BITE = frozenset({
    "screening_min_list_days",
    "screening_breakout_lookback_days",
    "low123_max_p1_p2_bars",
    "low123_max_breakout_gap",
    "low123_break_tolerance",
    "bottom_divergence_max_breakout_gap",
    "bottom_divergence_break_tolerance",
})


def _snapshot(config: Config) -> pd.DataFrame:
    # 复现缓存层构造 base config 的方式
    # （`bottom_divergence_v2_performance.py:596-599`），否则测的就不是
    # base pass。`test_cache_builds_the_base_pass_with_v2_disabled` 钉住了
    # 这个前提，生产侧一旦不再强制关闭 v2，那条测试会先变红。
    base_config = replace(config, bottom_divergence_v2_enabled=False)
    # db_manager 缺省会回落到 DatabaseManager.get_instance()；这里用哑对象，
    # persist=False 时 `self.db` 不会被触碰。
    service = FactorService(db_manager=MagicMock(), config=base_config)
    snapshot = service.build_factor_snapshot_from_groups(
        _FIXTURE_UNIVERSE, _FIXTURE_GROUPS, trade_date=_TRADE_DATE
    )
    assert len(snapshot) == len(_FIXTURE_GROUPS), (
        f"夹具有股票被丢弃：期望 {len(_FIXTURE_GROUPS)} 行，"
        f"实得 {len(snapshot)} 行；检查各 group 的末根日期是否都等于 "
        f"{_TRADE_DATE}，以及是否都有至少 20 根 K 线"
    )
    return snapshot


def _mutations(value, /):
    """给出若干"确实不同"的同类型值；不可变异时返回空元组。

    必须双向：实测多数检测器阈值的翻转点在收紧方向，
    只做放松（`+7`）会让这条测试对它们全部失明——8 个白名单字段里有
    3 个只在收紧方向才有翻转点。

    末尾要滤掉与原值相等的候选：`0.0 / 3.0` 与 `max(1 // 3, 1)` 都会
    退化成原值，白跑一遍还会虚增覆盖假象。
    """
    if isinstance(value, bool):
        candidates = (not value,)
    elif isinstance(value, int):
        candidates = (value + 7, max(value // 3, 1))
    elif isinstance(value, float):
        candidates = (value * 1.5 + 0.017, value / 3.0)
    else:
        return ()
    return tuple(
        candidate for candidate in candidates if candidate != value
    )


@pytest.mark.slow
def test_whitelist_covers_every_field_that_changes_base_factors():
    baseline_config = Config()
    baseline = _snapshot(baseline_config)

    unlisted_but_influential = []
    influential = set()
    skipped = []
    errored = []
    for field in fields(Config):
        current = getattr(baseline_config, field.name)
        candidates = (
            () if isinstance(current, _UNMUTABLE_TYPES)
            else _mutations(current)
        )
        if not candidates:
            skipped.append(field.name)
            continue

        for mutated_value in candidates:
            try:
                mutated_config = replace(
                    baseline_config, **{field.name: mutated_value}
                )
                actual = _snapshot(mutated_config)
            except Exception as exc:
                # 不能并进 skipped：算不出来的字段可能恰恰是影响 base 的那个
                errored.append(f"{field.name}={mutated_value!r} -> {exc!r}")
                continue
            if actual.equals(baseline):
                continue
            influential.add(field.name)
            if field.name not in BASE_FACTOR_CONFIG_FIELDS:
                unlisted_but_influential.append(field.name)
            break

    assert not unlisted_but_influential, (
        "以下字段会改变 base 因子输出却不在 BASE_FACTOR_CONFIG_FIELDS 里，"
        f"缓存会把旧值当新值复用：{sorted(set(unlisted_but_influential))}"
    )
    assert not errored, (
        "以下字段变异后算不出 base 快照，无法判定它是否影响 base，"
        f"必须逐个查明而不是当作安全：{errored}"
    )
    assert skipped, "跳过集合为空说明变异逻辑失效"
    missing_bite = sorted(_FIELDS_WITH_MEASURED_BITE - influential)
    assert not missing_bite, (
        "夹具对以下字段失去了约束力，这条测试正在退化成安慰剂："
        f"{missing_bite}。先修夹具（让它重新触发对应检测器分支），"
        "确认无法恢复时才更新 _FIELDS_WITH_MEASURED_BITE 并在文档里写明缺口"
    )


def test_cache_builds_the_base_pass_with_v2_disabled():
    """上面那条测试依赖生产侧强制关闭 v2，这里把该前提钉住。

    生产侧若不再强制关闭，base 输出就会随 `bottom_divergence_v2_enabled`
    变化，上面的变异测试会把它报成漏登记字段——而把它登记进白名单会直接
    摧毁 v1/v2 共享同一份 base 因子的性质
    （`test_v1_and_v2_legs_share_one_base_factor_pass`）。

    钉的是可观测行为而不是源码文本：无论调用方传进来的
    `bottom_divergence_v2_enabled` 是什么，跑 base pass 的那个
    FactorService 拿到的 config 必须是关着的。
    """
    observed = []
    real_init = FactorService.__init__

    def recording_init(self, *args, **kwargs):
        observed.append(kwargs.get("config"))
        real_init(self, *args, **kwargs)

    # 30 根 K 线：base pass 需要 ≥20 根，而 v2 评估在 <60 根时整段跳过
    # （`bottom_divergence_v2_performance.py:657`），因此这条前提测试
    # 不必付出跑完整条 v2 链路的代价。
    group = _with_snapshot_columns(
        "000001", _downtrend_then_breakout_ready_low123().tail(30)
    )
    universe = pd.DataFrame([{"code": "000001", "name": "A"}])
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(_TRADE_DATE,),
        bar_groups={"000001": group},
    )

    with patch.object(FactorService, "__init__", recording_init):
        cache.build_factor_snapshot(
            config=Config(bottom_divergence_v2_enabled=True),
            universe=universe,
            trade_date=_TRADE_DATE,
        )

    assert observed, "base pass 没有构造 FactorService，这条前提测试没测到东西"
    assert all(
        config is not None and config.bottom_divergence_v2_enabled is False
        for config in observed
    ), (
        "缓存层不再强制关闭 v2；请同步修正 _snapshot() 与白名单的边界假设，"
        f"实际观察到的 config：{observed}"
    )


def test_factor_lookback_days_changes_both_the_window_and_the_base_key():
    """`screening_factor_lookback_days` 的分工说明与覆盖。

    变异测试**结构上**够不到这个字段：`build_factor_snapshot_from_groups`
    根本不读它，它只在 `factor_service.py:76` 进构造器（那条路径被显式的
    lookback_days 参数绕开），以及在
    `bottom_divergence_v2_performance.py:581` 决定喂给 base pass 的窗口长度。
    所以它得由这条针对取窗行为的测试守。
    """
    groups = {"001337": _FIXTURE_GROUPS["001337"]}
    universe = pd.DataFrame([{"code": "001337", "name": "A"}])
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(_TRADE_DATE,),
        bar_groups=groups,
    )

    long_config = Config(screening_factor_lookback_days=200)
    short_config = Config(screening_factor_lookback_days=30)

    assert cache._base_config_hash(long_config) != cache._base_config_hash(
        short_config
    ), "取窗长度决定 base 快照内容，却没有进 base 快照键"

    long_snapshot = cache.build_factor_snapshot(
        config=long_config, universe=universe, trade_date=_TRADE_DATE
    )
    short_snapshot = cache.build_factor_snapshot(
        config=short_config, universe=universe, trade_date=_TRADE_DATE
    )

    assert cache.stats["base_snapshot_builds"] == 2, (
        "两个取窗长度共用了同一份 base 快照文件"
    )
    assert not long_snapshot.equals(short_snapshot), (
        "取窗长度没有改变 base 快照内容；夹具的 bar 数可能不足以让两个"
        "窗口产生差异"
    )
