# -*- coding: utf-8 -*-
"""Pure aggregation metrics for bottom-divergence v2 validation."""
from __future__ import annotations

import statistics
from datetime import date
from typing import Any, Dict, Optional, Sequence

from .bottom_divergence_v2_models import (
    CandidateEventEvidence,
    SampleSplit,
    ValidationSample,
    _STAGES,
    assign_tertile,
    compute_conversion_metrics,
    compute_equity_max_drawdown,
    compute_mae_mfe,
    compute_sample_returns,
    is_false_breakout,
    is_mature_sample,
    resolved_position_weight,
    wilson_lower_bound,
)


def _normalize_stage(stage: str) -> str:
    normalized = stage.strip().lower()
    aliases = {
        "near": "r1",
        "near_cleared": "r1",
        "major": "r2",
        "major_actionable": "r2",
    }
    return aliases.get(normalized, normalized)


def deduplicate_validation_samples(
    samples: Sequence[ValidationSample],
) -> tuple[ValidationSample, ...]:
    """Keep the first event for each strategy/candidate/stage identity."""
    first_by_key: dict[
        tuple[str, str, str, str],
        ValidationSample,
    ] = {}
    for sample in samples:
        key = (
            sample.strategy_version,
            sample.code,
            sample.candidate_version,
            _normalize_stage(sample.stage),
        )
        current = first_by_key.get(key)
        if current is None or (sample.signal_date, repr(sample)) < (
            current.signal_date,
            repr(current),
        ):
            first_by_key[key] = sample
    return tuple(
        sorted(
            first_by_key.values(),
            key=lambda item: (
                item.signal_date,
                item.strategy_version,
                item.code,
                item.candidate_version,
                _normalize_stage(item.stage),
                repr(item),
            ),
        )
    )


def _mean(values: Sequence[float]) -> Optional[float]:
    return statistics.fmean(values) if values else None


