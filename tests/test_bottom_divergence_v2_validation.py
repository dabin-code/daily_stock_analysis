# -*- coding: utf-8 -*-
"""Task 8: reproducible sample-out validation tests."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace as dc_replace
from datetime import date, timedelta
from copy import deepcopy
import json
from argparse import Namespace
from pathlib import Path
import subprocess
import sys
import statistics
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.validate_bottom_divergence_v2 import (
    CandidateEventEvidence,
    ReplayBatch,
    ReplayDependencies,
    _build_validation_sample,
    _parse_position_weight,
    _v1_breakout_floor,
    build_isolated_config,
    canonical_json_dumps,
    compute_pre_signal_features,
    isolated_replay_database,
    replay_historical_dates,
    replay_maturation_events,
    run_validation_cli,
)
from src.backtest.evaluators.base_evaluator import EvaluationResult
from src.config import Config
from src.backtest.services.bottom_divergence_v2_validation import (
    BottomDivergenceV2Validator,
    ValidationInputError,
    ValidationSample,
    assign_tertile,
    build_parameter_snapshots,
    canonical_parameter_hash,
    chronological_split,
    compute_conversion_metrics,
    compute_equity_max_drawdown,
    compute_mae_mfe,
    compute_sample_returns,
    evaluate_noninferiority_gates,
    fit_tertile_boundaries,
    is_mature_sample,
    is_false_breakout,
    resolved_position_weight,
    summarize_validation_samples,
    wilson_lower_bound,
)
from src.backtest.services.bottom_divergence_v2_performance import (
    CheckpointMismatchError,
)


def _sample(
    *,
    code: str = "000001",
    signal_date: date = date(2024, 1, 2),
    candidate_version: str = "candidate-a",
    strategy_version: str = "v2",
    stage: str = "r2",
    entry_close: float = 100.0,
    close_5d: float | None = 105.0,
    close_10d: float | None = 110.0,
    close_20d: float | None = 120.0,
    future_closes_20d: tuple[float, ...] = (101.0,) * 20,
    future_highs_20d: tuple[float, ...] = (112.0,) * 20,
    future_lows_20d: tuple[float, ...] = (95.0,) * 20,
    max_high_20d: float | None = 112.0,
    min_low_20d: float | None = 95.0,
    near_zone_lower: float | None = 99.0,
    major_zone_lower: float | None = 101.0,
    early_event_date: date | None = date(2024, 1, 2),
    near_cleared_event_date: date | None = date(2024, 1, 3),
    major_breakout_event_date: date | None = date(2024, 1, 4),
    market_regime: str = "balanced",
    volatility: float | None = 0.02,
    liquidity: float | None = 1_000_000.0,
    breakout_floor: float | None = 101.0,
    position_weight: float | None = None,
    is_executable: bool = True,
) -> ValidationSample:
    return ValidationSample(
        code=code,
        signal_date=signal_date,
        candidate_version=candidate_version,
        strategy_version=strategy_version,
        stage=stage,
        entry_close=entry_close,
        near_zone_lower=near_zone_lower,
        major_zone_lower=major_zone_lower,
        early_event_date=early_event_date,
        near_cleared_event_date=near_cleared_event_date,
        major_breakout_event_date=major_breakout_event_date,
        close_5d=close_5d,
        close_10d=close_10d,
        close_20d=close_20d,
        future_closes_20d=future_closes_20d,
        future_highs_20d=future_highs_20d,
        future_lows_20d=future_lows_20d,
        max_high_20d=max_high_20d,
        min_low_20d=min_low_20d,
        market_regime=market_regime,
        volatility=volatility,
        liquidity=liquidity,
        breakout_floor=breakout_floor,
        position_weight=position_weight,
        is_executable=is_executable,
    )


@pytest.mark.unit
def test_validation_sample_is_frozen() -> None:
    sample = _sample()
    with pytest.raises(FrozenInstanceError):
        sample.code = "changed"  # type: ignore[misc]


@pytest.mark.unit
def test_returns_apply_shared_round_trip_cost_formula() -> None:
    metrics = compute_sample_returns(
        _sample(),
        buy_cost_bps=5.0,
        sell_cost_bps=7.0,
        slippage_bps=4.0,
    )

    assert metrics == {
        "gross_return_5d": pytest.approx(5.0),
        "net_return_5d": pytest.approx(4.8),
        "gross_return_10d": pytest.approx(10.0),
        "net_return_10d": pytest.approx(9.8),
        "gross_return_20d": pytest.approx(20.0),
        "net_return_20d": pytest.approx(19.8),
        "round_trip_cost_bps": pytest.approx(20.0),
    }


@pytest.mark.unit
def test_returns_keep_missing_horizon_missing() -> None:
    metrics = compute_sample_returns(
        _sample(close_20d=None),
        buy_cost_bps=5.0,
        sell_cost_bps=5.0,
        slippage_bps=5.0,
    )
    assert metrics["gross_return_20d"] is None
    assert metrics["net_return_20d"] is None


@pytest.mark.unit
def test_mae_and_mfe_use_20_day_extremes() -> None:
    assert compute_mae_mfe(_sample()) == {
        "mae_5d": pytest.approx(-5.0),
        "mae": pytest.approx(-5.0),
        "mfe": pytest.approx(12.0),
    }


@pytest.mark.unit
def test_false_breakout_uses_stage_zone_and_first_three_closes() -> None:
    sample = _sample(
        stage="r2",
        future_closes_20d=(102.0, 100.0, 104.0, 90.0),
        future_highs_20d=(105.0,) * 4,
        future_lows_20d=(89.0,) * 4,
        major_zone_lower=101.0,
    )
    assert is_false_breakout(sample) is True


@pytest.mark.unit
def test_false_breakout_requires_profit_to_stay_below_three_percent() -> None:
    sample = _sample(
        stage="r1",
        future_closes_20d=(104.0, 98.0, 99.0),
        future_highs_20d=(105.0,) * 3,
        future_lows_20d=(97.0,) * 3,
        near_zone_lower=99.0,
    )
    assert is_false_breakout(sample) is False


@pytest.mark.unit
def test_drawdown_compounds_10d_net_returns_in_stable_order() -> None:
    samples = [
        _sample(code="B", signal_date=date(2024, 1, 2), close_10d=90.0),
        _sample(code="A", signal_date=date(2024, 1, 2), close_10d=120.0),
        _sample(code="A", signal_date=date(2024, 1, 3), close_10d=95.0),
    ]
    drawdown = compute_equity_max_drawdown(
        samples,
        buy_cost_bps=0.0,
        sell_cost_bps=0.0,
        slippage_bps=0.0,
    )
    assert drawdown == pytest.approx(5.0)


@pytest.mark.unit
def test_conversion_groups_by_candidate_version_without_cross_joining() -> None:
    samples = [
        _sample(candidate_version="a"),
        _sample(
            candidate_version="b",
            near_cleared_event_date=None,
            major_breakout_event_date=None,
        ),
        _sample(
            candidate_version="c",
            major_breakout_event_date=None,
        ),
    ]
    conversion = compute_conversion_metrics(samples)

    assert conversion["early_count"] == 3
    assert conversion["r1_count"] == 2
    assert conversion["r2_count"] == 1
    assert conversion["early_to_r1"] == pytest.approx(2 / 3)
    assert conversion["r1_to_r2"] == pytest.approx(1 / 2)
    assert conversion["early_to_r2"] == pytest.approx(1 / 3)


@pytest.mark.unit
def test_conversion_cohort_belongs_to_early_event_split() -> None:
    train_early = _sample(
        signal_date=date(2024, 1, 2),
        candidate_version="same-structure",
        early_event_date=date(2024, 1, 2),
        near_cleared_event_date=None,
        major_breakout_event_date=None,
    )
    test_r1 = _sample(
        signal_date=date(2024, 3, 1),
        candidate_version="same-structure",
        early_event_date=date(2024, 1, 2),
        near_cleared_event_date=date(2024, 3, 1),
        major_breakout_event_date=None,
    )

    train = compute_conversion_metrics(
        [train_early, test_r1],
        cohort_dates={date(2024, 1, 2)},
    )
    test = compute_conversion_metrics(
        [train_early, test_r1],
        cohort_dates={date(2024, 3, 1)},
    )

    assert train["early_count"] == 1
    assert train["r1_count"] == 1
    assert train["early_to_r1"] == 1.0
    assert test["early_count"] == 0
    assert test["r1_count"] == 0


@pytest.mark.unit
def test_conversion_uses_20_trade_day_window_and_reports_right_censoring() -> None:
    dates = tuple(date(2024, 1, 1) + timedelta(days=index) for index in range(30))
    mature_success = _sample(
        code="000001",
        candidate_version="success",
        signal_date=dates[2],
        early_event_date=dates[2],
        near_cleared_event_date=dates[7],
        major_breakout_event_date=None,
    )
    window_late = _sample(
        code="000002",
        candidate_version="late",
        signal_date=dates[3],
        early_event_date=dates[3],
        near_cleared_event_date=dates[24],
        major_breakout_event_date=None,
    )
    censored = _sample(
        code="000003",
        candidate_version="censored",
        signal_date=dates[15],
        early_event_date=dates[15],
        near_cleared_event_date=None,
        major_breakout_event_date=None,
    )

    metrics = compute_conversion_metrics(
        [mature_success, window_late, censored],
        cohort_dates=set(dates),
        observation_dates=dates,
    )

    assert metrics["early_count"] == 2
    assert metrics["r1_count"] == 1
    assert metrics["right_censored_count"] == 1
    assert metrics["early_to_r1"] == 0.5


@pytest.mark.unit
def test_test_maturation_evidence_only_updates_existing_test_early_cohort() -> None:
    test_dates = tuple(
        date(2024, 3, 1) + timedelta(days=index) for index in range(20)
    )
    future_dates = tuple(
        test_dates[-1] + timedelta(days=index)
        for index in range(1, 21)
    )
    early = _sample(
        code="000001",
        candidate_version="test-early",
        signal_date=test_dates[-1],
        early_event_date=test_dates[-1],
        near_cleared_event_date=None,
        major_breakout_event_date=None,
    )
    evidence = [
        CandidateEventEvidence(
            code="000001",
            candidate_version="test-early",
            near_cleared_event_date=future_dates[4],
            major_breakout_event_date=None,
        ),
        CandidateEventEvidence(
            code="000002",
            candidate_version="post-test-new",
            near_cleared_event_date=future_dates[1],
            major_breakout_event_date=None,
        ),
    ]

    metrics = compute_conversion_metrics(
        [early],
        cohort_dates=set(test_dates),
        observation_dates=tuple([*test_dates, *future_dates]),
        maturation_evidence=evidence,
    )

    assert metrics["early_count"] == 1
    assert metrics["r1_count"] == 1
    assert metrics["right_censored_count"] == 0


@pytest.mark.unit
def test_wilson_95_percent_lower_bound_uses_standard_formula() -> None:
    assert wilson_lower_bound(60, 100) == pytest.approx(0.5020025868)
    assert wilson_lower_bound(0, 0) == 0.0


@pytest.mark.unit
def test_split_uses_sorted_unique_signal_dates_without_overlap() -> None:
    start = date(2024, 1, 1)
    samples = [
        _sample(code=f"{index:06d}", signal_date=start + timedelta(days=index))
        for index in reversed(range(10))
    ]
    samples.append(_sample(code="duplicate", signal_date=start))

    split = chronological_split(samples)

    assert split.train_dates == tuple(start + timedelta(days=i) for i in range(6))
    assert split.validation_dates == tuple(
        start + timedelta(days=i) for i in range(6, 8)
    )
    assert split.test_dates == tuple(start + timedelta(days=i) for i in range(8, 10))
    assert len(split.train) == 7
    assert {item.signal_date for item in split.train}.isdisjoint(
        item.signal_date for item in split.validation
    )
    assert {item.signal_date for item in split.validation}.isdisjoint(
        item.signal_date for item in split.test
    )


@pytest.mark.unit
def test_split_is_ineligible_when_any_period_has_no_signal_date() -> None:
    samples = [
        _sample(signal_date=date(2024, 1, 1)),
        _sample(signal_date=date(2024, 1, 2)),
    ]
    with pytest.raises(ValueError, match="at least one unique signal date"):
        chronological_split(samples)


@pytest.mark.unit
def test_split_requires_ratios_to_cover_the_whole_sample() -> None:
    samples = [
        _sample(signal_date=date(2024, 1, 1) + timedelta(days=i))
        for i in range(10)
    ]
    with pytest.raises(ValueError, match="fixed at 0.6/0.2/0.2"):
        chronological_split(
            samples,
            train_ratio=0.5,
            validation_ratio=0.2,
            test_ratio=0.2,
        )


@pytest.mark.unit
def test_tertiles_are_fit_on_train_and_frozen_for_later_periods() -> None:
    train = [
        _sample(volatility=float(i), liquidity=float(i * 10))
        for i in range(1, 7)
    ]
    boundaries = fit_tertile_boundaries(train)

    assert boundaries["volatility"] == pytest.approx((2.6666666667, 4.3333333333))
    assert boundaries["liquidity"] == pytest.approx((26.666666667, 43.333333333))
    assert assign_tertile(1000.0, boundaries["volatility"]) == "high"
    assert fit_tertile_boundaries(
        train + [_sample(volatility=1000.0, liquidity=10000.0)]
    ) != boundaries
    assert boundaries["volatility"] == pytest.approx((2.6666666667, 4.3333333333))


@pytest.mark.unit
def test_tertile_assignment_has_deterministic_boundary_ownership() -> None:
    assert assign_tertile(1.0, (1.0, 2.0)) == "low"
    assert assign_tertile(2.0, (1.0, 2.0)) == "middle"
    assert assign_tertile(2.1, (1.0, 2.0)) == "high"


def _dataset(
    *,
    days: int = 500,
    strategy_version: str,
    return_10d: float,
    mae: float = -5.0,
) -> list[ValidationSample]:
    start = date(2022, 1, 1)
    rows: list[ValidationSample] = []
    for index in range(days):
        signal_date = start + timedelta(days=index)
        for stage in ("early", "r1", "r2"):
            rows.append(
                _sample(
                    signal_date=signal_date,
                    candidate_version=f"structure-{index}",
                    strategy_version=strategy_version,
                    stage=stage,
                    early_event_date=signal_date,
                    near_cleared_event_date=signal_date,
                    major_breakout_event_date=signal_date,
                    close_5d=100.0 * (1.0 + return_10d / 200.0),
                    close_10d=100.0 * (1.0 + return_10d / 100.0),
                    close_20d=100.0 * (1.0 + return_10d / 50.0),
                    min_low_20d=100.0 * (1.0 + mae / 100.0),
                    max_high_20d=105.0,
                    market_regime="balanced",
                    volatility=float(index % 9),
                    liquidity=float((index % 9) * 1_000_000 + 1),
                )
            )
    return rows


def _calendar(samples: list[ValidationSample]) -> tuple[date, ...]:
    return tuple(sorted({item.signal_date for item in samples}))


def _opportunities(
    trading_dates: tuple[date, ...],
    count: int = 3,
) -> dict[date, int]:
    return {item: count for item in trading_dates}


@pytest.mark.unit
def test_parameter_grid_has_exactly_18_canonical_snapshots() -> None:
    snapshots = build_parameter_snapshots()

    assert len(snapshots) == 18
    assert len(set(snapshots)) == 18
    assert {
        item["cluster_pct"] for item in snapshots.values()
    } == {0.01, 0.015, 0.02}
    assert {
        item["atr_gap_multiplier"] for item in snapshots.values()
    } == {0.5, 0.75}
    assert {
        item["zone_score_min"] for item in snapshots.values()
    } == {0.4, 0.45, 0.5}
    assert all(
        key == canonical_parameter_hash(snapshot)
        for key, snapshot in snapshots.items()
    )


@pytest.mark.unit
def test_parameter_selection_uses_validation_expectancy_then_hash() -> None:
    snapshots = build_parameter_snapshots()
    ordered_hashes = sorted(snapshots)
    v1 = _dataset(strategy_version="v1", return_10d=1.0)
    ordinary = _dataset(strategy_version="v2", return_10d=1.1)
    best_hash = ordered_hashes[-1]
    candidates = {key: ordinary for key in snapshots}
    candidates[best_hash] = _dataset(strategy_version="v2", return_10d=2.0)
    trading_dates = _calendar(v1)

    report = BottomDivergenceV2Validator.evaluate(
        v1_samples=v1,
        v2_samples_by_parameter_hash=candidates,
        parameter_snapshots=snapshots,
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
        trading_dates=trading_dates,
        opportunity_counts=_opportunities(trading_dates),
    )

    assert report["selected_parameter_hash"] == best_hash
    assert report["eligible"] is True
    assert report["passed"] is True
    assert report["metrics"]["test"]["v2"]["r2"]["sample_count"] == 100


@pytest.mark.unit
def test_parameter_selection_breaks_expectancy_ties_by_hash() -> None:
    snapshots = build_parameter_snapshots()
    v1 = _dataset(strategy_version="v1", return_10d=1.0)
    tied = _dataset(strategy_version="v2", return_10d=1.1)
    trading_dates = _calendar(v1)

    report = BottomDivergenceV2Validator.evaluate(
        v1_samples=v1,
        v2_samples_by_parameter_hash={key: tied for key in snapshots},
        parameter_snapshots=snapshots,
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
        trading_dates=trading_dates,
        opportunity_counts=_opportunities(trading_dates),
    )

    assert report["selected_parameter_hash"] == min(snapshots)


@pytest.mark.unit
def test_train_validation_purge_last_20_dates_and_aligns_coverage() -> None:
    snapshots = build_parameter_snapshots()
    v1 = _dataset(strategy_version="v1", return_10d=1.0)
    v2 = _dataset(strategy_version="v2", return_10d=1.1)
    trading_dates = _calendar(v1)
    report = BottomDivergenceV2Validator.evaluate(
        v1_samples=v1,
        v2_samples_by_parameter_hash={key: v2 for key in snapshots},
        parameter_snapshots=snapshots,
        trading_dates=trading_dates,
        opportunity_counts=_opportunities(trading_dates),
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
    )

    assert report["purge"]["train"] == {
        "raw_date_count": 300,
        "purged_count": 20,
        "effective_date_range": {
            "from": trading_dates[0].isoformat(),
            "to": trading_dates[279].isoformat(),
        },
    }
    assert report["purge"]["validation"]["purged_count"] == 20
    assert report["purge"]["validation"]["effective_date_range"]["to"] == (
        trading_dates[379].isoformat()
    )
    assert report["metrics"]["train"]["v2"]["r2"]["sample_count"] == 280
    assert report["metrics"]["validation"]["v2"]["r2"]["sample_count"] == 80
    assert report["raw_opportunity_counts"]["train"] == 900
    assert report["purged_opportunity_counts"]["train"] == 840


@pytest.mark.unit
def test_forward_path_crossing_split_end_is_purged_even_before_embargo() -> None:
    snapshots = build_parameter_snapshots()
    v1 = _dataset(strategy_version="v1", return_10d=1.0)
    v2 = _dataset(strategy_version="v2", return_10d=1.1)
    trading_dates = _calendar(v1)
    crossing_date = trading_dates[100]
    future_dates = tuple([
        *trading_dates[101:120],
        trading_dates[300],
    ])
    crossing = [
        dc_replace(sample, future_trade_dates_20d=future_dates)
        if sample.signal_date == crossing_date and sample.stage == "r2"
        else sample
        for sample in v2
    ]
    report = BottomDivergenceV2Validator.evaluate(
        v1_samples=v1,
        v2_samples_by_parameter_hash={key: crossing for key in snapshots},
        parameter_snapshots=snapshots,
        trading_dates=trading_dates,
        opportunity_counts=_opportunities(trading_dates),
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
    )

    assert report["metrics"]["train"]["v2"]["r2"]["sample_count"] == 279


@pytest.mark.unit
def test_purged_validation_future_prices_cannot_change_selected_hash() -> None:
    snapshots = build_parameter_snapshots()
    ordered_hashes = sorted(snapshots)
    v1 = _dataset(strategy_version="v1", return_10d=1.0)
    ordinary = _dataset(strategy_version="v2", return_10d=1.1)
    trading_dates = _calendar(v1)
    common = {
        "v1_samples": v1,
        "parameter_snapshots": snapshots,
        "trading_dates": trading_dates,
        "opportunity_counts": _opportunities(trading_dates),
        "buy_cost_bps": 1.0,
        "sell_cost_bps": 1.0,
        "slippage_bps": 1.0,
    }
    baseline = BottomDivergenceV2Validator.evaluate(
        v2_samples_by_parameter_hash={key: ordinary for key in snapshots},
        **common,
    )
    validation_purged_dates = set(trading_dates[380:400])
    poisoned = [
        dc_replace(
            sample,
            close_5d=500.0,
            close_10d=500.0,
            close_20d=500.0,
        )
        if sample.signal_date in validation_purged_dates
        else sample
        for sample in ordinary
    ]
    changed = BottomDivergenceV2Validator.evaluate(
        v2_samples_by_parameter_hash={
            **{key: ordinary for key in snapshots},
            ordered_hashes[-1]: poisoned,
        },
        **common,
    )

    assert baseline["selected_parameter_hash"] == ordered_hashes[0]
    assert changed["selected_parameter_hash"] == ordered_hashes[0]


@pytest.mark.unit
def test_selection_event_evidence_is_explicit_and_updates_conversion() -> None:
    snapshots = build_parameter_snapshots()
    v1 = _dataset(strategy_version="v1", return_10d=1.0)
    v2 = [
        dc_replace(
            sample,
            near_cleared_event_date=None,
            major_breakout_event_date=None,
        )
        for sample in _dataset(strategy_version="v2", return_10d=1.1)
    ]
    evidence = tuple(
        CandidateEventEvidence(
            code="000001",
            candidate_version=f"structure-{index}",
            near_cleared_event_date=date(2022, 1, 1) + timedelta(days=index),
            major_breakout_event_date=date(2022, 1, 1) + timedelta(days=index),
        )
        for index in range(500)
    )
    trading_dates = _calendar(v1)
    report = BottomDivergenceV2Validator.evaluate(
        v1_samples=v1,
        v2_samples_by_parameter_hash={key: v2 for key in snapshots},
        selection_event_evidence_by_parameter_hash={
            key: evidence for key in snapshots
        },
        parameter_snapshots=snapshots,
        trading_dates=trading_dates,
        opportunity_counts=_opportunities(trading_dates),
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
    )

    assert (
        report["selection_contract"]["metrics"]["train"]["v2"]["early"][
            "conversion"
        ]["early_to_r1"]
        == 1.0
    )


@pytest.mark.unit
def test_parameter_only_future_signal_does_not_change_calendar_split() -> None:
    snapshots = build_parameter_snapshots()
    full_v1 = _dataset(strategy_version="v1", return_10d=1.0)
    full_v2 = _dataset(strategy_version="v2", return_10d=1.1)
    trading_dates = _calendar(full_v1)
    tuning_end = trading_dates[400]
    v1 = [item for item in full_v1 if item.signal_date < tuning_end]
    tuning_v2 = [item for item in full_v2 if item.signal_date < tuning_end]
    candidates = {key: tuning_v2 for key in snapshots}
    common = {
        "v1_samples": v1,
        "parameter_snapshots": snapshots,
        "trading_dates": trading_dates,
        "opportunity_counts": _opportunities(trading_dates),
        "buy_cost_bps": 1.0,
        "sell_cost_bps": 1.0,
        "slippage_bps": 1.0,
    }

    without_future = BottomDivergenceV2Validator.evaluate(
        v2_samples_by_parameter_hash=candidates,
        **common,
    )
    one_hash = max(snapshots)
    with_future_signal = BottomDivergenceV2Validator.evaluate(
        v2_samples_by_parameter_hash={
            **candidates,
            one_hash: [*tuning_v2, full_v2[-1]],
        },
        **common,
    )

    assert with_future_signal["split"] == without_future["split"]
    assert with_future_signal["split"]["test_dates"][0] == (
        trading_dates[400].isoformat()
    )


@pytest.mark.unit
def test_selection_contract_is_frozen_against_late_test_r1_event() -> None:
    snapshots = build_parameter_snapshots()
    ordered_hashes = sorted(snapshots)
    selected_hash = ordered_hashes[-1]
    v1 = _dataset(strategy_version="v1", return_10d=1.0)
    ordinary = _dataset(strategy_version="v2", return_10d=1.1)
    candidates = {key: ordinary for key in snapshots}
    candidates[selected_hash] = _dataset(
        strategy_version="v2",
        return_10d=2.0,
    )
    trading_dates = _calendar(v1)
    opportunity_counts = _opportunities(trading_dates)
    selection = BottomDivergenceV2Validator.evaluate(
        v1_samples=v1,
        v2_samples_by_parameter_hash=candidates,
        parameter_snapshots=snapshots,
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
        trading_dates=trading_dates,
        opportunity_counts=opportunity_counts,
    )
    contract = deepcopy(selection["selection_contract"])
    train_candidate = candidates[selected_hash][0]
    late_test_row = dc_replace(
        candidates[selected_hash][-1],
        candidate_version=train_candidate.candidate_version,
        early_event_date=train_candidate.early_event_date,
        near_cleared_event_date=candidates[selected_hash][-1].signal_date,
    )
    with_late_event = {
        **candidates,
        selected_hash: [*candidates[selected_hash], late_test_row],
    }
    reselected = BottomDivergenceV2Validator.evaluate(
        v1_samples=v1,
        v2_samples_by_parameter_hash=with_late_event,
        parameter_snapshots=snapshots,
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
        trading_dates=trading_dates,
        opportunity_counts=opportunity_counts,
    )

    final = BottomDivergenceV2Validator.evaluate(
        v1_samples=v1,
        v2_samples_by_parameter_hash=with_late_event,
        parameter_snapshots=snapshots,
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
        trading_dates=trading_dates,
        opportunity_counts=opportunity_counts,
        selection_contract=contract,
    )

    assert final["selected_parameter_hash"] == selected_hash
    assert reselected["selected_parameter_hash"] == selected_hash
    assert (
        reselected["selection_contract"]["train_conversion_lower"]
        == contract["train_conversion_lower"]
    )
    assert final["selection_contract"] == contract
    assert (
        final["selection_contract"]["train_conversion_lower"]
        == contract["train_conversion_lower"]
    )


@pytest.mark.unit
def test_strategy_version_samples_cannot_mix() -> None:
    snapshots = build_parameter_snapshots()
    wrong = [_sample(strategy_version="v1")]
    trading_dates = tuple(date(2024, 1, day) for day in (1, 2, 3))
    with pytest.raises(ValueError, match="contains non-v2"):
        BottomDivergenceV2Validator.evaluate(
            v1_samples=[
                _sample(
                    strategy_version="v1",
                    signal_date=date(2024, 1, day),
                )
                for day in (1, 2, 3)
            ],
            v2_samples_by_parameter_hash={key: wrong for key in snapshots},
            parameter_snapshots=snapshots,
            buy_cost_bps=1.0,
            sell_cost_bps=1.0,
            slippage_bps=1.0,
            trading_dates=trading_dates,
            opportunity_counts=_opportunities(trading_dates, count=1),
        )


@pytest.mark.unit
def test_stage_report_uses_cross_stage_candidate_conversion_and_excludes_small_groups() -> None:
    signal_date = date(2024, 1, 2)
    samples = [
        _sample(
            candidate_version="same-structure",
            stage="early",
            early_event_date=signal_date,
            near_cleared_event_date=None,
            major_breakout_event_date=None,
        ),
        _sample(
            candidate_version="same-structure",
            stage="r1",
            early_event_date=signal_date,
            near_cleared_event_date=signal_date + timedelta(days=1),
            major_breakout_event_date=None,
        ),
        _sample(
            candidate_version="same-structure",
            stage="r2",
            early_event_date=signal_date,
            near_cleared_event_date=signal_date + timedelta(days=1),
            major_breakout_event_date=signal_date + timedelta(days=2),
        ),
    ]
    metrics = summarize_validation_samples(
        samples,
        boundaries={
            "volatility": (0.01, 0.03),
            "liquidity": (500_000.0, 1_500_000.0),
        },
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
        opportunity_count=3,
    )

    for stage in ("early", "r1", "r2"):
        assert metrics[stage]["conversion"]["early_to_r2"] == 1.0
        assert metrics[stage]["groups"]["eligible_count"] == 0
        assert metrics[stage]["groups"]["excluded_count"] == 3
        assert {
            "coverage",
            "win_rate",
            "payoff_ratio",
            "mean_net_return_5d",
            "median_net_return_10d",
            "mean_mae",
            "mean_mfe",
            "max_drawdown",
            "false_breakout_rate",
        } <= set(metrics[stage])


class _MetricReadBomb:
    """Allows split metadata reads but explodes if test metrics are evaluated."""

    def __init__(self, sample: ValidationSample) -> None:
        self._sample = sample

    @property
    def close_10d(self) -> float:
        raise AssertionError("unselected test metrics were read")

    def __getattr__(self, name: str):
        return getattr(self._sample, name)


@pytest.mark.unit
def test_unselected_parameter_test_metrics_are_never_read() -> None:
    snapshots = build_parameter_snapshots()
    ordered_hashes = sorted(snapshots)
    v1 = _dataset(strategy_version="v1", return_10d=1.0)
    ordinary = _dataset(strategy_version="v2", return_10d=1.1)
    best_hash = ordered_hashes[-1]
    poisoned_hash = ordered_hashes[0]
    candidates = {key: ordinary for key in snapshots}
    candidates[best_hash] = _dataset(strategy_version="v2", return_10d=2.0)
    candidates[poisoned_hash] = [
        _MetricReadBomb(item) if item.signal_date >= date(2023, 2, 5) else item
        for item in ordinary
    ]
    trading_dates = _calendar(v1)

    report = BottomDivergenceV2Validator.evaluate(
        v1_samples=v1,
        v2_samples_by_parameter_hash=candidates,
        parameter_snapshots=snapshots,
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
        trading_dates=trading_dates,
        opportunity_counts=_opportunities(trading_dates),
    )

    assert report["selected_parameter_hash"] == best_hash


def _gate_metrics() -> dict:
    stage = {
        "sample_count": 100,
        "mean_net_return_10d": 2.0,
        "median_mae": -5.0,
        "median_mae_5d": -5.0,
        "max_drawdown": 5.0,
        "false_breakout_rate": 5.0,
        "conversion": {"early_to_r1": 0.7, "early_to_r2": 0.7},
        "groups": {"eligible_count": 10, "positive_ratio": 0.8},
    }
    return {
        "early": deepcopy(stage),
        "r1": deepcopy(stage),
        "r2": deepcopy(stage),
        "overall": {
            "max_drawdown": 5.0,
            "false_breakout_rate": 5.0,
        },
    }


@pytest.mark.unit
def test_noninferiority_positive_r2_baseline_uses_98_percent_floor() -> None:
    v1 = _gate_metrics()
    v2 = _gate_metrics()
    v1["r2"]["mean_net_return_10d"] = 2.0
    v2["r2"]["mean_net_return_10d"] = 1.95

    result = evaluate_noninferiority_gates(v1, v2, train_conversion_lower=0.6)

    assert result["passed"] is False
    assert "r2_expectancy_noninferiority" in result["reasons"]


@pytest.mark.unit
def test_noninferiority_nonpositive_r2_baseline_must_not_worsen() -> None:
    v1 = _gate_metrics()
    v2 = _gate_metrics()
    v1["r2"]["mean_net_return_10d"] = -1.0
    v2["r2"]["mean_net_return_10d"] = -1.1

    result = evaluate_noninferiority_gates(v1, v2, train_conversion_lower=0.6)

    assert "r2_expectancy_noninferiority" in result["reasons"]


@pytest.mark.unit
def test_noninferiority_uses_v1_r2_as_baseline_for_new_v2_stages() -> None:
    v1 = _gate_metrics()
    v2 = _gate_metrics()
    for stage in ("early", "r1"):
        v1[stage]["sample_count"] = 0
        v1[stage]["median_mae"] = None

    result = evaluate_noninferiority_gates(v1, v2, train_conversion_lower=0.6)

    assert result == {"eligible": True, "passed": True, "reasons": []}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (
            lambda metrics: metrics["overall"].update(max_drawdown=7.01),
            "max_drawdown_degradation",
        ),
        (
            lambda metrics: metrics["overall"].update(false_breakout_rate=8.01),
            "false_breakout_degradation",
        ),
        (
            lambda metrics: metrics["r1"].update(mean_net_return_10d=0.0),
            "r1_expectancy_non_positive",
        ),
        (
            lambda metrics: metrics["r1"].update(median_mae_5d=-6.01),
            "r1_median_mae_degradation",
        ),
        (
            lambda metrics: metrics["early"].update(mean_net_return_10d=0.0),
            "early_expectancy_non_positive",
        ),
        (
            lambda metrics: metrics["early"]["conversion"].update(
                early_to_r1=0.59
            ),
            "early_conversion_below_train_wilson",
        ),
        (
            lambda metrics: metrics["r2"]["groups"].update(positive_ratio=0.69),
            "positive_group_ratio_below_70:r2",
        ),
        (
            lambda metrics: metrics["r2"].update(sample_count=99),
            "insufficient_test_samples:r2:v2",
        ),
    ],
)
def test_noninferiority_rejects_each_required_risk_branch(
    mutator,
    reason: str,
) -> None:
    v1 = _gate_metrics()
    v2 = _gate_metrics()
    mutator(v2)

    result = evaluate_noninferiority_gates(v1, v2, train_conversion_lower=0.6)

    assert reason in result["reasons"]
    assert result["eligible"] is False


@pytest.mark.unit
def test_zero_cost_model_is_ineligible_without_parameter_evaluation() -> None:
    report = BottomDivergenceV2Validator.evaluate(
        v1_samples=[],
        v2_samples_by_parameter_hash={},
        parameter_snapshots={},
        buy_cost_bps=0.0,
        sell_cost_bps=0.0,
        slippage_bps=0.0,
    )

    assert report["eligible"] is False
    assert report["passed"] is False
    assert report["reasons"] == ["zero_cost_model"]


class _FakeReplayBoundary:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.maturation_calls: list[dict] = []
        self._dates = [
            date(2022, 1, 1) + timedelta(days=index) for index in range(500)
        ]

    def resolve_universe(self, market: str, codes: list[str] | None):
        assert market == "cn"
        return {"codes": codes or ["000001"]}

    def list_trade_dates(
        self,
        *,
        date_from: date,
        date_to: date,
        market: str,
        universe,
    ) -> list[date]:
        del date_from, date_to, market, universe
        return list(self._dates)

    def replay(
        self,
        *,
        strategy_version: str,
        config: Config,
        trade_dates: list[date],
        universe,
    ) -> ReplayBatch:
        del universe
        self.calls.append(
            {
                "strategy_version": strategy_version,
                "config": config,
                "trade_dates": tuple(trade_dates),
            }
        )
        return_10d = (
            1.0
            if strategy_version == "v1"
            else 2.0
            if config.bottom_divergence_v2_zone_score_min == 0.5
            else 1.1
        )
        allowed = set(trade_dates)
        samples = [
            item
            for item in _dataset(
                strategy_version=strategy_version,
                return_10d=return_10d,
            )
            if item.signal_date in allowed
        ]
        return ReplayBatch(
            samples=tuple(samples),
            opportunity_counts={item: 3 for item in trade_dates},
        )

    def list_future_trade_dates(
        self,
        *,
        after_date: date,
        count: int,
        universe,
    ) -> list[date]:
        del universe
        return [
            after_date + timedelta(days=index)
            for index in range(1, count + 1)
        ]

    def mature_events(
        self,
        *,
        config: Config,
        maturation_dates: list[date],
        universe,
        target_candidates: set[tuple[str, str]],
    ) -> tuple[CandidateEventEvidence, ...]:
        del universe
        self.maturation_calls.append(
            {
                "config": config,
                "maturation_dates": tuple(maturation_dates),
                "target_candidates": set(target_candidates),
            }
        )
        return ()

    def data_version(self) -> str:
        return "fake-stock-daily-v1"

    def universe_identity(self, universe) -> dict:
        return {"codes": list(universe["codes"]), "count": 1}


@pytest.mark.unit
def test_isolated_config_sets_grid_without_mutating_base_or_singleton() -> None:
    base = Config(
        bottom_divergence_v2_enabled=False,
        bottom_divergence_v2_cluster_pct=0.015,
        bottom_divergence_v2_atr_gap_multiplier=0.5,
        bottom_divergence_v2_zone_score_min=0.45,
    )
    old_singleton = Config._instance
    Config._instance = base
    try:
        isolated = build_isolated_config(
            base,
            {
                "cluster_pct": 0.02,
                "atr_gap_multiplier": 0.75,
                "zone_score_min": 0.5,
            },
        )
        assert isolated is not base
        assert isolated.bottom_divergence_v2_enabled is True
        assert isolated.bottom_divergence_v2_cluster_pct == 0.02
        assert isolated.bottom_divergence_v2_atr_gap_multiplier == 0.75
        assert isolated.bottom_divergence_v2_zone_score_min == 0.5
        assert base.bottom_divergence_v2_enabled is False
        assert Config._instance is base
    finally:
        Config._instance = old_singleton


@pytest.mark.unit
def test_cli_replays_grid_on_tuning_dates_and_locked_hash_once_on_test(
    tmp_path,
) -> None:
    service = _FakeReplayBoundary()
    output = tmp_path / "validation.json"
    base = Config(
        bottom_divergence_v2_enabled=False,
        backtest_buy_cost_bps=1.0,
        backtest_sell_cost_bps=1.0,
        backtest_slippage_bps=1.0,
    )
    args = Namespace(
        date_from=date(2022, 1, 1),
        date_to=date(2023, 12, 31),
        market="cn",
        output=output,
        universe_codes=None,
    )

    exit_code, report = run_validation_cli(
        args,
        replay_service=service,
        base_config=base,
    )

    assert exit_code == 0
    assert report["eligible"] is True
    assert report["passed"] is True
    v2_calls = [
        item for item in service.calls if item["strategy_version"] == "v2"
    ]
    assert len(v2_calls) == 19
    assert all(len(item["trade_dates"]) == 400 for item in v2_calls[:18])
    assert len(v2_calls[-1]["trade_dates"]) == 100
    selected_snapshot = report["selected_parameter_snapshot"]
    selected_call_config = v2_calls[-1]["config"]
    assert selected_call_config.bottom_divergence_v2_cluster_pct == (
        selected_snapshot["cluster_pct"]
    )
    assert selected_call_config.bottom_divergence_v2_enabled is True
    assert base.bottom_divergence_v2_enabled is False

    raw = output.read_text(encoding="utf-8")
    assert raw == canonical_json_dumps(json.loads(raw)) + "\n"
    assert report["data_version"] == "fake-stock-daily-v1"
    assert report["universe_identity"] == {"codes": ["000001"], "count": 1}


@pytest.mark.unit
def test_cli_resume_matches_one_shot_and_skips_completed_grid(tmp_path) -> None:
    checkpoint = tmp_path / "validation.checkpoint.json"
    config = Config(
        backtest_buy_cost_bps=1.0,
        backtest_sell_cost_bps=1.0,
        backtest_slippage_bps=1.0,
    )

    def run(service, output, *, resume):
        return run_validation_cli(
            Namespace(
                date_from=date(2022, 1, 1),
                date_to=date(2023, 12, 31),
                market="cn",
                output=output,
                universe_codes=None,
                checkpoint=checkpoint,
                resume=resume,
            ),
            replay_service=service,
            base_config=config,
        )

    first_service = _FakeReplayBoundary()
    first_code, first_report = run(
        first_service,
        tmp_path / "first.json",
        resume=False,
    )
    resumed_service = _FakeReplayBoundary()
    resumed_code, resumed_report = run(
        resumed_service,
        tmp_path / "resumed.json",
        resume=True,
    )

    assert first_code == resumed_code == 0
    assert canonical_json_dumps(resumed_report) == canonical_json_dumps(
        first_report
    )
    assert len([
        item
        for item in resumed_service.calls
        if item["strategy_version"] == "v2"
    ]) == 1


@pytest.mark.unit
def test_cli_resume_rejects_parameter_yaml_and_data_mismatch(
    tmp_path,
    monkeypatch,
) -> None:
    from src.backtest.services import (
        bottom_divergence_v2_checkpoint as checkpoint_module,
    )

    checkpoint = tmp_path / "validation.checkpoint.json"
    copied_v2 = tmp_path / "strategy-v2.yaml"
    original_v2 = checkpoint_module.DEFAULT_V2_STRATEGY_PATH.read_bytes()
    copied_v2.write_bytes(original_v2)
    monkeypatch.setattr(
        checkpoint_module,
        "DEFAULT_V2_STRATEGY_PATH",
        copied_v2,
    )
    base = Config(
        backtest_buy_cost_bps=1.0,
        backtest_sell_cost_bps=1.0,
        backtest_slippage_bps=1.0,
    )

    def run(service, config, *, resume):
        return run_validation_cli(
            Namespace(
                date_from=date(2022, 1, 1),
                date_to=date(2023, 12, 31),
                market="cn",
                output=tmp_path / "report.json",
                universe_codes=None,
                checkpoint=checkpoint,
                resume=resume,
            ),
            replay_service=service,
            base_config=config,
        )

    run(_FakeReplayBoundary(), base, resume=False)
    with pytest.raises(CheckpointMismatchError):
        run(
            _FakeReplayBoundary(),
            dc_replace(base, bottom_divergence_v2_sync_window=4),
            resume=True,
        )

    copied_v2.write_text(
        copied_v2.read_text(encoding="utf-8") + "\n# identity-change\n",
        encoding="utf-8",
    )
    with pytest.raises(CheckpointMismatchError):
        run(_FakeReplayBoundary(), base, resume=True)
    copied_v2.write_bytes(original_v2)

    class OtherData(_FakeReplayBoundary):
        def data_version(self) -> str:
            return "fake-stock-daily-v2"

    with pytest.raises(CheckpointMismatchError):
        run(OtherData(), base, resume=True)


@pytest.mark.unit
def test_cli_keeps_trade_date_with_v2_signal_but_no_v1_signal(tmp_path) -> None:
    class NoV1OnOneDate(_FakeReplayBoundary):
        missing_v1_date = date(2022, 3, 1)

        def replay(self, **kwargs) -> ReplayBatch:
            batch = super().replay(**kwargs)
            if kwargs["strategy_version"] != "v1":
                return batch
            return ReplayBatch(
                samples=tuple(
                    item
                    for item in batch.samples
                    if item.signal_date != self.missing_v1_date
                ),
                opportunity_counts=batch.opportunity_counts,
            )

    service = NoV1OnOneDate()
    args = Namespace(
        date_from=date(2022, 1, 1),
        date_to=date(2023, 12, 31),
        market="cn",
        output=tmp_path / "no-v1-date.json",
        universe_codes=None,
    )
    exit_code, report = run_validation_cli(
        args,
        replay_service=service,
        base_config=Config(
            backtest_buy_cost_bps=1.0,
            backtest_sell_cost_bps=1.0,
            backtest_slippage_bps=1.0,
        ),
    )

    assert exit_code == 0
    tuning_v2_dates = next(
        call["trade_dates"]
        for call in service.calls
        if call["strategy_version"] == "v2"
    )
    assert service.missing_v1_date in tuning_v2_dates
    report_dates = {
        date.fromisoformat(value)
        for split_name in ("train_dates", "validation_dates", "test_dates")
        for value in report["split"][split_name]
    }
    assert service.missing_v1_date in report_dates


@pytest.mark.unit
def test_cli_only_matures_selected_hash_and_uses_20_day_event_window(
    tmp_path,
) -> None:
    class MaturationReplay(_FakeReplayBoundary):
        def replay(self, **kwargs) -> ReplayBatch:
            batch = super().replay(**kwargs)
            if (
                kwargs["strategy_version"] == "v2"
                and len(kwargs["trade_dates"]) == 100
            ):
                last_two_dates = set(kwargs["trade_dates"][-2:])
                return ReplayBatch(
                    samples=tuple(
                        dc_replace(
                            sample,
                            near_cleared_event_date=None,
                            major_breakout_event_date=None,
                        )
                        if sample.signal_date in last_two_dates
                        else sample
                        for sample in batch.samples
                    ),
                    opportunity_counts=batch.opportunity_counts,
                )
            return batch

        def mature_events(self, **kwargs):
            super().mature_events(**kwargs)
            dates = kwargs["maturation_dates"]
            targets = kwargs["target_candidates"]
            success_key = next(
                key for key in targets if key[1] == "structure-499"
            )
            late_key = next(
                key for key in targets if key[1] == "structure-498"
            )
            return (
                CandidateEventEvidence(
                    code=success_key[0],
                    candidate_version=success_key[1],
                    near_cleared_event_date=dates[4],
                    major_breakout_event_date=None,
                ),
                CandidateEventEvidence(
                    code=late_key[0],
                    candidate_version=late_key[1],
                    near_cleared_event_date=dates[-1]
                    + timedelta(days=1),
                    major_breakout_event_date=None,
                ),
                CandidateEventEvidence(
                    code="999999",
                    candidate_version="post-test-new",
                    near_cleared_event_date=dates[1],
                    major_breakout_event_date=None,
                ),
            )

    service = MaturationReplay()
    exit_code, report = run_validation_cli(
        Namespace(
            date_from=date(2022, 1, 1),
            date_to=date(2023, 12, 31),
            market="cn",
            output=tmp_path / "maturation.json",
            universe_codes=None,
        ),
        replay_service=service,
        base_config=Config(
            backtest_buy_cost_bps=1.0,
            backtest_sell_cost_bps=1.0,
            backtest_slippage_bps=1.0,
        ),
    )

    assert exit_code == 0
    assert len(service.maturation_calls) == 1
    selected = report["selected_parameter_snapshot"]
    maturation_config = service.maturation_calls[0]["config"]
    assert maturation_config.bottom_divergence_v2_cluster_pct == (
        selected["cluster_pct"]
    )
    assert len(service.maturation_calls[0]["maturation_dates"]) == 20
    assert report["metrics"]["test"]["v2"]["early"]["conversion"][
        "r1_count"
    ] == 99
    assert report["metrics"]["test"]["v2"]["early"]["conversion"][
        "early_count"
    ] == 100


@pytest.mark.unit
def test_evaluate_reports_ineligible_for_nonpositive_opportunities() -> None:
    snapshots = build_parameter_snapshots()
    samples = _dataset(strategy_version="v2", return_10d=1.0)
    trading_dates = _calendar(samples)
    opportunity_counts = _opportunities(trading_dates, count=1)
    for item in trading_dates[300:400]:
        opportunity_counts[item] = 0
    report = BottomDivergenceV2Validator.evaluate(
        v1_samples=_dataset(strategy_version="v1", return_10d=1.0),
        v2_samples_by_parameter_hash={key: samples for key in snapshots},
        parameter_snapshots=snapshots,
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
        trading_dates=trading_dates,
        opportunity_counts=opportunity_counts,
    )

    assert report["eligible"] is False
    assert report["reasons"] == ["invalid_opportunity_count:validation"]


@pytest.mark.unit
def test_cli_returns_one_for_ineligible_zero_cost_report(tmp_path) -> None:
    service = _FakeReplayBoundary()
    args = Namespace(
        date_from=date(2022, 1, 1),
        date_to=date(2023, 12, 31),
        market="cn",
        output=tmp_path / "validation.json",
        universe_codes=None,
    )
    exit_code, report = run_validation_cli(
        args,
        replay_service=service,
        base_config=Config(
            backtest_buy_cost_bps=0.0,
            backtest_sell_cost_bps=0.0,
            backtest_slippage_bps=0.0,
        ),
    )

    assert exit_code == 1
    assert report["reasons"] == ["zero_cost_model"]


@pytest.mark.unit
def test_cli_rejects_window_too_short_for_the_forward_label_purge(
    tmp_path,
) -> None:
    """窗口不够长时要在重放之前判死，而不是算完再报。

    2026-08-13 一次 400 只 × 82 交易日的实跑花了 104 分钟，最后停在
    `invalid_opportunity_count:validation`：验证段按 floor(82×0.2)=16 天切出来，
    而前瞻标签要清掉每段末尾 20 天，于是整段被清空。这个结论只依赖交易日历的
    长度，在任何因子计算之前就已经确定，没有理由让它跑满两个小时才浮出来。

    这里刻意取 100 天而不是事故现场的 82 天：100 天切出的验证段恰好是 20 天，
    正好被清光。82 天在 `<= 20` 和 `< 20` 两种写法下都会被拦，钉不住临界点；
    100 天只有 `<=` 拦得住，配合下面 105 天的反例才把边界夹死在 20/21 之间。
    """
    class ShortCalendar(_FakeReplayBoundary):
        def __init__(self) -> None:
            super().__init__()
            self._dates = self._dates[:100]

    service = ShortCalendar()
    with pytest.raises(ValidationInputError) as caught:
        run_validation_cli(
            Namespace(
                date_from=date(2022, 1, 1),
                date_to=date(2022, 4, 11),
                market="cn",
                output=tmp_path / "short.json",
                universe_codes=None,
            ),
            replay_service=service,
            base_config=Config(
                backtest_buy_cost_bps=1.0,
                backtest_sell_cost_bps=1.0,
                backtest_slippage_bps=1.0,
            ),
        )

    assert caught.value.error_code == "WINDOW_TOO_SHORT"
    assert "validation" in caught.value.message
    assert "105" in caught.value.message, "要告诉调用方至少需要多少个交易日"
    assert service.calls == [], "判死必须发生在任何重放之前"


@pytest.mark.unit
def test_cli_accepts_the_shortest_window_that_survives_the_purge(
    tmp_path,
) -> None:
    """反例：门槛不能顺手把刚好够用的窗口也拦掉。

    105 个交易日切出 floor(105×0.2)=21 天验证段，清掉 20 天后还剩 1 天，
    是能跑的最短窗口。这条钉住边界取在 `>` 而不是 `>=`。
    """
    class BoundaryCalendar(_FakeReplayBoundary):
        def __init__(self) -> None:
            super().__init__()
            self._dates = self._dates[:105]

    service = BoundaryCalendar()
    _, report = run_validation_cli(
        Namespace(
            date_from=date(2022, 1, 1),
            date_to=date(2022, 4, 16),
            market="cn",
            output=tmp_path / "boundary.json",
            universe_codes=None,
        ),
        replay_service=service,
        base_config=Config(
            backtest_buy_cost_bps=1.0,
            backtest_sell_cost_bps=1.0,
            backtest_slippage_bps=1.0,
        ),
    )

    assert report.get("error_code") != "WINDOW_TOO_SHORT"
    assert service.calls, "刚好够用的窗口应该真的跑起来"


@pytest.mark.unit
def test_cli_script_can_run_directly_from_repository_root() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/validate_bottom_divergence_v2.py",
            "--help",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--date-from" in result.stdout


def _full_window(
    *,
    close: float = 101.0,
    high: float = 105.0,
    low: float = 98.0,
) -> dict:
    return {
        "future_closes_20d": tuple(close for _ in range(20)),
        "future_highs_20d": tuple(high for _ in range(20)),
        "future_lows_20d": tuple(low for _ in range(20)),
        "close_5d": close,
        "close_10d": close,
        "close_20d": close,
        "max_high_20d": high,
        "min_low_20d": low,
    }


@pytest.mark.unit
def test_split_date_universe_keeps_dates_with_only_v2_signals() -> None:
    dates = tuple(date(2024, 1, day) for day in range(1, 11))
    samples = [_sample(signal_date=dates[0], strategy_version="v1")]

    split = chronological_split(samples, date_universe=dates)

    assert split.test_dates == dates[8:]
    assert split.test == ()


@pytest.mark.unit
def test_noninferiority_early_gate_uses_early_to_r1_wilson() -> None:
    v1 = _gate_metrics()
    v2 = _gate_metrics()
    v2["early"]["conversion"].update(
        early_to_r1=0.59,
        early_to_r2=0.99,
    )

    result = evaluate_noninferiority_gates(v1, v2, train_conversion_lower=0.6)

    assert "early_conversion_below_train_wilson" in result["reasons"]


@pytest.mark.unit
@pytest.mark.parametrize("missing_side", ["v1", "v2"])
def test_noninferiority_fails_when_false_breakout_denominator_missing(
    missing_side: str,
) -> None:
    v1 = _gate_metrics()
    v2 = _gate_metrics()
    target = v1 if missing_side == "v1" else v2
    target["overall"]["false_breakout_rate"] = None

    result = evaluate_noninferiority_gates(v1, v2, train_conversion_lower=0.6)

    assert result["eligible"] is False
    assert (
        "missing_false_breakout_baseline_or_candidate"
        in result["reasons"]
    )


@pytest.mark.unit
def test_noninferiority_fails_when_eligible_stage_has_no_floor_denominator() -> None:
    v1 = _gate_metrics()
    v2 = _gate_metrics()
    v2["r1"]["false_breakout_rate"] = None

    result = evaluate_noninferiority_gates(v1, v2, train_conversion_lower=0.6)

    assert (
        "missing_false_breakout_baseline_or_candidate"
        in result["reasons"]
    )


@pytest.mark.unit
def test_r1_gate_uses_first_five_lows_not_20d_mae() -> None:
    lows = (99.0, 98.0, 99.0, 100.0, 99.0) + (90.0,) * 15
    metrics = compute_mae_mfe(
        _sample(
            future_lows_20d=lows,
            min_low_20d=90.0,
        )
    )

    assert metrics["mae_5d"] == pytest.approx(-2.0)
    assert metrics["mae"] == pytest.approx(-10.0)


@pytest.mark.unit
def test_equity_uses_stage_weights_and_same_date_cohort_is_order_invariant() -> None:
    first = _sample(
        code="A",
        stage="early",
        **_full_window(close=120.0, high=121.0),
    )
    second = _sample(
        code="B",
        stage="r2",
        **_full_window(close=90.0, low=89.0),
    )

    forward = compute_equity_max_drawdown(
        [first, second],
        buy_cost_bps=0.0,
        sell_cost_bps=0.0,
        slippage_bps=0.0,
    )
    reverse = compute_equity_max_drawdown(
        [second, first],
        buy_cost_bps=0.0,
        sell_cost_bps=0.0,
        slippage_bps=0.0,
    )

    assert forward == pytest.approx(3.0)
    assert reverse == forward
    assert resolved_position_weight(first) == 0.2
    assert resolved_position_weight(second) == 1.0


@pytest.mark.unit
def test_unknown_explicit_position_fails_closed_from_execution_metrics() -> None:
    sample = _sample(position_weight=1.2, **_full_window())
    metrics = summarize_validation_samples(
        [sample],
        boundaries={
            "volatility": (0.01, 0.03),
            "liquidity": (500_000.0, 1_500_000.0),
        },
        opportunity_count=10,
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
    )

    assert resolved_position_weight(sample) is None
    assert metrics["r2"]["sample_count"] == 0
    assert metrics["r2"]["invalid_position_count"] == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1/10仓", 0.1),
        ("1/5仓", 0.2),
        ("1/3仓", 1 / 3),
        ("1/2仓", 0.5),
        ("目标仓位20%", 0.2),
        ("目标仓位50%", 0.5),
        ("目标仓位100%", 1.0),
    ],
)
def test_position_parser_supports_real_trade_plan_strings(
    text: str,
    expected: float,
) -> None:
    payload = json.dumps({"initial_position": text}, ensure_ascii=False)
    assert _parse_position_weight(payload) == pytest.approx(expected)


@pytest.mark.unit
def test_real_v1_fractional_trade_plan_forms_executable_sample() -> None:
    signal_date = date(2024, 1, 2)
    candidate = SimpleNamespace(
        code="000001",
        factor_snapshot={
            "close": 100.0,
            "bottom_divergence_candidate_version": "legacy-structure",
            "bottom_divergence_buy_points": [
                {
                    "level": 2,
                    "label": "水平阻力线突破",
                    "trigger_price": 99.0,
                    "triggered": True,
                }
            ],
        },
        trade_stage="probe_entry",
        trade_plan_json=json.dumps({"initial_position": "1/5仓"}),
        market_regime="balanced",
    )

    sample = _build_validation_sample(
        candidate=candidate,
        signal_date=signal_date,
        strategy_version="v1",
        config=Config(bottom_divergence_v2_enabled=False),
        stock_repository=_FakeStockRepository(),
    )

    assert sample.is_executable is True
    assert sample.position_weight == pytest.approx(0.2)
    metrics = summarize_validation_samples(
        [sample],
        boundaries={
            "volatility": (0.0, 1.0),
            "liquidity": (1.0, 2_000_000.0),
        },
        opportunity_count=1,
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
    )
    assert metrics["r2"]["sample_count"] == 1


@pytest.mark.unit
def test_sample_builder_uses_entry_evaluator_for_existing_metrics(
    monkeypatch,
) -> None:
    calls: list[int] = []

    def fake_evaluate(entry_price, forward_bars):
        assert entry_price == 100.0
        calls.append(len(forward_bars))
        if len(forward_bars) == 5:
            return EvaluationResult(mae=-2.5, mfe=4.0)
        return EvaluationResult(
            forward_return_5d=5.5,
            forward_return_10d=8.5,
            mae=-7.5,
            mfe=12.5,
        )

    monkeypatch.setattr(
        "scripts.validate_bottom_divergence_v2.EntrySignalEvaluator.evaluate",
        fake_evaluate,
    )
    candidate = SimpleNamespace(
        code="000001",
        factor_snapshot={
            "close": 100.0,
            "bottom_divergence_v2_stage": "early",
            "bottom_divergence_v2_candidate_version": "structure",
            "bottom_divergence_v2_near_zone_lower": 99.0,
        },
        trade_stage="probe_entry",
        trade_plan_json=json.dumps({"initial_position": "目标仓位20%"}),
        market_regime="balanced",
    )

    sample = _build_validation_sample(
        candidate=candidate,
        signal_date=date(2024, 1, 2),
        strategy_version="v2",
        config=Config(bottom_divergence_v2_enabled=True),
        stock_repository=_FakeStockRepository(),
    )

    assert calls == [20, 5]
    assert sample.evaluator_return_5d == 5.5
    assert sample.evaluator_return_10d == 8.5
    assert sample.mae_5d == -2.5
    assert sample.mae_20d == -7.5
    assert sample.mfe_20d == 12.5
    assert sample.close_20d == 101.0
    returns = compute_sample_returns(
        sample,
        buy_cost_bps=0.0,
        sell_cost_bps=0.0,
        slippage_bps=0.0,
    )
    assert returns["gross_return_5d"] == 5.5
    assert returns["gross_return_10d"] == 8.5
    assert compute_mae_mfe(sample) == {
        "mae_5d": -2.5,
        "mae": -7.5,
        "mfe": 12.5,
    }


@pytest.mark.unit
def test_immature_window_is_reported_and_excluded_from_thresholds() -> None:
    immature = _sample(
        future_closes_20d=(101.0,) * 19,
        future_highs_20d=(102.0,) * 19,
        future_lows_20d=(99.0,) * 19,
        close_20d=None,
    )
    metrics = summarize_validation_samples(
        [immature],
        boundaries={
            "volatility": (0.01, 0.03),
            "liquidity": (500_000.0, 1_500_000.0),
        },
        opportunity_count=10,
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
    )

    assert is_mature_sample(immature) is False
    assert metrics["r2"]["signal_count"] == 1
    assert metrics["r2"]["sample_count"] == 0
    assert metrics["r2"]["immature_count"] == 1


@pytest.mark.unit
def test_false_breakout_uses_generic_floor_and_reports_missing_floor() -> None:
    with_floor = _sample(
        breakout_floor=101.0,
        future_closes_20d=(102.0, 100.0) + (103.0,) * 18,
        **{
            key: value
            for key, value in _full_window().items()
            if key != "future_closes_20d"
        },
    )
    missing_floor = _sample(
        code="000002",
        breakout_floor=None,
        **_full_window(),
    )
    metrics = summarize_validation_samples(
        [with_floor, missing_floor],
        boundaries={
            "volatility": (0.01, 0.03),
            "liquidity": (500_000.0, 1_500_000.0),
        },
        opportunity_count=10,
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
    )

    assert is_false_breakout(with_floor) is True
    assert is_false_breakout(missing_floor) is None
    assert metrics["r2"]["false_breakout_count"] == 1
    assert metrics["r2"]["false_breakout_denominator"] == 1
    assert metrics["r2"]["missing_floor_count"] == 1


@pytest.mark.unit
def test_v1_breakout_floor_freezes_triggered_horizontal_resistance() -> None:
    factor = {
        "bottom_divergence_buy_points": [
            {
                "level": 1,
                "label": "趋势线突破",
                "trigger_price": 10.2,
                "triggered": True,
            },
            {
                "level": 2,
                "label": "水平阻力线突破",
                "trigger_price": 11.0,
                "triggered": True,
            },
        ]
    }

    assert _v1_breakout_floor(factor) == 11.0


@pytest.mark.unit
def test_pre_signal_features_use_20_prior_bars_and_unknown_when_insufficient() -> None:
    bars = [
        SimpleNamespace(close=100.0 + index, amount=1_000_000.0 + index)
        for index in range(20)
    ]
    volatility, liquidity = compute_pre_signal_features(bars)

    expected_returns = [
        (bars[index].close / bars[index - 1].close) - 1.0
        for index in range(1, 20)
    ]
    assert volatility == pytest.approx(statistics.stdev(expected_returns))
    assert liquidity == pytest.approx(
        statistics.fmean(item.amount for item in bars)
    )
    assert compute_pre_signal_features(bars[:9]) == (None, None)


@pytest.mark.unit
def test_unknown_group_values_do_not_enter_positive_group_denominator() -> None:
    samples = [
        _sample(
            code=f"{index:06d}",
            volatility=None,
            liquidity=None,
            **_full_window(),
        )
        for index in range(30)
    ]
    metrics = summarize_validation_samples(
        samples,
        boundaries={"volatility": (0.01, 0.03), "liquidity": (1.0, 2.0)},
        opportunity_count=100,
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
    )

    group_items = metrics["r2"]["groups"]["items"]
    unknown_items = [
        item
        for item in group_items
        if item["dimension"] in {"volatility", "liquidity"}
    ]
    assert all(item["label"] == "unknown" for item in unknown_items)
    assert all(item["eligible"] is False for item in unknown_items)
    assert metrics["r2"]["groups"]["eligible_count"] == 1


@pytest.mark.unit
def test_split_ratios_are_fixed_to_60_20_20() -> None:
    samples = [
        _sample(signal_date=date(2024, 1, day))
        for day in range(1, 11)
    ]
    with pytest.raises(ValueError, match="fixed at 0.6/0.2/0.2"):
        chronological_split(
            samples,
            train_ratio=0.5,
            validation_ratio=0.25,
            test_ratio=0.25,
        )


@pytest.mark.unit
def test_coverage_uses_stock_date_opportunities_not_signal_count() -> None:
    metrics = summarize_validation_samples(
        [_sample(**_full_window())],
        boundaries={
            "volatility": (0.01, 0.03),
            "liquidity": (500_000.0, 1_500_000.0),
        },
        opportunity_count=200,
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
    )

    assert metrics["r2"]["signal_count"] == 1
    assert metrics["r2"]["coverage"] == pytest.approx(0.005)


@pytest.mark.unit
def test_profit_samples_deduplicate_by_candidate_and_stage() -> None:
    repeated = [
        _sample(
            code="001337",
            signal_date=date(2026, 7, 23) + timedelta(days=index),
            candidate_version="candidate-001337",
            stage="r1",
        )
        for index in range(13)
    ]
    members = [
        *repeated,
        _sample(
            code="001337",
            signal_date=date(2026, 7, 24),
            candidate_version="candidate-001337",
            stage="r2",
        ),
        _sample(
            code="001337",
            signal_date=date(2026, 7, 25),
            candidate_version="candidate-other",
            stage="r1",
        ),
    ]
    metrics = summarize_validation_samples(
        list(reversed(members)),
        boundaries={
            "volatility": (0.01, 0.03),
            "liquidity": (500_000.0, 1_500_000.0),
        },
        opportunity_count=100,
        buy_cost_bps=1.0,
        sell_cost_bps=1.0,
        slippage_bps=1.0,
    )

    assert metrics["r1"]["sample_count"] == 2
    assert metrics["r2"]["sample_count"] == 1
    assert metrics["overall"]["sample_count"] == 3


class _FakeFactorService:
    def __init__(self, snapshot_df) -> None:
        self.snapshot_df = snapshot_df
        self.calls: list[dict] = []

    def build_factor_snapshot(self, universe, trade_date, persist):
        self.calls.append(
            {
                "universe": universe,
                "trade_date": trade_date,
                "persist": persist,
            }
        )
        return self.snapshot_df.copy()


class _FakeStockRepository:
    def __init__(self) -> None:
        self.forward_calls: list[dict] = []

    def get_forward_bars(self, *, code, analysis_date, eval_window_days):
        self.forward_calls.append(
            {
                "code": code,
                "analysis_date": analysis_date,
                "eval_window_days": eval_window_days,
            }
        )
        # pre_close 与前一日收盘逐位相等 = 窗口内没有除权，复权因子恒为 1。
        # 这批 bar 用来钉评估器的调用与取数，不该顺带引入价格缩放；缺
        # pre_close 或 adj_convention 则整个前瞻窗口按 fail-closed 丢弃，
        # 测的就不是原来那件事了。
        # 首根前瞻 bar 的 pre_close 必须等于信号日收盘（100.0，见各用例的
        # factor_snapshot），否则锚点到首根之间会被算出一次假除权。
        return [
            SimpleNamespace(
                date=analysis_date + timedelta(days=index + 1),
                close=101.0,
                pre_close=100.0 if index == 0 else 101.0,
                high=103.0,
                low=99.0,
                amount=1_000_000.0,
                adj_convention="raw",
            )
            for index in range(20)
        ]

    def get_prior_bars(self, *, code, signal_date, count):
        return [
            SimpleNamespace(
                close=100.0 + index,
                pre_close=100.0 + max(index - 1, 0),
                amount=1_000_000.0 + index,
                adj_convention="raw",
            )
            for index in range(count)
        ]

    def get_range(self, code, start_date, end_date):
        del code, start_date
        return [
            SimpleNamespace(date=end_date - timedelta(days=2)),
            SimpleNamespace(date=end_date - timedelta(days=1)),
            SimpleNamespace(date=end_date),
        ]


@pytest.mark.unit
def test_replay_calls_five_layer_and_only_executes_pipeline_trade_signals() -> None:
    import pandas as pd
    from src.services.five_layer_pipeline import FiveLayerPipeline

    signal_date = date(2024, 1, 2)
    factor = {
        "close": 100.0,
        "bottom_divergence_v2_stage": "early",
        "bottom_divergence_v2_candidate_version": "structure-a",
        "bottom_divergence_v2_near_zone_lower": 99.0,
        "bottom_divergence_v2_early_event_index": 2,
    }
    executable = SimpleNamespace(
        code="000001",
        factor_snapshot=dict(factor),
        trade_stage="probe_entry",
        trade_plan_json=json.dumps({"initial_position": "目标仓位20%"}),
        market_regime="balanced",
    )
    observation = SimpleNamespace(
        code="000002",
        factor_snapshot={
            **factor,
            "bottom_divergence_v2_candidate_version": "structure-b",
        },
        trade_stage="watch",
        trade_plan_json=None,
        market_regime="defensive",
    )
    pipeline = MagicMock(spec=FiveLayerPipeline)
    pipeline.run.return_value = SimpleNamespace(
        candidates=[executable, observation],
    )
    factor_service = _FakeFactorService(
        pd.DataFrame([{"code": "000001"}, {"code": "000002"}])
    )
    stock_repo = _FakeStockRepository()
    dependencies = ReplayDependencies(
        db_manager=MagicMock(),
        factor_service_factory=lambda config: factor_service,
        pipeline=pipeline,
        screener_factory=lambda version: (MagicMock(), MagicMock()),
        market_context_provider=lambda trade_date, snapshot_df: (
            SimpleNamespace(
                regime=SimpleNamespace(value="balanced"),
                risk_level=SimpleNamespace(value="medium"),
            ),
            SimpleNamespace(is_safe=True),
        ),
        stock_repository=stock_repo,
    )

    batch = replay_historical_dates(
        strategy_version="v2",
        config=Config(bottom_divergence_v2_enabled=True),
        trade_dates=[signal_date],
        universe=object(),
        dependencies=dependencies,
    )

    assert isinstance(batch, ReplayBatch)
    pipeline.run.assert_called_once()
    assert factor_service.calls[0]["persist"] is False
    assert batch.opportunity_counts == {signal_date: 2}
    assert len(batch.samples) == 1
    assert batch.samples[0].is_executable is True
    assert batch.samples[0].position_weight == 0.2
    assert batch.samples[0].market_regime == "balanced"
    assert len(batch.event_evidence) == 2
    assert stock_repo.forward_calls == [
        {
            "code": "000001",
            "analysis_date": signal_date,
            "eval_window_days": 20,
        },
    ]


@pytest.mark.unit
def test_replay_001337_emits_only_first_r1_event_independent_of_input_order() -> None:
    import pandas as pd
    from src.services.five_layer_pipeline import FiveLayerPipeline

    first_event_date = date(2026, 7, 23)
    trade_dates = [
        first_event_date + timedelta(days=index)
        for index in range((date(2026, 8, 4) - first_event_date).days + 1)
    ]
    factor = {
        "close": 100.0,
        "bottom_divergence_v2_stage": "near",
        "bottom_divergence_v2_candidate_version": "001337-structure",
        "bottom_divergence_v2_near_zone_lower": 99.0,
        "bottom_divergence_v2_near_event_index": 0,
    }
    candidate = SimpleNamespace(
        code="001337",
        factor_snapshot=factor,
        trade_stage="add_on_strength",
        trade_plan_json=json.dumps({"initial_position": "目标仓位50%"}),
        market_regime="balanced",
    )

    class FixedEventStockRepository(_FakeStockRepository):
        def get_range(self, code, start_date, end_date):
            del code, start_date
            return [
                SimpleNamespace(date=current)
                for current in trade_dates
                if current <= end_date
            ]

    def run(order):
        pipeline = MagicMock(spec=FiveLayerPipeline)
        pipeline.run.return_value = SimpleNamespace(
            candidates=[candidate, candidate],
        )
        stock_repo = FixedEventStockRepository()
        batch = replay_historical_dates(
            strategy_version="v2",
            config=Config(bottom_divergence_v2_enabled=True),
            trade_dates=order,
            universe=object(),
            dependencies=ReplayDependencies(
                db_manager=MagicMock(),
                factor_service_factory=lambda config: _FakeFactorService(
                    pd.DataFrame([{"code": "001337"}])
                ),
                pipeline=pipeline,
                screener_factory=lambda version: (MagicMock(), MagicMock()),
                market_context_provider=lambda trade_date, snapshot_df: (
                    SimpleNamespace(),
                    SimpleNamespace(),
                ),
                stock_repository=stock_repo,
            ),
        )
        return batch, stock_repo

    ascending, ascending_repo = run(trade_dates)
    descending, descending_repo = run(list(reversed(trade_dates)))

    assert len(ascending.samples) == 1
    assert ascending.samples[0].signal_date == first_event_date
    assert ascending.samples == descending.samples
    assert len(ascending.event_evidence) == 1
    assert ascending.event_evidence[0].near_cleared_event_date == first_event_date
    assert len(ascending_repo.forward_calls) == 1
    assert len(descending_repo.forward_calls) == 1


@pytest.mark.unit
def test_v1_replay_uses_first_legacy_confirmation_event_only() -> None:
    import pandas as pd

    trade_dates = [
        date(2026, 7, 23) + timedelta(days=index)
        for index in range(5)
    ]

    class LegacyPipeline:
        def run(self, *, trade_date, **_kwargs):
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        code="001337",
                        factor_snapshot={
                            "close": 100.0,
                            "confirmation_days": 0,
                            "bottom_divergence_buy_points": [
                                {
                                    "level": 2,
                                    "type": "horizontal_resistance",
                                    "trigger_price": 99.0,
                                    "triggered": True,
                                }
                            ],
                        },
                        trade_stage="add_on_strength",
                        trade_plan_json=json.dumps(
                            {"initial_position": "目标仓位100%"}
                        ),
                        market_regime="balanced",
                    )
                ]
            )

    stock_repo = _FakeStockRepository()
    batch = replay_historical_dates(
        strategy_version="v1",
        config=Config(bottom_divergence_v2_enabled=False),
        trade_dates=list(reversed(trade_dates)),
        universe=object(),
        dependencies=ReplayDependencies(
            db_manager=MagicMock(),
            factor_service_factory=lambda config: _FakeFactorService(
                pd.DataFrame([{"code": "001337"}])
            ),
            pipeline=LegacyPipeline(),
            screener_factory=lambda version: (MagicMock(), MagicMock()),
            market_context_provider=lambda trade_date, snapshot_df: (
                SimpleNamespace(),
                SimpleNamespace(),
            ),
            stock_repository=stock_repo,
        ),
    )

    assert len(batch.samples) == 1
    assert batch.samples[0].signal_date == trade_dates[0]
    assert batch.samples[0].candidate_version.startswith("v1:001337:")
    assert len(stock_repo.forward_calls) == 1


@pytest.mark.unit
def test_replay_respects_environment_and_theme_pipeline_exclusion() -> None:
    import pandas as pd
    from src.services.five_layer_pipeline import FiveLayerPipeline

    signal_date = date(2024, 1, 2)
    pipeline = MagicMock(spec=FiveLayerPipeline)
    pipeline.run.return_value = SimpleNamespace(candidates=[])
    dependencies = ReplayDependencies(
        db_manager=MagicMock(),
        factor_service_factory=lambda config: _FakeFactorService(
            pd.DataFrame([{"code": "000001"}])
        ),
        pipeline=pipeline,
        screener_factory=lambda version: (MagicMock(), MagicMock()),
        market_context_provider=lambda trade_date, snapshot_df: (
            SimpleNamespace(
                regime=SimpleNamespace(value="stand_aside"),
                risk_level=SimpleNamespace(value="high"),
            ),
            SimpleNamespace(is_safe=False),
        ),
        stock_repository=_FakeStockRepository(),
    )

    batch = replay_historical_dates(
        strategy_version="v2",
        config=Config(bottom_divergence_v2_enabled=True),
        trade_dates=[signal_date],
        universe=object(),
        dependencies=dependencies,
    )

    assert batch.samples == ()
    assert batch.opportunity_counts == {signal_date: 1}
    assert pipeline.run.call_args.kwargs["market_env"].regime.value == (
        "stand_aside"
    )


@pytest.mark.unit
def test_maturation_replay_emits_only_target_event_evidence() -> None:
    import pandas as pd

    maturation_date = date(2024, 4, 5)
    factor_service = _FakeFactorService(
        pd.DataFrame(
            [
                {
                    "code": "000001",
                    "bottom_divergence_v2_candidate_version": "test-early",
                    "bottom_divergence_v2_near_event_index": 2,
                },
                {
                    "code": "000002",
                    "bottom_divergence_v2_candidate_version": "post-test-new",
                    "bottom_divergence_v2_near_event_index": 2,
                },
            ]
        )
    )
    repository = _FakeStockRepository()
    dependencies = ReplayDependencies(
        db_manager=MagicMock(),
        factor_service_factory=lambda config: factor_service,
        pipeline=MagicMock(),
        screener_factory=lambda version: (MagicMock(), MagicMock()),
        market_context_provider=lambda trade_date, snapshot_df: (
            SimpleNamespace(
                regime=SimpleNamespace(value="balanced"),
                risk_level=SimpleNamespace(value="medium"),
            ),
            SimpleNamespace(is_safe=True),
        ),
        stock_repository=repository,
    )

    evidence = replay_maturation_events(
        config=Config(bottom_divergence_v2_enabled=True),
        maturation_dates=[maturation_date],
        universe=object(),
        target_candidates={("000001", "test-early")},
        dependencies=dependencies,
    )

    assert len(factor_service.calls) == 1
    assert factor_service.calls[0]["trade_date"] == maturation_date
    assert factor_service.calls[0]["persist"] is False
    assert evidence == (
        CandidateEventEvidence(
            code="000001",
            candidate_version="test-early",
            near_cleared_event_date=maturation_date,
            major_breakout_event_date=None,
        ),
    )
    assert repository.forward_calls == []


@pytest.mark.unit
def test_isolated_replay_database_copies_rows_without_mutating_source() -> None:
    import pandas as pd
    from sqlalchemy import func, select

    from src.storage import (
        DatabaseManager,
        InstrumentMaster,
        ScreeningRun,
        StockDaily,
    )

    source = object.__new__(DatabaseManager)
    DatabaseManager.__init__(source, "sqlite:///:memory:")
    singleton_before = DatabaseManager._instance
    start = date(2024, 1, 1)
    with source.get_session() as session:
        session.add(
            InstrumentMaster(
                code="000001",
                name="测试",
                market="cn",
                listing_status="active",
                is_st=False,
            )
        )
        session.add_all(
            [
                StockDaily(
                    code="000001",
                    date=start + timedelta(days=index),
                    open=100.0,
                    high=102.0,
                    low=99.0,
                    close=101.0,
                    volume=1_000.0,
                    amount=1_000_000.0,
                    pct_chg=1.0,
                )
                for index in range(80)
            ]
        )
        session.add(
            ScreeningRun(
                run_id="source-existing",
                trade_date=start,
                market="cn",
                status="completed",
            )
        )
        session.commit()

    def counts(db):
        with db.get_session() as session:
            return (
                session.execute(
                    select(func.count(StockDaily.id))
                ).scalar_one(),
                session.execute(
                    select(func.count(ScreeningRun.id))
                ).scalar_one(),
            )

    source_before = counts(source)
    temporary_path = None
    with isolated_replay_database(
        source_db=source,
        universe=pd.DataFrame([{"code": "000001"}]),
        date_from=start + timedelta(days=30),
        date_to=start + timedelta(days=50),
        market="cn",
        market_guard_index="sh000001",
    ) as temporary:
        temporary_path = Path(temporary._engine.url.database)
        assert temporary_path.exists()
        assert temporary is not source
        temporary_counts = counts(temporary)
        assert temporary_counts[0] > 0
        assert temporary_counts[1] == 1
        assert counts(source) == source_before
        assert DatabaseManager._instance is singleton_before

    assert counts(source) == source_before
    assert DatabaseManager._instance is singleton_before
    assert temporary_path is not None
    assert temporary_path.exists() is False
    source._engine.dispose()


@pytest.mark.unit
def test_cli_default_replay_uses_temp_db_and_leaves_source_runs_unchanged(
    tmp_path,
    monkeypatch,
) -> None:
    import pandas as pd
    from sqlalchemy import func, select

    from src.storage import (
        DatabaseManager,
        InstrumentMaster,
        ScreeningRun,
        StockDaily,
    )

    source = object.__new__(DatabaseManager)
    DatabaseManager.__init__(source, "sqlite:///:memory:")
    start = date(2024, 1, 1)
    with source.get_session() as session:
        session.add(
            InstrumentMaster(
                code="000001",
                name="测试",
                market="cn",
                listing_status="active",
                is_st=False,
            )
        )
        session.add_all(
            [
                StockDaily(
                    code="000001",
                    date=start + timedelta(days=index),
                    open=100.0,
                    high=102.0,
                    low=99.0,
                    close=101.0,
                    volume=1_000.0,
                    amount=1_000_000.0,
                    pct_chg=1.0,
                )
                for index in range(80)
            ]
        )
        session.add(
            ScreeningRun(
                run_id="source-run",
                trade_date=start,
                market="cn",
                status="completed",
            )
        )
        session.commit()

    def run_count(db) -> int:
        with db.get_session() as session:
            return session.execute(
                select(func.count(ScreeningRun.id))
            ).scalar_one()

    source_run_count = run_count(source)
    singleton_before = DatabaseManager._instance
    monkeypatch.setattr(
        DatabaseManager,
        "get_instance",
        classmethod(lambda cls: source),
    )
    monkeypatch.setattr(
        "src.backtest.services.bottom_divergence_v2_cli_service."
        "_resolve_local_universe",
        lambda db, market, codes: pd.DataFrame([{"code": "000001"}]),
    )
    observed: dict[str, object] = {}

    def fake_replay(**kwargs):
        temporary = kwargs["dependencies"].db_manager
        observed["replay_db"] = temporary
        observed["temp_run_count_during_replay"] = run_count(temporary)
        return ReplayBatch(
            samples=(),
            opportunity_counts={kwargs["trade_dates"][0]: 1},
        )

    def fake_core(args, *, replay_service, base_config):
        replay_service.replay(
            strategy_version="v1",
            config=base_config,
            trade_dates=[args.date_from],
            universe=pd.DataFrame([{"code": "000001"}]),
        )
        return 1, {"eligible": False}

    monkeypatch.setattr(
        "src.backtest.services.bottom_divergence_v2_cli_service."
        "replay_historical_dates",
        fake_replay,
    )
    monkeypatch.setattr(
        "src.backtest.services.bottom_divergence_v2_cli_service."
        "_run_validation_cli_core",
        fake_core,
    )

    run_validation_cli(
        Namespace(
            date_from=start + timedelta(days=30),
            date_to=start + timedelta(days=50),
            market="cn",
            output=tmp_path / "unused.json",
            universe_codes=None,
        ),
        base_config=Config(
            backtest_buy_cost_bps=1.0,
            backtest_sell_cost_bps=1.0,
            backtest_slippage_bps=1.0,
        ),
        isolation_observer=lambda source_db, temporary_db: observed.update(
            source_runs=run_count(source_db),
            temp_runs=run_count(temporary_db),
        ),
    )

    assert observed["replay_db"] is not source
    assert observed["temp_run_count_during_replay"] == 1
    assert observed["temp_runs"] == 1
    assert observed["source_runs"] == source_run_count
    assert run_count(source) == source_run_count
    assert DatabaseManager._instance is singleton_before
    source._engine.dispose()
