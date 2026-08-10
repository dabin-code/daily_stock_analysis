from dataclasses import FrozenInstanceError, replace
import json

import numpy as np
import pandas as pd
import pytest

from src.indicators.resistance_zone_detector import (
    ResistanceZoneDetector,
    ResistanceZoneMetadata,
    ResistanceZoneParams,
    _atr14,
    _canonical_json,
    _score,
    _volume_ratio_5,
    _weighted_quantile,
)


def test_params_defaults_are_frozen_and_weights_are_validated():
    params = ResistanceZoneParams()

    assert params.swing_order == 5
    assert params.cluster_pct == 0.015
    assert params.r1_touch_weight == 0.30
    assert params.r2_height_weight == 0.10
    with pytest.raises(FrozenInstanceError):
        params.cluster_pct = 0.02
    with pytest.raises(ValueError, match="R1 weights"):
        replace(params, r1_touch_weight=-0.01, r1_distance_weight=0.36)
    with pytest.raises(ValueError, match="R2 weights"):
        replace(params, r2_touch_weight=0.36)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cluster_pct", np.nan),
        ("cluster_pct", np.inf),
        ("cluster_pct", 0.0),
        ("atr_gap_multiplier", -0.1),
        ("long_wick_ratio", -0.1),
        ("long_wick_ratio", 1.1),
        ("rejection_wick_ratio", np.inf),
        ("rejection_atr_ratio", -0.1),
        ("score_min", np.nan),
        ("score_min", 1.1),
        ("overlap_ratio", 0.0),
        ("overlap_ratio", 1.1),
        ("breakout_buffer_pct", -0.1),
    ],
)
def test_params_reject_invalid_float_values(field, value):
    with pytest.raises(ValueError, match=field):
        replace(ResistanceZoneParams(), **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("swing_order", True),
        ("swing_order", 1.0),
        ("swing_order", 0),
        ("sync_window", False),
        ("sync_window", -1),
        ("invalidated_retention_bars", 1.0),
        ("invalidated_retention_bars", -1),
    ],
)
def test_params_reject_non_integer_or_negative_integer_values(field, value):
    with pytest.raises(ValueError, match=field):
        replace(ResistanceZoneParams(), **{field: value})


def test_params_accept_documented_boundaries():
    params = replace(
        ResistanceZoneParams(),
        atr_gap_multiplier=0.0,
        long_wick_ratio=0.0,
        rejection_wick_ratio=1.0,
        rejection_atr_ratio=0.0,
        score_min=1.0,
        overlap_ratio=1.0,
        breakout_buffer_pct=0.0,
        sync_window=0,
        invalidated_retention_bars=0,
    )

    assert params.overlap_ratio == 1.0


def test_params_reject_volume_only_weight_configuration():
    params = ResistanceZoneParams()
    with pytest.raises(ValueError, match="R1 non-volume"):
        replace(
            params,
            r1_touch_weight=0.0,
            r1_recency_weight=0.0,
            r1_volume_weight=1.0,
            r1_rejection_weight=0.0,
            r1_tightness_weight=0.0,
            r1_distance_weight=0.0,
        )
    with pytest.raises(ValueError, match="R2 non-volume"):
        replace(
            params,
            r2_touch_weight=0.0,
            r2_recency_weight=0.0,
            r2_volume_weight=1.0,
            r2_rejection_weight=0.0,
            r2_tightness_weight=0.0,
            r2_height_weight=0.0,
        )


def test_metadata_is_frozen():
    metadata = ResistanceZoneMetadata("provider", "forward")

    with pytest.raises(FrozenInstanceError):
        metadata.data_source = "other"


def test_canonical_json_sorts_keys_rounds_floats_and_serializes_dates_and_nan():
    payload = {
        "z": np.float64(np.nan),
        "date": pd.Timestamp("2026-08-05 12:34:56"),
        "nested": {"b": np.float64(1.23456789), "a": np.int64(2)},
    }

    encoded = _canonical_json(payload)

    assert encoded == (
        '{"date":"2026-08-05","nested":{"a":2,"b":1.234568},"z":null}'
    )
    assert json.loads(encoded)["z"] is None


