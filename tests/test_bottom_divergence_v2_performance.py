# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.backtest.services import bottom_divergence_v2_performance
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
from src.backtest.services.replay_strategies import BOTTOM_DIVERGENCE_V2
from src.config import Config
from src.services.adjustment_chain import (
    ANOMALOUS_SOURCE,
    TRUSTED_SOURCE,
)
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
    evidence_after_v1 = cache.stats["frozen_evidence_builds"]

    cache.build_factor_snapshot(
        config=v2_config, universe=universe, trade_date=trade_date
    )

    assert builds_after_v1 == 1, "第一条 leg 应当算一次基础因子"
    assert evidence_after_v1 == 0, (
        "v1 这条 leg 跑了 v2 的证据层；证据层归属由策略描述符决定，"
        "不该按配置以外的东西认领"
    )
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


def _adjustable_bars(
    code: str,
    size: int = 170,
    *,
    convention: str | None = "raw",
    with_pre_close: bool = True,
) -> pd.DataFrame:
    """`_bars` 加上复权链真正要读的两列，可按分支关掉其中任意一列。

    `_bars` 两列都不带，落在 `apply_read_adjustment` 的 fail-closed 分支上，
    覆盖不到「真的施加了复权」那一支。
    """
    frame = _bars(code, size)
    if with_pre_close:
        closes = frame["close"].to_numpy(dtype=float)
        frame["pre_close"] = np.concatenate(([closes[0]], closes[:-1]))
    if convention is not None:
        frame["adj_convention"] = convention
    return frame


@pytest.mark.parametrize(
    ("convention", "with_pre_close", "trusted"),
    [
        ("raw", True, True),
        # 口径守卫整窗拒绝：`mark_unadjustable` 只改列不改行。
        ("qfq", True, False),
        # 缺 `adj_convention` 列与非 raw 同等对待，同样整窗拒绝。
        (None, True, False),
        # 缺 `pre_close` 列走另一条 fail-closed 分支。
        ("raw", False, False),
    ],
)
def test_window_row_count_matches_the_adjusted_window_in_every_branch(
    convention,
    with_pre_close,
    trusted,
):
    """行数不变式：`_window_row_count` 必须等于真窗口的行数。

    这条不变式是 `as_of_index` 的地基。热路径只拿行数去拼
    `FrozenEvidenceCacheKey`，真正被评估的却是随后物化出来的窗口；两者一旦
    差一行，缓存键就指向了另一个窗口的证据——不报错，只算错。

    逐分支跑是必需的：`apply_read_adjustment` 有四条出口（正常施加、口径
    fail-closed、缺 `pre_close` fail-closed、空窗原样返回），只测其中一条
    等于赌另外三条也不改行数。
    """
    group = _adjustable_bars(
        "000001",
        90,
        convention=convention,
        with_pre_close=with_pre_close,
    )
    trade_date = group.iloc[-3]["date"]
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups={"000001": group},
    )
    try:
        # 依次是「整段都在窗口内」「窗口被 lookback 截断」「只剩回放日当天」。
        for lookback_days in (200, 20, 0):
            adjusted = cache._window(
                "000001", trade_date, lookback_days, adjust=True
            )
            unadjusted = cache._window(
                "000001", trade_date, lookback_days, adjust=False
            )
            counted = cache._window_row_count(
                "000001", trade_date, lookback_days
            )
            assert counted == len(adjusted) == len(unadjusted) > 0, (
                f"lookback={lookback_days} 上行数不一致，"
                "as_of_index 会与被评估的窗口错位"
            )
            expected_source = (
                TRUSTED_SOURCE if trusted else ANOMALOUS_SOURCE
            )
            assert (
                adjusted["adj_factor_source"] == expected_source
            ).all(), "夹具没有落在预期的复权分支上，这条参数化是空转的"

        # 取不到 bar 的 code：改动前是 `windows[code]` 拿到空 DataFrame，
        # 现在是行数 0，两者都必须让调用方走「不足 60 根」那条路。
        assert cache._window_row_count("999999", trade_date, 200) == 0
        assert cache._window("999999", trade_date, 200).empty
    finally:
        cache.close()


