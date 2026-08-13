# -*- coding: utf-8 -*-
"""三条读取路径接上复权后必须共用同一套口径（gate-3 / Task 2）。

本 Task 唯一不可接受的失败是「漏接一条路径」——那不会报错，只会让实盘与回测
在不同尺度的价格上各自算得头头是道。所以这里钉的不是「某个函数被调用了」，
而是三条路径给出的因子序列**逐行相等**，以及跨路径拼接处的收益率仍然等于
``close(t)/pre_close(t)``。

三条路径：

1. 回测因子路径：`ValidationFactorCache._window`
2. 前瞻收益路径：`bottom_divergence_v2_replay._adjusted_forward_bars`
3. 生产因子路径：`FactorService.build_factor_snapshot`

第 2 条的锚在窗口**首行**（D 在前瞻窗口之外），另外两条在**末行**，因此
「逐行相等」对它不适用；改钉更强的东西：把它接在 1/3 的输出后面，跨越 D 的
那一步收益率必须仍然成立。这正是 v1 计划漏掉这条路径时会碎掉的地方。
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import json
import pandas as pd
import pytest

from src.backtest.services.bottom_divergence_v2_performance import (
    BASE_FACTOR_CONFIG_FIELDS,
    ValidationFactorCache,
)
from src.backtest.services.bottom_divergence_v2_replay import (
    _adjusted_forward_bars,
    _adjusted_prior_bars,
    _build_validation_sample,
)
from src.config import Config
from src.services.adjustment_chain import (
    ANOMALOUS_SOURCE,
    TRUSTED_SOURCE,
    apply_read_adjustment,
)
from src.services.factor_service import FactorService
from src.storage import DatabaseManager, StockDaily


_CODE = "900913"
_TRADE_DATE = date(2026, 8, 5)
_LOOKBACK_DAYS = 200

# 除权落在窗口倒数第 10 根：10 送 10，pre_close 减半。
# 放在窗口内部而不是端点，是为了让「前缀被缩放、后缀不动」这件事在同一个
# 窗口里同时可观测。
_SPLIT_OFFSET = 10


def _raw_bars(
    count: int = 60,
    *,
    end: date = _TRADE_DATE,
    split_offset: int | None = _SPLIT_OFFSET,
    forward: int = 0,
) -> list[dict]:
    """一段带 pre_close 的原始日线；`forward` 根落在 `end` 之后。

    价格逐日 +0.1，除权日 pre_close 折半、close 随之折半，与真实的 10 送 10
    一致：`pre_close` 是交易所给出的复权参考价，`close` 在它的基础上继续走。
    """
    dates = pd.bdate_range(end=pd.Timestamp(end), periods=count)
    if forward:
        dates = dates.append(
            pd.bdate_range(
                start=pd.Timestamp(end) + pd.offsets.BDay(1), periods=forward
            )
        )
    split_index = (
        None if split_offset is None else len(dates) - forward - split_offset
    )
    rows: list[dict] = []
    close = 20.0
    for index, timestamp in enumerate(dates):
        previous_close = close
        if index == 0:
            pre_close = None
        elif index == split_index:
            pre_close = previous_close / 2.0
        else:
            pre_close = previous_close
        if index:
            close = pre_close + 0.1
        rows.append({
            "code": _CODE,
            "date": timestamp.date(),
            "open": close - 0.05,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "pre_close": pre_close,
            "volume": 1_000_000.0,
            "amount": close * 1_000_000.0,
            "pct_chg": 1.0,
            "data_source": "fixture",
            "adj_factor": 1.0,
            "adj_factor_source": "legacy_assume_one",
        })
    return rows


def _frame(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame


def _bars(rows: list[dict]) -> list[SimpleNamespace]:
    return [SimpleNamespace(**row) for row in rows]


@pytest.fixture
def seeded_database():
    """把夹具写进会话临时库，用完删掉自己的行。

    走真库而不是内存 DataFrame，是因为这条测试要覆盖的正是
    `build_factor_snapshot` 的取数列清单——`pre_close` 曾经就不在里面，
    只喂 DataFrame 的测试对那个缺口是瞎的。
    """
    db = DatabaseManager.get_instance()
    rows = _raw_bars()
    with db.get_session() as session:
        session.query(StockDaily).filter(StockDaily.code == _CODE).delete()
        for row in rows:
            session.add(StockDaily(**row))
        session.commit()
    try:
        yield db, rows
    finally:
        with db.get_session() as session:
            session.query(StockDaily).filter(StockDaily.code == _CODE).delete()
            session.commit()


def _universe() -> pd.DataFrame:
    return pd.DataFrame([{
        "code": _CODE,
        "name": "FIXTURE",
        "is_st": False,
        "list_date": date(2020, 1, 1),
    }])


def _production_group(db, config: Config) -> pd.DataFrame:
    """抓住生产路径真正喂给检测器的那个 group。"""
    captured: dict = {}
    service = FactorService(db_manager=db, config=config)

    def capture(group, latest, close_series):
        captured["group"] = group.copy()
        return {}

    with patch.object(
        service, "_compute_extended_factors", side_effect=capture
    ), patch.object(service, "_enrich_base_scores"):
        service.build_factor_snapshot(_universe(), trade_date=_TRADE_DATE)
    assert "group" in captured, "生产路径没有产出 group，夹具没喂到检测器"
    return captured["group"]


def _backtest_window(db, config: Config) -> pd.DataFrame:
    cache = ValidationFactorCache.from_database(
        db_manager=db,
        data_version="fixture",
        trade_dates=(_TRADE_DATE,),
        codes=[_CODE],
        lookback_days=config.screening_factor_lookback_days,
    )
    try:
        return cache._window(
            _CODE,
            _TRADE_DATE,
            config.screening_factor_lookback_days,
            adjust=config.adj_apply_on_read,
        )
    finally:
        cache.close()


def test_production_and_backtest_agree_row_by_row(seeded_database):
    """同一 code、同一日期，实盘与回测拿到的因子序列必须逐行相等。

    两条路径的取数、切窗、施加各写各的，一旦有一条没接上，这里立刻是两套
    数——而线上不会有任何报错，只会得到两份互相矛盾的回测结论。
    """
    db, _rows = seeded_database
    config = Config()

    production = _production_group(db, config)
    backtest = _backtest_window(db, config)

    assert len(production) == len(backtest)
    for column in (
        "open", "high", "low", "close", "pre_close", "volume", "adj_factor"
    ):
        pd.testing.assert_series_equal(
            production[column].reset_index(drop=True).astype(float),
            backtest[column].reset_index(drop=True).astype(float),
            check_names=False,
            obj=f"两条路径的 {column} 不一致",
        )
    assert list(production["adj_factor_source"]) == [TRUSTED_SOURCE] * len(
        production
    )
    assert list(backtest["adj_factor_source"]) == [TRUSTED_SOURCE] * len(
        backtest
    )


def test_production_group_satisfies_the_adjustment_invariant(seeded_database):
    """``f(t) * pre_close(t) == f(t-1) * close(t-1)``，逐行核对。

    等价形式是 ``close_adj(t)/close_adj(t-1) == close(t)/pre_close(t)``：
    复权序列必须复现真实收益率。写成 ``close(t-1) == pre_close(t)`` 是错的
    ——复权后前者带因子、后者是原始价。
    """
    db, rows = seeded_database
    group = _production_group(db, Config())

    factors = group["adj_factor"].to_numpy(dtype=float)
    raw_close = [row["close"] for row in rows]
    raw_pre_close = [row["pre_close"] for row in rows]
    adjusted_close = group["close"].to_numpy(dtype=float)

    assert len(factors) == len(raw_close)
    for index in range(1, len(factors)):
        assert factors[index] * raw_pre_close[index] == pytest.approx(
            factors[index - 1] * raw_close[index - 1], rel=1e-12
        )
        assert adjusted_close[index] / adjusted_close[index - 1] == (
            pytest.approx(raw_close[index] / raw_pre_close[index], rel=1e-12)
        )


def test_anchor_day_factor_is_one_so_entry_price_stays_tradable(
    seeded_database,
):
    """窗口末日因子恒为 1：入场价必须等于当日真实可成交价。

    末行一旦不是 1，`entry_close`（取自因子快照）就不再是那天能买到的价，
    而前瞻 bar 又是按 D 归一的，两者的尺度会差一个因子。
    """
    db, rows = seeded_database
    group = _production_group(db, Config())

    assert float(group["adj_factor"].iloc[-1]) == pytest.approx(1.0)
    assert float(group["close"].iloc[-1]) == pytest.approx(rows[-1]["close"])
    # 除权之前的 bar 被折半，除权当日及之后不动。
    split_index = len(rows) - _SPLIT_OFFSET
    assert float(group["adj_factor"].iloc[split_index]) == pytest.approx(1.0)
    assert float(group["adj_factor"].iloc[split_index - 1]) == pytest.approx(
        0.5
    )
    # volume 反方向缩放：老股本的成交量要放大到今天的口径才可比。
    assert float(group["volume"].iloc[0]) == pytest.approx(2_000_000.0)
    # amount 不动：价×量守恒，本就不受复权影响。
    assert float(group["amount"].iloc[0]) == pytest.approx(
        rows[0]["amount"]
    )


def test_switch_off_reproduces_the_pre_change_reads(seeded_database):
    """`ADJ_APPLY_ON_READ=false` 时两条因子路径都必须回到原始价。

    这是本计划声明的回滚方式（因子不落库，关掉即完全回退），
    没有这条断言，回滚承诺就只是文档里的一句话。
    """
    db, rows = seeded_database
    config = Config(adj_apply_on_read=False)

    production = _production_group(db, config)
    backtest = _backtest_window(db, config)

    for frame in (production, backtest):
        assert list(frame["close"].astype(float)) == pytest.approx(
            [row["close"] for row in rows]
        )
        assert list(frame["volume"].astype(float)) == pytest.approx(
            [row["volume"] for row in rows]
        )
        assert list(frame["adj_factor"].astype(float)) == pytest.approx(
            [row["adj_factor"] for row in rows]
        )
        assert set(frame["adj_factor_source"]) == {"legacy_assume_one"}


def test_forward_window_joins_the_factor_window_without_a_seam():
    """前瞻窗口接在因子窗口后面，跨越 D 的那一步收益率仍然成立。

    第 2 条路径的锚在窗口首行，另外两条在末行，没法逐行比因子；能比的是
    拼接处：`close_adj(D+1)/close_adj(D)` 必须等于 `close(D+1)/pre_close(D+1)`。
    锚点选错（比如照搬 `apply_adjustment` 归一到前瞻窗口自己的末行）时，
    这一步会整体偏掉一个 f(D+20)/f(D)。
    """
    rows = _raw_bars(count=40, forward=10, split_offset=None)
    history = [row for row in rows if row["date"] <= _TRADE_DATE]
    # 除权落在前瞻窗口内：第 3 根前瞻 bar 10 送 10。
    forward_rows = [dict(row) for row in rows if row["date"] > _TRADE_DATE]
    for index in range(2, len(forward_rows)):
        if index == 2:
            forward_rows[index]["pre_close"] = (
                forward_rows[index - 1]["close"] / 2.0
            )
        forward_rows[index]["close"] = forward_rows[index]["pre_close"] + 0.1
        if index + 1 < len(forward_rows):
            forward_rows[index + 1]["pre_close"] = forward_rows[index]["close"]

    adjusted_history = apply_read_adjustment(_frame(history))
    adjusted_forward = _adjusted_forward_bars(
        _bars(forward_rows),
        signal_date=_TRADE_DATE,
        anchor_close=float(adjusted_history["close"].iloc[-1]),
        config=Config(),
    )
    assert len(adjusted_forward) == len(forward_rows)

    stitched_close = (
        list(adjusted_history["close"].astype(float))
        + [bar.close for bar in adjusted_forward]
    )
    raw = history + forward_rows
    for index in range(1, len(raw)):
        assert stitched_close[index] / stitched_close[index - 1] == (
            pytest.approx(
                raw[index]["close"] / raw[index]["pre_close"], rel=1e-12
            )
        ), f"第 {index} 行接缝处的收益率被复权改写了"


def _dividend_sample(config: Config):
    """信号后第 3 天 10 送 10 的样本；除权前后真实价格纹丝不动。"""
    signal_date = date(2026, 8, 5)
    entry_close = 20.0
    forward_rows = []
    close = entry_close
    for index in range(20):
        pre_close = close / 2.0 if index == 2 else close
        close = pre_close
        forward_rows.append({
            "date": signal_date + timedelta(days=index + 1),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "pre_close": pre_close,
            "volume": 1_000_000.0,
            "amount": close * 1_000_000.0,
        })
    prior_rows = [
        {
            "date": signal_date - timedelta(days=20 - index),
            "close": entry_close,
            "pre_close": entry_close,
            "amount": 1_000_000.0,
            "volume": 1_000_000.0,
            "open": entry_close,
            "high": entry_close,
            "low": entry_close,
        }
        for index in range(20)
    ]
    repository = SimpleNamespace(
        get_range=lambda *_args, **_kwargs: (),
        get_forward_bars=lambda **_kwargs: _bars(forward_rows),
        get_prior_bars=lambda **_kwargs: _bars(prior_rows),
    )
    candidate = SimpleNamespace(
        code=_CODE,
        factor_snapshot={
            "close": entry_close,
            "bottom_divergence_v2_stage": "early",
            "bottom_divergence_v2_candidate_version": "structure",
            "bottom_divergence_v2_near_zone_lower": 19.0,
        },
        trade_stage="probe_entry",
        trade_plan_json=json.dumps({"initial_position": "目标仓位20%"}),
        market_regime="balanced",
    )
    return _build_validation_sample(
        candidate=candidate,
        signal_date=signal_date,
        strategy_version="v2",
        config=config,
        stock_repository=repository,
    )


def test_dividend_inside_the_forward_window_is_not_counted_as_a_loss():
    """承重：前瞻窗口内一次 10 送 10 不得被算成 -50% 的亏损。

    这是落点 2 存在的全部理由。全表有 1145 个送转事件，漏掉这条会让回测照常
    产出样本、每个样本都带着 33%~66% 的假亏损——「验收通过但数值全错」。

    对照组把开关关掉，让假亏损原形毕露：两个断言一起才有约束力，只留复权那
    一条时，一个恒返回 0 的实现也能骗过去。
    """
    adjusted = _dividend_sample(Config())
    assert adjusted.evaluator_return_5d == pytest.approx(0.0, abs=1e-9)
    assert adjusted.mae_20d == pytest.approx(0.0, abs=1e-9)
    assert adjusted.close_20d == pytest.approx(20.0)
    assert adjusted.future_lows_20d[-1] == pytest.approx(20.0)

    raw = _dividend_sample(Config(adj_apply_on_read=False))
    assert raw.evaluator_return_5d == pytest.approx(-50.0)
    assert raw.mae_20d == pytest.approx(-50.0)
    assert raw.close_20d == pytest.approx(10.0)


def test_forward_window_keeps_real_moves_after_the_dividend():
    """复权不能把真实涨跌也抹平——只该抹掉除权造成的台阶。"""
    signal_date = date(2026, 8, 5)
    rows = []
    close = 20.0
    for index in range(5):
        pre_close = close / 2.0 if index == 2 else close
        # 除权后第二天真涨 10%
        close = pre_close * 1.1 if index == 3 else pre_close
        rows.append({
            "date": signal_date + timedelta(days=index + 1),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "pre_close": pre_close,
            "volume": 1_000_000.0,
            "amount": close * 1_000_000.0,
        })

    adjusted = _adjusted_forward_bars(
        _bars(rows),
        signal_date=signal_date,
        anchor_close=20.0,
        config=Config(),
    )

    assert [bar.close for bar in adjusted] == pytest.approx(
        [20.0, 20.0, 20.0, 22.0, 22.0]
    )


def test_forward_window_fails_closed_on_a_broken_chain():
    """链条断了就整窗作废，不能拿原始价凑合算收益。

    断链意味着这段价格里有一处无法解释的跳变，跨过它算出来的收益是个错数；
    「这个样本没有前瞻窗口」比「这个样本有个错的收益」安全得多。
    """
    signal_date = date(2026, 8, 5)
    rows = [
        {
            "date": signal_date + timedelta(days=index + 1),
            "open": 20.0,
            "high": 20.0,
            "low": 20.0,
            "close": 20.0,
            # 第 3 根的 pre_close 高于前一日收盘：方向性错误，分红送转做不出来
            "pre_close": 25.0 if index == 2 else 20.0,
            "volume": 1_000_000.0,
            "amount": 20_000_000.0,
        }
        for index in range(5)
    ]

    assert _adjusted_forward_bars(
        _bars(rows),
        signal_date=signal_date,
        anchor_close=20.0,
        config=Config(),
    ) == []


def test_prior_window_dividend_does_not_inflate_volatility():
    """前置窗口里的除权同样是假波动，会污染波动率分层。

    前置窗口按自身末行归一（拿不到 pre_close(D)），与按 D 归一只差一个常数，
    而收益率的标准差对常数免疫——这条测试钉的正是「窗口内部的跳空被抹平」。
    """
    from src.backtest.services.bottom_divergence_v2_replay import (
        compute_pre_signal_features,
    )

    rows = []
    close = 20.0
    for index in range(20):
        pre_close = close / 2.0 if index == 10 else close
        close = pre_close
        rows.append({
            "date": date(2026, 7, 1) + timedelta(days=index),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "pre_close": pre_close,
            "volume": 1_000_000.0,
            "amount": 1_000_000.0,
        })

    raw_volatility, raw_liquidity = compute_pre_signal_features(
        _adjusted_prior_bars(_bars(rows), config=Config(adj_apply_on_read=False))
    )
    volatility, liquidity = compute_pre_signal_features(
        _adjusted_prior_bars(_bars(rows), config=Config())
    )

    assert raw_volatility == pytest.approx(0.1147, abs=1e-4)
    assert volatility == pytest.approx(0.0, abs=1e-12)
    # amount 不受复权影响，两侧必须完全一致
    assert liquidity == pytest.approx(raw_liquidity)


def test_switch_is_registered_in_the_base_factor_whitelist():
    """漏登记会让切换开关后读回上一次的 base 快照——是算错，不是算慢。"""
    assert "adj_apply_on_read" in BASE_FACTOR_CONFIG_FIELDS


def test_adjustment_switch_changes_both_the_window_and_the_base_key():
    """开关必须同时改变 base 快照键与 base 快照内容。

    `test_base_factor_cache_key_whitelist` 的变异测试**结构上**够不到这个
    字段：它直接调 `build_factor_snapshot_from_groups`，绕开了施加复权的
    `_window`，而它的夹具也没有 `pre_close` 列。所以这个字段由这条针对取窗
    行为的测试守，分工同 `screening_factor_lookback_days`。
    """
    # 除权放在倒数第 3 根：`volume_ratio` 是「最新量 / 前 5 日均量」
    # （`factor_service.py:196-198`），只有让除权落进那 5 天，成交量的同步
    # 缩放才会显形。
    groups = {_CODE: _frame(_raw_bars(count=60, split_offset=3))}
    universe = _universe()
    cache = ValidationFactorCache.from_groups(
        data_version="fixture",
        trade_dates=(_TRADE_DATE,),
        bar_groups=groups,
    )
    on = Config(adj_apply_on_read=True)
    off = Config(adj_apply_on_read=False)

    assert cache._base_config_hash(on) != cache._base_config_hash(off), (
        "开关决定 base 快照内容，却没有进 base 快照键"
    )

    adjusted = cache.build_factor_snapshot(
        config=on, universe=universe, trade_date=_TRADE_DATE
    )
    raw = cache.build_factor_snapshot(
        config=off, universe=universe, trade_date=_TRADE_DATE
    )

    assert cache.stats["base_snapshot_builds"] == 2, (
        "两个开关取值共用了同一份 base 快照文件"
    )
    assert not adjusted.equals(raw), "开关没有改变 base 快照内容"
    # 除权前的 5 日均量被放大，量比因此不同：这正是 4.3 要修的那个虚增。
    assert float(adjusted.iloc[0]["volume_ratio"]) != pytest.approx(
        float(raw.iloc[0]["volume_ratio"])
    )


def test_second_application_is_refused_rather_than_squared():
    """已施加过的窗口不许再施加一次。

    回测侧 `_window` 施加、`build_factor_snapshot_from_groups` 再施加一次的
    话因子会被平方，而结果依然「有因子、非空、为正」，能顺利通过下游门禁。
    """
    once = apply_read_adjustment(_frame(_raw_bars(count=30)))
    twice = apply_read_adjustment(once)

    pd.testing.assert_frame_equal(once, twice)


def test_window_without_pre_close_is_marked_unadjustable():
    """缺 pre_close 列时既不施加也不打可信标记。

    取数时的 `adj_factor_source` 必须被换掉：`tushare_native` 已经在
    `factor_service` 的信任白名单里，留着它等于让一段没复权的原始价顶着
    可信标记穿过门禁。
    """
    frame = _frame(_raw_bars(count=30)).drop(columns=["pre_close"])
    frame["adj_factor_source"] = "tushare_native"

    marked = apply_read_adjustment(frame)

    assert set(marked["adj_factor_source"]) == {ANOMALOUS_SOURCE}
    assert marked["adj_factor"].isna().all()
    assert list(marked["close"]) == list(frame["close"])
