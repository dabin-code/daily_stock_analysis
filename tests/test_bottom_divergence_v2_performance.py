# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from src.backtest.services.bottom_divergence_v2_performance import (
    BASE_FACTOR_CONFIG_FIELDS,
    CanonicalCheckpointStore,
    CheckpointCorruptionError,
    CheckpointMismatchError,
    FrozenEvidenceCacheKey,
    ValidationFactorCache,
    replay_batch_from_payload,
    replay_batch_to_payload,
    validation_checkpoint_config_hash,
)
from src.backtest.services.bottom_divergence_v2_metrics import (
    summarize_validation_samples,
)
from src.backtest.services.bottom_divergence_v2_models import (
    CandidateEventEvidence,
    ValidationSample,
    fit_tertile_boundaries,
)
from src.backtest.services.bottom_divergence_v2_replay import (
    ReplayBatch,
    ReplayDependencies,
    _event_dates,
    replay_historical_dates,
)
from src.backtest.services.bottom_divergence_v2_report import (
    canonical_json_dumps,
)
from src.config import Config
from src.services.factor_service import FactorService


def _bars(code: str, size: int = 170) -> pd.DataFrame:
    rng = np.random.RandomState(sum(ord(item) for item in code))
    close = np.linspace(30.0, 38.0, size) + rng.normal(0, 0.8, size)
    return pd.DataFrame({
        "code": code,
        "date": pd.bdate_range("2025-01-01", periods=size).date,
        "open": close - 0.2,
        "high": close + 0.8,
        "low": close - 0.8,
        "close": close,
        "volume": rng.randint(100_000, 500_000, size),
        "amount": close * rng.randint(100_000, 500_000, size),
        "pct_chg": 0.0,
        "data_source": "fixture",
        "adj_factor": 1.0,
        "adj_factor_source": "tushare_native",
    })


def test_factor_cache_matches_uncached_results_and_isolates_parameter_hash():
    groups = {
        "000002": _bars("000002"),
        "000001": _bars("000001"),
    }
    trade_date = groups["000001"].iloc[-2]["date"]
    universe = pd.DataFrame([
        {"code": "000002", "name": "B"},
        {"code": "000001", "name": "A"},
    ])
    base = Config(bottom_divergence_v2_enabled=True)
    first = replace(
        base,
        bottom_divergence_v2_cluster_pct=0.01,
        bottom_divergence_v2_zone_score_min=0.4,
    )
    second = replace(
        base,
        bottom_divergence_v2_cluster_pct=0.02,
        bottom_divergence_v2_zone_score_min=0.5,
    )
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
    )

    for config in (first, second):
        expected = FactorService(
            config=config,
        ).build_factor_snapshot_from_groups(
            universe,
            {
                code: frame[frame["date"] <= trade_date]
                for code, frame in groups.items()
            },
            trade_date=trade_date,
        ).sort_values("code").reset_index(drop=True)
        actual = cache.build_factor_snapshot(
            config=config,
            universe=universe,
            trade_date=trade_date,
        )
        pd.testing.assert_frame_equal(actual, expected)

    assert cache.stats["base_snapshot_builds"] == 1
    assert cache.stats["frozen_evidence_builds"] == 2
    assert cache.stats["parameter_evaluations"] == 4
    assert cache.stats["sql_bar_queries"] == 0
    assert list(actual["code"]) == ["000001", "000002"]


