# -*- coding: utf-8 -*-
"""Group summary aggregator for five-layer backtest evaluations.

Computes overall, single-dimension, and combo group summaries from
the candidate-level evaluation fact table. Uses snapshot fields
(not replayed) for historical_snapshot mode grouping.
"""
from __future__ import annotations

import json
import hashlib
import logging
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.backtest.aggregators.sample_threshold import SampleThresholdGate, ThresholdResult
from src.backtest.aggregators.stability_metrics import StabilityMetricsCalculator
from src.backtest.models.backtest_models import (
    FiveLayerBacktestEvaluation,
    FiveLayerBacktestGroupSummary,
)
from src.backtest.repositories.evaluation_repo import EvaluationRepository
from src.backtest.repositories.summary_repo import SummaryRepository

logger = logging.getLogger(__name__)

# ── Grouping dimensions ─────────────────────────────────────────────────────

SINGLE_DIMENSIONS = (
    "signal_family",
    "snapshot_setup_type",
    "snapshot_market_regime",
    "snapshot_theme_position",
    "snapshot_candidate_pool_level",
    "snapshot_entry_maturity",
    "snapshot_trade_stage",
)

SPLIT_BY_SIGNAL_FAMILY_DIMENSIONS = tuple(
    dim for dim in SINGLE_DIMENSIONS if dim != "signal_family"
)

COMBO_PAIRS: List[Tuple[str, str]] = [
    ("snapshot_theme_position", "snapshot_setup_type"),
    ("snapshot_candidate_pool_level", "snapshot_entry_maturity"),
]

STRATEGY_COHORT_DIMENSIONS = (
    "snapshot_market_regime",
    "snapshot_candidate_pool_level",
    "snapshot_entry_maturity",
)

# Map field name → group_type label
_DIMENSION_TO_GROUP_TYPE = {
    "signal_family": "signal_family",
    "snapshot_setup_type": "setup_type",
    "snapshot_market_regime": "market_regime",
    "snapshot_theme_position": "theme_position",
    "snapshot_candidate_pool_level": "candidate_pool_level",
    "snapshot_entry_maturity": "entry_maturity",
    "snapshot_trade_stage": "trade_stage",
}

# Signal strength band thresholds for factor_snapshot-based grouping
_STRENGTH_BANDS = (
    ("weak", 0.0, 0.3),
    ("medium", 0.3, 0.6),
    ("strong", 0.6, 1.01),  # 1.01 to include 1.0
)


