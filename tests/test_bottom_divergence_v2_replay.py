# -*- coding: utf-8 -*-
from pathlib import Path

import pandas as pd
import pytest

from src.indicators.bottom_divergence_breakout_detector import (
    BottomDivergenceBreakoutDetector,
)
from src.indicators.causal_bottom_divergence_detector import (
    CausalBottomDivergenceDetector,
)


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "001337_bottom_divergence_20251201_20260805.csv"
)
EXPECTED_COLUMNS = [
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "pct_chg",
    "data_source",
    "adj_factor_source",
]


def test_001337_replay_fixture_contract():
    fixture = pd.read_csv(FIXTURE_PATH, parse_dates=["date"])

    assert fixture.columns.tolist() == EXPECTED_COLUMNS
    assert fixture["date"].is_monotonic_increasing
    assert fixture["date"].is_unique
    assert len(fixture) == 165
    assert fixture["date"].iloc[0] == pd.Timestamp("2025-12-01")
    assert fixture["date"].iloc[-1] == pd.Timestamp("2026-08-05")

    by_date = fixture.set_index("date")
    assert by_date.at[pd.Timestamp("2026-07-21"), "low"] == 31.68
    assert by_date.at[pd.Timestamp("2026-07-22"), "close"] == 36.60
    assert by_date.at[pd.Timestamp("2026-07-23"), "close"] == 40.26
    assert by_date.at[pd.Timestamp("2026-08-05"), "close"] == 43.27


def test_legacy_detector_replay_state_guard():
    fixture = pd.read_csv(FIXTURE_PATH, parse_dates=["date"])
    expected_states = {
        pd.Timestamp("2026-07-22"): "divergence_only",
        pd.Timestamp("2026-08-05"): "confirmed",
    }
    actual_states = {}

    for end_index, row in fixture.iterrows():
        replay_slice = fixture.iloc[: end_index + 1]
        result = BottomDivergenceBreakoutDetector.detect(replay_slice)
        if row["date"] in expected_states:
            actual_states[row["date"]] = result["state"]

    assert actual_states == expected_states


def test_causal_detector_001337_daily_replay_contract():
    fixture = pd.read_csv(FIXTURE_PATH, parse_dates=["date"])
    expected = {
        "2026-07-22": ("early", "2026-07-22"),
        "2026-07-23": ("near_cleared", "2026-07-23"),
        "2026-08-05": ("major_unverified", "2026-08-05"),
    }
    actual = {}
    frozen_by_version = {}
    historical_events = {}
    previous_records = {}
    date_to_index = {
        value.strftime("%Y-%m-%d"): idx
        for idx, value in fixture["date"].items()
    }

    for end_index, row in fixture.iloc[34:].iterrows():
        day = row["date"].strftime("%Y-%m-%d")
        result = CausalBottomDivergenceDetector.detect(
            fixture,
            as_of_index=end_index,
            retention_bars=3,
        )
        current_records = {
            record["candidate_version"]: record
            for record in result["candidate_records"]
        }
        for version, previous in previous_records.items():
            if previous["lifecycle"] == "invalidated":
                invalidated_index = date_to_index[previous["invalidated_at"]["date"]]
                if end_index - invalidated_index <= 3:
                    assert version in current_records
            else:
                assert version in current_records
            current = current_records.get(version)
            if current is not None:
                if previous["lifecycle"] == "confirmed":
                    assert current["lifecycle"] != "provisional"
                if previous["invalidated_at"] is not None:
                    assert current["invalidated_at"] == previous["invalidated_at"]
        for record in result["candidate_records"]:
            for point in (record["a"], record["b"]):
                assert point["date"] <= day
            for side in ("a", "b"):
                assert record["macd"][side]["dif_point"]["date"] <= day
                assert record["macd"][side]["dea_point"]["date"] <= day
            for zone in record["zone"]["zones"]:
                assert all(
                    touch["date"] <= day for touch in zone["touch_points"]
                )

            frozen = {
                "a": record["a"],
                "b": record["b"],
                "macd": record["macd"],
                "zone_version": record["zone"]["zone_version"],
                "zone_bounds": [
                    (zone["zone_id"], zone["lower"], zone["upper"])
                    for zone in record["zone"]["zones"]
                ],
            }
            prior = frozen_by_version.setdefault(
                record["candidate_version"],
                frozen,
            )
            assert frozen == prior

            current_events = {
                "early": record["early_reversal"]["date"],
                "trendline": record["frozen_trendline"]["breakout_date"],
                "r1_entered": record["near_zone_events"]["entered"]["date"],
                "r1_accepted": record["near_zone_events"]["accepted"]["date"],
                "r1_crossed": record["near_zone_events"]["crossed"]["date"],
                "r1_cleared": record["near_zone_events"]["cleared_confirmed"]["date"],
                "major": record["major_zone_breakout"]["date"],
                "invalidated": (
                    record["invalidated_at"]["date"]
                    if record["invalidated_at"] is not None
                    else None
                ),
            }
            for event_name, event_date in current_events.items():
                key = (record["candidate_version"], event_name)
                if key in historical_events:
                    assert event_date == historical_events[key]
                if event_date is not None:
                    assert event_date <= day
                    assert historical_events.setdefault(key, event_date) == event_date
            if record["lifecycle"] == "invalidated":
                assert result["primary_candidate_version"] != record["candidate_version"]
        previous_records = current_records

        if day == "2026-07-22":
            event_date = result["early_reversal"]["date"]
            assert result["early_reversal"]["strength"] == pytest.approx(
                0.831397,
                abs=1e-6,
            )
        elif day == "2026-07-23":
            event_date = result["near_zone_events"]["crossed"]["date"]
            assert result["near_zone_events"]["cleared_confirmed"]["triggered"]
        else:
            event_date = None
        if day in expected:
            if day == "2026-08-05":
                event_date = result["major_zone_breakout"]["date"]
                assert result["major_zone_breakout"]["confirmed"]
                assert not result["major_zone_actionable_entry"]["actionable"]
                assert result["actionability_status"] == "adjustment_unknown"
            actual[day] = (result["stage"], event_date)

    assert actual == expected
    final = CausalBottomDivergenceDetector.detect(fixture)
    assert final["zone"]["r1"]["latest_touch_date"] <= "2026-07-21"
    assert final["zone"]["r1"]["lower"] == 37.46
    assert final["zone"]["r1"]["upper"] == 39.2
    assert final["zone"]["r2"]["lower"] == 40.61
    assert final["zone"]["r2"]["upper"] == 42.9
