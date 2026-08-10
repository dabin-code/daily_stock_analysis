# -*- coding: utf-8 -*-
"""Pure, in-memory sample-out validation for bottom-divergence v2."""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, Optional, Sequence


class ValidationInputError(ValueError):
    """Expected invalid/ineligible validation input with a stable error code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        data_version: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.data_version = data_version


def _require_finite(name: str, value: Optional[float]) -> None:
    if value is None:
        return
    try:
        finite = math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationInputError(
            "INVALID_INPUT",
            f"{name} must be a finite number",
        ) from exc
    if not finite:
        raise ValidationInputError(
            "INVALID_INPUT",
            f"{name} must be a finite number",
        )


@dataclass(frozen=True)
class ValidationSample:
    """One immutable point-in-time signal and its forward 20-session window."""

    code: str
    signal_date: date
    candidate_version: str
    strategy_version: str
    stage: str
    entry_close: float
    near_zone_lower: Optional[float]
    major_zone_lower: Optional[float]
    early_event_date: Optional[date]
    near_cleared_event_date: Optional[date]
    major_breakout_event_date: Optional[date]
    close_5d: Optional[float]
    close_10d: Optional[float]
    close_20d: Optional[float]
    future_closes_20d: tuple[float, ...]
    future_highs_20d: tuple[float, ...]
    future_lows_20d: tuple[float, ...]
    max_high_20d: Optional[float]
    min_low_20d: Optional[float]
    market_regime: str
    volatility: Optional[float]
    liquidity: Optional[float]
    future_trade_dates_20d: tuple[date, ...] = ()
    breakout_floor: Optional[float] = None
    position_weight: Optional[float] = None
    is_executable: bool = True
    evaluator_return_5d: Optional[float] = None
    evaluator_return_10d: Optional[float] = None
    mae_5d: Optional[float] = None
    mae_20d: Optional[float] = None
    mfe_20d: Optional[float] = None

    def __post_init__(self) -> None:
        scalar_fields = (
            "entry_close",
            "near_zone_lower",
            "major_zone_lower",
            "close_5d",
            "close_10d",
            "close_20d",
            "max_high_20d",
            "min_low_20d",
            "volatility",
            "liquidity",
            "breakout_floor",
            "position_weight",
            "evaluator_return_5d",
            "evaluator_return_10d",
            "mae_5d",
            "mae_20d",
            "mfe_20d",
        )
        for field_name in scalar_fields:
            _require_finite(field_name, getattr(self, field_name))
        for field_name in (
            "future_closes_20d",
            "future_highs_20d",
            "future_lows_20d",
        ):
            for index, value in enumerate(getattr(self, field_name)):
                _require_finite(f"{field_name}[{index}]", value)
                if float(value) <= 0:
                    raise ValidationInputError(
                        "INVALID_INPUT",
                        f"{field_name}[{index}] must be positive",
                    )
        path_lengths = {
            len(self.future_closes_20d),
            len(self.future_highs_20d),
            len(self.future_lows_20d),
        }
        if len(path_lengths) != 1:
            raise ValidationInputError(
                "INVALID_INPUT",
                "future OHLC paths must have equal lengths",
            )
        if (
            self.future_trade_dates_20d
            and len(self.future_trade_dates_20d)
            != len(self.future_closes_20d)
        ):
            raise ValidationInputError(
                "INVALID_INPUT",
                "future trade dates must match future OHLC path length",
            )
        if self.future_trade_dates_20d:
            ordered_future_dates = tuple(sorted(set(
                self.future_trade_dates_20d
            )))
            if (
                ordered_future_dates != self.future_trade_dates_20d
                or ordered_future_dates[0] <= self.signal_date
            ):
                raise ValidationInputError(
                    "INVALID_INPUT",
                    "future trade dates must be unique, ordered, and after signal",
                )
        for index, (low, close, high) in enumerate(zip(
            self.future_lows_20d,
            self.future_closes_20d,
            self.future_highs_20d,
        )):
            if not float(low) <= float(close) <= float(high):
                raise ValidationInputError(
                    "INVALID_INPUT",
                    f"future bar {index} must satisfy low <= close <= high",
                )
        positive_scalar_fields = (
            "entry_close",
            "close_5d",
            "close_10d",
            "close_20d",
            "max_high_20d",
            "min_low_20d",
        )
        for field_name in positive_scalar_fields:
            value = getattr(self, field_name)
            if value is not None and float(value) <= 0:
                raise ValidationInputError(
                    "INVALID_INPUT",
                    f"{field_name} must be positive",
                )
        if (
            self.max_high_20d is not None
            and self.min_low_20d is not None
            and float(self.max_high_20d) < float(self.min_low_20d)
        ):
            raise ValidationInputError(
                "INVALID_INPUT",
                "max_high_20d must be >= min_low_20d",
            )
        if self.strategy_version not in {"v1", "v2"}:
            raise ValidationInputError(
                "INVALID_INPUT",
                "strategy_version must be 'v1' or 'v2'",
            )


@dataclass(frozen=True)
class CandidateEventEvidence:
    """Independent post-split event evidence for an existing early cohort."""

    code: str
    candidate_version: str
    near_cleared_event_date: Optional[date]
    major_breakout_event_date: Optional[date]


@dataclass(frozen=True)
class SampleSplit:
    """Chronological train/validation/test partitions and their date boundaries."""

    train: tuple[ValidationSample, ...]
    validation: tuple[ValidationSample, ...]
    test: tuple[ValidationSample, ...]
    train_dates: tuple[date, ...]
    validation_dates: tuple[date, ...]
    test_dates: tuple[date, ...]


def chronological_split(
    samples: Sequence[ValidationSample],
    *,
    date_universe: Optional[Sequence[date]] = None,
    train_ratio: float = 0.6,
    validation_ratio: float = 0.2,
    test_ratio: float = 0.2,
) -> SampleSplit:
    """Split strictly by counts of sorted unique signal dates."""
    ratios = (train_ratio, validation_ratio, test_ratio)
    if ratios != (0.6, 0.2, 0.2):
        raise ValueError("split ratios are fixed at 0.6/0.2/0.2")
    unique_dates = tuple(sorted(
        set(date_universe)
        if date_universe is not None
        else {sample.signal_date for sample in samples}
    ))
    train_count = math.floor(len(unique_dates) * train_ratio)
    validation_count = math.floor(len(unique_dates) * validation_ratio)
    test_count = len(unique_dates) - train_count - validation_count
    if min(train_count, validation_count, test_count) < 1:
        raise ValueError("each split requires at least one unique signal date")

    train_dates = unique_dates[:train_count]
    validation_dates = unique_dates[
        train_count:train_count + validation_count
    ]
    test_dates = unique_dates[train_count + validation_count:]
    train_set = set(train_dates)
    validation_set = set(validation_dates)
    test_set = set(test_dates)
    ordered = sorted(samples, key=lambda item: (item.signal_date, item.code))
    return SampleSplit(
        train=tuple(item for item in ordered if item.signal_date in train_set),
        validation=tuple(
            item for item in ordered if item.signal_date in validation_set
        ),
        test=tuple(item for item in ordered if item.signal_date in test_set),
        train_dates=train_dates,
        validation_dates=validation_dates,
        test_dates=test_dates,
    )


def _linear_quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("tertile fitting requires at least one sample")
    position = (len(ordered) - 1) * probability
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    weight = position - lower_index
    return (
        ordered[lower_index] * (1.0 - weight)
        + ordered[upper_index] * weight
    )


def fit_tertile_boundaries(
    train_samples: Sequence[ValidationSample],
) -> Dict[str, tuple[Optional[float], Optional[float]]]:
    """Fit volatility/liquidity cut points once, using training rows only."""
    if not train_samples:
        raise ValueError("tertile fitting requires training samples")
    result = {}
    for field_name in ("volatility", "liquidity"):
        values = [
            float(value)
            for value in (getattr(item, field_name) for item in train_samples)
            if value is not None and math.isfinite(float(value))
        ]
        result[field_name] = (
            (
                _linear_quantile(values, 1.0 / 3.0),
                _linear_quantile(values, 2.0 / 3.0),
            )
            if values
            else (None, None)
        )
    return result


def assign_tertile(
    value: Optional[float],
    boundaries: tuple[Optional[float], Optional[float]],
) -> str:
    """Assign deterministic low/middle/high labels to frozen boundaries."""
    lower, upper = boundaries
    if (
        value is None
        or lower is None
        or upper is None
        or not math.isfinite(float(value))
    ):
        return "unknown"
    if value <= lower:
        return "low"
    if value <= upper:
        return "middle"
    return "high"


def _gross_return(entry_close: float, future_close: Optional[float]) -> Optional[float]:
    if future_close is None:
        return None
    return (future_close / entry_close - 1.0) * 100.0


def compute_sample_returns(
    sample: ValidationSample,
    *,
    buy_cost_bps: float,
    sell_cost_bps: float,
    slippage_bps: float,
) -> Dict[str, Optional[float]]:
    """Calculate gross and cost-adjusted returns for all required horizons."""
    for name, value in (
        ("buy_cost_bps", buy_cost_bps),
        ("sell_cost_bps", sell_cost_bps),
        ("slippage_bps", slippage_bps),
    ):
        _require_finite(name, value)
        if value < 0:
            raise ValidationInputError(
                "INVALID_INPUT",
                f"{name} must be non-negative",
            )
    round_trip_cost_bps = buy_cost_bps + sell_cost_bps + 2.0 * slippage_bps
    cost_pct = round_trip_cost_bps / 100.0
    result: Dict[str, Optional[float]] = {
        "round_trip_cost_bps": round_trip_cost_bps,
    }
    for horizon, future_close in (
        (5, sample.close_5d),
        (10, sample.close_10d),
        (20, sample.close_20d),
    ):
        evaluator_gross = (
            sample.evaluator_return_5d
            if horizon == 5
            else sample.evaluator_return_10d
            if horizon == 10
            else None
        )
        gross = (
            evaluator_gross
            if evaluator_gross is not None
            else _gross_return(sample.entry_close, future_close)
        )
        result[f"gross_return_{horizon}d"] = gross
        result[f"net_return_{horizon}d"] = (
            None if gross is None else gross - cost_pct
        )
    return result


def compute_mae_mfe(sample: ValidationSample) -> Dict[str, Optional[float]]:
    """Use 20-session low/high extremes, matching existing evaluator units."""
    first_five_lows = sample.future_lows_20d[:5]
    mae_5d = sample.mae_5d
    if mae_5d is None:
        mae_5d = (
            (min(first_five_lows) / sample.entry_close - 1.0) * 100.0
            if len(first_five_lows) == 5
            else None
        )
    mae = (
        sample.mae_20d
        if sample.mae_20d is not None
        else None
        if sample.min_low_20d is None
        else (sample.min_low_20d / sample.entry_close - 1.0) * 100.0
    )
    mfe = (
        sample.mfe_20d
        if sample.mfe_20d is not None
        else None
        if sample.max_high_20d is None
        else (sample.max_high_20d / sample.entry_close - 1.0) * 100.0
    )
    return {"mae_5d": mae_5d, "mae": mae, "mfe": mfe}


def is_mature_sample(sample: ValidationSample) -> bool:
    """Require all three price arrays and all horizon closes through day 20."""
    return (
        len(sample.future_closes_20d) >= 20
        and len(sample.future_highs_20d) >= 20
        and len(sample.future_lows_20d) >= 20
        and sample.close_5d is not None
        and sample.close_10d is not None
        and sample.close_20d is not None
    )


_STAGE_POSITION_WEIGHTS = {
    "early": 0.2,
    "r1": 0.5,
    "near": 0.5,
    "near_cleared": 0.5,
    "r2": 1.0,
    "major": 1.0,
    "major_actionable": 1.0,
}


def resolved_position_weight(sample: ValidationSample) -> Optional[float]:
    """Resolve finite target positions in (0, 1], otherwise use stage defaults."""
    if sample.position_weight is not None:
        value = float(sample.position_weight)
        return value if math.isfinite(value) and 0.0 < value <= 1.0 else None
    if sample.strategy_version == "v1":
        return 1.0
    return _STAGE_POSITION_WEIGHTS.get(sample.stage.strip().lower())


def is_false_breakout(sample: ValidationSample) -> Optional[bool]:
    """Return whether a confirmation failed within its first three closes."""
    if sample.breakout_floor is None:
        return None
    prior_max_profit = float("-inf")
    for close in sample.future_closes_20d[:3]:
        if close < sample.breakout_floor and prior_max_profit < 3.0:
            return True
        profit = (close / sample.entry_close - 1.0) * 100.0
        prior_max_profit = max(prior_max_profit, profit)
    return False


def compute_equity_max_drawdown(
    samples: Sequence[ValidationSample],
    *,
    buy_cost_bps: float,
    sell_cost_bps: float,
    slippage_bps: float,
) -> float:
    """Compound date cohorts after stage-position weighting."""
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    cohorts: Dict[date, list[float]] = {}
    for sample in samples:
        weight = resolved_position_weight(sample)
        if not sample.is_executable or not is_mature_sample(sample) or weight is None:
            continue
        net_return = compute_sample_returns(
            sample,
            buy_cost_bps=buy_cost_bps,
            sell_cost_bps=sell_cost_bps,
            slippage_bps=slippage_bps,
        )["net_return_10d"]
        if net_return is not None:
            cohorts.setdefault(sample.signal_date, []).append(net_return * weight)
    for signal_date in sorted(cohorts):
        cohort_return = statistics.fmean(cohorts[signal_date])
        equity *= 1.0 + cohort_return / 100.0
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak * 100.0
        max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown


def compute_conversion_metrics(
    samples: Iterable[ValidationSample],
    *,
    cohort_dates: Optional[set[date]] = None,
    observation_dates: Optional[Sequence[date]] = None,
    maturation_evidence: Sequence[CandidateEventEvidence] = (),
) -> Dict[str, Any]:
    """Evaluate causal early cohorts inside a fixed 20-trading-day window."""
    structures: Dict[tuple[str, str], Dict[str, Any]] = {}
    for sample in samples:
        if (
            cohort_dates is not None
            and sample.early_event_date not in cohort_dates
        ):
            continue
        key = (sample.code, sample.candidate_version)
        state = structures.setdefault(
            key,
            {
                "early_date": None,
                "early": False,
                "r1": False,
                "r2": False,
                "r1_date": None,
                "r2_date": None,
            },
        )
        if sample.early_event_date is not None:
            current = state["early_date"]
            state["early_date"] = (
                sample.early_event_date
                if current is None
                else min(current, sample.early_event_date)
            )
        state["early"] = state["early"] or sample.early_event_date is not None
        state["r1"] = state["r1"] or sample.near_cleared_event_date is not None
        state["r2"] = state["r2"] or sample.major_breakout_event_date is not None
        if sample.near_cleared_event_date is not None:
            current_r1 = state["r1_date"]
            state["r1_date"] = (
                sample.near_cleared_event_date
                if current_r1 is None
                else min(current_r1, sample.near_cleared_event_date)
            )
        if sample.major_breakout_event_date is not None:
            current_r2 = state["r2_date"]
            state["r2_date"] = (
                sample.major_breakout_event_date
                if current_r2 is None
                else min(current_r2, sample.major_breakout_event_date)
            )

    for evidence in maturation_evidence:
        state = structures.get((evidence.code, evidence.candidate_version))
        if state is None:
            continue
        if evidence.near_cleared_event_date is not None:
            current_r1 = state.get("r1_date")
            state["r1_date"] = (
                evidence.near_cleared_event_date
                if current_r1 is None
                else min(current_r1, evidence.near_cleared_event_date)
            )
        if evidence.major_breakout_event_date is not None:
            current_r2 = state.get("r2_date")
            state["r2_date"] = (
                evidence.major_breakout_event_date
                if current_r2 is None
                else min(current_r2, evidence.major_breakout_event_date)
            )

    ordered_observation_dates = (
        tuple(sorted(set(observation_dates)))
        if observation_dates is not None
        else None
    )
    observation_index = (
        {
            observation_date: index
            for index, observation_date in enumerate(ordered_observation_dates)
        }
        if ordered_observation_dates is not None
        else {}
    )
    eligible_structures = []
    right_censored_count = 0
    for item in structures.values():
        early_date = item["early_date"]
        if cohort_dates is not None and early_date not in cohort_dates:
            continue
        if ordered_observation_dates is not None:
            early_index = observation_index.get(early_date)
            if (
                early_index is None
                or early_index + 20 >= len(ordered_observation_dates)
            ):
                right_censored_count += 1
                continue
            item["window_end"] = ordered_observation_dates[early_index + 20]
        else:
            item["window_end"] = None
        eligible_structures.append(item)

    early_count = sum(item["early"] for item in eligible_structures)
    r1_count = sum(
        item["early"] and _event_within_window(item, "r1")
        for item in eligible_structures
    )
    r2_count = sum(
        item["early"]
        and _event_within_window(item, "r1")
        and _event_within_window(item, "r2")
        for item in eligible_structures
    )
    return {
        "early_count": early_count,
        "r1_count": r1_count,
        "r2_count": r2_count,
        "right_censored_count": right_censored_count,
        "early_to_r1": r1_count / early_count if early_count else 0.0,
        "r1_to_r2": r2_count / r1_count if r1_count else 0.0,
        "early_to_r2": r2_count / early_count if early_count else 0.0,
    }


def _event_within_window(state: Dict[str, Any], event_name: str) -> bool:
    event_date = state.get(f"{event_name}_date")
    if event_date is None:
        return bool(state.get(event_name)) if state.get("window_end") is None else False
    early_date = state["early_date"]
    window_end = state.get("window_end")
    return event_date >= early_date and (
        window_end is None or event_date <= window_end
    )


def wilson_lower_bound(successes: int, trials: int) -> float:
    """Standard two-sided 95% Wilson score interval lower endpoint."""
    if trials <= 0:
        return 0.0
    if successes < 0 or successes > trials:
        raise ValueError("successes must be between zero and trials")
    z = 1.959963984540054
    proportion = successes / trials
    z_squared = z * z
    denominator = 1.0 + z_squared / trials
    centre = proportion + z_squared / (2.0 * trials)
    margin = z * math.sqrt(
        proportion * (1.0 - proportion) / trials
        + z_squared / (4.0 * trials * trials)
    )
    return (centre - margin) / denominator


_CLUSTER_PCT_VALUES = (0.01, 0.015, 0.02)
_ATR_GAP_VALUES = (0.5, 0.75)
_ZONE_SCORE_MIN_VALUES = (0.4, 0.45, 0.5)
_STAGES = ("early", "r1", "r2")


def canonical_parameter_hash(snapshot: Dict[str, float]) -> str:
    """Hash a parameter snapshot using stable JSON bytes."""
    payload = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_parameter_snapshots() -> Dict[str, Dict[str, float]]:
    """Return the immutable preregistered 3×2×3 parameter grid."""
    snapshots: Dict[str, Dict[str, float]] = {}
    for cluster_pct in _CLUSTER_PCT_VALUES:
        for atr_gap_multiplier in _ATR_GAP_VALUES:
            for zone_score_min in _ZONE_SCORE_MIN_VALUES:
                snapshot = {
                    "cluster_pct": cluster_pct,
                    "atr_gap_multiplier": atr_gap_multiplier,
                    "zone_score_min": zone_score_min,
                }
                snapshots[canonical_parameter_hash(snapshot)] = snapshot
    return dict(sorted(snapshots.items()))
