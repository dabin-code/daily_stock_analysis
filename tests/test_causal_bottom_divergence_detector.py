from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.indicators.resistance_zone_detector import ResistanceZoneMetadata
from src.indicators.resistance_zone_detector import ResistanceZoneParams


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "001337_bottom_divergence_20251201_20260805.csv"
)


def _fixture() -> pd.DataFrame:
    fixture = pd.read_csv(FIXTURE_PATH, parse_dates=["date"])
    fixture["data_source"] = "fixture_source"
    fixture["adj_factor"] = 1.0
    fixture["adj_factor_source"] = "tushare_native"
    return fixture


def _detect(df: pd.DataFrame, **kwargs):
    from src.indicators.causal_bottom_divergence_detector import (
        CausalBottomDivergenceDetector,
    )

    return CausalBottomDivergenceDetector.detect(df, **kwargs)


def _primary(result):
    version = result["primary_candidate_version"]
    return next(
        record
        for record in result["candidate_records"]
        if record["candidate_version"] == version
    )


def test_as_of_boundaries_and_insufficient_data_return_empty_template():
    df = _fixture()

    for as_of in (-1, len(df), 10):
        result = _detect(df, as_of_index=as_of)
        assert result["candidate_records"] == []
        assert result["primary_candidate_version"] is None
        assert result["found"] is False
        assert result["stage"] is None
        assert result["zone"] is None


def test_b_plus_one_provisional_then_confirmed_keeps_frozen_evidence():
    df = _fixture()
    provisional = _detect(df, as_of_index=154)
    confirmed = _detect(df, as_of_index=158)
    p_record = _primary(provisional)
    c_record = next(
        record
        for record in confirmed["candidate_records"]
        if record["candidate_version"] == p_record["candidate_version"]
    )

    assert p_record["b"]["date"] == "2026-07-21"
    assert p_record["lifecycle"] == "provisional"
    assert c_record["lifecycle"] == "confirmed"
    for key in (
        "candidate_version",
        "a",
        "b",
        "macd",
        "h",
        "pattern",
        "zone",
    ):
        assert c_record[key] == p_record[key]
    frozen_p = dict(p_record["frozen_trendline"])
    frozen_c = dict(c_record["frozen_trendline"])
    for key in ("breakout_bar_index", "breakout_date", "projected_value_at_breakout"):
        frozen_p.pop(key, None)
        frozen_c.pop(key, None)
    assert frozen_c == frozen_p


def test_as_of_slice_ignores_all_later_row_perturbations():
    df = _fixture()
    expected = _detect(df, as_of_index=154)
    changed = df.copy()
    changed.loc[155:, ["open", "high", "low", "close", "volume", "pct_chg"]] = [
        999.0,
        1000.0,
        1.0,
        500.0,
        1.0,
        99.0,
    ]

    assert _detect(changed, as_of_index=154) == expected


def test_frozen_candidate_evidence_is_reusable_immutable_and_causal():
    from src.indicators.causal_bottom_divergence_detector import (
        CausalBottomDivergenceDetector,
    )

    df = _fixture()
    as_of = 164
    base_params = ResistanceZoneParams()
    frozen = CausalBottomDivergenceDetector.freeze_evidence(
        df,
        as_of_index=as_of,
        zone_params=base_params,
    )
    assert json.loads(json.dumps(frozen.to_dict()))["content_hash"] == (
        frozen.content_hash
    )
    with pytest.raises(FrozenInstanceError):
        frozen.payload_json = "{}"

    for params in (
        replace(base_params, cluster_pct=0.01, score_min=0.4),
        replace(
            base_params,
            cluster_pct=0.02,
            atr_gap_multiplier=0.75,
            score_min=0.5,
        ),
    ):
        expected = _detect(
            df,
            as_of_index=as_of,
            zone_params=params,
        )
        actual = CausalBottomDivergenceDetector.evaluate_frozen_evidence(
            df,
            frozen,
            zone_params=params,
        )
        assert actual == expected

    changed = df.copy()
    changed.loc[as_of + 1:, "close"] = 999.0
    assert CausalBottomDivergenceDetector.evaluate_frozen_evidence(
        changed,
        frozen,
        zone_params=base_params,
    ) == CausalBottomDivergenceDetector.evaluate_frozen_evidence(
        df,
        frozen,
        zone_params=base_params,
    )


