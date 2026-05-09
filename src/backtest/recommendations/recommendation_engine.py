# -*- coding: utf-8 -*-
"""Recommendation engine for five-layer backtest.

RED LINE: This engine ONLY outputs structured suggestions.
It NEVER modifies production rules, thresholds, classification
mappings, or execution parameters. All 'actionable' recommendations
still require human review or independent replay/calibration
verification before entering any rule-change workflow.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.backtest.aggregators.sample_threshold import (
    ACTIONABLE_MIN,
    OBSERVATION_MIN,
    SUGGESTION_MIN,
    SampleThresholdGate,
    ThresholdResult,
)
from src.backtest.models.backtest_models import (
    FiveLayerBacktestGroupSummary,
    FiveLayerBacktestRecommendation,
)
from src.backtest.recommendations.evidence_builder import EvidenceBuilder
from src.backtest.repositories.evaluation_repo import EvaluationRepository
from src.backtest.repositories.recommendation_repo import RecommendationRepository
from src.backtest.repositories.summary_repo import SummaryRepository
from src.backtest.utils.summary_metrics import (
    compute_family_share,
    get_aggregatable_sample_count,
    get_sample_baseline,
    load_summary_metrics,
)

logger = logging.getLogger(__name__)

# ── Gate thresholds ─────────────────────────────────────────────────────────

TIME_BUCKET_STABILITY_MAX = 0.15   # max stddev of bucket win rates
EXTREME_SAMPLE_RATIO_MAX = 0.10    # max fraction of extreme outliers
WIN_RATE_STRONG_THRESHOLD = 60.0   # win_rate above this = strong signal
WIN_RATE_WEAK_THRESHOLD = 40.0     # win_rate below this = weak signal

# D4: family-share confidence penalties.
#
# When the recommendation inference is anchored on a family that is the
# minority slice of the group (e.g. an entry-family inference on a group
# that is 80 % observation samples), the headline numbers become much less
# reliable and confidence must be discounted accordingly. Two thresholds:
#
# * ``FAMILY_SHARE_STRICT_MIN`` (50 %) — below this, the inferring family
#   is no longer the dominant population. Apply a meaningful penalty.
# * ``FAMILY_SHARE_HARD_MIN`` (20 %) — below this, the inferring family is
#   a small minority and confidence must be heavily discounted regardless
#   of stability/consistency results.
FAMILY_SHARE_STRICT_MIN = 0.50
FAMILY_SHARE_HARD_MIN = 0.20
FAMILY_SHARE_PENALTY_STRICT = 0.10
FAMILY_SHARE_PENALTY_HARD = 0.20
FAMILY_SHARE_DOMINANT_BONUS = 0.05


@dataclass
class RecommendationDraft:
    """Internal draft before gate checks."""

    group_summary: FiveLayerBacktestGroupSummary
    recommendation_type: str
    target_scope: str
    target_key: str
    current_rule: str
    suggested_change: str
    threshold_result: ThresholdResult
    stability_passed: bool
    consistency_passed: bool
    # Which subset of the summary the recommendation was derived from.
    # ``entry`` and ``observation`` come from family_breakdown[family];
    # ``mixed`` falls back to the legacy mixed-family fields (only used for
    # ``signal_family`` rows that are themselves single-family).
    family_scope: str
    inference_metrics: Dict[str, Any]
    # D4: structured snapshot of how each family contributes to this group.
    # Used by ``_compute_confidence`` to discount recommendations whose
    # inferring family is a minority slice of the group's actual sample mix.
    family_share: Dict[str, Any]


class RecommendationEngine:
    """Generates graded recommendations from backtest summaries.

    IMPORTANT — Permission boundary:
      This engine ONLY produces structured suggestions stored in
      five_layer_backtest_recommendations. It has NO write access
      to production config, rules, thresholds, or parameters.

    Grading:
      observation  — sample >= 5, no stability requirement
      hypothesis   — sample >= 20, stability check passed
      actionable   — sample >= 50, stability + consistency + evidence
    """

    def __init__(
        self,
        summary_repo: Optional[SummaryRepository] = None,
        eval_repo: Optional[EvaluationRepository] = None,
        recommendation_repo: Optional[RecommendationRepository] = None,
    ):
        self.summary_repo = summary_repo or SummaryRepository()
        self.eval_repo = eval_repo or EvaluationRepository()
        self.recommendation_repo = recommendation_repo or RecommendationRepository()

    def generate_recommendations(
        self,
        backtest_run_id: str,
    ) -> List[FiveLayerBacktestRecommendation]:
        """Generate all recommendations for a completed run.

        ONLY outputs suggestions. NEVER modifies rules/thresholds/parameters.

        Steps:
          1. Load all group summaries (excluding 'overall')
          2. For each group, evaluate whether a recommendation is warranted
          3. Apply gates to determine recommendation level
          4. Build evidence chain
          5. Persist and return
        """
        summaries = self.summary_repo.get_by_run(backtest_run_id)
        if not summaries:
            logger.info("No summaries for run %s, skipping recommendations", backtest_run_id)
            return []

        recommendations: List[FiveLayerBacktestRecommendation] = []

        for summary in summaries:
            if summary.group_type == "overall":
                continue

            draft = self._evaluate_group(summary)
            if draft is None:
                continue

            rec = self._build_recommendation(backtest_run_id, draft)
            if rec is not None:
                recommendations.append(rec)

        if recommendations:
            self.recommendation_repo.save_batch(recommendations)
            logger.info(
                "Generated %d recommendations for run %s",
                len(recommendations), backtest_run_id,
            )

        return recommendations

    def _evaluate_group(
        self,
        summary: FiveLayerBacktestGroupSummary,
    ) -> Optional[RecommendationDraft]:
        """Evaluate a single group summary for recommendation potential.

        Inference metrics (win_rate / avg_return) are pulled from
        ``family_breakdown[family]`` whenever it is available, because the
        legacy mixed-family ``summary.win_rate_pct`` averages
        ``forward_return_5d`` (entry) with ``risk_avoided_pct`` (observation)
        — that mixed number is unsafe for "increase weight" / "decrease weight"
        recommendations. Falls back to the mixed fields only for groups that
        are themselves single-family (``signal_family``).
        """
        threshold = SampleThresholdGate.check(get_aggregatable_sample_count(summary))
        if not threshold.can_display:
            return None

        family_scope, inference_metrics = _resolve_inference_source(summary)
        if not inference_metrics:
            return None

        rec_type, current_rule, suggested_change = _infer_recommendation(
            summary=summary,
            family_scope=family_scope,
            metrics=inference_metrics,
        )
        if rec_type is None:
            return None

        # Stability check now reads stability metrics from the same source as
        # the recommendation inference (family-correct when family_breakdown
        # is available, mixed summary fields otherwise). This closes the
        # earlier asymmetry where inference used family-correct numbers but
        # the gate validated against the mixed-pollued summary fields.
        stability_passed = _check_stability(inference_metrics)

        # Consistency uses the same family-correct numbers as inference so
        # the gate doesn't accept a recommendation that is internally
        # inconsistent at the family level.
        consistency_passed = _check_consistency(inference_metrics)

        # D4: snapshot the group's family share so confidence can discount
        # recommendations whose inferring family is a minority slice. The
        # snapshot is also forwarded to evidence_builder so reviewers see
        # exactly the same numbers that drove confidence.
        family_share = compute_family_share(summary)

        return RecommendationDraft(
            group_summary=summary,
            recommendation_type=rec_type,
            target_scope=summary.group_type,
            target_key=summary.group_key,
            current_rule=current_rule,
            suggested_change=suggested_change,
            threshold_result=threshold,
            stability_passed=stability_passed,
            consistency_passed=consistency_passed,
            family_scope=family_scope,
            inference_metrics=inference_metrics,
            family_share=family_share,
        )

    def _build_recommendation(
        self,
        backtest_run_id: str,
        draft: RecommendationDraft,
    ) -> Optional[FiveLayerBacktestRecommendation]:
        """Apply gates and build final recommendation record."""
        level = _determine_level(
            draft.threshold_result,
            draft.stability_passed,
            draft.consistency_passed,
        )
        if level is None:
            return None

        # Build evidence from evaluation samples
        evaluations = self.eval_repo.get_by_run(backtest_run_id)
        field_map = _group_type_to_field(draft.target_scope)
        sample_evals = [
            e for e in evaluations
            if field_map and getattr(e, field_map, None) == draft.target_key
        ][:10]

        evidence_json = EvidenceBuilder.build(
            group_summary=draft.group_summary,
            evaluations_sample=sample_evals,
            threshold_result=draft.threshold_result,
            family_scope=draft.family_scope,
            inference_metrics=draft.inference_metrics,
        )

        confidence = _compute_confidence(draft)

        summary = draft.group_summary
        # ``metrics_before_json`` carries BOTH the mixed summary numbers
        # (for backward compatibility) AND the family-correct numbers
        # actually used to make the recommendation, so reviewers can compare
        # the two without re-querying the summary table.
        metrics_snapshot = {
            "avg_return_pct": summary.avg_return_pct,
            "win_rate_pct": summary.win_rate_pct,
            "median_return_pct": summary.median_return_pct,
            "sample_count": summary.sample_count,
            "aggregatable_sample_count": get_aggregatable_sample_count(summary),
            "family_scope": draft.family_scope,
            "inference_metrics": draft.inference_metrics,
            # D4: surface the family-share snapshot used by confidence so
            # reviewers can audit the discount path without re-querying.
            "family_share": draft.family_share,
        }

        return FiveLayerBacktestRecommendation(
            backtest_run_id=backtest_run_id,
            recommendation_type=draft.recommendation_type,
            target_scope=draft.target_scope,
            target_key=draft.target_key,
            current_rule=draft.current_rule,
            suggested_change=draft.suggested_change,
            recommendation_level=level,
            sample_count=draft.threshold_result.sample_count,
            confidence=round(confidence, 4),
            validation_status="pending",
            evidence_json=evidence_json,
            metrics_before_json=json.dumps(metrics_snapshot, ensure_ascii=False),
            created_at=datetime.now(),
        )


# ── Pure gate / inference functions ─────────────────────────────────────────

def _determine_level(
    threshold: ThresholdResult,
    stability_passed: bool,
    consistency_passed: bool,
) -> Optional[str]:
    """Determine recommendation level based on gates.

    Returns None if below observation threshold.
    Small samples CANNOT produce actionable — this is a hard red line.
    """
    if not threshold.can_display:
        return None

    if threshold.can_action and stability_passed and consistency_passed:
        return "actionable"
    elif threshold.can_suggest and stability_passed:
        return "hypothesis"
    elif threshold.can_display:
        return "observation"

    return None


def _check_stability(metrics: Dict[str, Any]) -> bool:
    """Check time-bucket stability and extreme sample ratio.

    Operates on a metrics dict (family-level when available, otherwise the
    summary-level mixed fields exposed via :func:`_summary_metrics_to_dict`)
    so the gate evaluates the same numerical surface that the recommendation
    inference is anchored on. Without this alignment, a recommendation could
    be inferred from family-correct ``win_rate``/``avg_return`` and then
    accepted (or rejected) by stability metrics that average entry's
    ``forward_return_5d`` with observation's ``risk_avoided_pct`` — the very
    contamination ``family_breakdown`` was introduced to avoid.
    """
    tbs = metrics.get("time_bucket_stability")
    esr = metrics.get("extreme_sample_ratio")

    if tbs is not None and tbs > TIME_BUCKET_STABILITY_MAX:
        return False
    if esr is not None and esr > EXTREME_SAMPLE_RATIO_MAX:
        return False
    return True


def _check_consistency(metrics: Dict[str, Any]) -> bool:
    """Check that win_rate and avg_return agree on direction.

    Operates on a metrics dict (family-level when available, otherwise the
    summary-level mixed fields) so consistency is evaluated against the same
    source the recommendation type was inferred from.
    """
    wr = metrics.get("win_rate_pct")
    ar = metrics.get("avg_return_pct")
    if wr is None or ar is None:
        return False

    # Both positive or both negative/weak
    if ar > 0 and wr >= 50:
        return True
    if ar <= 0 and wr < 50:
        return True
    return False


def _infer_recommendation(
    summary: FiveLayerBacktestGroupSummary,
    family_scope: str,
    metrics: Dict[str, Any],
) -> tuple:
    """Infer recommendation type from family-correct group metrics.

    Returns (recommendation_type, current_rule, suggested_change) or
    (None, None, None) when no recommendation is warranted.

    ``family_scope`` is appended to the human-readable strings so reviewers
    can immediately see which subset (entry / observation / mixed) the
    suggestion is anchored on.
    """
    wr = metrics.get("win_rate_pct")
    ar = metrics.get("avg_return_pct")

    if wr is None or ar is None:
        return None, None, None

    group_desc = f"{summary.group_type}={summary.group_key} [{family_scope}]"

    if wr >= WIN_RATE_STRONG_THRESHOLD and ar > 0:
        return (
            "weight_increase",
            f"{group_desc}: current weight normal",
            f"Consider increasing weight/priority for {group_desc} "
            f"(win_rate={wr:.1f}%, avg_return={ar:.2f}%)",
        )
    elif wr <= WIN_RATE_WEAK_THRESHOLD and ar < 0:
        return (
            "weight_decrease",
            f"{group_desc}: current weight normal",
            f"Consider decreasing weight/filtering out {group_desc} "
            f"(win_rate={wr:.1f}%, avg_return={ar:.2f}%)",
        )
    elif wr >= 50 and ar < 0:
        return (
            "execution_review",
            f"{group_desc}: win_rate positive but returns negative",
            f"Review execution model for {group_desc} — wins are too small "
            f"or losses too large (win_rate={wr:.1f}%, avg_return={ar:.2f}%)",
        )

    return None, None, None


def _resolve_inference_source(
    summary: FiveLayerBacktestGroupSummary,
) -> tuple[str, Dict[str, Any]]:
    """Pick the family-correct metrics dict to drive recommendation inference.

    Priority:
      1. ``signal_family`` rows are themselves single-family — use the mixed
         summary fields directly with ``family_scope = group_key`` so the
         recommendation correctly attributes "entry" vs "observation".
      2. For any other group_type, prefer ``family_breakdown[entry]`` because
         "increase weight" / "decrease weight" decisions are about tradable
         entry signals; observation rows describe wait-correctness, which
         ``execution_review`` semantics don't fit.
      3. If only ``observation`` data exists, return that — recommendations
         derived from observation will be inherently weaker (handled later
         by the level/confidence gates).
      4. As a last resort, fall back to the mixed summary fields and mark
         ``family_scope = "mixed"`` so reviewers know not to trust the number.
    """
    metrics_payload = load_summary_metrics(summary)
    family_breakdown = metrics_payload.get("family_breakdown") or {}

    if summary.group_type == "signal_family":
        scope = (summary.group_key or "mixed").strip().lower() or "mixed"
        return scope, _summary_metrics_to_dict(summary)

    if isinstance(family_breakdown, dict):
        entry_metrics = family_breakdown.get("entry")
        if isinstance(entry_metrics, dict) and entry_metrics.get("win_rate_pct") is not None:
            return "entry", entry_metrics
        observation_metrics = family_breakdown.get("observation")
        if (
            isinstance(observation_metrics, dict)
            and observation_metrics.get("win_rate_pct") is not None
        ):
            return "observation", observation_metrics

    return "mixed", _summary_metrics_to_dict(summary)


def _summary_metrics_to_dict(summary: FiveLayerBacktestGroupSummary) -> Dict[str, Any]:
    """Project the summary row's mixed-family columns into a metrics dict.

    Used as the fallback metrics surface when :func:`_resolve_inference_source`
    can't find a family_breakdown entry to use. ``time_bucket_stability`` and
    ``extreme_sample_ratio`` are included so :func:`_check_stability` reads
    the same underlying numerical source the recommendation inference came
    from — see _check_stability for why this matters.
    """
    return {
        "win_rate_pct": summary.win_rate_pct,
        "avg_return_pct": summary.avg_return_pct,
        "median_return_pct": summary.median_return_pct,
        "profit_factor": summary.profit_factor,
        "sample_count": summary.sample_count,
        "time_bucket_stability": summary.time_bucket_stability,
        "extreme_sample_ratio": summary.extreme_sample_ratio,
    }


def _compute_confidence(draft: RecommendationDraft) -> float:
    """Compute confidence score from draft attributes.

    Base components (max 1.0):
      * Sample size      0.0 – 0.4
      * Stability        0.0 – 0.3
      * Consistency      0.0 – 0.3

    D4 family-share adjustment (max ±0.05 to ±-0.20):
      * If family_share is unavailable (no family_breakdown payload) the
        adjustment is skipped — the legacy mixed-family path is unchanged.
      * If the inferring family dominates the group (share ≥ 50 %), apply
        a small bonus to acknowledge the recommendation is anchored on the
        majority population.
      * If the inferring family is below 50 % share, apply a strict penalty.
      * If the inferring family is below 20 % share, apply a hard penalty
        that survives even the strongest sample/stability scores. This is
        the family equivalent of the aggregatable-ratio downgrade the
        SystemGrader already applies for compressed samples.

    Penalties skip ``signal_family`` rows because those are single-family
    by construction and ``family_share`` is not meaningful there.
    """
    score = 0.0
    t = draft.threshold_result

    if t.can_action:
        score += 0.4
    elif t.can_suggest:
        score += 0.25
    elif t.can_display:
        score += 0.1

    if draft.stability_passed:
        score += 0.3

    if draft.consistency_passed:
        score += 0.3

    score += _family_share_confidence_adjustment(
        draft.family_scope,
        draft.family_share,
        draft.target_scope,
    )

    # Clamp to [0, 1] — penalties can drag a borderline recommendation
    # below zero in pathological cases.
    return max(0.0, min(score, 1.0))


def _family_share_confidence_adjustment(
    family_scope: str,
    family_share: Dict[str, Any],
    target_scope: str,
) -> float:
    """Return the family-share-driven confidence delta for a draft.

    Pure function so it can be unit-tested independently of the engine.
    See :func:`_compute_confidence` for the threshold semantics.
    """
    if target_scope == "signal_family":
        # signal_family rows are themselves single-family — no meaningful
        # share to evaluate.
        return 0.0
    if not family_share or not family_share.get("available"):
        return 0.0

    # ``mixed`` inferences mean the engine couldn't anchor on a specific
    # family — apply a mild penalty regardless of the dominant share, since
    # the recommendation is built on contaminated numbers.
    if family_scope == "mixed":
        return -FAMILY_SHARE_PENALTY_STRICT

    inferring_share: Optional[float] = None
    if family_scope == "entry":
        inferring_share = family_share.get("entry_share")
    elif family_scope == "observation":
        inferring_share = family_share.get("observation_share")

    if inferring_share is None:
        return 0.0

    if inferring_share < FAMILY_SHARE_HARD_MIN:
        return -FAMILY_SHARE_PENALTY_HARD
    if inferring_share < FAMILY_SHARE_STRICT_MIN:
        return -FAMILY_SHARE_PENALTY_STRICT
    return FAMILY_SHARE_DOMINANT_BONUS


def _group_type_to_field(group_type: str) -> Optional[str]:
    """Map group_type back to evaluation field name for sample lookup."""
    mapping = {
        "signal_family": "signal_family",
        "setup_type": "snapshot_setup_type",
        "market_regime": "snapshot_market_regime",
        "theme_position": "snapshot_theme_position",
        "candidate_pool_level": "snapshot_candidate_pool_level",
        "entry_maturity": "snapshot_entry_maturity",
        "trade_stage": "snapshot_trade_stage",
    }
    # Handle combo types — use first dimension
    base_type = group_type.split("+")[0] if "+" in group_type else group_type
    return mapping.get(base_type)