def _median(values: Sequence[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def _return_values(
    samples: Sequence[ValidationSample],
    horizon: int,
    *,
    buy_cost_bps: float,
    sell_cost_bps: float,
    slippage_bps: float,
) -> list[float]:
    values: list[float] = []
    key = f"net_return_{horizon}d"
    for sample in samples:
        value = compute_sample_returns(
            sample,
            buy_cost_bps=buy_cost_bps,
            sell_cost_bps=sell_cost_bps,
            slippage_bps=slippage_bps,
        )[key]
        if value is not None:
            values.append(value)
    return values


def _group_metrics(
    samples: Sequence[ValidationSample],
    boundaries: Dict[str, tuple[Optional[float], Optional[float]]],
    *,
    buy_cost_bps: float,
    sell_cost_bps: float,
    slippage_bps: float,
) -> Dict[str, Any]:
    grouped: Dict[tuple[str, str], list[ValidationSample]] = {}
    for sample in samples:
        labels = {
            "market_regime": sample.market_regime,
            "volatility": assign_tertile(
                sample.volatility,
                boundaries["volatility"],
            ),
            "liquidity": assign_tertile(
                sample.liquidity,
                boundaries["liquidity"],
            ),
        }
        for dimension, label in labels.items():
            grouped.setdefault((dimension, str(label)), []).append(sample)

    groups = []
    for (dimension, label), members in sorted(grouped.items()):
        returns = _return_values(
            members,
            10,
            buy_cost_bps=buy_cost_bps,
            sell_cost_bps=sell_cost_bps,
            slippage_bps=slippage_bps,
        )
        expectancy = _mean(returns)
        groups.append(
            {
                "dimension": dimension,
                "label": label,
                "sample_count": len(members),
                "eligible": (
                    len(members) >= 30
                    and label != "unknown"
                ),
                "mean_net_return_10d": expectancy,
                "positive": expectancy is not None and expectancy > 0.0,
            }
        )
    eligible = [item for item in groups if item["eligible"]]
    positive_count = sum(item["positive"] for item in eligible)
    return {
        "items": groups,
        "eligible_count": len(eligible),
        "excluded_count": len(groups) - len(eligible),
        "positive_count": positive_count,
        "positive_ratio": (
            positive_count / len(eligible) if eligible else 0.0
        ),
    }


def _stage_metrics(
    samples: Sequence[ValidationSample],
    *,
    opportunity_count: int,
    boundaries: Dict[str, tuple[Optional[float], Optional[float]]],
    buy_cost_bps: float,
    sell_cost_bps: float,
    slippage_bps: float,
) -> Dict[str, Any]:
    immature_count = sum(
        item.is_executable and not is_mature_sample(item)
        for item in samples
    )
    invalid_position_count = sum(
        item.is_executable
        and is_mature_sample(item)
        and resolved_position_weight(item) is None
        for item in samples
    )
    execution_samples = [
        item
        for item in samples
        if item.is_executable
        and is_mature_sample(item)
        and resolved_position_weight(item) is not None
    ]
    returns = {
        horizon: _return_values(
            execution_samples,
            horizon,
            buy_cost_bps=buy_cost_bps,
            sell_cost_bps=sell_cost_bps,
            slippage_bps=slippage_bps,
        )
        for horizon in (5, 10, 20)
    }
    wins = [value for value in returns[10] if value > 0.0]
    losses = [value for value in returns[10] if value < 0.0]
    mae_values = [
        value
        for value in (
            compute_mae_mfe(item)["mae"] for item in execution_samples
        )
        if value is not None
    ]
    mae_5d_values = [
        value
        for value in (
            compute_mae_mfe(item)["mae_5d"] for item in execution_samples
        )
        if value is not None
    ]
    mfe_values = [
        value
        for value in (
            compute_mae_mfe(item)["mfe"] for item in execution_samples
        )
        if value is not None
    ]
    conversion = compute_conversion_metrics(samples)
    false_values = [
        is_false_breakout(item) for item in execution_samples
    ]
    known_false_values = [value for value in false_values if value is not None]
    false_breakout_count = sum(bool(value) for value in known_false_values)
    return {
        "signal_count": len(samples),
        "sample_count": len(execution_samples),
        "immature_count": immature_count,
        "invalid_position_count": invalid_position_count,
        "coverage": len(samples) / opportunity_count,
        "mean_net_return_5d": _mean(returns[5]),
        "median_net_return_5d": _median(returns[5]),
        "mean_net_return_10d": _mean(returns[10]),
        "median_net_return_10d": _median(returns[10]),
        "mean_net_return_20d": _mean(returns[20]),
        "median_net_return_20d": _median(returns[20]),
        "win_rate": len(wins) / len(returns[10]) if returns[10] else 0.0,
        "payoff_ratio": (
            statistics.fmean(wins) / abs(statistics.fmean(losses))
            if wins and losses
            else None
        ),
        "mean_mae": _mean(mae_values),
        "median_mae": _median(mae_values),
        "mean_mae_5d": _mean(mae_5d_values),
        "median_mae_5d": _median(mae_5d_values),
        "mean_mfe": _mean(mfe_values),
        "median_mfe": _median(mfe_values),
        "max_drawdown": compute_equity_max_drawdown(
            execution_samples,
            buy_cost_bps=buy_cost_bps,
            sell_cost_bps=sell_cost_bps,
            slippage_bps=slippage_bps,
        ),
        "false_breakout_count": false_breakout_count,
        "false_breakout_denominator": len(known_false_values),
        "missing_floor_count": len(false_values) - len(known_false_values),
        "false_breakout_rate": (
            false_breakout_count / len(known_false_values) * 100.0
            if known_false_values
            else None
        ),
        "conversion": conversion,
        "groups": _group_metrics(
            execution_samples,
            boundaries,
            buy_cost_bps=buy_cost_bps,
            sell_cost_bps=sell_cost_bps,
            slippage_bps=slippage_bps,
        ),
    }


def summarize_validation_samples(
    samples: Sequence[ValidationSample],
    *,
    boundaries: Dict[str, tuple[Optional[float], Optional[float]]],
    opportunity_count: int,
    conversion_samples: Optional[Sequence[ValidationSample]] = None,
    conversion_dates: Optional[set[date]] = None,
    conversion_observation_dates: Optional[Sequence[date]] = None,
    maturation_evidence: Sequence[CandidateEventEvidence] = (),
    buy_cost_bps: float,
    sell_cost_bps: float,
    slippage_bps: float,
) -> Dict[str, Any]:
    if opportunity_count <= 0:
        raise ValueError("opportunity_count must be positive")
    samples = deduplicate_validation_samples(samples)
    normalized_conversion_samples = deduplicate_validation_samples(
        conversion_samples if conversion_samples is not None else samples
    )
    result = {}
    cross_stage_conversion = compute_conversion_metrics(
        normalized_conversion_samples,
        cohort_dates=conversion_dates,
        observation_dates=conversion_observation_dates,
        maturation_evidence=maturation_evidence,
    )
    for stage in _STAGES:
        stage_samples = [
            item for item in samples if _normalize_stage(item.stage) == stage
        ]
        result[stage] = _stage_metrics(
            stage_samples,
            opportunity_count=opportunity_count,
            boundaries=boundaries,
            buy_cost_bps=buy_cost_bps,
            sell_cost_bps=sell_cost_bps,
            slippage_bps=slippage_bps,
        )
        result[stage]["conversion"] = cross_stage_conversion
    execution_samples = [
        item
        for item in samples
        if item.is_executable
        and is_mature_sample(item)
        and resolved_position_weight(item) is not None
    ]
    overall_false_values = [
        is_false_breakout(item) for item in execution_samples
    ]
    overall_known_false = [
        value for value in overall_false_values if value is not None
    ]
    overall_false_count = sum(bool(value) for value in overall_known_false)
    overall_returns = _return_values(
        execution_samples,
        10,
        buy_cost_bps=buy_cost_bps,
        sell_cost_bps=sell_cost_bps,
        slippage_bps=slippage_bps,
    )
    result["overall"] = {
        "signal_count": len(samples),
        "sample_count": len(execution_samples),
        "mean_net_return_10d": _mean(overall_returns),
        "max_drawdown": compute_equity_max_drawdown(
            execution_samples,
            buy_cost_bps=buy_cost_bps,
            sell_cost_bps=sell_cost_bps,
            slippage_bps=slippage_bps,
        ),
        "false_breakout_rate": (
            overall_false_count / len(overall_known_false) * 100.0
            if overall_known_false
            else None
        ),
        "false_breakout_denominator": len(overall_known_false),
        "missing_floor_count": (
            len(overall_false_values) - len(overall_known_false)
        ),
    }
    return result


def evaluate_noninferiority_gates(
    v1_metrics: Dict[str, Any],
    v2_metrics: Dict[str, Any],
    *,
    train_conversion_lower: float,
    minimum_stage_samples: int = 100,
) -> Dict[str, Any]:
    """Apply all preregistered release gates without substituting proxies."""
    reasons: list[str] = []
    for stage in _STAGES:
        if v2_metrics[stage]["sample_count"] < minimum_stage_samples:
            reasons.append(f"insufficient_test_samples:{stage}:v2")
    if v1_metrics["r2"]["sample_count"] < minimum_stage_samples:
        reasons.append("insufficient_test_samples:r2:v1")

    v1_expectancy = v1_metrics["r2"]["mean_net_return_10d"]
    v2_expectancy = v2_metrics["r2"]["mean_net_return_10d"]
    if v1_expectancy is None or v2_expectancy is None:
        reasons.append("r2_expectancy_unavailable")
    else:
        floor = 0.98 * v1_expectancy if v1_expectancy > 0.0 else v1_expectancy
        if v2_expectancy < floor:
            reasons.append("r2_expectancy_noninferiority")

    if (
        v2_metrics["overall"]["max_drawdown"]
        - v1_metrics["overall"]["max_drawdown"]
        > 2.0
    ):
        reasons.append("max_drawdown_degradation")
    v1_false_breakout = v1_metrics["overall"]["false_breakout_rate"]
    v2_false_breakout = v2_metrics["overall"]["false_breakout_rate"]
    stage_false_breakout_missing = (
        (
            v1_metrics["r2"]["sample_count"] > 0
            and v1_metrics["r2"]["false_breakout_rate"] is None
        )
        or any(
            v2_metrics[stage]["sample_count"] > 0
            and v2_metrics[stage]["false_breakout_rate"] is None
            for stage in _STAGES
        )
    )
    if (
        stage_false_breakout_missing
        or v1_false_breakout is None
        or v2_false_breakout is None
    ):
        reasons.append("missing_false_breakout_baseline_or_candidate")
    elif v2_false_breakout - v1_false_breakout > 3.0:
        reasons.append("false_breakout_degradation")

    r1_expectancy = v2_metrics["r1"]["mean_net_return_10d"]
    if r1_expectancy is None or r1_expectancy <= 0.0:
        reasons.append("r1_expectancy_non_positive")
    v1_r1_mae = v1_metrics["r2"]["median_mae_5d"]
    v2_r1_mae = v2_metrics["r1"]["median_mae_5d"]
    if v1_r1_mae is None or v2_r1_mae is None:
        reasons.append("r1_median_mae_unavailable")
    elif v2_r1_mae < v1_r1_mae - 1.0:
        reasons.append("r1_median_mae_degradation")

    early_expectancy = v2_metrics["early"]["mean_net_return_10d"]
    if early_expectancy is None or early_expectancy <= 0.0:
        reasons.append("early_expectancy_non_positive")
    conversion = v2_metrics["early"]["conversion"]["early_to_r1"]
    if conversion < train_conversion_lower:
        reasons.append("early_conversion_below_train_wilson")

    for stage in _STAGES:
        groups = v2_metrics[stage]["groups"]
        if groups["eligible_count"] == 0:
            reasons.append(f"no_eligible_groups:{stage}")
        elif groups["positive_ratio"] < 0.7:
            reasons.append(f"positive_group_ratio_below_70:{stage}")

    unique_reasons = list(dict.fromkeys(reasons))
    return {
        "eligible": not unique_reasons,
        "passed": not unique_reasons,
        "reasons": unique_reasons,
    }


def _partition_by_dates(
    samples: Sequence[ValidationSample],
    split: SampleSplit,
) -> Dict[str, tuple[ValidationSample, ...]]:
    date_sets = {
        "train": set(split.train_dates),
        "validation": set(split.validation_dates),
        "test": set(split.test_dates),
    }
    return {
        name: tuple(
            sample for sample in samples if sample.signal_date in dates
        )
        for name, dates in date_sets.items()
    }


def _conversion_lower(metrics: Dict[str, Any]) -> float:
    conversion = metrics["early"]["conversion"]
    return wilson_lower_bound(
        conversion["r1_count"],
        conversion["early_count"],
    )
