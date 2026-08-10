"""Versioned semantic and validation helpers for causal divergence v2."""

from __future__ import annotations

import math
from numbers import Real
from typing import Any

import numpy as np
import pandas as pd

from src.indicators.resistance_zone_detector import (
    ResistanceZoneMetadata,
    ResistanceZoneParams,
)


VALID_PATTERNS_V2: dict[tuple[str, str], dict[str, str]] = {
    ("down", "up"): {
        "code": "price_down_macd_up",
        "label": "经典底背离",
        "family": "price_down",
    },
    ("down", "flat"): {
        "code": "price_down_macd_flat",
        "label": "价格创新低·MACD持平",
        "family": "price_down",
    },
    ("flat", "up"): {
        "code": "price_flat_macd_up",
        "label": "价格持平·MACD抬升",
        "family": "price_flat",
    },
    ("flat", "down"): {
        "code": "price_flat_macd_down",
        "label": "价格持平·MACD走弱",
        "family": "price_flat",
    },
    ("up", "down"): {
        "code": "price_up_macd_down",
        "label": "强势回撤·MACD走弱",
        "family": "price_up",
    },
    ("up", "flat"): {
        "code": "price_up_macd_flat",
        "label": "强势回撤·MACD持平",
        "family": "price_up",
    },
}


def find_swing_lows_v2(series: pd.Series, order: int = 5) -> list[int]:
    """Frozen strict v2 swing-low semantics, independent from legacy v1."""
    lows: list[int] = []
    values = series.values
    n = len(values)
    for i in range(order, n):
        left_window = values[i - order:i]
        right_bars = min(order, n - 1 - i)
        if right_bars < 1:
            continue
        right_window = values[i + 1:i + right_bars + 1]
        full_window = np.concatenate([left_window, [values[i]], right_window])
        if not np.all(np.isfinite(full_window)):
            continue
        if values[i] < np.min(left_window) and values[i] <= np.min(right_window):
            lows.append(i)
    return lows


def find_swing_highs_v2(series: pd.Series, order: int = 5) -> list[int]:
    """Frozen strict v2 swing-high semantics, independent from legacy v1."""
    highs: list[int] = []
    values = series.values
    n = len(values)
    for i in range(order, n):
        left_window = values[i - order:i]
        right_bars = min(order, n - 1 - i)
        if right_bars < 1:
            continue
        right_window = values[i + 1:i + right_bars + 1]
        full_window = np.concatenate([left_window, [values[i]], right_window])
        if not np.all(np.isfinite(full_window)):
            continue
        if values[i] > np.max(left_window) and values[i] >= np.max(right_window):
            highs.append(i)
    return highs


def date_at_v2(df: pd.DataFrame, idx: int) -> str:
    for column in ("date", "trade_date"):
        if column in df.columns:
            value = df.iloc[idx][column]
            if pd.notna(value):
                return pd.Timestamp(value).strftime("%Y-%m-%d")
    return f"index:{idx}"