def test_a_base_snapshot_wider_than_the_universe_fails_loudly(tmp_path):
    """base 快照含 universe 之外的 code 时必须报错，不得静默返回超集。

    取窗改成按需物化之前，v2 循环里的 `windows[code]` 会对这种 code 抛
    `KeyError`——一道没人写但一直生效的结构性断言。换成 `_window_row_count`
    之后它消失了：多出来的 code 必然在 `_bar_groups` 里（它们的 base 行就是
    从那儿算出来的），于是会被正常评估并写进返回值，**调用方拿到一份超出自己
    universe 的快照且没有任何提示**。

    这条路今天不可达（base 快照的两个来源都以 `codes` 为上界）。它会变成活的，
    是在「多条 leg 共享同一次因子计算」从同策略多网格推广到多策略时：对 union
    universe 建一次 base 快照、各策略读自己的子集。这里直接把那一刻的形状写死。
    """
    groups = {"000001": _bars("000001"), "000002": _bars("000002")}
    trade_date = groups["000001"].iloc[-2]["date"]
    narrow_universe = pd.DataFrame([{"code": "000001", "name": "A"}])
    wide_universe = pd.DataFrame([
        {"code": "000001", "name": "A"},
        {"code": "000002", "name": "B"},
    ])
    # base 快照键只覆盖 8 字段白名单，v2 开关不在其中（base pass 强制关闭 v2），
    # 所以开与关落在同一个 base 文件上——正是下面覆写生效的前提。
    base_off = Config(bottom_divergence_v2_enabled=False)
    base_on = Config(bottom_divergence_v2_enabled=True)

    narrow_cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
        cache_directory=tmp_path / "narrow",
    )
    wide_cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
        cache_directory=tmp_path / "wide",
    )
    try:
        narrow_cache.build_factor_snapshot(
            config=base_off,
            universe=narrow_universe,
            trade_date=trade_date,
        )
        wide_snapshot = wide_cache.build_factor_snapshot(
            config=base_off,
            universe=wide_universe,
            trade_date=trade_date,
        )
        assert len(wide_snapshot) == 2, "夹具没造出两行，这条测试是空转的"

        base_files = list((tmp_path / "narrow").glob("base-*"))
        assert len(base_files) == 1, f"预期一个 base 快照文件，实际 {base_files}"
        wide_snapshot.to_pickle(base_files[0], compression="gzip")

        with pytest.raises(ValueError, match="universe 之外的 code"):
            narrow_cache.build_factor_snapshot(
                config=base_on,
                universe=narrow_universe,
                trade_date=trade_date,
            )
    finally:
        narrow_cache.close()
        wide_cache.close()


def test_a_fully_cached_snapshot_neither_takes_nor_adjusts_a_window():
    """三层全命中时，取窗与复权必须一次都不发生。

    取窗此前是 `build_factor_snapshot` 开头一个覆盖全 universe 的 dict
    comprehension，位置在 base 快照落盘判定与三层查表**之前**，于是什么都
    不重算的热运行也要把每一只都取窗并复权一遍。实测 15 只股池 32 天里这是
    7230 次调用、12.90s，其中复权 8.77s 占整趟 25%，而这些窗口除了行数之外
    没有任何消费方。

    这里钉的是「热路径不碰窗口」，不是「快了多少」：把物化改回提前执行，
    下面两个计数立刻非零。
    """
    groups = {"000001": _adjustable_bars("000001")}
    trade_date = groups["000001"].iloc[-1]["date"]
    universe = pd.DataFrame([{"code": "000001", "name": "A"}])
    config = Config(bottom_divergence_v2_enabled=True)
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
    )
    try:
        first = cache.build_factor_snapshot(
            config=config,
            universe=universe,
            trade_date=trade_date,
        )
        assert cache.stats["base_snapshot_builds"] == 1
        assert cache.stats["parameter_evaluations"] == 1

        calls = {"window": 0, "adjust": 0}
        original_window = ValidationFactorCache._window
        original_adjust = (
            bottom_divergence_v2_performance.apply_read_adjustment
        )

        def counting_window(self, *args, **kwargs):
            calls["window"] += 1
            return original_window(self, *args, **kwargs)

        def counting_adjust(frame):
            calls["adjust"] += 1
            return original_adjust(frame)

        with patch.object(
            ValidationFactorCache, "_window", counting_window
        ), patch.object(
            bottom_divergence_v2_performance,
            "apply_read_adjustment",
            counting_adjust,
        ):
            second = cache.build_factor_snapshot(
                config=config,
                universe=universe,
                trade_date=trade_date,
            )

        assert calls == {"window": 0, "adjust": 0}, (
            f"热运行仍在取窗/复权（{calls}），取窗又跑到查表前面去了"
        )
        assert cache.stats["base_snapshot_builds"] == 1
        assert cache.stats["frozen_evidence_builds"] == 1
        assert cache.stats["parameter_evaluations"] == 1
        pd.testing.assert_frame_equal(second, first)
    finally:
        cache.close()