def test_v1_and_v2_legs_share_one_base_factor_pass():
    """两条不同策略的 leg 必须共用同一次基础因子计算。

    这是历史重放在算力上能否成立的前提，不是优化项：实测 500 股单日因子
    14.8 秒，串行外推全期约 82 小时，每条 leg 各算一遍因子直接不可行。

    成立的原因是 base 缓存键剔除了 `bottom_divergence_v2_enabled`，
    v1（关闭）与 v2（开启）因此落在同一个 base 分区上。既有用例只覆盖了
    同策略不同网格参数的情形，而 CLI 真正跑的第一条 leg 是 v1，
    把它加回缓存键不会让任何结果变错，只会让全期成本翻倍——静默且昂贵。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-2]["date"]
    universe = pd.DataFrame([{"code": "000001", "name": "A"}])
    v1_config = Config(bottom_divergence_v2_enabled=False)
    v2_config = Config(bottom_divergence_v2_enabled=True)

    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
    )

    cache.build_factor_snapshot(
        config=v1_config, universe=universe, trade_date=trade_date
    )
    builds_after_v1 = cache.stats["base_snapshot_builds"]

    cache.build_factor_snapshot(
        config=v2_config, universe=universe, trade_date=trade_date
    )

    assert builds_after_v1 == 1, "第一条 leg 应当算一次基础因子"
    assert cache.stats["base_snapshot_builds"] == 1, (
        "第二条 leg 重算了基础因子，跨策略的因子复用已失效"
    )
    assert cache.stats["sql_bar_queries"] == 0


def test_base_snapshot_key_ignores_fields_the_base_pass_never_reads():
    """与 base 快照无关的字段不该进入 base 快照键。

    base pass 强制关闭 v2（`bottom_divergence_v2_performance.py:447-450`），
    所以 v2 的这些参数不可能改变 base 快照的取值。
    """
    left = Config(bottom_divergence_v2_sync_window=3)
    right = Config(bottom_divergence_v2_sync_window=9)

    assert ValidationFactorCache._base_config_hash(
        left
    ) == ValidationFactorCache._base_config_hash(right)


def test_evidence_key_still_separates_v2_evidence_parameters():
    """证据层的键必须继续区分 v2 参数——这层不能跟着收窄。

    `sync_window` 决定 `major.confirmed`
    （`causal_bottom_divergence_detector.py:1174`），`retention_bars` 在冻结阶段
    就被烘进证据（`:679`）。它们只由这个键覆盖，`_parameter_hash` 不含它们；
    一旦收窄，缓存会把一套参数的证据当成另一套参数的结果返回。
    """
    left = Config(bottom_divergence_v2_sync_window=3)
    right = Config(bottom_divergence_v2_sync_window=9)

    assert ValidationFactorCache._config_hash(
        left
    ) != ValidationFactorCache._config_hash(right)


def test_base_snapshot_key_tracks_every_field_the_base_pass_reads():
    """base 路径真正读取的字段必须改变 base 快照键。"""
    baseline = Config()
    for field_name in BASE_FACTOR_CONFIG_FIELDS:
        current = getattr(baseline, field_name)
        mutated = replace(baseline, **{field_name: current + 1})
        assert ValidationFactorCache._base_config_hash(
            baseline
        ) != ValidationFactorCache._base_config_hash(mutated), (
            f"{field_name} 影响 base 因子，却没有进入 base 快照键"
        )


def test_base_snapshot_path_separates_different_universes():
    """universe 是 base 快照的输入，不同 universe 不得复用同一份缓存文件。

    base 快照按 code 逐行构成。键里不含 universe 时，两条 leg 若配了不同的
    预筛 universe 却撞上同一个 config 哈希，第二条会读回第一条的行集合。
    """
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(date(2024, 3, 1),),
        bar_groups={},
    )
    left = cache._base_path(
        date(2024, 3, 1),
        "same-hash",
        universe=pd.DataFrame([{"code": "000001", "name": "A"}]),
    )
    right = cache._base_path(
        date(2024, 3, 1),
        "same-hash",
        universe=pd.DataFrame([
            {"code": "000001", "name": "A"},
            {"code": "000002", "name": "B"},
        ]),
    )
    assert left != right


# universe 里除 code 之外同样会改变 base 快照取值的列，以及一对确实不同的取值。
# `code` 之外这四列的消费点：`list_date` 在 `factor_service.py:183-188` 决定
# `days_since_listed`，进而经 `:207-212` 影响 `risk_flags`；`is_st` 在 `:208`
# 与 `:231`；`circ_mv` 在 `:230`；`name` 在 `:219`。
_UNIVERSE_METADATA_VARIANTS = (
    ("list_date", "2015-01-05", "2025-06-02"),
    ("is_st", False, True),
    ("circ_mv", 1.0e9, 2.0e9),
    ("name", "A", "B"),
)


def _one_stock_universe(**overrides) -> pd.DataFrame:
    return pd.DataFrame([{"code": "000001", **overrides}])


@pytest.mark.parametrize(
    ("column", "left_value", "right_value"),
    _UNIVERSE_METADATA_VARIANTS,
)
def test_universe_metadata_changes_the_base_snapshot_contents(
    column,
    left_value,
    right_value,
):
    """先证明这几列真的会改变 base 快照，键的断言才不是空对空。

    没有这条打底，「不同 universe 走不同缓存文件」就只是在钉一个实现细节：
    万一某列其实不影响输出，分键只是白费算力而非修正正确性。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-1]["date"]
    service = FactorService(db_manager=MagicMock(), config=Config())

    left = service.build_factor_snapshot_from_groups(
        _one_stock_universe(**{column: left_value}),
        groups,
        trade_date=trade_date,
    )
    right = service.build_factor_snapshot_from_groups(
        _one_stock_universe(**{column: right_value}),
        groups,
        trade_date=trade_date,
    )

    assert not left.empty and not right.empty, "夹具没产出行，测试没覆盖到目标"
    assert not left.equals(right), (
        f"universe 的 {column} 列不影响 base 快照，"
        f"这条参数化用例的前提不成立，需要换取值或删掉它"
    )


