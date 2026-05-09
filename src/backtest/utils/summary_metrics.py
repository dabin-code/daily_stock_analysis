# -*- coding: utf-8 -*-
"""Helpers for reading structured summary baseline metadata."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional


def load_summary_metrics(summary: Any) -> Dict[str, Any]:
    """Parse summary.metrics_json defensively."""
    payload = getattr(summary, "metrics_json", None)
    if not payload:
        return {}
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def get_sample_baseline(summary: Any) -> Dict[str, Any]:
    """Return structured sample baseline metadata for a summary."""
    metrics = load_summary_metrics(summary)
    baseline = metrics.get("sample_baseline")
    return baseline if isinstance(baseline, dict) else {}


def get_aggregatable_sample_count(summary: Any) -> int:
    """Return the number of samples that actually contributed to metrics."""
    baseline = get_sample_baseline(summary)
    value = baseline.get("aggregatable_sample_count")
    if isinstance(value, int):
        return value
    sample_count = getattr(summary, "sample_count", None)
    return sample_count if isinstance(sample_count, int) else 0


def compute_family_share(summary: Any) -> Dict[str, Any]:
    """Return family-share breakdown for a group summary (D4).

    Reads ``metrics_json.family_breakdown`` and returns a structured snapshot
    of how many samples each family contributed to the group, plus which
    family dominates and what fraction of the group it represents.

    Output schema::

        {
            "entry_sample_count": int,
            "observation_sample_count": int,
            "total_family_sample_count": int,
            "entry_share": float | None,        # 0.0–1.0
            "observation_share": float | None,  # 0.0–1.0
            "dominant_family": str | None,      # "entry" / "observation" / None
            "dominant_share": float | None,     # 0.0–1.0
            "available": bool,                  # False when family_breakdown
                                                #  is missing / unparseable
        }

    The aggregatable sample count is preferred over the raw count (for
    consistency with the rest of the pipeline). When ``family_breakdown``
    is absent the helper returns ``available=False`` so callers can decide
    whether to fall back, skip the gate, or downgrade confidence.

    NOTE: Single-family group rows (``signal_family``) are still handled
    correctly — only the present family contributes a non-zero count.
    """
    result: Dict[str, Any] = {
        "entry_sample_count": 0,
        "observation_sample_count": 0,
        "total_family_sample_count": 0,
        "entry_share": None,
        "observation_share": None,
        "dominant_family": None,
        "dominant_share": None,
        "available": False,
    }

    metrics = load_summary_metrics(summary)
    breakdown = metrics.get("family_breakdown")
    if not isinstance(breakdown, dict) or not breakdown:
        return result

    def _read_count(family: str) -> int:
        block = breakdown.get(family)
        if not isinstance(block, dict):
            return 0
        agg = block.get("aggregatable_sample_count")
        if isinstance(agg, int) and agg >= 0:
            return agg
        raw = block.get("sample_count")
        if isinstance(raw, int) and raw >= 0:
            return raw
        return 0

    entry = _read_count("entry")
    observation = _read_count("observation")
    total = entry + observation
    if total == 0:
        # family_breakdown exists but holds zero counts (degenerate case);
        # mark as available so callers can distinguish "no family data" from
        # "all families empty".
        result["available"] = True
        return result

    entry_share = entry / total
    observation_share = observation / total
    dominant_family, dominant_share = (
        ("entry", entry_share)
        if entry >= observation
        else ("observation", observation_share)
    )

    result.update({
        "entry_sample_count": entry,
        "observation_sample_count": observation,
        "total_family_sample_count": total,
        "entry_share": round(entry_share, 4),
        "observation_share": round(observation_share, 4),
        "dominant_family": dominant_family,
        "dominant_share": round(dominant_share, 4),
        "available": True,
    })
    return result