class GroupSummaryAggregator:
    """Computes and persists group summaries for a backtest run.

    Produces five kinds of summaries:
      1. overall  — single row aggregating all evaluations
      2. single-dimension — one row per unique value of each dimension
      3. single-dimension split by signal family — one row per unique value+family
      4. combo — one row per unique (dim_a, dim_b) pair
      5. strategy_cohort — one row per strategy/bucket/context combination
    """

    def __init__(
        self,
        eval_repo: Optional[EvaluationRepository] = None,
        summary_repo: Optional[SummaryRepository] = None,
    ):
        self.eval_repo = eval_repo or EvaluationRepository()
        self.summary_repo = summary_repo or SummaryRepository()

    def compute_all_summaries(
        self,
        backtest_run_id: str,
    ) -> List[FiveLayerBacktestGroupSummary]:
        """Compute and persist all group summaries for a run.

        Returns list of all persisted summary records.
        """
        evaluations = self.eval_repo.get_by_run(backtest_run_id)
        if not evaluations:
            logger.warning("No evaluations found for run %s", backtest_run_id)
            return []

        results: List[FiveLayerBacktestGroupSummary] = []

        # 1. Overall summary
        overall = self._persist_group(
            backtest_run_id, "overall", "all", evaluations,
        )
        if overall:
            results.append(overall)

        # 2. Single-dimension summaries
        for dim in SINGLE_DIMENSIONS:
            group_type = _DIMENSION_TO_GROUP_TYPE.get(dim, dim)
            grouped = _group_by_field(evaluations, dim)
            for group_key, evals in grouped.items():
                summary = self._persist_group(
                    backtest_run_id, group_type, group_key, evals,
                )
                if summary:
                    results.append(summary)

        # 2b. Single-dimension summaries split by signal family
        for dim in SPLIT_BY_SIGNAL_FAMILY_DIMENSIONS:
            base_group_type = _DIMENSION_TO_GROUP_TYPE.get(dim, dim)
            split_group_type = f"{base_group_type}_signal_family"
            grouped = _group_by_combo(evaluations, dim, "signal_family")
            for group_key, evals in grouped.items():
                summary = self._persist_group(
                    backtest_run_id, split_group_type, group_key, evals,
                )
                if summary:
                    results.append(summary)

        # 3. Combo summaries
        for dim_a, dim_b in COMBO_PAIRS:
            group_type = "combo"
            grouped = _group_by_combo(evaluations, dim_a, dim_b)
            for group_key, evals in grouped.items():
                summary = self._persist_group(
                    backtest_run_id, group_type, group_key, evals,
                )
                if summary:
                    results.append(summary)

        # 4. Strategy cohort summaries
        strategy_cohorts = _group_by_strategy_cohort(evaluations)
        for group_key, evals in strategy_cohorts.items():
            summary = self._persist_group(
                backtest_run_id, "strategy_cohort", group_key, evals,
            )
            if summary:
                results.append(summary)

        # 5. Pattern code summaries (from factor_snapshot_json)
        pattern_groups = _group_by_pattern_code(evaluations)
        for group_key, evals in pattern_groups.items():
            summary = self._persist_group(
                backtest_run_id, "pattern_code", group_key, evals,
            )
            if summary:
                results.append(summary)

        # 6. Signal strength band summaries (from factor_snapshot_json)
        strength_groups = _group_by_signal_strength_band(evaluations)
        for group_key, evals in strength_groups.items():
            summary = self._persist_group(
                backtest_run_id, "signal_strength_band", group_key, evals,
            )
            if summary:
                results.append(summary)

        # 7. AI override status summaries (from metrics_json)
        ai_groups = _group_by_ai_override(evaluations)
        for group_key, evals in ai_groups.items():
            summary = self._persist_group(
                backtest_run_id, "ai_override", group_key, evals,
            )
            if summary:
                results.append(summary)

        logger.info(
            "Computed %d summaries for run %s", len(results), backtest_run_id,
        )
        return results

    def _persist_group(
        self,
        backtest_run_id: str,
        group_type: str,
        group_key: str,
        evaluations: List[FiveLayerBacktestEvaluation],
    ) -> Optional[FiveLayerBacktestGroupSummary]:
        """Aggregate a group of evaluations and persist to DB.

        Always persists the summary. Threshold info is stored as advisory
        metadata in metrics_json — gating is the RecommendationEngine's job.
        """
        metrics = aggregate_group(evaluations)
        if metrics is None:
            return None

        threshold = SampleThresholdGate.check(metrics["aggregatable_sample_count"])

        # Store threshold info in metrics_json (advisory, not a gate)
        extra = {
            "sample_baseline": metrics["sample_baseline"],
            "threshold_check": {
                "can_display": threshold.can_display,
                "can_suggest": threshold.can_suggest,
                "can_action": threshold.can_action,
                "reason": threshold.reason,
            },
        }
        # Always expose family_breakdown for mixed-capable group types so
        # downstream consumers can read family-correct metrics
        # (avg_return_pct/win_rate_pct/profit_factor/stage_accuracy_rate) per
        # signal_family without falling back to the legacy mixed fields.
        # ``signal_family`` rows are themselves single-family — skip there to
        # avoid a redundant nested object.
        if group_type != "signal_family":
            family_breakdown = _build_family_breakdown(evaluations)
            if family_breakdown:
                extra["family_breakdown"] = family_breakdown
        if group_type == "strategy_cohort":
            extra["strategy_cohort_context"] = _build_strategy_cohort_context(evaluations)

        return self.summary_repo.upsert_summary(
            backtest_run_id=backtest_run_id,
            group_type=group_type,
            group_key=group_key,
            sample_count=metrics["sample_count"],
            avg_return_pct=metrics["avg_return_pct"],
            median_return_pct=metrics["median_return_pct"],
            win_rate_pct=metrics["win_rate_pct"],
            avg_mae=metrics["avg_mae"],
            avg_mfe=metrics["avg_mfe"],
            avg_drawdown=metrics["avg_drawdown"],
            p25_return_pct=metrics["p25_return_pct"],
            p75_return_pct=metrics["p75_return_pct"],
            extreme_sample_ratio=metrics["extreme_sample_ratio"],
            time_bucket_stability=metrics["time_bucket_stability"],
            profit_factor=metrics["profit_factor"],
            avg_holding_days=metrics["avg_holding_days"],
            max_consecutive_losses=metrics["max_consecutive_losses"],
            plan_execution_rate=metrics["plan_execution_rate"],
            stage_accuracy_rate=metrics["stage_accuracy_rate"],
            metrics_json=json.dumps(extra, ensure_ascii=False),
            computed_at=datetime.now(),
        )


