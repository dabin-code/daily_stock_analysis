# -*- coding: utf-8 -*-
"""Input-contract tests for bottom-divergence v2 validation."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.backtest.services.bottom_divergence_v2_validation import (
    BottomDivergenceV2Validator,
    ValidationInputError,
    ValidationSample,
    build_parameter_snapshots,
    compute_sample_returns,
)


def _sample(signal_date: date) -> ValidationSample:
    prices = (101.0,) * 20
    return ValidationSample(
        code="000001",
        signal_date=signal_date,
        candidate_version=f"candidate:{signal_date}",
        strategy_version="v1",
        stage="r2",
        entry_close=100.0,
        near_zone_lower=None,
        major_zone_lower=None,
        early_event_date=signal_date,
        near_cleared_event_date=signal_date,
        major_breakout_event_date=signal_date,
        close_5d=101.0,
        close_10d=101.0,
        close_20d=101.0,
        future_closes_20d=prices,
        future_highs_20d=(102.0,) * 20,
        future_lows_20d=(99.0,) * 20,
        max_high_20d=102.0,
        min_low_20d=99.0,
        market_regime="balanced",
        volatility=0.01,
        liquidity=1_000_000.0,
    )


def _evaluate(**overrides):
    dates = tuple(date(2024, 1, 1) + timedelta(days=index) for index in range(10))
    snapshots = build_parameter_snapshots()
    kwargs = {
        "v1_samples": [_sample(dates[0])],
        "v2_samples_by_parameter_hash": {key: () for key in snapshots},
        "parameter_snapshots": snapshots,
        "trading_dates": dates,
        "opportunity_counts": {item: 1 for item in dates},
        "buy_cost_bps": 1.0,
        "sell_cost_bps": 1.0,
        "slippage_bps": 1.0,
    }
    kwargs.update(overrides)
    return BottomDivergenceV2Validator.evaluate(**kwargs)


@pytest.mark.parametrize("trading_dates", [None, ()])
def test_evaluate_requires_explicit_nonempty_trading_dates(trading_dates) -> None:
    with pytest.raises(ValidationInputError) as caught:
        _evaluate(trading_dates=trading_dates)
    assert caught.value.error_code == "NO_TRADING_DATES"


def test_evaluate_rejects_sample_date_outside_trading_calendar() -> None:
    with pytest.raises(ValidationInputError) as caught:
        _evaluate(v1_samples=[_sample(date(2030, 1, 1))])
    assert caught.value.error_code == "SAMPLE_DATE_OUTSIDE_CALENDAR"


def test_evaluate_requires_opportunities_for_exact_calendar() -> None:
    dates = tuple(date(2024, 1, 1) + timedelta(days=index) for index in range(10))
    with pytest.raises(ValidationInputError) as caught:
        _evaluate(
            trading_dates=dates,
            opportunity_counts={item: 1 for item in dates[:-1]},
        )
    assert caught.value.error_code == "INVALID_OPPORTUNITY_COUNTS"


@pytest.mark.parametrize("invalid_count", [True, 1.0, -1])
def test_evaluate_rejects_noninteger_or_negative_daily_opportunity(
    invalid_count,
) -> None:
    dates = tuple(date(2024, 1, 1) + timedelta(days=index) for index in range(10))
    opportunities = {item: 2 for item in dates}
    opportunities[dates[0]] = invalid_count
    with pytest.raises(ValidationInputError) as caught:
        _evaluate(
            trading_dates=dates,
            opportunity_counts=opportunities,
        )
    assert caught.value.error_code == "INVALID_OPPORTUNITY_COUNTS"


@pytest.mark.parametrize("field", ["buy_cost_bps", "sell_cost_bps", "slippage_bps"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_evaluate_rejects_nonfinite_costs(field: str, value: float) -> None:
    with pytest.raises(ValidationInputError) as caught:
        _evaluate(**{field: value})
    assert caught.value.error_code == "INVALID_INPUT"


def test_validation_sample_rejects_nonfinite_forward_price() -> None:
    with pytest.raises(ValidationInputError) as caught:
        replace(_sample(date(2024, 1, 1)), close_5d=float("nan"))
    assert caught.value.error_code == "INVALID_INPUT"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("close_5d", 0.0),
        ("close_10d", -1.0),
        ("close_20d", 0.0),
        ("max_high_20d", -1.0),
        ("min_low_20d", 0.0),
    ],
)
def test_validation_sample_rejects_nonpositive_optional_prices(
    field: str,
    value: float,
) -> None:
    with pytest.raises(ValidationInputError) as caught:
        replace(_sample(date(2024, 1, 1)), **{field: value})
    assert caught.value.error_code == "INVALID_INPUT"


@pytest.mark.parametrize(
    "changes",
    [
        {"future_closes_20d": (0.0,) + (101.0,) * 19},
        {"future_highs_20d": (-1.0,) + (102.0,) * 19},
        {"future_lows_20d": (0.0,) + (99.0,) * 19},
        {
            "future_lows_20d": (103.0,) + (99.0,) * 19,
            "future_closes_20d": (101.0,) * 20,
            "future_highs_20d": (104.0,) + (102.0,) * 19,
        },
        {
            "future_lows_20d": (99.0,) * 20,
            "future_closes_20d": (105.0,) + (101.0,) * 19,
            "future_highs_20d": (104.0,) + (102.0,) * 19,
        },
        {"future_highs_20d": (102.0,) * 19},
    ],
)
def test_validation_sample_rejects_invalid_forward_bar_path(changes) -> None:
    with pytest.raises(ValidationInputError) as caught:
        replace(_sample(date(2024, 1, 1)), **changes)
    assert caught.value.error_code == "INVALID_INPUT"


def test_direct_return_api_rejects_nonfinite_cost() -> None:
    with pytest.raises(ValidationInputError) as caught:
        compute_sample_returns(
            _sample(date(2024, 1, 1)),
            buy_cost_bps=float("inf"),
            sell_cost_bps=1.0,
            slippage_bps=1.0,
        )
    assert caught.value.error_code == "INVALID_INPUT"
