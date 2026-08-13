# -*- coding: utf-8 -*-
"""``pre_close_chain`` 进三处信任白名单（gate-3 / Task 4）。

Task 1~3 把复权算对、接进读取路径、全库抽查过了，但 `actionability_status` 仍然
全部是 `adjustment_unknown`——门禁只认 `adj_factor_source` 字符串，而
`pre_close_chain` 还不在任何一份白名单里。这一步开门。

三份白名单是同义副本，必须一起改：

1. `src/services/factor_service.py` 的 `_bottom_divergence_v2_metadata`（生产因子快照）
2. `src/indicators/causal_bottom_divergence_detector.py` 的 `_candidate_metadata`（检测器）
3. `src/strategies/bottom_divergence_layered_entry.py` 的 `_has_trusted_adjustment`
   （Strategy E v2 **实盘**封装）

漏掉第三处不会报错，只会「回测放行、实盘拒绝」——同一份数据在两个入口给出相反结论。
所以这里既钉三份相等，也逐处钉行为。

`pre_close_chain_anomalous` 必须被拒：它标的是被切段作废、**没有**施加复权的行。
每条拒绝用例都把 `adj_factor` 设成合法的 1.0，好让唯一起作用的判据是来源字符串本身
——否则数值检查会替白名单挡下来，白名单被改坏也测不出来。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from src.indicators.causal_bottom_divergence_detector import (
    _TRUSTED_ADJUSTMENT_SOURCES as _DETECTOR_WHITELIST,
)
from src.config import Config
from src.indicators.resistance_zone_detector import ResistanceZoneMetadata
from src.services.adjustment_chain import ANOMALOUS_SOURCE, TRUSTED_SOURCE
from src.services.factor_service import FactorService
from src.strategies.bottom_divergence_layered_entry import (
    _TRUSTED_ADJUSTMENT_SOURCES as _STRATEGY_WHITELIST,
    BottomDivergenceLayeredEntryStrategy,
)


_FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "001337_bottom_divergence_20251201_20260805.csv"
)


def _detector_fixture(source: str) -> pd.DataFrame:
    fixture = pd.read_csv(_FIXTURE_PATH, parse_dates=["date"])
    fixture["data_source"] = "fixture_source"
    fixture["adj_factor"] = 1.0
    fixture["adj_factor_source"] = source
    return fixture


def _factor_service_whitelist() -> set[str]:
    """把 `_bottom_divergence_v2_metadata` 里的局部白名单探出来。

    它是函数体内的局部变量，没有模块级名字可以 import。用一批探针来源逐个过
    `adjustment_unknown`，反推出被接受的集合——这样这条测试不依赖它将来是写成
    局部集合、模块常量还是配置项。
    """
    probes = sorted(
        set(_DETECTOR_WHITELIST)
        | set(_STRATEGY_WHITELIST)
        | {TRUSTED_SOURCE, ANOMALOUS_SOURCE}
    )
    accepted = set()
    for probe in probes:
        group = pd.DataFrame({
            "data_source": ["fixture_source"],
            "adj_factor": [1.0],
            "adj_factor_source": [probe],
        })
        _metadata, unknown = FactorService._bottom_divergence_v2_metadata(group)
        if not unknown:
            accepted.add(probe)
    return accepted


def _strategy_result(source: str) -> dict:
    """跑实盘封装，检测器结果固定，唯一变量是来源字符串。"""
    detector_result = {
        "found": True,
        "stage": "major_actionable",
        "candidate_version": "candidate-v2",
        "zone": {
            "zone_version": "zone-v2",
            "r1": {"score": 0.6},
            "r2": {"score": 0.8},
        },
        "early_reversal": {"bar_index": 2, "strength": 0.5},
        "near_zone_events": {"cleared_confirmed": {"bar_index": 4}},
        "major_zone_breakout": {"bar_index": 6},
        "major_zone_actionable_entry": {"actionable": True},
        "actionability_status": "actionable",
        "stop_loss_price": 8.8,
        "layered_buy_points": [{"level": "r2", "price": 16.0, "stop": 8.8}],
    }
    config = SimpleNamespace(
        bottom_divergence_v2_enabled=True,
        _bottom_divergence_v2_parse_errors=(),
        bottom_divergence_v2_cluster_pct=0.02,
        bottom_divergence_v2_atr_gap_multiplier=0.7,
        bottom_divergence_v2_zone_score_min=0.6,
        bottom_divergence_v2_breakout_buffer_pct=0.004,
        bottom_divergence_v2_sync_window=4,
        bottom_divergence_v2_retention_bars=25,
        bottom_divergence_v2_r1_weights=(0.2, 0.2, 0.2, 0.2, 0.1, 0.1),
        bottom_divergence_v2_r2_weights=(0.1, 0.2, 0.2, 0.2, 0.2, 0.1),
    )
    frame = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=8, freq="B"),
        "open": [9.5, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5],
        "high": [10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5],
        "low": [9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0],
        "close": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0],
        "volume": [1000] * 8,
    })
    with patch(
        "src.strategies.bottom_divergence_layered_entry."
        "CausalBottomDivergenceDetector.detect",
        return_value=detector_result,
    ):
        return BottomDivergenceLayeredEntryStrategy.evaluate(
            frame,
            config=config,
            metadata=ResistanceZoneMetadata(
                data_source="fixture",
                adj_factor_source=source,
            ),
        )


def test_the_three_whitelists_are_the_same_set():
    """三份同义副本必须逐值相等。

    这条是「漏改一处」的直接探针。回测走 1/2，实盘走 3，任何一份漏改都不会报错，
    只会让同一份数据在两个入口给出相反结论。
    """
    factor_service = _factor_service_whitelist()

    assert set(_DETECTOR_WHITELIST) == set(_STRATEGY_WHITELIST), (
        "检测器与实盘封装的信任白名单不一致：回测放行、实盘拒绝"
    )
    assert factor_service == set(_DETECTOR_WHITELIST), (
        "生产因子快照与检测器的信任白名单不一致"
    )


@pytest.mark.parametrize(
    "whitelist",
    [_DETECTOR_WHITELIST, _STRATEGY_WHITELIST],
    ids=["detector", "strategy"],
)
def test_trusted_source_is_in_and_anomalous_is_out(whitelist):
    """收 `pre_close_chain`，拒 `pre_close_chain_anomalous`。

    后者进白名单等于让一段被切段作废、没有施加复权的原始价顶着可信标记穿过门禁
    ——比现在的「诚实拒绝」更糟。
    """
    assert TRUSTED_SOURCE in whitelist
    assert ANOMALOUS_SOURCE not in whitelist


def test_factor_service_accepts_the_chain_and_rejects_the_anomalous_segment():
    """生产因子快照：`adjustment_unknown` 的取值只由来源字符串决定。

    两个用例的 `adj_factor` 都是合法的 1.0，数值检查不参与判定。
    """
    def unknown_for(source: str) -> bool:
        group = pd.DataFrame({
            "data_source": ["fixture_source", "fixture_source"],
            "adj_factor": [1.0, 1.0],
            "adj_factor_source": [source, source],
        })
        metadata, unknown = FactorService._bottom_divergence_v2_metadata(group)
        assert metadata.adj_factor_source == (
            "unknown" if unknown else source
        )
        return unknown

    assert unknown_for(TRUSTED_SOURCE) is False
    assert unknown_for(ANOMALOUS_SOURCE) is True


def test_factor_service_still_rejects_a_nan_factor_on_a_trusted_source():
    """可信来源 + NaN 因子仍然是 unknown。

    `mark_unadjustable` 写的是 NaN + anomalous 两件事，这里钉住即便只剩数值这一道，
    fail-closed 依然成立——白名单放宽不等于数值检查可以退场。
    """
    group = pd.DataFrame({
        "data_source": ["fixture_source"],
        "adj_factor": [float("nan")],
        "adj_factor_source": [TRUSTED_SOURCE],
    })

    _metadata, unknown = FactorService._bottom_divergence_v2_metadata(group)

    assert unknown is True


def test_detector_lets_the_chain_reach_actionable():
    """检测器：同一份夹具，来源换成 `pre_close_chain` 就该出可执行结论。

    这是本计划的验收面——Task 3 之前它必然是 `adjustment_unknown`，因为
    `actionability_v2` 只看这个字符串。
    """
    from src.indicators.causal_bottom_divergence_detector import (
        CausalBottomDivergenceDetector,
    )

    trusted = CausalBottomDivergenceDetector.detect(
        _detector_fixture(TRUSTED_SOURCE)
    )
    anomalous = CausalBottomDivergenceDetector.detect(
        _detector_fixture(ANOMALOUS_SOURCE)
    )

    assert trusted["actionability_status"] == "actionable"
    assert trusted["major_zone_actionable_entry"]["actionable"] is True
    assert anomalous["actionability_status"] == "adjustment_unknown"
    assert anomalous["major_zone_actionable_entry"]["actionable"] is False
    assert anomalous["stage"] == "major_unverified"


@pytest.mark.parametrize("anomalous_prefix_rows", [1, 5])
def test_anomalous_prefix_outside_the_a_b_segment_still_gates(
    anomalous_prefix_rows,
):
    """断链前缀落在 A/B 段之外时，整组仍然必须判 unknown。

    `apply_read_adjustment` 遇到断链只作废**断点之前**的前缀，那段保留原始价、标
    `pre_close_chain_anomalous`；而检测器把 provenance 冻结到 A/B 前缀那一段
    （`visible.iloc[a_idx:b_idx+1]`），断点落在 A 之前时它看不见。可阻力区是在整个
    可见窗口上找摆动高点的，那段原始价照样参与计算。

    这条洞在 `pre_close_chain` 进白名单之前是死的——没有任何来源被信任，整组必然
    unknown。Task 4 把它变活，所以判定改成两级取「或」。
    """
    frame = _detector_fixture(TRUSTED_SOURCE)
    frame.loc[:anomalous_prefix_rows - 1, "adj_factor_source"] = (
        ANOMALOUS_SOURCE
    )
    frame.loc[:anomalous_prefix_rows - 1, "adj_factor"] = float("nan")
    service = FactorService(
        db_manager=object(),
        config=Config(bottom_divergence_v2_enabled=True),
    )

    clean = service._compute_bottom_divergence_v2_factors(
        _detector_fixture(TRUSTED_SOURCE)
    )
    gated = service._compute_bottom_divergence_v2_factors(frame)

    # 对照组：整组干净时这份夹具本来是可执行的，否则下面的断言会因为
    # 「本来就出不了信号」而恒绿。
    assert clean["bottom_divergence_v2_actionability_status"] == "actionable"
    assert clean["bottom_divergence_v2_major_actionable_entry"] is True

    assert gated["bottom_divergence_v2_actionability_status"] == (
        "adjustment_unknown"
    )
    assert gated["bottom_divergence_v2_major_actionable_entry"] is False
    assert gated["bottom_divergence_v2_stage"] == "major_unverified"


def test_live_strategy_executes_on_the_chain_and_refuses_the_anomalous_one():
    """实盘封装：漏改这一处就是「回测放行、实盘拒绝」。

    检测器结果被固定成同一个可执行结论，唯一的变量是 `adj_factor_source`，
    因此这条测试只可能因为白名单而变色。
    """
    trusted = _strategy_result(TRUSTED_SOURCE)
    anomalous = _strategy_result(ANOMALOUS_SOURCE)

    assert trusted["triggered"] is True
    assert trusted["actionable_entry"] is True
    assert trusted["entry_price"] == pytest.approx(16.0)
    assert anomalous["triggered"] is False
    assert anomalous["actionable_entry"] is False
    assert anomalous["entry_price"] is None
    assert anomalous["reason"] == "bottom divergence v2 adjustment_unknown"