def _frame(
    highs,
    *,
    closes=None,
    lows=None,
    opens=None,
    volumes=None,
    start="2026-01-01",
):
    size = len(highs)
    closes = closes if closes is not None else [9.5] * size
    lows = lows if lows is not None else [9.0] * size
    data = {
        "date": pd.date_range(start, periods=size, freq="D"),
        "high": highs,
        "low": lows,
        "close": closes,
    }
    if opens is not None:
        data["open"] = opens
    if volumes is not None:
        data["volume"] = volumes
    return pd.DataFrame(data)


def test_atr14_uses_true_range_simple_mean_through_b_only():
    df = _frame(
        [11.0] * 15 + [1000.0],
        closes=[10.0] * 16,
        lows=[9.0] * 15 + [1.0],
        opens=[10.0] * 16,
        volumes=[100.0] * 16,
    )

    atr = _atr14(df.iloc[:15])

    assert atr == pytest.approx(2.0)


def test_extracts_swing_and_rejection_touch_once_per_day():
    highs = [10.0] * 20
    highs[5] = 12.0
    highs[10] = 12.05
    closes = [9.5] * 20
    closes[5] = closes[10] = 10.0
    result = ResistanceZoneDetector.calculate(
        _frame(
            highs,
            closes=closes,
            opens=[9.8] * 20,
            volumes=[100.0] * 20,
        ),
        a_idx=0,
        b_idx=19,
        candidate_version="candidate-v1",
        params=replace(ResistanceZoneParams(), swing_order=1),
    )

    touch_dates = [
        point["date"]
        for zone in result["zones"]
        for point in zone["touch_points"]
    ]
    assert touch_dates.count("2026-01-06") == 1
    assert touch_dates.count("2026-01-11") == 1
    assert result["degradation_reasons"] == []


def test_frozen_zone_evidence_is_immutable_serializable_and_equivalent(
    monkeypatch,
):
    highs = [10.0] * 20
    highs[5] = 12.0
    highs[10] = 12.05
    closes = [9.5] * 20
    closes[5] = closes[10] = 10.0
    frame = _frame(
        highs,
        closes=closes,
        opens=[9.8] * 20,
        volumes=[100.0] * 20,
    )
    base = replace(ResistanceZoneParams(), swing_order=1)
    calls = {"prefix": 0, "touches": 0}
    original_prefix = ResistanceZoneDetector._canonical_prefix
    original_touches = ResistanceZoneDetector._extract_touches

    def count_prefix(prefix):
        calls["prefix"] += 1
        return original_prefix(prefix)

    def count_touches(*args, **kwargs):
        calls["touches"] += 1
        return original_touches(*args, **kwargs)

    monkeypatch.setattr(
        ResistanceZoneDetector,
        "_canonical_prefix",
        staticmethod(count_prefix),
    )
    monkeypatch.setattr(
        ResistanceZoneDetector,
        "_extract_touches",
        staticmethod(count_touches),
    )
    frozen = ResistanceZoneDetector.freeze_evidence(
        frame,
        a_idx=0,
        b_idx=19,
        candidate_version="candidate-v1",
        params=base,
    )
    assert json.loads(json.dumps(frozen.to_dict()))["content_hash"] == (
        frozen.content_hash
    )
    with pytest.raises(FrozenInstanceError):
        frozen.payload_json = "{}"

    for params in (
        replace(base, cluster_pct=0.01, score_min=0.4),
        replace(base, cluster_pct=0.015, score_min=0.45),
        replace(
            base,
            cluster_pct=0.02,
            atr_gap_multiplier=0.75,
            score_min=0.5,
        ),
    ):
        expected = ResistanceZoneDetector.calculate(
            frame,
            a_idx=0,
            b_idx=19,
            candidate_version="candidate-v1",
            params=params,
        )
        actual = ResistanceZoneDetector.evaluate_frozen_evidence(
            frozen,
            params=params,
        )
        assert actual == expected

    assert calls == {"prefix": 4, "touches": 4}
    calls_before_reuse = dict(calls)
    ResistanceZoneDetector.evaluate_frozen_evidence(
        frozen,
        params=base,
    )
    assert calls == calls_before_reuse


def test_missing_open_and_volume_use_fallbacks_and_record_unique_reasons():
    highs = [10.0, 10.0, 12.0, 10.0, 12.05, 10.0, 10.0]
    closes = [9.8, 9.8, 10.0, 9.8, 10.0, 9.8, 9.0]
    result = ResistanceZoneDetector.calculate(
        _frame(highs, closes=closes),
        a_idx=0,
        b_idx=6,
        candidate_version="candidate-v1",
        params=replace(ResistanceZoneParams(), swing_order=1),
    )

    assert result["degradation_reasons"] == [
        "atr_unavailable",
        "missing_open",
        "missing_volume",
    ]
    touches = result["zones"][0]["touch_points"]
    assert touches[0]["body_top"] == touches[0]["close"]
    assert all(point["volume_ratio_5"] is None for point in touches)


