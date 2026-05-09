# -*- coding: utf-8 -*-
"""Evidence builder for backtest recommendations.

Constructs traceable evidence JSON linking a recommendation back to
the group summary, evaluation samples, and threshold checks that
support it.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from src.backtest.aggregators.sample_threshold import SampleThresholdGate, ThresholdResult
from src.backtest.models.backtest_models import (
    FiveLayerBacktestEvaluation,
    FiveLayerBacktestGroupSummary,
)
from src.backtest.utils.summary_metrics import (
    compute_family_share,
    get_aggregatable_sample_count,
    get_sample_baseline,
    load_summary_metrics,
)


class EvidenceBuilder:
    """Builds traceable evidence chains for recommendations.

    Every recommendation must carry an evidence_json that allows
    auditors to trace back to:
      - The group summary it was derived from
      - A sample of evaluation IDs supporting the claim
      - The threshold check result
      - Key metric snapshot at time of recommendation
      - Sample-quality context: which family was used, aggregatable ratio,
        suppressed-sample reasons (so reviewers can spot heavily-suppressed
        groups without joining back to evaluations).
    """

    @staticmethod
    def build(
        group_summary: FiveLayerBacktestGroupSummary,
        evaluations_sample: List[FiveLayerBacktestEvaluation],
        threshold_result: ThresholdResult,
        family_scope: Optional[str] = None,
        inference_metrics: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Build evidence JSON string.

        Args:
            group_summary: The summary this recommendation is based on.
            evaluations_sample: Representative evaluation records (up to 10).
            threshold_result: Result of sample threshold check.
            family_scope: Subset the recommendation was anchored on
                (``entry`` / ``observation`` / ``mixed`` / family name).
            inference_metrics: The exact family-correct dict used to derive
                the suggestion (mirrors what the engine read).
            extra: Additional context to include.

        Returns:
            JSON string with full evidence chain.
        """
        sample_baseline = get_sample_baseline(group_summary)
        aggregatable = get_aggregatable_sample_count(group_summary)
        raw_count = group_summary.sample_count or 0

        # Aggregatable ratio: 1.0 == every sample contributed; small values
        # indicate the headline metrics are computed on a biased subset.
        aggregatable_ratio: Optional[float] = None
        if raw_count > 0:
            aggregatable_ratio = round(aggregatable / raw_count, 4)

        suppressed_reasons = sample_baseline.get("suppressed_reasons") or {}
        suppressed_total = (
            sum(v for v in suppressed_reasons.values() if isinstance(v, (int, float)))
            if isinstance(suppressed_reasons, dict)
            else 0
        )

        # Pull the per-family breakdown so reviewers can compare entry vs
        # observation numbers without re-querying.
        metrics_payload = load_summary_metrics(group_summary)
        family_breakdown = metrics_payload.get("family_breakdown") or None

        # D4: surface the family share so reviewers can immediately see
        # whether a recommendation is anchored on a sample mix where the
        # inferring family actually dominates. A "weight_increase" backed by
        # an entry inference but built on a group that is 90 % observation
        # samples is a much weaker signal than the headline numbers suggest.
        family_share = compute_family_share(group_summary)

        evidence: Dict[str, Any] = {
            "source_summary": {
                "group_type": group_summary.group_type,
                "group_key": group_summary.group_key,
                "sample_count": group_summary.sample_count,
                "sample_baseline": sample_baseline,
                "avg_return_pct": group_summary.avg_return_pct,
                "median_return_pct": group_summary.median_return_pct,
                "win_rate_pct": group_summary.win_rate_pct,
                "p25_return_pct": group_summary.p25_return_pct,
                "p75_return_pct": group_summary.p75_return_pct,
                "extreme_sample_ratio": group_summary.extreme_sample_ratio,
                "time_bucket_stability": group_summary.time_bucket_stability,
            },
            "threshold_check": {
                "sample_count": threshold_result.sample_count,
                "can_display": threshold_result.can_display,
                "can_suggest": threshold_result.can_suggest,
                "can_action": threshold_result.can_action,
                "reason": threshold_result.reason,
            },
            "sample_quality": {
                "aggregatable_sample_count": aggregatable,
                "raw_sample_count": raw_count,
                "aggregatable_ratio": aggregatable_ratio,
                "suppressed_sample_count": suppressed_total,
                "suppressed_reasons": suppressed_reasons
                if isinstance(suppressed_reasons, dict)
                else {},
                # D4: family_share is the structured snapshot of how many
                # samples each family contributed to the group + which family
                # dominates. Lets reviewers spot recommendations whose
                # inferring family is actually a minority slice without
                # joining back to family_breakdown counts manually.
                "family_share": family_share,
            },
            "inference_source": {
                "family_scope": family_scope or "mixed",
                "inference_metrics": inference_metrics or {},
                "family_breakdown": family_breakdown,
                # D4: cross-check whether the inferring family dominates the
                # group's actual family mix. ``True`` means the inference is
                # consistent with the dominant population; ``False`` means the
                # inference is anchored on a minority slice and reviewers
                # should weight the recommendation accordingly.
                "dominant_family_match": (
                    bool(
                        family_share.get("available")
                        and family_share.get("dominant_family") == (family_scope or "")
                    )
                ),
            },
            "sample_evaluation_ids": [
                e.id for e in evaluations_sample[:10] if e.id is not None
            ],
            "sample_codes": [
                e.code for e in evaluations_sample[:10] if e.code is not None
            ],
        }

        if extra:
            evidence["extra"] = extra

        return json.dumps(evidence, ensure_ascii=False)
