"""Opt-in Strategy E v2 layered-entry wrapper."""

from __future__ import annotations

import math
from typing import Any, Optional

import pandas as pd

from src.config import get_config
from src.indicators.causal_bottom_divergence_detector import (
    CausalBottomDivergenceDetector,
)
from src.indicators.resistance_zone_detector import (
    ResistanceZoneMetadata,
    ResistanceZoneParams,
)


_TRUSTED_ADJUSTMENT_SOURCES = frozenset({
    "tushare_native",
    "akshare_qfq_div_raw",
})
_UNSAFE_DATA_SOURCE_MARKERS = frozenset({
    "unknown",
    "fetcher_unset",
    "legacy_assume_one",
})
_STAGE_EVENT_PATHS = {
    "early": ("early_reversal",),
    "near_cleared": ("near_zone_events", "cleared_confirmed"),
    "major_actionable": ("major_zone_breakout",),
}


def _empty_result(reason: str, *, stage: str = "rejected") -> dict[str, Any]:
    return {
        "triggered": False,
        "stage": stage,
        "actionable_entry": False,
        "candidate_version": None,
        "zone_version": None,
        "entry_price": None,
        "stop_loss_price": None,
        "score": 0.0,
        "reason": f"bottom divergence v2 {reason}",
        "layered_buy_points": [],
    }


def _has_trusted_adjustment(
    metadata: Optional[ResistanceZoneMetadata],
) -> bool:
    if metadata is None:
        return False
    adjustment = str(metadata.adj_factor_source or "").strip().lower()
    data_sources = [
        value.strip().lower()
        for value in str(metadata.data_source or "").split("|")
        if value.strip()
    ]
    return (
        adjustment in _TRUSTED_ADJUSTMENT_SOURCES
        and bool(data_sources)
        and not any(
            marker in source
            for source in data_sources
            for marker in _UNSAFE_DATA_SOURCE_MARKERS
        )
    )


def _is_execution_allowed(
    *,
    stage: str,
    status: str,
    major_actionable: bool,
) -> bool:
    expected_status = (
        "actionable"
        if stage == "major_actionable"
        else "major_not_confirmed"
        if stage in {"early", "near_cleared"}
        else None
    )
    return (
        expected_status is not None
        and status == expected_status
        and status != "adjustment_unknown"
        and (stage != "major_actionable" or major_actionable)
    )


def _zone_params(config: Any) -> ResistanceZoneParams:
    if getattr(config, "_bottom_divergence_v2_parse_errors", ()):
        raise ValueError("invalid parsed config")
    sync_window = config.bottom_divergence_v2_sync_window
    retention_bars = config.bottom_divergence_v2_retention_bars
    if (
        type(sync_window) is not int
        or sync_window <= 0
        or type(retention_bars) is not int
        or retention_bars <= 0
    ):
        raise ValueError("invalid window config")

    weights_r1 = config.bottom_divergence_v2_r1_weights
    weights_r2 = config.bottom_divergence_v2_r2_weights
    return ResistanceZoneParams(
        swing_order=5,
        cluster_pct=config.bottom_divergence_v2_cluster_pct,
        atr_gap_multiplier=config.bottom_divergence_v2_atr_gap_multiplier,
        long_wick_ratio=0.5,
        rejection_wick_ratio=0.35,
        rejection_atr_ratio=0.5,
        score_min=config.bottom_divergence_v2_zone_score_min,
        overlap_ratio=0.60,
        breakout_buffer_pct=config.bottom_divergence_v2_breakout_buffer_pct,
        sync_window=sync_window,
        invalidated_retention_bars=retention_bars,
        r1_touch_weight=weights_r1[0],
        r1_recency_weight=weights_r1[1],
        r1_volume_weight=weights_r1[2],
        r1_rejection_weight=weights_r1[3],
        r1_tightness_weight=weights_r1[4],
        r1_distance_weight=weights_r1[5],
        r2_touch_weight=weights_r2[0],
        r2_recency_weight=weights_r2[1],
        r2_volume_weight=weights_r2[2],
        r2_rejection_weight=weights_r2[3],
        r2_tightness_weight=weights_r2[4],
        r2_height_weight=weights_r2[5],
    )