def test_one_frozen_range_replays_multiple_as_of_dates_without_future_leak():
    from src.indicators.causal_bottom_divergence_detector import (
        CausalBottomDivergenceDetector,
    )

    df = _fixture()
    frozen = CausalBottomDivergenceDetector.freeze_evidence(
        df,
        as_of_index=164,
        evaluation_as_of_indices=(154, 164),
    )
    expected = _detect(df, as_of_index=154)
    changed = df.copy()
    changed.loc[155:, ["open", "high", "low", "close"]] = [
        900.0,
        1000.0,
        1.0,
        500.0,
    ]

    assert CausalBottomDivergenceDetector.evaluate_frozen_evidence(
        df,
        frozen,
        as_of_index=154,
    ) == expected
    assert CausalBottomDivergenceDetector.evaluate_frozen_evidence(
        changed,
        frozen,
        as_of_index=154,
    ) == expected


def test_lower_low_invalidates_and_record_expires_after_twenty_bars():
    df = _fixture()
    df.loc[155, "low"] = 30.0
    invalidated = _detect(df, as_of_index=155)
    record = next(
        item
        for item in invalidated["candidate_records"]
        if item["b"]["date"] == "2026-07-21"
    )

    assert record["lifecycle"] == "invalidated"
    assert record["invalidated_at"]["bar_index"] == 155
    assert invalidated["primary_candidate_version"] is None

    last = df.iloc[-1].copy()
    suffix = []
    for offset in range(1, 23):
        row = last.copy()
        row["date"] = pd.Timestamp(last["date"]) + pd.offsets.BDay(offset)
        row[["open", "high", "low", "close"]] = [34.0, 35.0, 33.0, 34.0]
        suffix.append(row)
    extended = pd.concat([df, pd.DataFrame(suffix)], ignore_index=True)

    retained = _detect(extended, as_of_index=175)
    expired = _detect(extended, as_of_index=176)
    assert any(item["candidate_version"] == record["candidate_version"] for item in retained["candidate_records"])
    assert all(item["candidate_version"] != record["candidate_version"] for item in expired["candidate_records"])


def test_retention_override_is_single_source_for_behavior_and_zone_version():
    df = _fixture()
    df.loc[155, "low"] = 30.0
    default = _detect(df, as_of_index=155)
    immediate = _detect(df, as_of_index=155, retention_bars=0)
    expired = _detect(df, as_of_index=156, retention_bars=0)
    one_bar = _detect(df, as_of_index=156, retention_bars=1)
    inherited = _detect(
        df,
        as_of_index=156,
        zone_params=replace(
            ResistanceZoneParams(),
            invalidated_retention_bars=1,
        ),
    )

    default_record = next(
        item for item in default["candidate_records"] if item["b"]["date"] == "2026-07-21"
    )
    immediate_record = next(
        item for item in immediate["candidate_records"] if item["b"]["date"] == "2026-07-21"
    )
    one_bar_record = next(
        item for item in one_bar["candidate_records"] if item["b"]["date"] == "2026-07-21"
    )
    inherited_record = next(
        item for item in inherited["candidate_records"] if item["b"]["date"] == "2026-07-21"
    )
    assert immediate_record["zone"]["parameter_snapshot"]["invalidated_retention_bars"] == 0
    assert immediate_record["zone"]["zone_version"] != default_record["zone"]["zone_version"]
    assert all(item["b"]["date"] != "2026-07-21" for item in expired["candidate_records"])
    assert one_bar_record["zone"]["parameter_snapshot"]["invalidated_retention_bars"] == 1
    assert inherited_record["zone"]["zone_version"] == one_bar_record["zone"]["zone_version"]


def test_prefix_rebuild_finds_candidate_created_before_current_lookback_tail():
    df = _fixture()
    result = _detect(df, as_of_index=164, lookback=20)

    assert any(item["b"]["date"] == "2026-07-21" for item in result["candidate_records"])