def test_a_cold_snapshot_still_feeds_adjusted_windows_to_both_layers():
    """冷运行必须仍然把**已复权**的窗口喂给 base pass 与证据层。

    推迟物化的反向风险是推过头：某条路径拿到未复权窗口也不会报错，只会在
    带除权跳空的原始价上算因子。这里用同一份数据构造两个缓存——一个开
    `adj_apply_on_read`、一个关——若两者产出相同，说明复权根本没被施加。
    """
    groups = {"000001": _adjustable_bars("000001")}
    trade_date = groups["000001"].iloc[-1]["date"]
    universe = pd.DataFrame([{"code": "000001", "name": "A"}])
    adjusted_config = Config(
        bottom_divergence_v2_enabled=True,
        adj_apply_on_read=True,
    )
    caches = {}
    try:
        for name, config in (
            ("on", adjusted_config),
            ("off", replace(adjusted_config, adj_apply_on_read=False)),
        ):
            caches[name] = ValidationFactorCache.from_groups(
                data_version="data-a",
                trade_dates=(trade_date,),
                bar_groups=groups,
            )
            window = caches[name]._window(
                "000001",
                trade_date,
                config.screening_factor_lookback_days,
                adjust=config.adj_apply_on_read,
            )
            caches[name].build_factor_snapshot(
                config=config,
                universe=universe,
                trade_date=trade_date,
            )
            assert caches[name].stats["parameter_evaluations"] == 1
            if config.adj_apply_on_read:
                assert "adj_factor_source" in window.columns
                assert (
                    window["adj_factor_source"] == TRUSTED_SOURCE
                ).all()
            else:
                assert (
                    window["adj_factor_source"] != TRUSTED_SOURCE
                ).all()
    finally:
        for cache in caches.values():
            cache.close()


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
        strategy=BOTTOM_DIVERGENCE_V2,
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
        strategy=BOTTOM_DIVERGENCE_V2,
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


# ─── 持久化缓存目录：跨进程复用与失效条件 ──────────────────────────────
#
# `cache_directory` 透传之前，缓存目录恒为进程内临时目录，键不完备的后果只是
# 每次重算；透传之后缓存文件会跨进程、跨代码版本活下来，同一个缺口就变成
# 静默返回错数据。下面每一条失效条件都必须有独立的钉子。


def _build_once(
    *,
    cache_directory,
    data_version,
    groups,
    universe,
    trade_date,
    config,
):
    """模拟一次独立进程：构造 -> 算一天 -> close。返回三层计数与快照。"""
    cache = ValidationFactorCache.from_groups(
        data_version=data_version,
        trade_dates=(trade_date,),
        bar_groups=groups,
        cache_directory=cache_directory,
    )
    try:
        snapshot = cache.build_factor_snapshot(
            config=config,
            universe=universe,
            trade_date=trade_date,
        )
    finally:
        cache.close()
    return dict(cache.stats), snapshot