def _unit_score(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return min(max(parsed, 0.0), 1.0)


def _score(detector_result: dict[str, Any]) -> float:
    early = detector_result.get("early_reversal") or {}
    zone = detector_result.get("zone") or {}
    near_zone = zone.get("r1") or {}
    major_zone = zone.get("r2") or {}
    score = (
        _unit_score(early.get("strength")) * 30.0
        + _unit_score(near_zone.get("score")) * 30.0
        + _unit_score(major_zone.get("score")) * 40.0
    )
    return round(min(max(score, 0.0), 100.0), 4)


def _stage_event(detector_result: dict[str, Any], stage: str) -> dict[str, Any]:
    current: Any = detector_result
    for key in _STAGE_EVENT_PATHS.get(stage, ()):
        current = current.get(key) if isinstance(current, dict) else None
    return current if isinstance(current, dict) else {}


def _event_close(
    df: pd.DataFrame,
    detector_result: dict[str, Any],
    stage: str,
) -> Optional[float]:
    event = _stage_event(detector_result, stage)
    bar_index = event.get("bar_index")
    if type(bar_index) is not int or not 0 <= bar_index < len(df):
        return None
    try:
        close = float(df.iloc[bar_index]["close"])
    except (KeyError, TypeError, ValueError):
        return None
    return close if math.isfinite(close) and close > 0 else None


class BottomDivergenceLayeredEntryStrategy:
    """Adapt the causal v2 detector to the direct-entry strategy contract."""

    @staticmethod
    def evaluate(
        df: pd.DataFrame,
        *,
        config: Any = None,
        metadata: Optional[ResistanceZoneMetadata] = None,
    ) -> dict[str, Any]:
        effective_config = config if config is not None else get_config()
        if not bool(
            getattr(effective_config, "bottom_divergence_v2_enabled", False)
        ):
            return _empty_result("disabled", stage="disabled")
        try:
            params = _zone_params(effective_config)
        except (AttributeError, IndexError, TypeError, ValueError):
            return _empty_result("invalid_config")

        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return _empty_result("insufficient_data")

        detector_result = CausalBottomDivergenceDetector.detect(
            df,
            as_of_index=len(df) - 1,
            zone_params=params,
            metadata=metadata or ResistanceZoneMetadata(),
        )
        stage = str(detector_result.get("stage") or "rejected")
        status = str(
            detector_result.get("actionability_status")
            or detector_result.get("rejection_reason")
            or stage
        )
        major_actionable = bool(
            (detector_result.get("major_zone_actionable_entry") or {}).get(
                "actionable",
                False,
            )
        )
        adjustment_trusted = _has_trusted_adjustment(metadata)
        effective_status = (
            "adjustment_unknown"
            if (
                not adjustment_trusted
                and status in {"major_not_confirmed", "actionable"}
            )
            else status
        )
        actionable = _is_execution_allowed(
            stage=stage,
            status=effective_status,
            major_actionable=major_actionable,
        )
        entry_price = (
            _event_close(df, detector_result, stage) if actionable else None
        )
        triggered = actionable and entry_price is not None
        zone = detector_result.get("zone") or {}

        return {
            "triggered": triggered,
            "stage": stage,
            "actionable_entry": triggered,
            "candidate_version": detector_result.get("candidate_version"),
            "zone_version": zone.get("zone_version"),
            "entry_price": entry_price if triggered else None,
            "stop_loss_price": detector_result.get("stop_loss_price"),
            "score": _score(detector_result),
            "reason": (
                f"bottom divergence v2 {stage} entry"
                if triggered
                else f"bottom divergence v2 {effective_status}"
            ),
            "layered_buy_points": list(
                detector_result.get("layered_buy_points") or []
            ),
        }