def test_rows_after_b_do_not_change_result_or_atr():
    prefix = _frame(
        [10.0, 10.0, 12.0, 10.0, 12.05, 10.0, 10.0],
        closes=[9.8, 9.8, 10.0, 9.8, 10.0, 9.8, 9.0],
        opens=[9.8] * 7,
        volumes=[100.0] * 7,
    )
    suffix = _frame(
        [1000.0, 2000.0],
        closes=[500.0, 1000.0],
        lows=[1.0, 1.0],
        opens=[10.0, 10.0],
        volumes=[1_000_000.0, 2_000_000.0],
        start="2027-01-01",
    )
    params = replace(ResistanceZoneParams(), swing_order=1)

    before = ResistanceZoneDetector.calculate(
        prefix,
        a_idx=0,
        b_idx=6,
        candidate_version="candidate-v1",
        params=params,
    )
    after = ResistanceZoneDetector.calculate(
        pd.concat([prefix, suffix], ignore_index=True),
        a_idx=0,
        b_idx=6,
        candidate_version="candidate-v1",
        params=params,
    )

    assert after == before


def test_weighted_quantile_uses_first_cumulative_weight_without_interpolation():
    values = [30.0, 10.0, 20.0]
    weights = [1.0, 1.0, 2.0]

    assert _weighted_quantile(values, weights, 0.25) == 10.0
    assert _weighted_quantile(values, weights, 0.50) == 20.0
    assert _weighted_quantile(values, weights, 0.90) == 30.0


def _touch(anchor, tolerance, *, day, body=None, high=None):
    return {
        "anchor_price": anchor,
        "tolerance": tolerance,
        "date": day,
        "body_top": anchor if body is None else body,
        "high": anchor if high is None else high,
        "weight": 1.0,
    }


def test_connected_components_sort_by_anchor_and_allow_transitive_chains():
    touches = [
        _touch(10.36, 0.20, day="2026-01-03"),
        _touch(10.00, 0.20, day="2026-01-01"),
        _touch(10.18, 0.20, day="2026-01-02"),
        _touch(11.00, 0.20, day="2026-01-04"),
    ]

    components = ResistanceZoneDetector._connected_components(touches)

    assert [[point["date"] for point in group] for group in components] == [
        ["2026-01-01", "2026-01-02", "2026-01-03"],
        ["2026-01-04"],
    ]


def test_sixty_percent_overlap_merges_and_recomputes_bounds():
    left = [
        _touch(10.0, 0.1, day="2026-01-01", body=10.0, high=11.5),
        _touch(10.1, 0.1, day="2026-01-02", body=10.5, high=11.5),
        _touch(10.2, 0.1, day="2026-01-03", body=11.0, high=11.5),
        _touch(10.3, 0.1, day="2026-01-04", body=11.5, high=11.5),
    ]
    right = [
        _touch(10.8, 0.1, day="2026-01-05", body=10.4, high=11.9),
        _touch(10.9, 0.1, day="2026-01-06", body=10.9, high=11.9),
        _touch(11.0, 0.1, day="2026-01-07", body=11.4, high=11.9),
        _touch(11.1, 0.1, day="2026-01-08", body=11.9, high=11.9),
    ]

    merged = ResistanceZoneDetector._merge_overlaps(
        [left, right],
        atr_b=None,
        params=ResistanceZoneParams(),
    )

    assert len(merged) == 1
    assert ResistanceZoneDetector._bounds(merged[0], None) == (
        10.4,
        10.9,
        11.4,
        11.445,
    )


def test_bounds_keep_upper_at_or_above_upper_body_when_atr_cap_is_lower():
    touches = [
        _touch(10.0 + idx, 0.1, day=f"2026-02-0{idx + 1}", body=10.0 + idx, high=13.0)
        for idx in range(4)
    ]

    bounds = ResistanceZoneDetector._bounds(touches, atr_b=0.1)

    assert bounds == (10.0, 11.0, 12.0, 12.0)
    assert bounds[0] <= bounds[1] <= bounds[2] <= bounds[3]