def test_fixture_early_r1_and_r2_events_are_first_occurrence_and_irreversible():
    df = _fixture()
    early = _detect(df, as_of_index=154)
    r1 = _detect(df, as_of_index=155)
    major = _detect(df, as_of_index=164)

    assert early["stage"] == "early"
    assert early["early_reversal"]["date"] == "2026-07-22"
    assert early["early_reversal"]["strength"] == pytest.approx(0.831397, abs=1e-6)
    assert r1["near_zone_events"]["entered"]["date"] == "2026-07-23"
    assert r1["near_zone_events"]["crossed"]["date"] == "2026-07-23"
    assert r1["near_zone_events"]["cleared_confirmed"]["date"] == "2026-07-23"
    assert major["major_zone_breakout"]["date"] == "2026-08-05"
    assert major["major_zone_breakout"]["confirmed"] is True
    assert major["major_zone_actionable_entry"]["actionable"] is True

    last = df.iloc[-1].copy()
    last["date"] = pd.Timestamp(last["date"]) + pd.offsets.BDay()
    last[["open", "high", "low", "close", "pct_chg"]] = [
        40.0,
        41.0,
        39.0,
        40.0,
        -7.56,
    ]
    replayed = _detect(pd.concat([df, last.to_frame().T], ignore_index=True))
    original = next(
        item
        for item in replayed["candidate_records"]
        if item["candidate_version"] == major["candidate_version"]
    )
    assert original["major_zone_breakout"]["date"] == "2026-08-05"
    assert original["major_zone_actionable_entry"]["actionable"] is False


def test_early_strength_drops_missing_open_and_volume_weights_then_renormalizes():
    df = _fixture().drop(columns=["open", "volume"])
    result = _detect(df, as_of_index=154)
    early = result["early_reversal"]

    expected = (
        early["components"]["close_position"] * 0.30
        + early["components"]["return"] * 0.20
    ) / 0.50
    assert early["triggered"] is True
    assert early["strength"] == pytest.approx(expected, abs=1e-6)
    assert set(early["weights"]) == {"close_position", "return"}
    assert {"missing_open", "missing_volume"} <= set(result["degradation_reasons"])


def test_r1_acceptance_and_no_volume_cross_requires_next_day_hold():
    df = _fixture()
    accepted = _detect(df, as_of_index=158)
    assert accepted["near_zone_events"]["accepted"]["date"] == "2026-07-28"

    no_volume = df.drop(columns=["volume"]).copy()
    no_volume.loc[156, ["high", "close"]] = [40.0, 39.2]
    crossed = _detect(no_volume, as_of_index=155)
    held = _detect(no_volume, as_of_index=156)
    assert crossed["near_zone_events"]["crossed"]["triggered"] is True
    assert crossed["near_zone_events"]["cleared_confirmed"]["triggered"] is False
    assert held["near_zone_events"]["cleared_confirmed"]["date"] == "2026-07-24"
    assert (
        held["near_zone_events"]["cleared_confirmed"]["confirmation"]
        == "next_day_hold"
    )


def _append_bar(df: pd.DataFrame, *, close: float, low: float | None = None):
    row = df.iloc[-1].copy()
    row["date"] = pd.Timestamp(row["date"]) + pd.offsets.BDay()
    row["open"] = close
    row["high"] = close + 0.5
    row["low"] = close - 0.5 if low is None else low
    row["close"] = close
    row["pct_chg"] = (close / float(df.iloc[-1]["close"]) - 1) * 100
    return pd.concat([df, row.to_frame().T], ignore_index=True)