def test_a_second_run_reuses_a_persistent_factor_cache(tmp_path):
    """本次改动的承重测试：指向同一持久化目录的第二次运行不得重算。

    同一进程内多条 leg 早就共享一次因子计算，但两次独立构造此前不可能复用
    ——`from_groups` / `from_database` 都不透传 `cache_directory`，目录恒为
    临时目录。这条用例钉的就是「透传之后跨进程复用真的生效」，三层计数
    （base 快照 / 冻结证据 / 参数评估）必须全部归零，且结果与第一次逐值相等。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-2]["date"]
    universe = _one_stock_universe(name="A")
    config = Config(bottom_divergence_v2_enabled=True)
    arguments = {
        "cache_directory": tmp_path,
        "data_version": "data-a",
        "groups": groups,
        "universe": universe,
        "trade_date": trade_date,
        "config": config,
    }

    first_stats, first = _build_once(**arguments)
    assert first_stats["base_snapshot_builds"] == 1
    assert first_stats["frozen_evidence_builds"] == 1
    assert first_stats["parameter_evaluations"] == 1, (
        "第一次运行没算出三层产物，第二次的归零断言就是空对空"
    )

    second_stats, second = _build_once(**arguments)

    assert second_stats["base_snapshot_builds"] == 0, (
        "第二次运行重算了 base 快照，跨进程因子复用没有生效"
    )
    assert second_stats["frozen_evidence_builds"] == 0, (
        "第二次运行重算了冻结证据；分区文件要么没落盘，要么键对不上"
    )
    assert second_stats["parameter_evaluations"] == 0, (
        "第二次运行重算了参数评估层"
    )
    pd.testing.assert_frame_equal(second, first)


def test_the_default_temporary_directory_keeps_the_previous_behaviour(tmp_path):
    """不给 `cache_directory` 时行为必须与改动前一致：没有任何跨进程复用。

    默认路径不能被这次改动带着变——持久化是 opt-in 的能力，不是新默认值。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-2]["date"]
    universe = _one_stock_universe(name="A")
    config = Config(bottom_divergence_v2_enabled=True)
    arguments = {
        "cache_directory": None,
        "data_version": "data-a",
        "groups": groups,
        "universe": universe,
        "trade_date": trade_date,
        "config": config,
    }

    first_stats, first = _build_once(**arguments)
    second_stats, second = _build_once(**arguments)

    assert first_stats["base_snapshot_builds"] == 1
    assert second_stats["base_snapshot_builds"] == 1, (
        "默认（临时目录）行为下第二次运行复用了因子，默认行为被改变了"
    )
    assert second_stats["frozen_evidence_builds"] == 1
    assert second_stats["parameter_evaluations"] == 1
    # 复用与否不该改变结果，只该改变成本。
    pd.testing.assert_frame_equal(second, first)
    # 临时目录不落在用户指定的位置。
    assert not list(tmp_path.iterdir())


def test_close_deletes_a_temporary_cache_but_keeps_a_persistent_one(tmp_path):
    """两种目录的 `close()` 语义相反，必须分开处理。

    临时目录留下就是垃圾；持久化目录删掉等于这次改动白做——下一个进程
    什么也复用不到，而它恰恰是被显式要求保留的那个。
    """
    trade_date = date(2024, 3, 1)
    temporary_cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups={},
    )
    temporary_directory = temporary_cache._cache_directory
    assert temporary_directory.exists()
    temporary_cache.close()
    assert not temporary_directory.exists(), (
        "临时缓存目录没有随 close() 消失"
    )

    persistent_directory = tmp_path / "factor-cache"
    persistent_cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups={},
        cache_directory=persistent_directory,
    )
    assert persistent_cache._temporary_directory is None
    marker = persistent_directory / "base-marker.pkl.gz"
    marker.write_bytes(b"cached")
    persistent_cache.close()

    assert marker.exists(), "close() 删掉了持久化缓存目录里的内容"