# ── Pure aggregation functions ──────────────────────────────────────────────

def aggregate_group(
    evaluations: List[FiveLayerBacktestEvaluation],
    family_filter: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Compute aggregate metrics for a group of evaluations.

    Args:
        evaluations: candidate-level evaluation rows.
        family_filter: when provided, only the matching ``signal_family`` rows
            are aggregated. ``None`` keeps the legacy mixed-family behaviour
            (kept for backward compatibility — read ``family_breakdown`` for
            the family-aware metrics).

    DEPRECATED MIX (family_filter=None):
        ``avg_return_pct / win_rate_pct / profit_factor / stage_accuracy_rate``
        below mix entry's forward_return_5d with observation's risk_avoided_pct
        and combine ``win`` + ``correct_wait`` into the same numerator. They are
        kept for legacy callers; new consumers MUST read the per-family metrics
        from ``family_breakdown[family]`` instead, which uses the correct
        per-family return source and outcome set.

    Returns None if no evaluations.
    """
    raw_count = len(evaluations)
    if raw_count == 0:
        return None

    if family_filter is not None:
        evaluations = [e for e in evaluations if e.signal_family == family_filter]
        if not evaluations:
            return None
        raw_count = len(evaluations)

    sample_baseline = _build_sample_baseline(evaluations)

    # Filter to evaluated entries with a return metric
    valid = [e for e in evaluations if _is_aggregatable(e)]
    aggregatable_count = len(valid)

    # Primary return source depends on which family we are aggregating.
    # - family_filter='entry'      → only forward_return_5d (directional pnl)
    # - family_filter='observation'→ only risk_avoided_pct (avoided drawdown)
    # - family_filter=None         → legacy mix (forward_return_5d for entry,
    #   risk_avoided_pct for observation), preserved for backward compatibility
    returns = []
    for e in valid:
        if family_filter == "entry":
            if e.forward_return_5d is not None:
                returns.append(e.forward_return_5d)
        elif family_filter == "observation":
            if e.risk_avoided_pct is not None:
                returns.append(e.risk_avoided_pct)
        else:
            if e.forward_return_5d is not None:
                returns.append(e.forward_return_5d)
            elif e.risk_avoided_pct is not None:
                returns.append(e.risk_avoided_pct)

    if not returns:
        return {
            "sample_count": raw_count,
            "aggregatable_sample_count": aggregatable_count,
            "sample_baseline": sample_baseline,
            "avg_return_pct": None,
            "median_return_pct": None,
            "win_rate_pct": None,
            "avg_mae": None,
            "avg_mfe": None,
            "avg_drawdown": None,
            "p25_return_pct": None,
            "p75_return_pct": None,
            "extreme_sample_ratio": None,
            "time_bucket_stability": None,
            "profit_factor": None,
            "avg_holding_days": None,
            "max_consecutive_losses": None,
            "plan_execution_rate": None,
            "stage_accuracy_rate": None,
        }

    # Trade dates for stability
    trade_dates = [e.trade_date for e in valid if e.trade_date is not None]

    # Win-outcome set depends on what family we are aggregating:
    # - entry only:        winners are outcome=='win'
    # - observation only:  winners are outcome=='correct_wait'
    # - mixed (legacy):    union of both, kept for backward compatibility but
    #                      cross-language ("win" and "correct_wait" mean
    #                      different things). Prefer family_breakdown.
    if family_filter == "entry":
        _WIN_OUTCOMES = frozenset({"win"})
    elif family_filter == "observation":
        _WIN_OUTCOMES = frozenset({"correct_wait"})
    else:
        _WIN_OUTCOMES = frozenset({"win", "correct_wait"})
    win_count = sum(1 for e in valid if e.outcome in _WIN_OUTCOMES)
    win_rate = (win_count / aggregatable_count) * 100 if aggregatable_count > 0 else 0.0

    # MAE / MFE / Drawdown
    mae_vals = [_as_number(e.mae) for e in valid]
    mae_vals = [value for value in mae_vals if value is not None]
    mfe_vals = [_as_number(e.mfe) for e in valid]
    mfe_vals = [value for value in mfe_vals if value is not None]
    dd_vals = [_as_number(e.max_drawdown_from_peak) for e in valid]
    dd_vals = [value for value in dd_vals if value is not None]
    holding_vals = [
        value for value in (_as_number(e.holding_days) for e in valid)
        if value is not None and value > 0
    ]

    # Stability metrics
    stability = StabilityMetricsCalculator.compute(returns, trade_dates)

    positive_returns = [r for r in returns if r > 0]
    negative_returns = [r for r in returns if r < 0]
    total_positive = sum(positive_returns) if positive_returns else 0
    total_negative = abs(sum(negative_returns)) if negative_returns else 0
    profit_factor = round(total_positive / total_negative, 4) if total_negative > 0 else None

    sorted_evals = sorted(
        [e for e in valid if e.trade_date is not None and isinstance(e.outcome, str)],
        key=lambda e: (e.trade_date, e.code),
    )
    max_consecutive_losses = 0
    current_streak = 0
    for evaluation in sorted_evals:
        if evaluation.outcome == "loss":
            current_streak += 1
            max_consecutive_losses = max(max_consecutive_losses, current_streak)
            continue
        current_streak = 0

    plan_evals = [e for e in valid if _as_bool(e.plan_success) is not None]
    plan_execution_rate = (
        round(sum(1 for e in plan_evals if _as_bool(e.plan_success)) / len(plan_evals), 4)
        if plan_evals else None
    )

    stage_correct = 0
    stage_total = 0
    for evaluation in valid:
        if evaluation.signal_family == "entry" and evaluation.forward_return_5d is not None:
            stage_total += 1
            if evaluation.forward_return_5d > 0:
                stage_correct += 1
        elif evaluation.signal_family == "observation" and _as_bool(evaluation.stage_success) is not None:
            stage_total += 1
            if _as_bool(evaluation.stage_success):
                stage_correct += 1

    return {
        "sample_count": raw_count,
        "aggregatable_sample_count": aggregatable_count,
        "sample_baseline": sample_baseline,
        "avg_return_pct": round(statistics.mean(returns), 4) if returns else None,
        "median_return_pct": stability.median,
        "win_rate_pct": round(win_rate, 2),
        "avg_mae": round(statistics.mean(mae_vals), 4) if mae_vals else None,
        "avg_mfe": round(statistics.mean(mfe_vals), 4) if mfe_vals else None,
        "avg_drawdown": round(statistics.mean(dd_vals), 4) if dd_vals else None,
        "p25_return_pct": stability.p25,
        "p75_return_pct": stability.p75,
        "extreme_sample_ratio": stability.extreme_sample_ratio,
        "time_bucket_stability": stability.time_bucket_stability,
        "profit_factor": profit_factor,
        "avg_holding_days": round(statistics.mean(holding_vals), 2) if holding_vals else None,
        "max_consecutive_losses": max_consecutive_losses,
        "plan_execution_rate": plan_execution_rate,
        "stage_accuracy_rate": round(stage_correct / stage_total, 4) if stage_total > 0 else None,
    }


def _is_aggregatable(evaluation: FiveLayerBacktestEvaluation) -> bool:
    return evaluation.forward_return_5d is not None or evaluation.risk_avoided_pct is not None


def _build_sample_baseline(
    evaluations: List[FiveLayerBacktestEvaluation],
) -> Dict[str, Any]:
    """Build an explainable sample baseline for one run/group."""
    entry_count = sum(1 for item in evaluations if item.signal_family == "entry")
    observation_count = sum(1 for item in evaluations if item.signal_family == "observation")
    suppressed_reasons: Dict[str, int] = defaultdict(int)
    aggregatable_count = 0

    for evaluation in evaluations:
        if _is_aggregatable(evaluation):
            aggregatable_count += 1
            continue
        reason = _infer_suppression_reason(evaluation)
        suppressed_reasons[reason] += 1

    raw_count = len(evaluations)
    suppressed_count = raw_count - aggregatable_count

    return {
        "raw_sample_count": raw_count,
        "evaluated_sample_count": raw_count,
        "aggregatable_sample_count": aggregatable_count,
        "entry_sample_count": entry_count,
        "observation_sample_count": observation_count,
        "suppressed_sample_count": suppressed_count,
        "suppressed_reasons": dict(suppressed_reasons),
    }


def _infer_suppression_reason(
    evaluation: FiveLayerBacktestEvaluation,
) -> str:
    """Resolve the explanatory code for why an evaluation is non-aggregatable.

    Prefers the ``suppression_reason`` column that the evaluators write
    explicitly (E2). Falls back to inferring from the missing metric column
    for legacy rows produced before the column existed.
    """
    persisted = getattr(evaluation, "suppression_reason", None)
    if persisted:
        return persisted

    signal_family = str(evaluation.signal_family or "").lower()
    if signal_family == "observation":
        return "missing_risk_avoided_pct"
    if signal_family == "entry":
        return "missing_forward_return_5d"
    return "missing_primary_metric"


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    return None


def _group_by_field(
    evaluations: List[FiveLayerBacktestEvaluation],
    field: str,
) -> Dict[str, List[FiveLayerBacktestEvaluation]]:
    """Group evaluations by a single snapshot field value."""
    groups: Dict[str, List[FiveLayerBacktestEvaluation]] = defaultdict(list)
    for e in evaluations:
        val = getattr(e, field, None)
        if val is not None:
            groups[str(val)].append(e)
    return dict(groups)


def _group_by_combo(
    evaluations: List[FiveLayerBacktestEvaluation],
    field_a: str,
    field_b: str,
) -> Dict[str, List[FiveLayerBacktestEvaluation]]:
    """Group evaluations by a combination of two snapshot fields."""
    groups: Dict[str, List[FiveLayerBacktestEvaluation]] = defaultdict(list)
    for e in evaluations:
        val_a = getattr(e, field_a, None)
        val_b = getattr(e, field_b, None)
        if val_a is not None and val_b is not None:
            combo_key = f"{field_a}={val_a}|{field_b}={val_b}"
            groups[combo_key].append(e)
    return dict(groups)


def _build_family_breakdown(
    evaluations: List[FiveLayerBacktestEvaluation],
) -> Dict[str, Dict[str, Any]]:
    """Build per-family metrics so mixed/legacy fields can be re-derived.

    For each ``signal_family`` present in ``evaluations``, run
    ``aggregate_group(..., family_filter=family)`` so that the per-family
    metrics use the family-correct return source (forward_return_5d for entry,
    risk_avoided_pct for observation) and outcome set (``win`` for entry,
    ``correct_wait`` for observation), avoiding the cross-family contamination
    that the legacy mixed metrics suffer from.
    """
    grouped = _group_by_field(evaluations, "signal_family")
    breakdown: Dict[str, Dict[str, Any]] = {}
    for family, family_evals in grouped.items():
        metrics = aggregate_group(family_evals, family_filter=family)
        if metrics is not None:
            breakdown[family] = metrics
    return breakdown


def _group_by_strategy_cohort(
    evaluations: List[FiveLayerBacktestEvaluation],
) -> Dict[str, List[FiveLayerBacktestEvaluation]]:
    """Group evaluations by primary strategy, sample bucket and snapshot context."""
    groups: Dict[str, List[FiveLayerBacktestEvaluation]] = defaultdict(list)
    for evaluation in evaluations:
        evidence = _load_json_dict(evaluation.evidence_json)
        metrics = _load_json_dict(evaluation.metrics_json)
        primary_strategy = str(evidence.get("primary_strategy") or "").strip()
        sample_bucket = str(metrics.get("sample_bucket") or "").strip()
        cohort_values = {
            dimension: getattr(evaluation, dimension, None)
            for dimension in STRATEGY_COHORT_DIMENSIONS
        }
        if (
            not primary_strategy
            or not sample_bucket
            or any(value is None for value in cohort_values.values())
        ):
            continue
        group_key = _build_strategy_cohort_key(
            primary_strategy=primary_strategy,
            sample_bucket=sample_bucket,
            cohort_values=cohort_values,
        )
        groups[group_key].append(evaluation)
    return dict(groups)


def _load_json_dict(payload: Optional[str]) -> Dict[str, Any]:
    """Parse a JSON object field defensively."""
    if not payload:
        return {}
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_strategy_cohort_context(
    evaluations: List[FiveLayerBacktestEvaluation],
) -> Dict[str, Any]:
    """Expose the full readable context behind a short strategy cohort key."""
    if not evaluations:
        return {}
    evaluation = evaluations[0]
    evidence = _load_json_dict(evaluation.evidence_json)
    metrics = _load_json_dict(evaluation.metrics_json)
    return {
        "primary_strategy": evidence.get("primary_strategy"),
        "sample_bucket": metrics.get("sample_bucket"),
        "snapshot_market_regime": getattr(evaluation, "snapshot_market_regime", None),
        "snapshot_candidate_pool_level": getattr(evaluation, "snapshot_candidate_pool_level", None),
        "snapshot_entry_maturity": getattr(evaluation, "snapshot_entry_maturity", None),
    }


def _build_strategy_cohort_key(
    primary_strategy: Any,
    sample_bucket: Any,
    cohort_values: Dict[str, Any],
) -> str:
    """Build a stable cohort key that stays within the DB column limit."""
    normalized = {
        "ps": str(primary_strategy).strip(),
        "sb": str(sample_bucket).strip(),
        "mr": str(cohort_values["snapshot_market_regime"]).strip(),
        "cp": str(cohort_values["snapshot_candidate_pool_level"]).strip(),
        "em": str(cohort_values["snapshot_entry_maturity"]).strip(),
    }
    full_key = "|".join(f"{key}={value}" for key, value in normalized.items())
    if len(full_key) <= 128:
        return full_key

    digest = hashlib.sha1(full_key.encode("utf-8")).hexdigest()[:10]
    shortened = {
        "ps": normalized["ps"][:24],
        "sb": normalized["sb"][:12],
        "mr": normalized["mr"][:16],
        "cp": normalized["cp"][:16],
        "em": normalized["em"][:16],
        "h": digest,
    }
    return "|".join(f"{key}={value}" for key, value in shortened.items())


def _group_by_pattern_code(
    evaluations: List[FiveLayerBacktestEvaluation],
) -> Dict[str, List[FiveLayerBacktestEvaluation]]:
    """Group evaluations by bottom_divergence_pattern_code from factor_snapshot_json."""
    groups: Dict[str, List[FiveLayerBacktestEvaluation]] = defaultdict(list)
    for e in evaluations:
        factor_snapshot = _load_json_dict(e.factor_snapshot_json)
        pattern_code = factor_snapshot.get("bottom_divergence_pattern_code")
        if pattern_code:
            groups[str(pattern_code)].append(e)
    return dict(groups)


def _group_by_signal_strength_band(
    evaluations: List[FiveLayerBacktestEvaluation],
) -> Dict[str, List[FiveLayerBacktestEvaluation]]:
    """Group evaluations by bottom_divergence_signal_strength bands from factor_snapshot_json."""
    groups: Dict[str, List[FiveLayerBacktestEvaluation]] = defaultdict(list)
    for e in evaluations:
        factor_snapshot = _load_json_dict(e.factor_snapshot_json)
        strength = factor_snapshot.get("bottom_divergence_signal_strength")
        if strength is None:
            continue
        try:
            strength_val = float(strength)
        except (TypeError, ValueError):
            continue
        for band_name, low, high in _STRENGTH_BANDS:
            if low <= strength_val < high:
                groups[band_name].append(e)
                break
    return dict(groups)


def _group_by_ai_override(
    evaluations: List[FiveLayerBacktestEvaluation],
) -> Dict[str, List[FiveLayerBacktestEvaluation]]:
    """Group evaluations by AI override status from metrics_json."""
    groups: Dict[str, List[FiveLayerBacktestEvaluation]] = defaultdict(list)
    for e in evaluations:
        metrics = _load_json_dict(e.metrics_json)
        ai_overridden = metrics.get("ai_overridden")
        if ai_overridden is None:
            continue
        key = "ai_overridden" if ai_overridden else "ai_not_overridden"
        groups[key].append(e)
    return dict(groups)