def test_volume_ratio_uses_only_prior_five_valid_values():
    volume = pd.Series(
        [10.0, 20.0, 30.0, 40.0, 50.0, np.nan, np.nan, 0.0, -1.0, np.nan, 100.0]
    )

    assert _volume_ratio_5(volume, 10) == pytest.approx(100.0 / 30.0)
    assert _volume_ratio_5(volume, 0) is None


def test_mixed_atr_rejection_components_share_one_scoring_scale():
    prefix = _frame(
        [10.0, 12.0, 10.0, 12.0, 9.0],
        closes=[9.8, 11.64, 9.8, 11.0, 9.0],
        lows=[9.0, 11.0, 9.0, 10.0, 8.5],
        opens=[9.8, 11.64, 9.8, 11.0, 9.0],
        volumes=[100.0] * 5,
    )
    degradation = set()
    touches = ResistanceZoneDetector._extract_touches(
        prefix,
        original_a_idx=0,
        atr_values=pd.Series([np.nan, np.nan, np.nan, 2.0, 2.0]),
        atr_b=2.0,
        params=replace(ResistanceZoneParams(), swing_order=1),
        degradation=degradation,
    )
    zone = ResistanceZoneDetector._build_zone(
        touches,
        atr_b=2.0,
        b_idx=4,
        b_close=9.0,
        b_low=8.5,
        max_high=12.0,
        zone_version="v" * 64,
        params=replace(ResistanceZoneParams(), swing_order=1),
    )
    public = ResistanceZoneDetector._public_zone(zone, zone["_r1_score"])

    expected_fallback_component = ((12.0 - 11.64) / 11.64) / 0.03
    assert zone["features"]["rejection"] == pytest.approx(
        (expected_fallback_component + 0.5) / 2
    )
    assert public["touch_points"][0]["rejection_atr_ratio"] is None
    assert public["touch_points"][0]["rejection_pct"] == pytest.approx(0.030928)
    assert public["touch_points"][0]["rejection_score_component"] == pytest.approx(
        expected_fallback_component
    )
    assert public["touch_points"][1]["rejection_atr_ratio"] == 0.5
    assert public["touch_points"][1]["rejection_pct"] == pytest.approx(0.090909)
    assert public["touch_points"][1]["rejection_score_component"] == 0.5


def _zone_rejection(*components):
    touches = [
        {
            "idx": idx,
            "_local_idx": idx,
            "date": f"2026-01-{idx + 1:02d}",
            "high": 10.1,
            "body_top": 10.0,
            "weight": 1.0,
            "volume_ratio_5": 1.0,
            "rejection_score_component": component,
        }
        for idx, component in enumerate(components)
    ]
    zone = ResistanceZoneDetector._build_zone(
        touches,
        atr_b=1.0,
        b_idx=len(touches),
        b_close=9.0,
        b_low=8.0,
        max_high=10.1,
        zone_version="v" * 64,
        params=ResistanceZoneParams(),
    )
    return zone["features"]["rejection"]


def test_rejection_clips_only_after_weighted_mean():
    assert _zone_rejection(2.0, 0.2) == 1.0


def test_rejection_preserves_subunit_weighted_mean():
    assert _zone_rejection(0.4, 0.2) == pytest.approx(0.3)


def test_missing_volume_score_drops_weight_and_renormalizes():
    params = ResistanceZoneParams()
    features = {
        "touch": 0.5,
        "recency": 0.8,
        "volume": 0.0,
        "rejection": 0.6,
        "tightness": 0.7,
        "distance": 0.9,
        "height": 0.4,
    }

    score = _score(features, params, "r1", has_volume=False)

    expected = (
        0.5 * 0.30
        + 0.8 * 0.25
        + 0.6 * 0.15
        + 0.7 * 0.10
        + 0.9 * 0.05
    ) / 0.85
    assert score == pytest.approx(expected)