def test_closing_twice_is_safe(tmp_path):
    """`close()` 在 `finally` 里被调用，重复调用不得炸掉调用方。

    临时目录那一支删完目录后若不清掉活动分区，第二次 close() 会去往一个
    已经不存在的目录落盘。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-2]["date"]
    for cache_directory in (None, tmp_path):
        cache = ValidationFactorCache.from_groups(
            data_version="data-a",
            trade_dates=(trade_date,),
            bar_groups=groups,
            cache_directory=cache_directory,
        )
        cache.build_factor_snapshot(
            config=Config(bottom_divergence_v2_enabled=True),
            universe=_one_stock_universe(name="A"),
            trade_date=trade_date,
        )
        cache.close()
        cache.close()


def test_persistent_cache_is_invalidated_by_the_base_algorithm_version(
    tmp_path,
    monkeypatch,
):
    """改了 base 因子的计算代码却不 bump 版本号，就会读回旧算法的快照。

    这是登记在进度文档 §8 的隐患：base 快照键此前只覆盖配置、universe 与
    `data_version`，计算代码本身没有任何来源。临时目录时代它是死条款，
    持久化之后是活漏洞。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-2]["date"]
    arguments = {
        "cache_directory": tmp_path,
        "data_version": "data-a",
        "groups": groups,
        "universe": _one_stock_universe(name="A"),
        "trade_date": trade_date,
        "config": Config(bottom_divergence_v2_enabled=False),
    }

    first_stats, _ = _build_once(**arguments)
    assert first_stats["base_snapshot_builds"] == 1
    # 对照组：什么都不改必须命中。没有它，「重算了」这条断言在缓存根本
    # 没生效时同样成立，测的就不是失效条件。
    control_stats, _ = _build_once(**arguments)
    assert control_stats["base_snapshot_builds"] == 0

    monkeypatch.setattr(
        bottom_divergence_v2_performance,
        "BASE_SNAPSHOT_ALGORITHM_VERSION",
        "base-factor-snapshot-probe",
    )
    second_stats, _ = _build_once(**arguments)

    assert second_stats["base_snapshot_builds"] == 1, (
        "算法版本变了，缓存却把旧算法算出的 base 快照当成新结果返回"
    )


def test_persistent_cache_is_invalidated_by_the_data_version(tmp_path):
    """bar 数据换了一套，base 快照与冻结证据都必须重算。

    `data_version` 是调用方对「喂进来的 bar 是哪一份」的承诺，两层都靠它。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-2]["date"]
    arguments = {
        "cache_directory": tmp_path,
        "groups": groups,
        "universe": _one_stock_universe(name="A"),
        "trade_date": trade_date,
        "config": Config(bottom_divergence_v2_enabled=True),
    }

    first_stats, _ = _build_once(data_version="data-a", **arguments)
    assert first_stats["base_snapshot_builds"] == 1
    assert first_stats["frozen_evidence_builds"] == 1
    control_stats, _ = _build_once(data_version="data-a", **arguments)
    assert control_stats["base_snapshot_builds"] == 0
    assert control_stats["frozen_evidence_builds"] == 0

    second_stats, _ = _build_once(data_version="data-b", **arguments)

    assert second_stats["base_snapshot_builds"] == 1, (
        "换了 data_version 却读回上一份数据算出的 base 快照"
    )
    assert second_stats["frozen_evidence_builds"] == 1, (
        "换了 data_version 却读回上一份数据冻结的证据"
    )


def test_persistent_cache_is_invalidated_by_the_universe(tmp_path):
    """universe 是 base 快照逐行的输入，换了就必须重算。"""
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-2]["date"]
    arguments = {
        "cache_directory": tmp_path,
        "data_version": "data-a",
        "groups": groups,
        "trade_date": trade_date,
        "config": Config(bottom_divergence_v2_enabled=False),
    }

    first_stats, first = _build_once(
        universe=_one_stock_universe(name="A"),
        **arguments,
    )
    assert first_stats["base_snapshot_builds"] == 1
    control_stats, _ = _build_once(
        universe=_one_stock_universe(name="A"),
        **arguments,
    )
    assert control_stats["base_snapshot_builds"] == 0

    second_stats, second = _build_once(
        universe=_one_stock_universe(name="B"),
        **arguments,
    )

    assert second_stats["base_snapshot_builds"] == 1, (
        "换了 universe 却命中上一个 universe 的 base 缓存文件"
    )
    assert not first.equals(second), (
        "两个 universe 的 base 快照相同，这条用例的前提不成立"
    )


def test_frozen_partition_files_do_not_leak_across_data_versions(tmp_path):
    """分区文件名必须与它内部的键同口径，否则会串运行。

    分区文件的内容一直按完整键存（含 `data_version` 与算法版本），所以查表
    不会返回错的证据；但文件名此前只有日期。持久化之后同一个文件会被两个
    `data_version` 共写：整份载入、整份写回会让后写的一方抹掉先写的条目，
    而 `frozen_cache_keys` / `evaluation_cache_keys` 会把别的运行的键当成
    本次运行的键返回。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-2]["date"]
    universe = _one_stock_universe(name="A")
    config = Config(bottom_divergence_v2_enabled=True)

    producer = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
        cache_directory=tmp_path,
    )
    producer.build_factor_snapshot(
        config=config,
        universe=universe,
        trade_date=trade_date,
    )
    producer.close()
    assert producer.frozen_cache_keys, "生产方没写出任何冻结证据"

    stranger = ValidationFactorCache.from_groups(
        data_version="data-b",
        trade_dates=(trade_date,),
        bar_groups=groups,
        cache_directory=tmp_path,
    )

    assert stranger.frozen_cache_keys == (), (
        "另一个 data_version 的分区文件被当成了本次运行的冻结证据"
    )
    assert stranger.evaluation_cache_keys == (), (
        "另一个 data_version 的分区文件被当成了本次运行的已评估因子"
    )
    assert stranger._frozen_path(trade_date) != producer._frozen_path(
        trade_date
    ), "两个 data_version 共用了同一个分区文件名"


