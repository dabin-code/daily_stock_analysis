"""Event replay and current actionability for causal divergence v2."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from src.indicators.causal_bottom_divergence_support import (
    date_at_v2,
    event_v2,
    safe_number_v2,
)
from src.indicators.resistance_zone_detector import (
    ResistanceZoneMetadata,
    ResistanceZoneParams,
    _volume_ratio_5,
)


EARLY_PATTERNS_V2 = {
    "price_down_macd_up",
    "price_down_macd_flat",
    "price_flat_macd_up",
}


def with_trendline_breakout_v2(
    frozen_line: dict[str, Any],
    visible: pd.DataFrame,
    *,
    b_idx: int,
) -> dict[str, Any]:
    result = dict(frozen_line)
    if not result["found"]:
        return result
    for idx in range(b_idx + 1, len(visible)):
        projected = result["slope"] * idx + result["intercept"]
        if float(visible.iloc[idx]["close"]) > projected:
            result.update(
                {
                    "breakout_confirmed": True,
                    "breakout_bar_index": idx,
                    "breakout_date": date_at_v2(visible, idx),
                    "projected_value_at_breakout": round(projected, 6),
                }
            )
            break
    return result


def _early_score(df: pd.DataFrame, idx: int) -> tuple[float, dict[str, Any]]:
    row = df.iloc[idx]
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    full_range = high - low
    components: dict[str, float | None] = {}
    weights: dict[str, float] = {"close_position": 0.30, "return": 0.20}
    components["close_position"] = (
        min(max((close - low) / full_range, 0.0), 1.0)
        if full_range > 0
        else 0.0
    )
    open_price = safe_number_v2(row["open"]) if "open" in df else None
    if open_price is not None:
        weights["body"] = 0.25
        body_ratio = abs(close - open_price) / full_range if full_range > 0 else 0.0
        components["body"] = min(max(body_ratio / 0.5, 0.0), 1.0)
    else:
        components["body"] = None
    volume = df["volume"].reset_index(drop=True) if "volume" in df else None
    volume_ratio = _volume_ratio_5(volume, idx)
    if volume_ratio is not None:
        weights["volume"] = 0.25
        components["volume"] = min(max(volume_ratio / 2.0, 0.0), 1.0)
    else:
        components["volume"] = None
    pct_change = safe_number_v2(row["pct_chg"]) if "pct_chg" in df else None
    if pct_change is None:
        previous = float(df.iloc[idx - 1]["close"])
        pct_change = (close - previous) / previous * 100 if previous else 0.0
    components["return"] = min(max(pct_change, 0.0) / 6.0, 1.0)
    denominator = sum(weights.values())
    score = sum(
        float(components[name]) * weight
        for name, weight in weights.items()
        if components[name] is not None
    ) / denominator
    return score, {
        "components": components,
        "weights": weights,
        "volume_ratio_5": (
            round(volume_ratio, 6) if volume_ratio is not None else None
        ),
        "pct_chg": round(pct_change, 6),
    }


def scan_early_v2(
    visible: pd.DataFrame,
    *,
    b_idx: int,
    pattern_code: str,
) -> dict[str, Any]:
    if pattern_code not in EARLY_PATTERNS_V2:
        return event_v2(visible, strength=0.0, components={})
    for idx in range(b_idx + 1, len(visible)):
        if float(visible.iloc[idx]["close"]) <= float(
            visible.iloc[idx - 1]["high"]
        ):
            continue
        score, detail = _early_score(visible, idx)
        if score >= 0.65:
            return event_v2(
                visible,
                idx,
                float(visible.iloc[idx]["close"]),
                strength=round(score, 6),
                **detail,
            )
    return event_v2(visible, strength=0.0, components={})


def zone_events_v2(
    visible: pd.DataFrame,
    *,
    b_idx: int,
    zone: dict[str, Any],
    params: ResistanceZoneParams,
    atr_values: pd.Series,
) -> tuple[dict[str, Any], dict[str, Any]]:
    names = ("entered", "accepted", "crossed", "cleared_confirmed")
    near = {name: event_v2(visible) for name in names}
    r1 = zone.get("r1")
    volume = visible["volume"].reset_index(drop=True) if "volume" in visible else None
    if r1 is not None:
        lower = float(r1["lower"])
        upper = float(r1["upper"])
        for idx in range(b_idx + 1, len(visible)):
            close = float(visible.iloc[idx]["close"])
            if not near["entered"]["triggered"] and close >= lower:
                near["entered"] = event_v2(visible, idx, close)
            if (
                not near["accepted"]["triggered"]
                and idx > b_idx + 1
                and close >= lower
                and float(visible.iloc[idx - 1]["close"]) >= lower
            ):
                near["accepted"] = event_v2(visible, idx, close)
            atr = safe_number_v2(atr_values.iloc[idx])
            buffer_pct = max(
                params.breakout_buffer_pct,
                (atr * 0.1 / close) if atr is not None and close > 0 else 0.0,
            )
            if (
                not near["crossed"]["triggered"]
                and close > upper * (1 + buffer_pct)
            ):
                ratio = _volume_ratio_5(volume, idx)
                near["crossed"] = event_v2(
                    visible,
                    idx,
                    close,
                    buffer_pct=round(buffer_pct, 6),
                    volume_ratio_5=(
                        round(ratio, 6) if ratio is not None else None
                    ),
                )
                if ratio is not None and ratio >= 1.2:
                    near["cleared_confirmed"] = event_v2(
                        visible,
                        idx,
                        close,
                        confirmation="cross_volume",
                    )
            crossed_idx = near["crossed"]["bar_index"]
            if (
                crossed_idx is not None
                and not near["cleared_confirmed"]["triggered"]
                and idx == crossed_idx + 1
                and close >= upper
            ):
                near["cleared_confirmed"] = event_v2(
                    visible,
                    idx,
                    close,
                    confirmation="next_day_hold",
                )

    major = event_v2(
        visible,
        confirmed=False,
        trendline_breakout_bar_index=None,
        sync_gap=None,
        buffer_pct=None,
    )
    r2 = zone.get("r2")
    if r2 is not None:
        upper = float(r2["upper"])
        for idx in range(b_idx + 1, len(visible)):
            close = float(visible.iloc[idx]["close"])
            atr = safe_number_v2(atr_values.iloc[idx])
            buffer_pct = max(
                params.breakout_buffer_pct,
                (atr * 0.1 / close) if atr is not None and close > 0 else 0.0,
            )
            if close > upper * (1 + buffer_pct):
                major = event_v2(
                    visible,
                    idx,
                    close,
                    confirmed=False,
                    trendline_breakout_bar_index=None,
                    sync_gap=None,
                    buffer_pct=round(buffer_pct, 6),
                )
                break
    return near, major


def actionability_v2(
    visible: pd.DataFrame,
    *,
    major: dict[str, Any],
    zone: dict[str, Any],
    structure_break: dict[str, Any],
    metadata: ResistanceZoneMetadata,
) -> tuple[dict[str, Any], str]:
    result = {
        "actionable": False,
        "bar_index": major.get("bar_index"),
        "date": major.get("date"),
        "price": major.get("price"),
        "confirmation_days": None,
        "extended_pct_raw": None,
        "extended_pct": None,
    }
    if not major.get("confirmed"):
        return result, "major_not_confirmed"
    if structure_break["triggered"]:
        return result, "structure_floor_broken"
    adjustment = (metadata.adj_factor_source or "").strip().lower()
    if adjustment == "unknown" or bool(
        getattr(metadata, "adjustment_unknown", False)
    ):
        return result, "adjustment_unknown"
    idx = int(major["bar_index"])
    confirmation_days = len(visible) - 1 - idx
    latest_close = float(visible.iloc[-1]["close"])
    breakout_close = float(major["price"])
    extended = (latest_close - breakout_close) / breakout_close * 100
    result["confirmation_days"] = confirmation_days
    result["extended_pct_raw"] = extended
    result["extended_pct"] = round(extended, 6)
    if confirmation_days > 3:
        return result, "confirmation_too_old"
    within_upper_bound = extended <= 10 or math.isclose(
        extended,
        10.0,
        rel_tol=0.0,
        abs_tol=1e-12,
    )
    if extended < 0 or not within_upper_bound:
        return result, "extension_out_of_range"
    r2 = zone.get("r2")
    if r2 is None or latest_close < float(r2["upper"]):
        return result, "below_r2"
    result["actionable"] = True
    return result, "actionable"