def test_calculate_without_volume_renormalizes_public_score_without_division_error():
    params = replace(
        ResistanceZoneParams(),
        swing_order=1,
        r1_touch_weight=0.01,
        r1_recency_weight=0.0,
        r1_volume_weight=0.99,
        r1_rejection_weight=0.0,
        r1_tightness_weight=0.0,
        r1_distance_weight=0.0,
        r2_touch_weight=0.01,
        r2_recency_weight=0.0,
        r2_volume_weight=0.99,
        r2_rejection_weight=0.0,
        r2_tightness_weight=0.0,
        r2_height_weight=0.0,
    )
    result = ResistanceZoneDetector.calculate(
        _frame(
            [10.0, 10.0, 12.0, 10.0, 12.05, 10.0, 10.0],
            closes=[9.8, 9.8, 10.0, 9.8, 10.0, 9.8, 9.0],
        ),
        a_idx=0,
        b_idx=6,
        candidate_version="candidate-v1",
        params=params,
    )

    assert result["r1"]["features"]["volume"] == 0.0
    assert result["r1"]["features"]["touch"] == 0.5
    assert result["r1"]["score"] == 0.5
    assert "missing_volume" in result["degradation_reasons"]


def test_single_long_wick_is_never_r1_but_high_volume_strong_rejection_is_low_r2():
    result = ResistanceZoneDetector.calculate(
        _frame(
            [9.0, 10.0, 12.0, 11.5, 11.0, 10.0, 9.0],
            closes=[8.9, 9.9, 10.0, 11.4, 10.9, 9.9, 9.0],
            lows=[8.5, 9.5, 9.0, 11.0, 10.5, 9.5, 8.5],
            opens=[8.9, 9.9, 10.0, 11.4, 10.9, 9.9, 9.0],
            volumes=[100.0, 100.0, 300.0, 100.0, 100.0, 100.0, 100.0],
        ),
        a_idx=0,
        b_idx=6,
        candidate_version="candidate-v1",
        params=replace(ResistanceZoneParams(), swing_order=1),
    )

    assert result["found"] is True
    assert result["r1"] is None
    assert result["r2"]["confidence"] == "low"
    assert result["r2"]["touch_count"] == 1
    assert result["zone_count"] == 1


def test_unqualified_single_touch_produces_no_zone():
    result = ResistanceZoneDetector.calculate(
        _frame(
            [9.0, 10.0, 12.0, 11.5, 11.0, 10.0, 9.0],
            closes=[8.9, 9.9, 10.0, 11.4, 10.9, 9.9, 9.0],
            lows=[8.5, 9.5, 9.0, 11.0, 10.5, 9.5, 8.5],
            opens=[8.9, 9.9, 10.0, 11.4, 10.9, 9.9, 9.0],
            volumes=[100.0] * 7,
        ),
        a_idx=0,
        b_idx=6,
        candidate_version="candidate-v1",
        params=replace(ResistanceZoneParams(), swing_order=1),
    )

    assert result["found"] is False
    assert result["zones"] == []
    assert result["zone_count"] == 0


def test_selects_nearest_r1_and_next_higher_r2_with_zone_count_two():
    highs = [9.5] * 15
    closes = [9.2] * 15
    opens = [9.2] * 15
    for idx, high, body in (
        (2, 11.0, 10.8),
        (5, 11.05, 10.8),
        (8, 15.0, 14.8),
        (11, 15.05, 14.8),
    ):
        highs[idx] = high
        closes[idx] = body
        opens[idx] = body
    result = ResistanceZoneDetector.calculate(
        _frame(
            highs,
            closes=closes,
            lows=[8.8] * 15,
            opens=opens,
            volumes=[100.0] * 15,
        ),
        a_idx=0,
        b_idx=14,
        candidate_version="candidate-v1",
        params=replace(ResistanceZoneParams(), swing_order=1),
    )

    assert result["zone_count"] == 2
    assert result["r1"]["center"] == 10.8
    assert result["r2"]["center"] == 14.8
    assert result["r2"]["lower"] > result["r1"]["upper"]
    assert result["zones"] == sorted(result["zones"], key=lambda zone: zone["zone_id"])


def test_no_r1_selects_best_r2_directly():
    result = ResistanceZoneDetector.calculate(
        _frame(
            [10.0, 10.0, 12.0, 10.0, 12.05, 10.0, 10.0],
            closes=[10.2, 10.2, 10.0, 10.2, 10.0, 10.2, 10.5],
            lows=[9.0] * 7,
            opens=[10.0] * 7,
            volumes=[100.0] * 7,
        ),
        a_idx=0,
        b_idx=6,
        candidate_version="candidate-v1",
        params=replace(ResistanceZoneParams(), swing_order=1),
    )

    assert result["r1"] is None
    assert result["r2"] is not None
    assert result["zone_count"] == 1