@pytest.mark.parametrize(
    ("column", "left_value", "right_value"),
    _UNIVERSE_METADATA_VARIANTS,
)
def test_base_snapshot_path_separates_universe_metadata(
    column,
    left_value,
    right_value,
):
    """只哈希 code 不够：同一批 code 配不同元数据也是不同的输入。

    两个 universe 的 code 集合完全一致，只有元数据不同。键若只覆盖 code，
    第二次调用会读回第一次的 pickle——正是本计划要消灭的
    「拿一个输入的结果当另一个输入的结果」。
    """
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(date(2024, 3, 1),),
        bar_groups={},
    )

    left = cache._base_path(
        date(2024, 3, 1),
        "same-hash",
        universe=_one_stock_universe(**{column: left_value}),
    )
    right = cache._base_path(
        date(2024, 3, 1),
        "same-hash",
        universe=_one_stock_universe(**{column: right_value}),
    )

    assert left != right, (
        f"universe 只差 {column} 却撞上同一份 base 缓存文件"
    )


@pytest.mark.parametrize(
    ("column", "left_value", "right_value"),
    _UNIVERSE_METADATA_VARIANTS,
)
def test_cache_rebuilds_the_base_snapshot_for_changed_universe_metadata(
    column,
    left_value,
    right_value,
):
    """端到端：同一个 cache 换 universe 元数据后必须重算而不是读回旧行。

    这条比路径相等性更硬——它比对的是真正返回给调用方的快照内容。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-1]["date"]
    config = Config(bottom_divergence_v2_enabled=False)
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
    )

    first = cache.build_factor_snapshot(
        config=config,
        universe=_one_stock_universe(**{column: left_value}),
        trade_date=trade_date,
    )
    second = cache.build_factor_snapshot(
        config=config,
        universe=_one_stock_universe(**{column: right_value}),
        trade_date=trade_date,
    )

    assert not first.equals(second), (
        f"universe 的 {column} 变了，缓存却返回了同一份 base 快照"
    )
    assert cache.stats["base_snapshot_builds"] == 2, (
        "第二个 universe 命中了第一个的 base 缓存文件"
    )


def test_absent_universe_column_differs_from_a_present_null():
    """列缺失与列存在但为空不是一回事，先证明它们的输出真的不同。

    `factor_service.py:208` 走 `bool(info.get("is_st", False))`：
    缺列拿到 `False`，列存在但为 `NaN` 时 `bool(nan)` 是 `True`，
    还会多出一个 `st` 风险标记（`:231`、`:207-212`）。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-1]["date"]
    service = FactorService(db_manager=MagicMock(), config=Config())

    absent = service.build_factor_snapshot_from_groups(
        _one_stock_universe(name="A"),
        groups,
        trade_date=trade_date,
    )
    present_null = service.build_factor_snapshot_from_groups(
        _one_stock_universe(name="A", is_st=float("nan")),
        groups,
        trade_date=trade_date,
    )

    assert not absent.equals(present_null), (
        "缺 is_st 列与 is_st 为 NaN 的输出相同，这条前提不成立"
    )