def test_major_actionability_boundaries_and_adjustment_status():
    df = _fixture()
    base = _detect(df)
    version = base["candidate_version"]
    breakout = base["major_zone_breakout"]["price"]

    at_ten = _detect(_append_bar(df, close=breakout * 1.10))
    over_ten = _detect(
        _append_bar(df, close=breakout * (1 + 10.0000004 / 100))
    )
    unknown = _detect(
        df.drop(columns=["data_source", "adj_factor", "adj_factor_source"]),
        metadata=ResistanceZoneMetadata(
            data_source="fixture",
            adj_factor_source="unknown",
        ),
    )
    assert next(
        item for item in at_ten["candidate_records"] if item["candidate_version"] == version
    )["major_zone_actionable_entry"]["actionable"] is True
    assert next(
        item for item in over_ten["candidate_records"] if item["candidate_version"] == version
    )["major_zone_actionable_entry"] == {
        "actionable": False,
        "bar_index": 164,
        "date": "2026-08-05",
        "price": 43.27,
        "confirmation_days": 1,
        "extended_pct_raw": pytest.approx(10.0000004),
        "extended_pct": 10.0,
    }
    extended_record = next(
        item for item in over_ten["candidate_records"] if item["candidate_version"] == version
    )
    assert extended_record["actionability_status"] == "extension_out_of_range"
    assert extended_record["major_zone_breakout"]["confirmed"] is True
    assert extended_record["near_zone_events"]["cleared_confirmed"]["triggered"] is True
    assert extended_record["stage"] == "extended"
    assert unknown["major_zone_actionable_entry"]["actionable"] is False
    assert unknown["actionability_status"] == "adjustment_unknown"
    assert unknown["major_zone_breakout"]["confirmed"] is True
    assert unknown["stage"] == "major_unverified"


def test_candidate_metadata_is_frozen_to_a_b_prefix():
    df = _fixture()
    baseline = _detect(
        df,
        metadata=ResistanceZoneMetadata(
            data_source="visible_baseline",
            adj_factor_source="tushare_native",
        ),
    )
    baseline_record = next(
        item
        for item in baseline["candidate_records"]
        if item["b"]["date"] == "2026-07-21"
    )
    changed = df.copy()
    b_idx = baseline_record["b"]["idx"]
    changed.loc[b_idx + 1:, "data_source"] = "post_b_changed"
    changed.loc[b_idx + 1:, "adj_factor_source"] = (
        "akshare_qfq_div_raw_fallback"
    )
    changed.loc[b_idx + 1:, "adj_factor"] = np.nan
    replayed = _detect(
        changed,
        metadata=ResistanceZoneMetadata(
            data_source="visible_changed",
            adj_factor_source="unknown",
        ),
    )
    replayed_record = next(
        item
        for item in replayed["candidate_records"]
        if item["candidate_version"] == baseline_record["candidate_version"]
    )

    assert replayed_record["candidate_version"] == baseline_record["candidate_version"]
    assert replayed_record["zone"]["zone_version"] == baseline_record["zone"]["zone_version"]
    assert replayed_record["zone"]["metadata"] == {
        "data_source": "fixture_source",
        "adj_factor_source": "tushare_native",
    }


@pytest.mark.parametrize(
    "trusted_source",
    ["tushare_native", "akshare_qfq_div_raw"],
)
def test_candidate_adjustment_source_whitelist_allows_major_actionability(
    trusted_source,
):
    df = _fixture()
    df["adj_factor_source"] = trusted_source

    result = _detect(df)

    assert result["major_zone_actionable_entry"]["actionable"] is True
    assert result["actionability_status"] == "actionable"


def test_fallback_adjustment_source_blocks_major_but_preserves_early():
    df = _fixture()
    df["adj_factor_source"] = "akshare_qfq_div_raw_fallback"

    result = _detect(df)

    assert result["early_reversal"]["triggered"] is True
    assert result["major_zone_breakout"]["confirmed"] is True
    assert result["major_zone_actionable_entry"]["actionable"] is False
    assert result["actionability_status"] == "adjustment_unknown"
    assert result["stage"] == "major_unverified"