def _switch_and_evaluate(cache, *, universe, trade_date, configs):
    for config in configs:
        cache.build_factor_snapshot(
            config=config,
            universe=universe,
            trade_date=trade_date,
        )


def test_each_leg_only_pages_in_its_own_evaluated_shard(tmp_path):
    """已评估因子按参数哈希分片，一条 leg 不得把别人的那份搬进内存。

    这是本次提速的承重结构。实测一个日期分区解包后 9.84 MB，其中 `evaluated`
    占 9.54 MB（96.9%），而网格是 3×2×3=18 条 leg，每条只读写其中 1/18。
    合住一个文件时每次换页都在搬另外 17/18 的死重——15 只 × 32 天的重放里
    这一项占了整趟 84%。

    断言分两半，缺一不可：
    - 落盘要分开（否则换页量没降）；
    - 内存里只能有当前 leg 的键（否则分了文件却仍整份载入，等于没分）。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-2]["date"]
    universe = _one_stock_universe(name="A")
    first = Config(
        bottom_divergence_v2_enabled=True,
        bottom_divergence_v2_zone_score_min=0.4,
    )
    second = replace(first, bottom_divergence_v2_zone_score_min=0.5)

    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
        cache_directory=tmp_path,
    )
    _switch_and_evaluate(
        cache,
        universe=universe,
        trade_date=trade_date,
        configs=(first, second),
    )
    resident_hash = cache._active_evaluated_hash
    assert resident_hash == cache._parameter_hash(second)
    assert cache._evaluated, "第二条 leg 什么都没评估，断言是空对空"
    assert {key.parameter_hash for key in cache._evaluated} == {
        resident_hash
    }, "换 leg 之后内存里仍留着上一条 leg 的已评估因子，分片没有生效"
    cache.close()

    shards = sorted(
        path.name for path in tmp_path.glob("frozen-*-eval-*.pkl.gz")
    )
    assert len(shards) == 2, (
        f"两条 leg 没有各自落一个分片文件：{shards}"
    )
    for config in (first, second):
        assert cache._evaluated_path(
            trade_date, cache._parameter_hash(config)
        ).exists()

    # 共享分区只装冻结证据，不再夹带任何 leg 私有的已评估因子。
    shared = cache._read_partition_file(cache._frozen_path(trade_date))
    assert set(shared) == {"frozen", "lookup"}, (
        f"共享分区里仍有 leg 私有数据：{sorted(shared)}"
    )

    # 分片之后 `evaluation_cache_keys` 仍要给出跨 leg 的完整并集。
    reader = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
        cache_directory=tmp_path,
    )
    assert {
        key.parameter_hash for key in reader.evaluation_cache_keys
    } == {
        cache._parameter_hash(first),
        cache._parameter_hash(second),
    }, "分片之后 evaluation_cache_keys 漏了某条 leg 的键"
    assert reader.frozen_cache_keys, (
        "分片文件把共享分区的冻结证据键挤掉了"
    )


def test_an_unchanged_partition_is_not_written_back(tmp_path):
    """没算出新东西的一趟不得写回任何分区。

    热运行的三层计数全为 0，也就是每一次写回都在把刚读进来的字节原样写出去。
    实测这一项占整趟 62%（128.6s / 206.8s），而它买不到任何东西。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-2]["date"]
    universe = _one_stock_universe(name="A")
    config = Config(bottom_divergence_v2_enabled=True)
    arguments = {
        "data_version": "data-a",
        "trade_dates": (trade_date,),
        "bar_groups": groups,
        "cache_directory": tmp_path,
    }

    producer = ValidationFactorCache.from_groups(**arguments)
    producer.build_factor_snapshot(
        config=config,
        universe=universe,
        trade_date=trade_date,
    )
    producer.close()
    assert producer.stats["frozen_partition_dumps"] > 0, (
        "第一趟没写出任何分区，第二趟的归零断言就是空对空"
    )

    consumer = ValidationFactorCache.from_groups(**arguments)
    consumer.build_factor_snapshot(
        config=config,
        universe=universe,
        trade_date=trade_date,
    )
    consumer.close()

    assert consumer.stats["parameter_evaluations"] == 0, (
        "第二趟重算了因子，这条用例测的不是纯复用路径"
    )
    assert consumer.stats["frozen_partition_dumps"] == 0, (
        "纯复用的一趟仍在把读进来的分区原样写回"
    )
    assert consumer.stats["frozen_partition_loads"] > 0, (
        "第二趟连读都没读，说明它根本没走到分区换页"
    )