def test_base_snapshot_path_separates_absent_column_from_present_null():
    """指纹必须区分「没有这列」与「有这列但值为空」。"""
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(date(2024, 3, 1),),
        bar_groups={},
    )

    absent = cache._base_path(
        date(2024, 3, 1),
        "same-hash",
        universe=_one_stock_universe(name="A"),
    )
    present_null = cache._base_path(
        date(2024, 3, 1),
        "same-hash",
        universe=_one_stock_universe(name="A", is_st=float("nan")),
    )

    assert absent != present_null, (
        "缺 is_st 列与 is_st 为 NaN 撞了同一份 base 缓存文件"
    )


def test_base_snapshot_path_includes_the_data_version():
    """bar 数据决定 base 快照，data_version 必须进 base 快照文件名。

    证据两层的键都带 `data_version`（`FrozenEvidenceCacheKey` 与
    `_temporary_frozen_key`），base 快照此前没有。今天缓存目录恒为进程内
    临时目录所以撞不上，但键的正确性不该依赖这个偶然条件——阶段 2 计划开
    跨进程复用，届时同一目录下两个 data_version 会直接读到彼此的快照。
    """
    trade_date = date(2024, 3, 1)
    universe = _one_stock_universe(name="A")
    left = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups={},
    )
    right = ValidationFactorCache.from_groups(
        data_version="data-b",
        trade_dates=(trade_date,),
        bar_groups={},
    )

    # 两个 cache 各有自己的临时目录，所以只比文件名。
    assert (
        left._base_path(trade_date, "same-hash", universe=universe).name
        != right._base_path(trade_date, "same-hash", universe=universe).name
    ), "两个 data_version 生成了同一个 base 快照文件名"


