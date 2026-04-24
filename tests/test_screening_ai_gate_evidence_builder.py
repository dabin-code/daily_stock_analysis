"""Tests for ScreeningAiGateEvidenceBuilder – strategy-specific evidence packages."""

from __future__ import annotations

import pytest

from src.schemas.screening_ai_gate import ScreeningAiGateConfig
from src.services.screening_ai_gate_evidence_builder import (
    ScreeningAiGateEvidenceBuilder,
    EvidencePackage,
)


def _make_bottom_divergence_config() -> ScreeningAiGateConfig:
    return ScreeningAiGateConfig(
        strategy_name="bottom_divergence_double_breakout",
        version="v1",
        ai_priority=1,
        supported=True,
        playbook={"goal": "confirm structure"},
        stage_definitions={
            "confirm_entry_window": {"label": "确认入场窗口"},
            "structure_forming": {"label": "结构形成中"},
        },
        hard_veto_rules=["ST直接reject"],
        news_focus=["行业政策"],
        payload_fields=[
            "signal_strength",
            "divergence_pattern",
            "sync_breakout",
            "price_low_a",
            "price_low_b",
            "rebound_high",
            "horizontal_resistance",
        ],
    )


def _make_full_factor_snapshot() -> dict:
    return {
        "code": "000001.SZ",
        "name": "平安银行",
        "close": 12.5,
        "volume_ratio": 1.8,
        "trend_score": 65.0,
        "is_st": False,
        "bottom_divergence_state": "confirmed",
        "bottom_divergence_signal_strength": 0.85,
        "bottom_divergence_pattern_code": "price_down_macd_up",
        "bottom_divergence_sync_breakout": True,
        "bottom_divergence_price_low_a": {"idx": 80, "price": 10.5},
        "bottom_divergence_price_low_b": {"idx": 120, "price": 10.8},
        "bottom_divergence_macd_low_a": {"idx": 82, "dif": -0.15, "dea": -0.12},
        "bottom_divergence_macd_low_b": {"idx": 122, "dif": -0.08, "dea": -0.06},
        "bottom_divergence_rebound_high": {"idx": 100, "price": 12.0},
        "bottom_divergence_horizontal_resistance": 12.0,
        "bottom_divergence_downtrend_line": {
            "found": True,
            "slope": -0.02,
            "touch_count": 3,
            "breakout_confirmed": True,
        },
        "bottom_divergence_rejection_reason": None,
    }


def _make_incomplete_factor_snapshot() -> dict:
    return {
        "code": "000002.SZ",
        "name": "万科A",
        "close": 8.0,
        "volume_ratio": 1.2,
        "trend_score": 40.0,
        "is_st": False,
        "bottom_divergence_state": "rejected",
        "bottom_divergence_signal_strength": 0.0,
        "bottom_divergence_price_low_a": None,
        "bottom_divergence_price_low_b": None,
        "bottom_divergence_rebound_high": None,
        "bottom_divergence_horizontal_resistance": None,
        "bottom_divergence_downtrend_line": None,
    }


class TestEvidenceBuilderComplete:
    """Evidence builder produces valid packages for supported strategies."""

    def test_builds_sufficient_evidence_for_confirmed_divergence(self):
        config = _make_bottom_divergence_config()
        snapshot = _make_full_factor_snapshot()
        builder = ScreeningAiGateEvidenceBuilder()

        evidence = builder.build(
            factor_snapshot=snapshot,
            strategy_config=config,
        )

        assert isinstance(evidence, EvidencePackage)
        assert evidence.data_quality == "sufficient"
        assert evidence.missing_fields == []
        assert evidence.strategy_snapshot is not None
        assert evidence.strategy_raw_evidence is not None

    def test_evidence_includes_candidate_meta(self):
        config = _make_bottom_divergence_config()
        snapshot = _make_full_factor_snapshot()
        builder = ScreeningAiGateEvidenceBuilder()

        evidence = builder.build(factor_snapshot=snapshot, strategy_config=config)

        assert evidence.candidate_meta["code"] == "000001.SZ"
        assert evidence.candidate_meta["name"] == "平安银行"

    def test_evidence_includes_raw_evidence_fields(self):
        config = _make_bottom_divergence_config()
        snapshot = _make_full_factor_snapshot()
        builder = ScreeningAiGateEvidenceBuilder()

        evidence = builder.build(factor_snapshot=snapshot, strategy_config=config)

        raw = evidence.strategy_raw_evidence
        assert raw["bottom_divergence_price_low_a"] is not None
        assert raw["bottom_divergence_rebound_high"] is not None


class TestEvidenceBuilderIncomplete:
    """Missing key evidence marks the package insufficient."""

    def test_marks_missing_anchor_points(self):
        config = _make_bottom_divergence_config()
        snapshot = _make_incomplete_factor_snapshot()
        builder = ScreeningAiGateEvidenceBuilder()

        evidence = builder.build(factor_snapshot=snapshot, strategy_config=config)

        assert evidence.data_quality == "insufficient"
        assert len(evidence.missing_fields) > 0

    def test_insufficient_evidence_still_has_meta(self):
        config = _make_bottom_divergence_config()
        snapshot = _make_incomplete_factor_snapshot()
        builder = ScreeningAiGateEvidenceBuilder()

        evidence = builder.build(factor_snapshot=snapshot, strategy_config=config)

        assert evidence.candidate_meta["code"] == "000002.SZ"


class TestEvidenceBuilderMarketFilter:
    """Evidence includes market filter snapshot."""

    def test_includes_market_filter_fields(self):
        config = _make_bottom_divergence_config()
        snapshot = _make_full_factor_snapshot()
        builder = ScreeningAiGateEvidenceBuilder()

        evidence = builder.build(factor_snapshot=snapshot, strategy_config=config)

        assert "volume_ratio" in evidence.market_filter_snapshot
        assert "trend_score" in evidence.market_filter_snapshot