def test_confirmation_three_day_limit_structure_floor_and_sync_window():
    df = _fixture()
    version = _detect(df)["candidate_version"]
    extended = df
    for close in (43.3, 43.4, 43.5):
        extended = _append_bar(extended, close=close)
    three_days = _detect(extended)
    record = next(
        item
        for item in three_days["candidate_records"]
        if item["candidate_version"] == version
    )
    assert record["major_zone_actionable_entry"]["confirmation_days"] == 3
    assert record["major_zone_actionable_entry"]["actionable"] is True
    four_days = _detect(_append_bar(extended, close=43.6))
    record = next(
        item
        for item in four_days["candidate_records"]
        if item["candidate_version"] == version
    )
    assert record["actionability_status"] == "confirmation_too_old"
    assert record["major_zone_breakout"]["confirmed"] is True
    assert record["near_zone_events"]["cleared_confirmed"]["triggered"] is True
    assert record["stage"] == "stale"

    broken = df.copy()
    broken.loc[160, "low"] = 30.0
    broken_record = next(
        item
        for item in _detect(broken)["candidate_records"]
        if item["candidate_version"] == version
    )
    assert broken_record["major_zone_breakout"]["confirmed"] is True
    assert broken_record["actionability_status"] == "structure_floor_broken"
    assert broken_record["stage"] == "invalidated"

    sync_three = df.copy()
    sync_three.loc[161, ["high", "close"]] = [41.2, 40.6]
    synced = next(
        item
        for item in _detect(sync_three)["candidate_records"]
        if item["candidate_version"] == version
    )
    assert synced["frozen_trendline"]["breakout_date"] == "2026-07-31"
    assert synced["major_zone_breakout"]["sync_gap"] == 3
    assert synced["major_zone_breakout"]["confirmed"] is True
    desynced = next(
        item
        for item in _detect(sync_three, sync_window=2)["candidate_records"]
        if item["candidate_version"] == version
    )
    assert desynced["major_zone_breakout"]["confirmed"] is False
    assert synced["zone"]["parameter_snapshot"]["sync_window"] == 3
    assert desynced["zone"]["parameter_snapshot"]["sync_window"] == 2
    assert desynced["zone"]["zone_version"] != synced["zone"]["zone_version"]

    params_two = replace(ResistanceZoneParams(), sync_window=2)
    inherited = next(
        item
        for item in _detect(sync_three, zone_params=params_two)["candidate_records"]
        if item["candidate_version"] == version
    )
    assert inherited["major_zone_breakout"]["confirmed"] is False
    assert inherited["zone"]["parameter_snapshot"]["sync_window"] == 2
    assert inherited["zone"]["zone_version"] == desynced["zone"]["zone_version"]


def test_macd_threshold_and_flat_classification_use_raw_values_before_rounding():
    from src.indicators.causal_bottom_divergence_detector import _macd_semantics

    threshold_crossing = _macd_semantics(
        a_price=1.0,
        dif_a=-0.0049996,
        dif_b=-0.003,
        dea_a=-0.006,
        dea_b=-0.004,
        tolerance=0.30,
    )
    flat_crossing = _macd_semantics(
        a_price=100.0,
        dif_a=-1.0,
        dif_b=-0.6999996,
        dea_a=-1.0,
        dea_b=-0.6999996,
        tolerance=0.30,
    )

    assert round(-0.0049996, 6) == -0.005
    assert threshold_crossing is None
    assert round(-0.6999996, 6) == -0.7
    assert flat_crossing == {
        "dif_relation": "up",
        "dea_relation": "up",
        "macd_relation": "up",
    }


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"lookback": True}, "lookback"),
        ({"lookback": 0}, "lookback"),
        ({"min_ab_gap": 0}, "min_ab_gap"),
        ({"max_ab_gap": 9, "min_ab_gap": 10}, "max_ab_gap"),
        ({"ab_match_window": -1}, "ab_match_window"),
        ({"flat_tolerance": np.nan}, "flat_tolerance"),
        ({"macd_flat_tolerance": -0.1}, "macd_flat_tolerance"),
        ({"break_tolerance": 1.1}, "break_tolerance"),
        ({"sync_window": True}, "sync_window"),
        ({"sync_window": -1}, "sync_window"),
        ({"retention_bars": False}, "retention_bars"),
        ({"retention_bars": -1}, "retention_bars"),
    ],
)
def test_invalid_parameters_raise_value_error_without_bool_coercion(kwargs, match):
    with pytest.raises(ValueError, match=match):
        _detect(_fixture(), **kwargs)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda df: df.assign(close=np.inf), "non_finite:close"),
        (lambda df: df.assign(high=df["low"] - 1), "high_below_low"),
        (lambda df: df.assign(date="not-a-date"), "invalid_date:date"),
        (
            lambda df: df.assign(date=list(reversed(df["date"].tolist()))),
            "non_monotonic_date:date",
        ),
        (
            lambda df: df.assign(date=[df["date"].iloc[0]] * len(df)),
            "duplicate_date:date",
        ),
    ],
)
def test_invalid_market_data_returns_stable_empty_template(mutate, reason):
    invalid = mutate(_fixture())
    result = _detect(invalid, as_of_index=100)

    assert result["found"] is False
    assert result["candidate_records"] == []
    assert result["rejection_reason"] == "invalid_market_data"
    assert reason in result["degradation_reasons"]
    assert _detect(invalid, as_of_index=100) == result