def test_evidence_layer_is_not_collapsed_by_the_narrowed_base_key():
    """证据层若跟着 base 键收窄，两套 v2 参数会共用同一份冻结证据。

    这两个 config 只差 `bottom_divergence_v2_sync_window`，它不在
    `BASE_FACTOR_CONFIG_FIELDS` 里（base pass 强制关闭 v2，它确实不影响
    base 快照），但它决定 v2 证据的取值。因此 base 快照只算一次，
    而冻结证据必须各算一次。

    误伤形态取决于把三处证据键（`config_hash=base_hash`）改成
    `base_snapshot_hash` 的范围：只改 `_temporary_frozen_key` 那一处时，
    第二次调用会复用第一次冻结的证据，随后在 `evaluate_frozen_evidence`
    抛 `ValueError("frozen causal evidence parameter mismatch")`；三处全改
    才会安静地退化成 `frozen_evidence_builds == 1`。两种都算捕获成功。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-2]["date"]
    universe = pd.DataFrame([{"code": "000001", "name": "A"}])
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
    )

    for sync_window in (3, 9):
        cache.build_factor_snapshot(
            config=Config(
                bottom_divergence_v2_enabled=True,
                bottom_divergence_v2_sync_window=sync_window,
            ),
            universe=universe,
            trade_date=trade_date,
        )

    assert cache.stats["frozen_evidence_builds"] == 2, (
        "两套 v2 参数共用了同一份冻结证据，证据层缓存键被收窄了"
    )
    assert cache.stats["base_snapshot_builds"] == 1, (
        "sync_window 不影响 base 快照，两次调用应共用同一份 base 缓存"
    )


def test_cache_keys_isolate_data_candidate_asof_algorithm_and_parameter():
    group = _bars("000001")
    dates = (group.iloc[-2]["date"], group.iloc[-1]["date"])
    universe = pd.DataFrame([{"code": "000001", "name": "A"}])
    first = Config(
        bottom_divergence_v2_enabled=True,
        bottom_divergence_v2_zone_score_min=0.4,
    )
    second = replace(
        first,
        bottom_divergence_v2_zone_score_min=0.5,
    )
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=dates,
        bar_groups={"000001": group},
    )
    for trade_date in dates:
        for config in (first, second):
            cache.build_factor_snapshot(
                config=config,
                universe=universe,
                trade_date=trade_date,
            )

    frozen_keys = cache.frozen_cache_keys
    evaluation_keys = cache.evaluation_cache_keys
    assert frozen_keys
    assert evaluation_keys
    assert all(isinstance(key, FrozenEvidenceCacheKey) for key in frozen_keys)
    assert all(key.data_version == "data-a" for key in frozen_keys)
    assert all(key.code == "000001" for key in frozen_keys)
    assert all(key.candidate_version != "frozen" for key in frozen_keys)
    assert all(key.algorithm_version for key in frozen_keys)
    assert {key.as_of_index for key in frozen_keys} == {
        len(group[group["date"] <= item]) - 1 for item in dates
    }
    assert all(key.parameter_hash is None for key in frozen_keys)
    assert len({key.parameter_hash for key in evaluation_keys}) == 2
    assert {
        (
            key.data_version,
            key.code,
            key.candidate_version,
            key.as_of_index,
            key.algorithm_version,
        )
        for key in evaluation_keys
    } == {
        (
            key.data_version,
            key.code,
            key.candidate_version,
            key.as_of_index,
            key.algorithm_version,
        )
        for key in frozen_keys
    }


def test_factor_cache_does_not_read_bars_after_trade_date():
    group = _bars("000001")
    trade_date = group.iloc[-5]["date"]
    universe = pd.DataFrame([{"code": "000001", "name": "A"}])
    config = Config(bottom_divergence_v2_enabled=True)
    expected_cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups={"000001": group},
    )
    changed = group.copy()
    changed.loc[changed["date"] > trade_date, "close"] = 9999.0
    changed_cache = ValidationFactorCache.from_groups(
        data_version="data-b",
        trade_dates=(trade_date,),
        bar_groups={"000001": changed},
    )

    expected = expected_cache.build_factor_snapshot(
        config=config,
        universe=universe,
        trade_date=trade_date,
    )
    actual = changed_cache.build_factor_snapshot(
        config=config,
        universe=universe,
        trade_date=trade_date,
    )
    pd.testing.assert_frame_equal(actual, expected)


@pytest.mark.skipif(
    __import__("sys").platform != "win32",
    reason="Windows spawn contract",
)
def test_factor_cache_windows_spawn_workers_keep_stable_order():
    groups = {
        "000002": _bars("000002", 100),
        "000001": _bars("000001", 100),
    }
    trade_date = groups["000001"].iloc[-1]["date"]
    universe = pd.DataFrame([
        {"code": "000002", "name": "B"},
        {"code": "000001", "name": "A"},
    ])
    config = Config(bottom_divergence_v2_enabled=True)
    serial_cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
        workers=1,
    )
    parallel_cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
        workers=12,
    )
    serial = serial_cache.build_factor_snapshot(
        config=config,
        universe=universe,
        trade_date=trade_date,
    )
    parallel = parallel_cache.build_factor_snapshot(
        config=config,
        universe=universe,
        trade_date=trade_date,
    )
    serial_cache.close()
    parallel_cache.close()

    pd.testing.assert_frame_equal(parallel, serial)
    assert list(parallel["code"]) == ["000001", "000002"]


def test_progress_on_and_off_produce_identical_canonical_results():
    groups = {
        "000002": _bars("000002", 100),
        "000001": _bars("000001", 100),
    }
    trade_date = groups["000001"].iloc[-1]["date"]
    universe = pd.DataFrame([
        {"code": "000002", "name": "B"},
        {"code": "000001", "name": "A"},
    ])
    config = Config(bottom_divergence_v2_enabled=True)
    events = []
    silent_cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
        progress_callback=None,
    )
    verbose_cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
        progress_every=1,
        progress_callback=events.append,
    )
    silent = silent_cache.build_factor_snapshot(
        config=config,
        universe=universe,
        trade_date=trade_date,
    )
    verbose = verbose_cache.build_factor_snapshot(
        config=config,
        universe=universe,
        trade_date=trade_date,
    )

    pd.testing.assert_frame_equal(verbose, silent)
    assert events
    assert events[-1]["completed"] == events[-1]["total"] == 2


def test_001337_nonzero_fixture_is_field_equivalent_through_cache_and_resume():
    fixture = pd.read_csv(
        Path(__file__).parent
        / "fixtures"
        / "001337_bottom_divergence_20251201_20260805.csv",
        parse_dates=["date"],
    )
    fixture["code"] = "001337"
    fixture["amount"] = fixture["close"] * fixture["volume"]
    trade_dates = (
        date(2026, 7, 22),
        date(2026, 7, 23),
        date(2026, 8, 5),
    )
    universe = pd.DataFrame([{"code": "001337", "name": "四川黄金"}])
    config = Config(
        bottom_divergence_v2_enabled=True,
        screening_factor_lookback_days=365,
    )
    cache = ValidationFactorCache.from_groups(
        data_version="001337-fixture-v1",
        trade_dates=trade_dates,
        bar_groups={"001337": fixture},
    )
    records_by_date = {}
    for trade_date in trade_dates:
        visible = fixture[fixture["date"].dt.date <= trade_date]
        baseline = FactorService(
            config=config,
        ).build_factor_snapshot_from_groups(
            universe,
            {"001337": visible},
            trade_date=trade_date,
        )
        optimized = cache.build_factor_snapshot(
            config=config,
            universe=universe,
            trade_date=trade_date,
        )
        pd.testing.assert_frame_equal(
            optimized,
            baseline,
            check_dtype=False,
        )
        records = optimized.iloc[0][
            "bottom_divergence_v2_candidate_records"
        ]
        assert records
        records_by_date[trade_date] = records

    final_record = records_by_date[trade_dates[-1]][0]
    candidate_version = final_record["candidate_version"]
    future_dates = tuple(
        trade_dates[-1] + timedelta(days=index)
        for index in range(1, 21)
    )
    sample = ValidationSample(
        code="001337",
        signal_date=trade_dates[0],
        candidate_version=candidate_version,
        strategy_version="v2",
        stage="early",
        entry_close=36.60,
        near_zone_lower=37.46,
        major_zone_lower=40.61,
        early_event_date=trade_dates[0],
        near_cleared_event_date=trade_dates[1],
        major_breakout_event_date=trade_dates[2],
        close_5d=40.0,
        close_10d=42.0,
        close_20d=43.0,
        future_closes_20d=(40.0,) * 20,
        future_highs_20d=(44.0,) * 20,
        future_lows_20d=(35.0,) * 20,
        max_high_20d=44.0,
        min_low_20d=35.0,
        market_regime="balanced",
        volatility=0.02,
        liquidity=10_000_000.0,
        future_trade_dates_20d=future_dates,
        breakout_floor=37.46,
        position_weight=0.2,
    )
    evidence = CandidateEventEvidence(
        code="001337",
        candidate_version=candidate_version,
        near_cleared_event_date=trade_dates[1],
        major_breakout_event_date=trade_dates[2],
    )
    baseline_batch = ReplayBatch(
        samples=(sample,),
        opportunity_counts={trade_dates[0]: 1},
        event_evidence=(evidence,),
    )
    payload = replay_batch_to_payload(baseline_batch)
    restored = replay_batch_from_payload(payload)
    assert [asdict(item) for item in restored.samples] == [
        asdict(item) for item in baseline_batch.samples
    ]
    assert [asdict(item) for item in restored.event_evidence] == [
        asdict(item) for item in baseline_batch.event_evidence
    ]
    assert restored.opportunity_counts == baseline_batch.opportunity_counts
    boundaries = fit_tertile_boundaries((sample,))
    metric_kwargs = {
        "boundaries": boundaries,
        "opportunity_count": 1,
        "buy_cost_bps": 1.0,
        "sell_cost_bps": 1.0,
        "slippage_bps": 1.0,
    }
    baseline_metrics = summarize_validation_samples(
        baseline_batch.samples,
        **metric_kwargs,
    )
    restored_metrics = summarize_validation_samples(
        restored.samples,
        **metric_kwargs,
    )
    assert restored_metrics == baseline_metrics
    assert baseline_metrics["early"]["sample_count"] == 1
    canonical = canonical_json_dumps(payload)
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_event_dates_use_frozen_candidate_record_not_dataframe_index_dtype():
    version = "candidate-a"

    class ForbiddenRepository:
        def get_range(self, *_args, **_kwargs):
            raise AssertionError("frozen candidate dates must be authoritative")

    result = _event_dates(
        factor={
            "bottom_divergence_v2_candidate_version": version,
            "bottom_divergence_v2_early_event_index": 101.0,
            "bottom_divergence_v2_near_event_index": 102.0,
            "bottom_divergence_v2_major_event_index": 111.0,
            "bottom_divergence_v2_candidate_records": [{
                "candidate_version": version,
                "early_reversal": {"date": "2026-07-22"},
                "near_zone_events": {
                    "cleared_confirmed": {"date": "2026-07-23"},
                },
                "major_zone_breakout": {
                    "date": "2026-08-05",
                    "confirmed": True,
                },
            }],
        },
        code="001337",
        signal_date=date(2026, 8, 5),
        config=Config(bottom_divergence_v2_enabled=True),
        stock_repository=ForbiddenRepository(),
    )
    assert result == {
        "early": date(2026, 7, 22),
        "r1": date(2026, 7, 23),
        "r2": date(2026, 8, 5),
    }


def test_unconfirmed_major_bar_is_not_an_r2_event():
    version = "candidate-a"

    class ForbiddenRepository:
        def get_range(self, *_args, **_kwargs):
            raise AssertionError("frozen candidate dates must be authoritative")

    result = _event_dates(
        factor={
            "bottom_divergence_v2_candidate_version": version,
            "bottom_divergence_v2_candidate_records": [{
                "candidate_version": version,
                "early_reversal": {"date": "2026-07-22"},
                "near_zone_events": {
                    "cleared_confirmed": {"date": "2026-07-23"},
                },
                "major_zone_breakout": {
                    "date": "2026-08-05",
                    "bar_index": 164,
                    "confirmed": False,
                },
            }],
        },
        code="001337",
        signal_date=date(2026, 8, 5),
        config=Config(bottom_divergence_v2_enabled=True),
        stock_repository=ForbiddenRepository(),
    )
    assert result == {
        "early": date(2026, 7, 22),
        "r1": date(2026, 7, 23),
        "r2": None,
    }


@pytest.mark.parametrize(
    ("confirmed", "expected_samples", "expected_event"),
    [
        (False, 0, None),
        (True, 1, date(2026, 8, 5)),
    ],
)
def test_major_confirmation_controls_replay_sample_and_event_evidence(
    confirmed,
    expected_samples,
    expected_event,
):
    signal_date = date(2026, 8, 5)
    version = "candidate-a"
    factor = {
        "code": "001337",
        "close": 43.27,
        "bottom_divergence_v2_stage": "major",
        "bottom_divergence_v2_candidate_version": version,
        "bottom_divergence_v2_near_zone_lower": 37.46,
        "bottom_divergence_v2_major_zone_lower": 40.61,
        "bottom_divergence_v2_candidate_records": [{
            "candidate_version": version,
            "early_reversal": {"date": "2026-07-22"},
            "near_zone_events": {
                "cleared_confirmed": {"date": "2026-07-23"},
            },
            "major_zone_breakout": {
                "date": signal_date.isoformat(),
                "bar_index": 164,
                "confirmed": confirmed,
            },
        }],
    }
    candidate = __import__("types").SimpleNamespace(
        code="001337",
        factor_snapshot=factor,
        trade_stage="add_on_strength",
        trade_plan_json=json.dumps({"initial_position": "目标仓位100%"}),
        market_regime="balanced",
    )
    bars = [
        __import__("types").SimpleNamespace(
            date=signal_date + timedelta(days=index),
            close=44.0,
            # 首根接回信号日收盘（factor["close"] = 43.27），其余与前一日收盘
            # 相等：窗口内无除权，复权因子恒为 1，这批 bar 的作用是钉样本与
            # 事件证据，不该顺带引入价格缩放。缺这一列会让前瞻窗口整段作废。
            pre_close=43.27 if index == 1 else 44.0,
            high=45.0,
            low=42.0,
            amount=10_000_000.0,
        )
        for index in range(1, 21)
    ]
    repository = __import__("types").SimpleNamespace(
        get_range=lambda *_args, **_kwargs: (),
        get_forward_bars=lambda **_kwargs: bars,
        get_prior_bars=lambda **_kwargs: bars,
    )
    dependencies = ReplayDependencies(
        db_manager=object(),
        factor_service_factory=lambda _config: __import__(
            "types"
        ).SimpleNamespace(
            build_factor_snapshot=lambda *_args, **_kwargs: pd.DataFrame(
                [factor]
            ),
        ),
        pipeline=__import__("types").SimpleNamespace(
            run=lambda **_kwargs: __import__("types").SimpleNamespace(
                candidates=[candidate]
            ),
        ),
        screener_factory=lambda _version: (object(), object()),
        market_context_provider=lambda *_args: (object(), object()),
        stock_repository=repository,
    )

    batch = replay_historical_dates(
        strategy_version="v2",
        config=Config(bottom_divergence_v2_enabled=True),
        trade_dates=(signal_date,),
        universe=pd.DataFrame([{"code": "001337"}]),
        dependencies=dependencies,
    )

    assert len(batch.samples) == expected_samples
    assert batch.event_evidence[0].major_breakout_event_date == expected_event


def test_checkpoint_is_atomic_resumable_and_rejects_identity_mismatch(
    tmp_path,
):
    path = tmp_path / "checkpoint.json"
    store = CanonicalCheckpointStore(
        path,
        data_version="data-a",
        config_hash="config-a",
    )
    store.save_partition(
        parameter_hash="param-a",
        partition="train",
        payload={"codes": ["000002", "000001"]},
    )

    assert not path.with_suffix(".json.tmp").exists()
    assert store.completed_partitions("param-a") == ("train",)
    assert store.load_partition("param-a", "train") == {
        "codes": ["000002", "000001"]
    }
    json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(CheckpointMismatchError):
        CanonicalCheckpointStore(
            path,
            data_version="data-b",
            config_hash="config-a",
        )


def test_checkpoint_identity_covers_complete_config_and_yaml(tmp_path):
    v1_path = tmp_path / "v1.yaml"
    v2_path = tmp_path / "v2.yaml"
    v1_path.write_text("version: v1\n", encoding="utf-8")
    v2_path.write_text("version: v2\n", encoding="utf-8")
    config = Config()
    kwargs = {
        "config": config,
        "date_from": "2026-01-01",
        "date_to": "2026-06-30",
        "market": "cn",
        "trading_dates": ["2026-01-05"],
        "universe_identity": {"codes": ["000001"], "count": 1},
        "data_version": "data-a",
        "costs": {
            "buy_cost_bps": 1.0,
            "sell_cost_bps": 1.0,
            "slippage_bps": 1.0,
        },
        "parameter_snapshots": {"hash-a": {"cluster_pct": 0.01}},
        "v1_strategy_path": v1_path,
        "v2_strategy_path": v2_path,
    }
    expected = validation_checkpoint_config_hash(**kwargs)
    assert validation_checkpoint_config_hash(**kwargs) == expected
    for field_name, value in (
        ("bottom_divergence_v2_sync_window", 4),
        ("bottom_divergence_v2_retention_bars", 21),
        ("bottom_divergence_v2_r1_weights", (0.1, 0.2, 0.7)),
        ("bottom_divergence_v2_r2_weights", (0.2, 0.3, 0.5)),
        ("bottom_divergence_v2_zone_score_min", 0.51),
    ):
        changed = dict(kwargs)
        changed["config"] = replace(config, **{field_name: value})
        assert validation_checkpoint_config_hash(**changed) != expected
    v2_path.write_text("version: v2-changed\n", encoding="utf-8")
    assert validation_checkpoint_config_hash(**kwargs) != expected
    changed = dict(kwargs, data_version="data-b")
    assert validation_checkpoint_config_hash(**changed) != expected


def test_checkpoint_recovers_valid_atomic_temp_after_main_corruption(tmp_path):
    path = tmp_path / "checkpoint.json"
    store = CanonicalCheckpointStore(
        path,
        data_version="data-a",
        config_hash="config-a",
    )
    store.save_partition(
        parameter_hash="param-a",
        partition="selection",
        payload={"samples": [1]},
    )
    valid = path.read_text(encoding="utf-8")
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(valid, encoding="utf-8")
    path.write_text("{corrupted", encoding="utf-8")

    recovered = CanonicalCheckpointStore(
        path,
        data_version="data-a",
        config_hash="config-a",
    )
    assert recovered.load_partition("param-a", "selection") == {
        "samples": [1]
    }
    assert not temporary.exists()
    json.loads(path.read_text(encoding="utf-8"))

    path.write_text("{corrupted", encoding="utf-8")
    with pytest.raises(CheckpointCorruptionError):
        CanonicalCheckpointStore(
            path,
            data_version="data-a",
            config_hash="config-a",
        )