def safe_number_v2(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def event_v2(
    df: pd.DataFrame,
    idx: int | None = None,
    price: float | None = None,
    **extra: Any,
) -> dict[str, Any]:
    result = {
        "triggered": idx is not None,
        "bar_index": int(idx) if idx is not None else None,
        "date": date_at_v2(df, idx) if idx is not None else None,
        "price": round(float(price), 6) if price is not None else None,
    }
    result.update(extra)
    return result


def classify_relation_v2(value_a: float, value_b: float, tolerance: float) -> str:
    if value_a == 0:
        return "flat"
    ratio = (value_b - value_a) / abs(value_a)
    if ratio < -tolerance:
        return "down"
    if ratio > tolerance:
        return "up"
    return "flat"


def macd_semantics_v2(
    *,
    a_price: float,
    dif_a: float,
    dif_b: float,
    dea_a: float,
    dea_b: float,
    tolerance: float,
) -> dict[str, str] | None:
    """Apply threshold and relation rules to unrounded MACD evidence."""
    if dif_a > -a_price * 0.005:
        return None
    dif_relation = classify_relation_v2(dif_a, dif_b, tolerance)
    dea_relation = classify_relation_v2(dea_a, dea_b, tolerance)
    if {dif_relation, dea_relation} == {"up", "down"}:
        return None
    return {
        "dif_relation": dif_relation,
        "dea_relation": dea_relation,
        "macd_relation": (
            dea_relation if dif_relation == "flat" else dif_relation
        ),
    }


def has_required_context_v2(
    df: pd.DataFrame,
    ref_idx: int,
    pattern_family: str,
) -> bool:
    """Apply the frozen causal-v2 prior-trend semantics."""
    if pattern_family in {"price_down", "price_flat"}:
        return _prior_downtrend_v2(df, ref_idx)
    if pattern_family == "price_up":
        return _prior_uptrend_v2(df, ref_idx)
    return False


def _prior_downtrend_v2(
    df: pd.DataFrame,
    ref_idx: int,
    window: int = 50,
    swing_order: int = 5,
) -> bool:
    """Frozen copy of the causal-v2 two-of-three downtrend gate."""
    start = max(0, ref_idx - window)
    segment = df.iloc[start:ref_idx + 1].reset_index(drop=True)
    if len(segment) < 10:
        return False
    close = segment["close"]
    high = segment["high"] if "high" in segment else close
    score = 0
    swings = find_swing_highs_v2(
        high.reset_index(drop=True),
        order=min(swing_order, 3),
    )
    if len(swings) >= 2:
        values = [float(high.iloc[idx]) for idx in swings[-2:]]
        score += int(values[-1] < values[-2])
    if len(close) >= 20:
        score += int(float(close.tail(10).mean()) < float(close.tail(20).mean()))
    if len(close) >= 5:
        slope = np.polyfit(
            np.arange(len(close), dtype=float),
            close.to_numpy(dtype=float),
            1,
        )[0]
        score += int(slope < 0)
    return score >= 2


def _prior_uptrend_v2(
    df: pd.DataFrame,
    ref_idx: int,
    window: int = 50,
    swing_order: int = 5,
) -> bool:
    """Frozen copy of the causal-v2 two-of-three uptrend gate."""
    start = max(0, ref_idx - window)
    segment = df.iloc[start:ref_idx + 1].reset_index(drop=True)
    if len(segment) < 10:
        return False
    close = segment["close"]
    low = segment["low"] if "low" in segment else close
    score = 0
    swings = find_swing_lows_v2(
        low.reset_index(drop=True),
        order=min(swing_order, 3),
    )
    if len(swings) >= 2:
        values = [float(low.iloc[idx]) for idx in swings[-2:]]
        score += int(values[-1] > values[-2])
    if len(close) >= 20:
        score += int(float(close.tail(10).mean()) > float(close.tail(20).mean()))
    if len(close) >= 5:
        slope = np.polyfit(
            np.arange(len(close), dtype=float),
            close.to_numpy(dtype=float),
            1,
        )[0]
        score += int(slope > 0)
    return score >= 2


def _validate_int(name: str, value: Any, minimum: int) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(
            f"{name} must be an integer greater than or equal to {minimum}"
        )


def _validate_real(
    name: str,
    value: Any,
    minimum: float,
    maximum: float | None = None,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
        or float(value) < minimum
        or (maximum is not None and float(value) > maximum)
    ):
        upper = f" and at most {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be finite, at least {minimum}{upper}")