def test_indicator_work_is_precomputed_once_for_long_visible_frame(monkeypatch):
    import src.indicators.causal_bottom_divergence_detector as detector_module

    fixture = _fixture()
    long_frame = pd.concat([fixture] * 4, ignore_index=True)
    long_frame["date"] = pd.date_range("2024-01-01", periods=len(long_frame), freq="B")
    calls = {"macd": 0, "atr": 0}
    real_macd = detector_module.compute_macd
    real_atr = detector_module._atr_series

    def counted_macd(*args, **kwargs):
        calls["macd"] += 1
        return real_macd(*args, **kwargs)

    def counted_atr(*args, **kwargs):
        calls["atr"] += 1
        return real_atr(*args, **kwargs)

    monkeypatch.setattr(detector_module, "compute_macd", counted_macd)
    monkeypatch.setattr(detector_module, "_atr_series", counted_atr)
    result = detector_module.CausalBottomDivergenceDetector.detect(long_frame)

    assert result["candidate_records"]
    assert calls == {"macd": 1, "atr": 1}


def test_v2_context_wrapper_matches_legacy_contract():
    from src.indicators.causal_bottom_divergence_support import (
        has_required_context_v2,
    )

    down = pd.DataFrame(
        {
            "close": np.linspace(20.0, 10.0, 30),
            "high": np.linspace(20.5, 10.5, 30),
            "low": np.linspace(19.5, 9.5, 30),
        }
    )
    up = pd.DataFrame(
        {
            "close": np.linspace(10.0, 20.0, 30),
            "high": np.linspace(10.5, 20.5, 30),
            "low": np.linspace(9.5, 19.5, 30),
        }
    )
    flat = pd.DataFrame(
        {"close": [10.0] * 30, "high": [10.5] * 30, "low": [9.5] * 30}
    )

    assert has_required_context_v2(down, 29, "price_down") is True
    assert has_required_context_v2(down, 29, "price_up") is False
    assert has_required_context_v2(up, 29, "price_up") is True
    assert has_required_context_v2(up, 29, "price_down") is False
    assert has_required_context_v2(flat, 29, "price_down") is False
    assert has_required_context_v2(flat, 29, "price_up") is False


def test_v2_frozen_swing_helpers_reject_flat_plateaus():
    from src.indicators.causal_bottom_divergence_support import (
        find_swing_highs_v2,
        find_swing_lows_v2,
    )

    flat = pd.Series([5.0] * 20)

    assert find_swing_lows_v2(flat, order=2) == []
    assert find_swing_highs_v2(flat, order=2) == []


