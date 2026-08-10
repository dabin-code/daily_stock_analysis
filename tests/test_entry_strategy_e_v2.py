from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from src.indicators.causal_bottom_divergence_detector import (
    CausalBottomDivergenceDetector,
)
from src.indicators.resistance_zone_detector import ResistanceZoneMetadata
from src.strategies.bottom_divergence_layered_entry import (
    BottomDivergenceLayeredEntryStrategy,
)


def _df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=8, freq="B"),
            "open": [9.5, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5],
            "high": [10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5],
            "low": [9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0],
            "close": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0],
            "volume": [1000] * 8,
        }
    )


def _config(**overrides):
    values = {
        "bottom_divergence_v2_enabled": True,
        "_bottom_divergence_v2_parse_errors": (),
        "bottom_divergence_v2_cluster_pct": 0.02,
        "bottom_divergence_v2_atr_gap_multiplier": 0.7,
        "bottom_divergence_v2_zone_score_min": 0.6,
        "bottom_divergence_v2_breakout_buffer_pct": 0.004,
        "bottom_divergence_v2_sync_window": 4,
        "bottom_divergence_v2_retention_bars": 25,
        "bottom_divergence_v2_r1_weights": (0.2, 0.2, 0.2, 0.2, 0.1, 0.1),
        "bottom_divergence_v2_r2_weights": (0.1, 0.2, 0.2, 0.2, 0.2, 0.1),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _detector_result(stage: str, *, actionable: bool = True) -> dict:
    return {
        "found": True,
        "stage": stage,
        "candidate_version": "candidate-v2",
        "zone": {
            "zone_version": "zone-v2",
            "r1": {"score": 0.6},
            "r2": {"score": 0.8},
        },
        "early_reversal": {
            "bar_index": 2,
            "strength": 0.5,
        },
        "near_zone_events": {
            "cleared_confirmed": {"bar_index": 4},
        },
        "major_zone_breakout": {
            "bar_index": 6,
        },
        "major_zone_actionable_entry": {
            "actionable": actionable,
        },
        "actionability_status": (
            "actionable"
            if stage == "major_actionable" and actionable
            else "major_not_confirmed"
            if actionable
            else "extended"
        ),
        "stop_loss_price": 8.8,
        "layered_buy_points": [
            {"level": "early", "price": 12.0, "stop": 8.8},
            {"level": "r1", "price": 14.0, "stop": 8.8},
            {"level": "r2", "price": 16.0, "stop": 8.8},
        ],
    }


def _trusted_metadata() -> ResistanceZoneMetadata:
    return ResistanceZoneMetadata(
        data_source="fixture",
        adj_factor_source="tushare_native",
    )


@pytest.mark.parametrize(
    ("stage", "expected_price"),
    [
        ("early", 12.0),
        ("near_cleared", 14.0),
        ("major_actionable", 16.0),
    ],
)
def test_actionable_stages_trigger_at_first_stage_event_close(stage, expected_price):
    metadata = _trusted_metadata()
    with patch(
        "src.strategies.bottom_divergence_layered_entry."
        "CausalBottomDivergenceDetector.detect",
        return_value=_detector_result(stage),
    ) as detect_mock:
        result = BottomDivergenceLayeredEntryStrategy.evaluate(
            _df(),
            config=_config(),
            metadata=metadata,
        )

    assert result == {
        "triggered": True,
        "stage": stage,
        "actionable_entry": True,
        "candidate_version": "candidate-v2",
        "zone_version": "zone-v2",
        "entry_price": expected_price,
        "stop_loss_price": 8.8,
        "score": 65.0,
        "reason": f"bottom divergence v2 {stage} entry",
        "layered_buy_points": [
            {"level": "early", "price": 12.0, "stop": 8.8},
            {"level": "r1", "price": 14.0, "stop": 8.8},
            {"level": "r2", "price": 16.0, "stop": 8.8},
        ],
    }
    _, kwargs = detect_mock.call_args
    assert kwargs["as_of_index"] == len(_df()) - 1
    assert kwargs["metadata"] == metadata
    assert kwargs["zone_params"].cluster_pct == 0.02
    assert kwargs["zone_params"].sync_window == 4
    assert kwargs["zone_params"].invalidated_retention_bars == 25


@pytest.mark.parametrize(
    ("stage", "status"),
    [
        ("forming", "forming"),
        ("invalidated", "candidate_invalidated"),
        ("stale", "stale"),
        ("extended", "extended"),
        ("major_unverified", "adjustment_unknown"),
        ("breakout_failed", "below_r2"),
        ("rejected", "rejected"),
    ],
)
def test_non_actionable_stages_never_trigger(stage, status):
    detector_result = _detector_result(stage, actionable=False)
    detector_result["actionability_status"] = status

    with patch(
        "src.strategies.bottom_divergence_layered_entry."
        "CausalBottomDivergenceDetector.detect",
        return_value=detector_result,
    ):
        result = BottomDivergenceLayeredEntryStrategy.evaluate(
            _df(),
            config=_config(),
        )

    assert result["triggered"] is False
    assert result["actionable_entry"] is False
    assert result["entry_price"] is None
    assert status in result["reason"]


def test_major_stage_without_actionability_does_not_trigger_or_chase():
    with patch(
        "src.strategies.bottom_divergence_layered_entry."
        "CausalBottomDivergenceDetector.detect",
        return_value=_detector_result("major_actionable", actionable=False),
    ):
        result = BottomDivergenceLayeredEntryStrategy.evaluate(
            _df(),
            config=_config(),
        )

    assert result["triggered"] is False
    assert result["entry_price"] is None
    assert result["reason"] == "bottom divergence v2 extended"


def test_real_stale_major_shape_cannot_fake_near_entry():
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "001337_bottom_divergence_20251201_20260805.csv"
    )
    df = pd.read_csv(fixture_path, parse_dates=["date"])
    df["data_source"] = "fixture_source"
    df["adj_factor"] = 1.0
    df["adj_factor_source"] = "tushare_native"
    for close in (43.3, 43.4, 43.5, 43.6):
        row = df.iloc[-1].copy()
        row["date"] = pd.Timestamp(row["date"]) + pd.offsets.BDay()
        row["open"] = close
        row["high"] = close + 0.5
        row["low"] = close - 0.5
        row["close"] = close
        row["pct_chg"] = (close / float(df.iloc[-1]["close"]) - 1) * 100
        df = pd.concat([df, row.to_frame().T], ignore_index=True)

    stale_shape = CausalBottomDivergenceDetector.detect(df)
    assert stale_shape["major_zone_breakout"]["confirmed"] is True
    assert stale_shape["near_zone_events"]["cleared_confirmed"]["triggered"] is True
    assert stale_shape["actionability_status"] == "confirmation_too_old"
    stale_shape["stage"] = "near_cleared"

    with patch(
        "src.strategies.bottom_divergence_layered_entry."
        "CausalBottomDivergenceDetector.detect",
        return_value=stale_shape,
    ):
        result = BottomDivergenceLayeredEntryStrategy.evaluate(
            df,
            config=_config(),
        )

    assert result["triggered"] is False
    assert result["actionable_entry"] is False
    assert result["entry_price"] is None
    assert result["reason"] == "bottom divergence v2 confirmation_too_old"