def test_zones_exclude_multi_touch_cluster_that_qualifies_for_neither_role():
    params = replace(
        ResistanceZoneParams(),
        swing_order=1,
        r1_touch_weight=0.0,
        r1_recency_weight=0.0,
        r1_volume_weight=0.0,
        r1_rejection_weight=0.0,
        r1_tightness_weight=0.0,
        r1_distance_weight=1.0,
        r2_touch_weight=0.0,
        r2_recency_weight=0.0,
        r2_volume_weight=0.0,
        r2_rejection_weight=0.0,
        r2_tightness_weight=0.0,
        r2_height_weight=1.0,
    )
    result = ResistanceZoneDetector.calculate(
        _frame(
            [10.0, 10.0, 12.0, 10.0, 12.05, 10.0, 10.0],
            closes=[10.2, 10.2, 10.0, 10.2, 10.0, 10.2, 10.5],
            lows=[9.0] * 7,
            opens=[10.0] * 7,
            volumes=[100.0] * 7,
        ),
        a_idx=0,
        b_idx=6,
        candidate_version="candidate-v1",
        params=params,
    )

    assert result["zones"] == []
    assert result["found"] is False
    assert result["zone_count"] == 0


def test_r1_and_r2_ties_fall_back_to_zone_id():
    base = {
        "touch_count": 2,
        "lower": 11.0,
        "upper": 11.5,
        "center": 11.25,
        "_r1_score": 0.6,
        "_r2_score": 0.6,
        "_latest_idx": 5,
        "features": {"distance": 0.8},
    }
    zones = [base | {"zone_id": "b"}, base | {"zone_id": "a"}]
    params = ResistanceZoneParams()

    assert ResistanceZoneDetector._select_r1(zones, 10.0, params)["zone_id"] == "a"
    assert ResistanceZoneDetector._select_r2(zones, None, params)["zone_id"] == "a"


def test_versions_are_stable_and_change_with_params_weights_prefix_and_metadata():
    df = _frame(
        [10.0, 10.0, 12.0, 10.0, 12.05, 10.0, 10.0],
        closes=[9.8, 9.8, 10.0, 9.8, 10.0, 9.8, 9.0],
        opens=[9.8] * 7,
        volumes=[100.0] * 7,
    )
    params = replace(ResistanceZoneParams(), swing_order=1)

    def calculate(frame=df, selected_params=params, metadata=ResistanceZoneMetadata()):
        return ResistanceZoneDetector.calculate(
            frame,
            a_idx=0,
            b_idx=6,
            candidate_version="unchanged-candidate",
            params=selected_params,
            metadata=metadata,
        )

    first = calculate()
    assert calculate() == first
    assert len(first["zone_version"]) == 64
    assert all(len(zone["zone_id"]) == 64 for zone in first["zones"])
    assert calculate(selected_params=replace(params, cluster_pct=0.02))["zone_version"] != first["zone_version"]
    changed_weights = replace(
        params,
        r1_touch_weight=0.31,
        r1_distance_weight=0.04,
    )
    assert calculate(selected_params=changed_weights)["zone_version"] != first["zone_version"]
    changed_df = df.copy()
    changed_df.loc[0, "close"] = 9.81
    assert calculate(frame=changed_df)["zone_version"] != first["zone_version"]
    assert calculate(
        metadata=ResistanceZoneMetadata("provider", "forward")
    )["zone_version"] != first["zone_version"]
    assert first["candidate_version"] == "unchanged-candidate"


def test_nan_volume_is_serialized_as_null_in_hashable_prefix():
    df = _frame(
        [10.0, 10.0, 12.0, 10.0],
        closes=[9.8, 9.8, 10.0, 9.0],
        opens=[9.8] * 4,
        volumes=[100.0, 100.0, np.nan, 100.0],
    )

    result = ResistanceZoneDetector.calculate(
        df,
        a_idx=0,
        b_idx=3,
        candidate_version="candidate-v1",
        params=replace(ResistanceZoneParams(), swing_order=1),
    )

    assert len(result["zone_version"]) == 64
    assert result["degradation_reasons"] == [
        "atr_unavailable",
        "missing_volume",
    ]