def test_a_failed_cache_write_leaves_the_previous_file_intact(tmp_path):
    """写到一半失败不得毁掉已经落盘的那一份。

    临时目录里这条无所谓——进程死了目录也没了。持久化目录里则是新增的
    失败模式：分批跑时进程被 Ctrl-C 或 OOM 杀掉，直写会在目标路径上留下
    半个 gzip 文件，下一次运行读它时崩在 `pickle.load` 上，而不是重算。
    """
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(date(2024, 3, 1),),
        bar_groups={},
        cache_directory=tmp_path,
    )
    target = tmp_path / "base-probe.pkl.gz"
    cache._write_atomically(target, lambda path: path.write_bytes(b"good"))
    assert target.read_bytes() == b"good"

    def fail_midway(path):
        path.write_bytes(b"half")
        raise OSError("disk full")

    with pytest.raises(OSError):
        cache._write_atomically(target, fail_midway)

    assert target.read_bytes() == b"good", (
        "写失败的缓存文件覆盖了上一份完好的产物"
    )
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "base-probe.pkl.gz"
    ], "失败的写入留下了中间文件"


def test_persistent_cache_directory_holds_no_half_written_leftovers(tmp_path):
    """缓存文件一律先写临时文件再原子替换，目录里不该留下中间产物。

    持久化目录会被多个进程共享，写到一半被 Ctrl-C 或 OOM 杀掉时，半个
    gzip 文件会让下一次运行在 `pickle.load` 上崩掉而不是重算。中间文件还
    必须避开 `base-*` / `frozen-*` 两个 glob，否则它自己就会被当成缓存读。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-2]["date"]
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
        cache_directory=tmp_path,
    )
    cache.build_factor_snapshot(
        config=Config(bottom_divergence_v2_enabled=True),
        universe=_one_stock_universe(name="A"),
        trade_date=trade_date,
    )
    cache.close()

    names = sorted(path.name for path in tmp_path.iterdir())
    assert names, "什么都没写出来，这条用例没覆盖到目标"
    assert all(
        name.startswith(("base-", "frozen-")) for name in names
    ), f"缓存目录里留下了中间产物：{names}"


def test_from_groups_defaults_to_a_temporary_directory():
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(date(2024, 3, 1),),
        bar_groups={},
    )
    try:
        assert cache._temporary_directory is not None
    finally:
        cache.close()


def test_from_database_forwards_the_cache_directory(tmp_path, monkeypatch):
    """`from_database` 是 CLI 真正走的工厂，不透传就等于这次改动没落地。"""
    monkeypatch.setattr(
        bottom_divergence_v2_performance,
        "iter_query_batches",
        lambda session, statement: iter(()),
    )
    db_manager = MagicMock()
    persistent_directory = tmp_path / "factor-cache"

    cache = ValidationFactorCache.from_database(
        db_manager=db_manager,
        data_version="data-a",
        trade_dates=(date(2024, 3, 1),),
        codes=("000001",),
        lookback_days=120,
        cache_directory=persistent_directory,
    )
    try:
        assert cache._cache_directory == persistent_directory
        assert cache._temporary_directory is None
    finally:
        cache.close()