def test_invalidated_candidate_cannot_trigger_events_or_actionability_after_b():
    from src.indicators.causal_bottom_divergence_detector import (
        CausalBottomDivergenceDetector,
    )

    visible = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=15, freq="B"),
            "open": [6.0] * 15,
            "high": [6.5] * 15,
            "low": [5.8] * 15,
            "close": [6.0] * 15,
            "volume": [100.0] * 15,
            "pct_chg": [0.0] * 15,
        }
    )
    visible.loc[11, "low"] = 6.0
    visible.loc[12, ["open", "high", "low", "close"]] = [6.0, 11.5, 5.5, 11.0]
    evidence = {
        "candidate_version": "price-up-invalidated",
        "a": {"idx": 0, "date": "2026-01-01", "price": 5.0},
        "b": {"idx": 10, "date": "2026-01-15", "price": 6.0},
        "h": {"idx": 5, "date": "2026-01-08", "price": 8.0},
        "macd": {
            "a": {"dif": -1.0},
            "b": {"dif": -1.2},
        },
        "pattern": {"code": "price_up_macd_down"},
        "zone": {"found": True, "r1": None, "r2": {"upper": 10.0}},
        "frozen_trendline": {
            "found": True,
            "slope": 0.0,
            "intercept": 7.0,
            "touches": [],
            "touch_points": [],
            "breakout_confirmed": False,
            "breakout_bar_index": None,
            "breakout_date": None,
            "projected_value_at_breakout": None,
        },
        "degradation_reasons": [],
    }
    record = CausalBottomDivergenceDetector._replay_candidate(
        visible,
        evidence=evidence,
        break_tolerance=0.0,
        retention_bars=20,
        zone_params=ResistanceZoneParams(),
        metadata=ResistanceZoneMetadata(),
        atr_values=pd.Series([np.nan] * len(visible)),
    )

    assert record["lifecycle"] == "invalidated"
    assert record["invalidated_at"]["bar_index"] == 12
    assert record["structure_break"]["triggered"] is False
    assert record["frozen_trendline"]["breakout_confirmed"] is False
    assert record["major_zone_breakout"]["triggered"] is False
    assert record["major_zone_actionable_entry"]["actionable"] is False
    assert record["actionability_status"] == "candidate_invalidated"
    assert all(not point["triggered"] for point in record["layered_buy_points"])


@pytest.mark.parametrize(
    ("column", "value", "reason"),
    [
        ("low", 0.0, "non_positive:low"),
        ("high", -1.0, "non_positive:high"),
        ("close", 0.0, "non_positive:close"),
        ("open", 0.0, "non_positive:open"),
        ("open", np.inf, "non_finite:open"),
        ("volume", np.inf, "non_finite:volume"),
        ("volume", -1.0, "negative_volume"),
    ],
)
def test_invalid_ohlcv_values_return_market_data_rejection(column, value, reason):
    df = _fixture()
    df.loc[50, column] = value
    result = _detect(df, as_of_index=100)

    assert result["rejection_reason"] == "invalid_market_data"
    assert reason in result["degradation_reasons"]


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"open": 50.0, "high": 40.0}, "body_outside_range"),
        ({"close": 50.0, "high": 40.0}, "body_outside_range"),
        ({"close": 5.0, "low": 6.0}, "body_outside_range"),
    ],
)
def test_ohlc_body_must_be_inside_daily_range(updates, reason):
    df = _fixture()
    for column, value in updates.items():
        df.loc[50, column] = value

    result = _detect(df, as_of_index=100)
    assert result["rejection_reason"] == "invalid_market_data"
    assert reason in result["degradation_reasons"]


def test_nan_optional_open_and_volume_are_allowed():
    df = _fixture()
    df.loc[50, ["open", "volume"]] = np.nan

    result = _detect(df, as_of_index=100)
    assert result["rejection_reason"] is None


@pytest.mark.parametrize("metadata", [None, {}, "metadata"])
def test_metadata_must_be_resistance_zone_metadata(metadata):
    with pytest.raises((TypeError, ValueError), match="metadata"):
        _detect(_fixture(), metadata=metadata)


def test_primary_sorting_is_stable_and_invalidated_records_follow_active():
    result = _detect(_fixture())
    records = result["candidate_records"]

    assert records[0]["candidate_version"] == result["primary_candidate_version"]
    invalidated_positions = [
        idx for idx, item in enumerate(records) if item["lifecycle"] == "invalidated"
    ]
    assert invalidated_positions
    assert min(invalidated_positions) > max(
        idx for idx, item in enumerate(records) if item["lifecycle"] != "invalidated"
    )
    assert _detect(_fixture())["primary_candidate_version"] == result["primary_candidate_version"]
