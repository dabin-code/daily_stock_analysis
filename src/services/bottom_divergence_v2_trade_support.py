"""Shared trade helpers for causal bottom-divergence v2 snapshots."""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional


_STAGE_LEVEL_ALIASES = {
    "early": frozenset({"early"}),
    "near_cleared": frozenset({"r1", "near"}),
    "major_actionable": frozenset({"r2", "major"}),
}
_STAGE_ACTIONABILITY_STATUS = {
    "early": "major_not_confirmed",
    "near_cleared": "major_not_confirmed",
    "major_actionable": "actionable",
}


def is_v2_execution_allowed(snapshot: Mapping[str, Any]) -> bool:
    """Return whether a v2 snapshot is safe to turn into an order."""
    stage = str(
        snapshot.get("bottom_divergence_v2_stage") or ""
    ).strip().lower()
    status = str(
        snapshot.get("bottom_divergence_v2_actionability_status") or ""
    ).strip().lower()
    if status == "adjustment_unknown":
        return False
    if status != _STAGE_ACTIONABILITY_STATUS.get(stage):
        return False
    if stage == "major_actionable":
        return (
            snapshot.get(
                "bottom_divergence_v2_major_actionable_entry"
            )
            is True
        )
    return stage in {"early", "near_cleared"}


def _positive_finite_price(value: object) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def resolve_current_stage_buy_point(
    snapshot: Mapping[str, Any],
) -> Optional[dict]:
    """Return the triggered layered point matching the actionable v2 stage."""
    stage = str(snapshot.get("bottom_divergence_v2_stage") or "").strip().lower()
    allowed_levels = _STAGE_LEVEL_ALIASES.get(stage)
    if allowed_levels is None:
        return None

    points = snapshot.get("bottom_divergence_v2_layered_buy_points")
    if not isinstance(points, list):
        return None
    return next(
        (
            point
            for point in points
            if isinstance(point, dict)
            and point.get("triggered") is True
            and str(point.get("level") or "").strip().lower() in allowed_levels
        ),
        None,
    )


def resolve_current_stage_stop_loss(
    snapshot: Mapping[str, Any],
) -> Optional[float]:
    """Resolve a finite stop from the global field or current stage point."""
    stage = str(snapshot.get("bottom_divergence_v2_stage") or "").strip().lower()
    if stage not in _STAGE_LEVEL_ALIASES:
        return None

    global_stop = _positive_finite_price(
        snapshot.get("bottom_divergence_v2_stop_loss_price")
    )
    if global_stop is not None:
        return global_stop

    point = resolve_current_stage_buy_point(snapshot)
    if point is None:
        return None
    return _positive_finite_price(point.get("stop")) or _positive_finite_price(
        point.get("stop_loss_price")
    )