def validate_detector_params(
    *,
    zone_params: ResistanceZoneParams,
    lookback: int,
    min_ab_gap: int,
    max_ab_gap: int,
    ab_match_window: int,
    flat_tolerance: float,
    macd_flat_tolerance: float,
    break_tolerance: float,
    sync_window: int | None,
    retention_bars: int | None,
    metadata: ResistanceZoneMetadata,
) -> None:
    if not isinstance(zone_params, ResistanceZoneParams):
        raise ValueError("zone_params must be ResistanceZoneParams")
    if not isinstance(metadata, ResistanceZoneMetadata):
        raise TypeError("metadata must be ResistanceZoneMetadata")
    _validate_int("lookback", lookback, 1)
    _validate_int("min_ab_gap", min_ab_gap, 1)
    _validate_int("max_ab_gap", max_ab_gap, min_ab_gap)
    _validate_int("ab_match_window", ab_match_window, 0)
    _validate_real("flat_tolerance", flat_tolerance, 0.0)
    _validate_real("macd_flat_tolerance", macd_flat_tolerance, 0.0)
    _validate_real("break_tolerance", break_tolerance, 0.0, 1.0)
    if sync_window is not None:
        _validate_int("sync_window", sync_window, 0)
    if retention_bars is not None:
        _validate_int("retention_bars", retention_bars, 0)


def normalize_visible_market_data(
    visible: pd.DataFrame,
) -> tuple[pd.DataFrame | None, list[str]]:
    normalized = visible.copy()
    reasons: set[str] = set()
    for column in ("high", "low", "close"):
        if column not in normalized.columns:
            reasons.add(f"missing_column:{column}")
            continue
        values = pd.to_numeric(normalized[column], errors="coerce")
        normalized[column] = values
        if not np.isfinite(values.to_numpy(dtype=float)).all():
            reasons.add(f"non_finite:{column}")
        if (values.dropna() <= 0).any():
            reasons.add(f"non_positive:{column}")
    if "open" in normalized.columns:
        original_open = normalized["open"]
        open_values = pd.to_numeric(original_open, errors="coerce")
        normalized["open"] = open_values
        present = original_open.notna()
        if not np.isfinite(open_values[present].to_numpy(dtype=float)).all():
            reasons.add("non_finite:open")
        if (open_values[present].dropna() <= 0).any():
            reasons.add("non_positive:open")
    if "volume" in normalized.columns:
        original_volume = normalized["volume"]
        volume_values = pd.to_numeric(original_volume, errors="coerce")
        normalized["volume"] = volume_values
        present = original_volume.notna()
        if not np.isfinite(volume_values[present].to_numpy(dtype=float)).all():
            reasons.add("non_finite:volume")
        if (volume_values[present].dropna() < 0).any():
            reasons.add("negative_volume")
    if "pct_chg" in normalized.columns:
        normalized["pct_chg"] = pd.to_numeric(
            normalized["pct_chg"],
            errors="coerce",
        )
    required_valid = all(
        column in normalized
        and np.isfinite(normalized[column].to_numpy(dtype=float)).all()
        and (normalized[column] > 0).all()
        for column in ("high", "low", "close")
    )
    if required_valid:
        if (normalized["high"] < normalized["low"]).any():
            reasons.add("high_below_low")
        body_low = normalized[["low", "close"]].min(axis=1)
        body_high = normalized[["low", "close"]].max(axis=1)
        if "open" in normalized:
            present_open = normalized["open"].notna()
            body_low.loc[present_open] = normalized.loc[
                present_open, ["open", "close"]
            ].min(axis=1)
            body_high.loc[present_open] = normalized.loc[
                present_open, ["open", "close"]
            ].max(axis=1)
        if (
            (body_low < normalized["low"])
            | (body_high > normalized["high"])
        ).any():
            reasons.add("body_outside_range")
    for column in ("date", "trade_date"):
        if column not in normalized.columns:
            continue
        parsed = pd.to_datetime(normalized[column], errors="coerce")
        if parsed.isna().any():
            reasons.add(f"invalid_date:{column}")
            continue
        normalized[column] = parsed
        if parsed.duplicated().any():
            reasons.add(f"duplicate_date:{column}")
        elif not parsed.is_monotonic_increasing:
            reasons.add(f"non_monotonic_date:{column}")
    if reasons:
        return None, sorted(reasons)
    return normalized, []