@pytest.mark.parametrize(
    "bad_config",
    [
        _config(_bottom_divergence_v2_parse_errors=(("BAD", "bad"),)),
        _config(bottom_divergence_v2_cluster_pct=0),
        _config(bottom_divergence_v2_sync_window=True),
        _config(bottom_divergence_v2_r1_weights=(1.0,)),
    ],
)
def test_invalid_config_returns_stable_non_triggering_result(bad_config):
    result = BottomDivergenceLayeredEntryStrategy.evaluate(
        _df(),
        config=bad_config,
    )

    assert result == {
        "triggered": False,
        "stage": "rejected",
        "actionable_entry": False,
        "candidate_version": None,
        "zone_version": None,
        "entry_price": None,
        "stop_loss_price": None,
        "score": 0.0,
        "reason": "bottom divergence v2 invalid_config",
        "layered_buy_points": [],
    }


def test_default_config_is_resolved_lazily():
    with (
        patch(
            "src.strategies.bottom_divergence_layered_entry.get_config",
            return_value=_config(),
        ) as get_config_mock,
        patch(
            "src.strategies.bottom_divergence_layered_entry."
            "CausalBottomDivergenceDetector.detect",
            return_value=_detector_result("early"),
        ),
    ):
        result = BottomDivergenceLayeredEntryStrategy.evaluate(
            _df(),
            metadata=_trusted_metadata(),
        )

    get_config_mock.assert_called_once_with()
    assert result["triggered"] is True


def test_score_is_clamped_to_zero_and_one_hundred():
    high = _detector_result("early")
    high["early_reversal"]["strength"] = 4
    high["zone"]["r1"]["score"] = 4
    high["zone"]["r2"]["score"] = 4
    low = _detector_result("early")
    low["early_reversal"]["strength"] = -4
    low["zone"]["r1"]["score"] = -4
    low["zone"]["r2"]["score"] = -4

    with patch(
        "src.strategies.bottom_divergence_layered_entry."
        "CausalBottomDivergenceDetector.detect",
        side_effect=[high, low],
    ):
        high_result = BottomDivergenceLayeredEntryStrategy.evaluate(
            _df(), config=_config(), metadata=_trusted_metadata()
        )
        low_result = BottomDivergenceLayeredEntryStrategy.evaluate(
            _df(), config=_config(), metadata=_trusted_metadata()
        )

    assert high_result["score"] == 100.0
    assert low_result["score"] == 0.0


def test_disabled_config_returns_before_detector_call():
    with patch(
        "src.strategies.bottom_divergence_layered_entry."
        "CausalBottomDivergenceDetector.detect",
    ) as detect_mock:
        result = BottomDivergenceLayeredEntryStrategy.evaluate(
            _df(),
            config=_config(bottom_divergence_v2_enabled=False),
            metadata=_trusted_metadata(),
        )

    detect_mock.assert_not_called()
    assert result["triggered"] is False
    assert result["actionable_entry"] is False
    assert result["stage"] == "disabled"
    assert result["reason"] == "bottom divergence v2 disabled"


@pytest.mark.parametrize("stage", ["early", "near_cleared"])
def test_unknown_adjustment_keeps_evidence_but_never_triggers(stage):
    detector_result = _detector_result(stage)
    with patch(
        "src.strategies.bottom_divergence_layered_entry."
        "CausalBottomDivergenceDetector.detect",
        return_value=detector_result,
    ):
        result = BottomDivergenceLayeredEntryStrategy.evaluate(
            _df(),
            config=_config(),
            metadata=ResistanceZoneMetadata(
                data_source="fixture",
                adj_factor_source="unknown",
            ),
        )

    assert result["triggered"] is False
    assert result["actionable_entry"] is False
    assert result["entry_price"] is None
    assert result["candidate_version"] == "candidate-v2"
    assert result["layered_buy_points"]


def test_trusted_adjustment_allows_early_trigger():
    with patch(
        "src.strategies.bottom_divergence_layered_entry."
        "CausalBottomDivergenceDetector.detect",
        return_value=_detector_result("early"),
    ):
        result = BottomDivergenceLayeredEntryStrategy.evaluate(
            _df(),
            config=_config(),
            metadata=_trusted_metadata(),
        )

    assert result["triggered"] is True