def test_missing_date_uses_global_index_identity_and_preserves_fallback_anchor():
    df = _frame(
        [8.0, 8.5, 9.0, 10.0, 12.0, 11.5, 11.0, 10.0, 9.0],
        closes=[7.9, 8.4, 8.9, 9.9, 10.0, 11.4, 10.9, 9.9, 9.0],
        lows=[7.5, 8.0, 8.5, 9.5, 9.0, 11.0, 10.5, 9.5, 8.5],
        opens=[7.9, 8.4, 8.9, 9.9, 10.0, 11.4, 10.9, 9.9, 9.0],
        volumes=[100.0, 100.0, 100.0, 100.0, 300.0, 100.0, 100.0, 100.0, 100.0],
    ).drop(columns="date")
    df.index = pd.date_range("2026-03-01", periods=len(df), freq="D")

    result = ResistanceZoneDetector.calculate(
        df,
        a_idx=2,
        b_idx=8,
        candidate_version="candidate-v1",
        params=replace(ResistanceZoneParams(), swing_order=1),
    )

    point = result["r2"]["touch_points"][0]
    assert point["date"] == "index:4"
    assert point["anchor_price"] == 10.3
    assert "missing_date" in result["degradation_reasons"]


def test_date_presence_does_not_change_zone_or_touch_counts():
    dated = _frame(
        [9.0, 10.0, 12.0, 11.5, 11.0, 10.0, 9.0],
        closes=[8.9, 9.9, 10.0, 11.4, 10.9, 9.9, 9.0],
        lows=[8.5, 9.5, 9.0, 11.0, 10.5, 9.5, 8.5],
        opens=[8.9, 9.9, 10.0, 11.4, 10.9, 9.9, 9.0],
        volumes=[100.0, 100.0, 300.0, 100.0, 100.0, 100.0, 100.0],
    )
    undated = dated.drop(columns="date")
    params = replace(ResistanceZoneParams(), swing_order=1)

    with_date = ResistanceZoneDetector.calculate(
        dated,
        a_idx=0,
        b_idx=6,
        candidate_version="candidate-v1",
        params=params,
    )
    without_date = ResistanceZoneDetector.calculate(
        undated,
        a_idx=0,
        b_idx=6,
        candidate_version="candidate-v1",
        params=params,
    )

    assert without_date["zone_count"] == with_date["zone_count"]
    assert [zone["touch_count"] for zone in without_date["zones"]] == [
        zone["touch_count"] for zone in with_date["zones"]
    ]
    assert without_date["r2"]["touch_points"][0]["date"] == "index:2"
    assert "missing_date" in without_date["degradation_reasons"]
    assert "missing_date" not in with_date["degradation_reasons"]


def test_output_schema_snapshot_and_candidate_version_affect_hash():
    df = _frame(
        [9.0, 10.0, 12.0, 11.5, 11.0, 10.0, 9.0],
        closes=[8.9, 9.9, 10.0, 11.4, 10.9, 9.9, 9.0],
        lows=[8.5, 9.5, 9.0, 11.0, 10.5, 9.5, 8.5],
        opens=[8.9, 9.9, 10.0, 11.4, 10.9, 9.9, 9.0],
        volumes=[100.0, 100.0, 300.0, 100.0, 100.0, 100.0, 100.0],
    )
    params = replace(ResistanceZoneParams(), swing_order=1)
    first = ResistanceZoneDetector.calculate(
        df,
        a_idx=0,
        b_idx=6,
        candidate_version="candidate-v1",
        params=params,
        metadata=ResistanceZoneMetadata("provider", "forward"),
    )
    second = ResistanceZoneDetector.calculate(
        df,
        a_idx=0,
        b_idx=6,
        candidate_version="candidate-v2",
        params=params,
        metadata=ResistanceZoneMetadata("provider", "forward"),
    )

    assert set(first) == {
        "found",
        "algorithm_version",
        "candidate_version",
        "zone_version",
        "parameter_snapshot",
        "metadata",
        "zones",
        "r1",
        "r2",
        "zone_count",
        "degradation_reasons",
    }
    assert first["algorithm_version"] == "resistance-zone-v2"
    assert first["parameter_snapshot"] == {
        field: getattr(params, field)
        for field in params.__dataclass_fields__
    }
    assert first["metadata"] == {
        "data_source": "provider",
        "adj_factor_source": "forward",
    }
    assert second["zone_version"] != first["zone_version"]
    assert second["zones"][0]["zone_id"] != first["zones"][0]["zone_id"]
