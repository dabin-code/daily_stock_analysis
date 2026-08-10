"""Deterministic resistance-zone calculation for a frozen A/B prefix."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
import hashlib
import json
import math
from numbers import Real
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from src.indicators.divergence_detector import find_swing_highs


ALGORITHM_VERSION = "resistance-zone-v2"
_EPSILON = 1e-12


@dataclass(frozen=True)
class ResistanceZoneParams:
    swing_order: int = 5
    cluster_pct: float = 0.015
    atr_gap_multiplier: float = 0.5
    long_wick_ratio: float = 0.5
    rejection_wick_ratio: float = 0.35
    rejection_atr_ratio: float = 0.5
    score_min: float = 0.45
    overlap_ratio: float = 0.60
    breakout_buffer_pct: float = 0.003
    sync_window: int = 3
    invalidated_retention_bars: int = 20
    r1_touch_weight: float = 0.30
    r1_recency_weight: float = 0.25
    r1_volume_weight: float = 0.15
    r1_rejection_weight: float = 0.15
    r1_tightness_weight: float = 0.10
    r1_distance_weight: float = 0.05
    r2_touch_weight: float = 0.35
    r2_recency_weight: float = 0.15
    r2_volume_weight: float = 0.15
    r2_rejection_weight: float = 0.15
    r2_tightness_weight: float = 0.10
    r2_height_weight: float = 0.10

    def __post_init__(self) -> None:
        float_fields = (
            "cluster_pct",
            "atr_gap_multiplier",
            "long_wick_ratio",
            "rejection_wick_ratio",
            "rejection_atr_ratio",
            "score_min",
            "overlap_ratio",
            "breakout_buffer_pct",
        )
        for field_name in float_fields:
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, Real)
                or not math.isfinite(value)
            ):
                raise ValueError(f"{field_name} must be finite")

        if self.cluster_pct <= 0:
            raise ValueError("cluster_pct must be greater than 0")
        if self.atr_gap_multiplier < 0:
            raise ValueError("atr_gap_multiplier must be non-negative")
        for field_name in ("long_wick_ratio", "rejection_wick_ratio", "score_min"):
            if not 0 <= getattr(self, field_name) <= 1:
                raise ValueError(f"{field_name} must be between 0 and 1")
        if self.rejection_atr_ratio < 0:
            raise ValueError("rejection_atr_ratio must be non-negative")
        if not 0 < self.overlap_ratio <= 1:
            raise ValueError("overlap_ratio must be greater than 0 and at most 1")
        if self.breakout_buffer_pct < 0:
            raise ValueError("breakout_buffer_pct must be non-negative")

        self._validate_integer("swing_order", self.swing_order, minimum=1)
        self._validate_integer("sync_window", self.sync_window, minimum=0)
        self._validate_integer(
            "invalidated_retention_bars",
            self.invalidated_retention_bars,
            minimum=0,
        )

        r1 = (
            self.r1_touch_weight,
            self.r1_recency_weight,
            self.r1_volume_weight,
            self.r1_rejection_weight,
            self.r1_tightness_weight,
            self.r1_distance_weight,
        )
        r2 = (
            self.r2_touch_weight,
            self.r2_recency_weight,
            self.r2_volume_weight,
            self.r2_rejection_weight,
            self.r2_tightness_weight,
            self.r2_height_weight,
        )
        self._validate_weights("R1", r1)
        self._validate_weights("R2", r2)
        if sum(r1) - self.r1_volume_weight <= 0:
            raise ValueError("R1 non-volume weights must have a positive sum")
        if sum(r2) - self.r2_volume_weight <= 0:
            raise ValueError("R2 non-volume weights must have a positive sum")

    @staticmethod
    def _validate_integer(name: str, value: Any, *, minimum: int) -> None:
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} must be an integer greater than or equal to {minimum}")

    @staticmethod
    def _validate_weights(name: str, weights: Sequence[float]) -> None:
        if any(
            isinstance(value, bool)
            or not isinstance(value, Real)
            or not math.isfinite(value)
            or value < 0
            for value in weights
        ):
            raise ValueError(f"{name} weights must be finite and non-negative")
        if not math.isclose(sum(weights), 1.0, abs_tol=1e-9):
            raise ValueError(f"{name} weights must sum to 1")


@dataclass(frozen=True)
class ResistanceZoneMetadata:
    data_source: str | None = None
    adj_factor_source: str | None = None


@dataclass(frozen=True)
class FrozenResistanceZoneEvidence:
    """Immutable JSON evidence shared by all registered zone parameters."""

    payload_json: str
    content_hash: str
    invariant_params_json: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonicalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime, date, np.datetime64)):
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return round(number, 6) if math.isfinite(number) else None
    if value is pd.NA:
        return None
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _weighted_quantile(
    values: Iterable[float],
    weights: Iterable[float],
    quantile: float,
) -> float:
    pairs = sorted(
        (float(value), float(weight))
        for value, weight in zip(values, weights)
        if math.isfinite(float(value))
        and math.isfinite(float(weight))
        and float(weight) > 0
    )
    if not pairs:
        raise ValueError("weighted quantile requires a positive finite weight")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1")
    threshold = quantile * sum(weight for _, weight in pairs)
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return pairs[-1][0]


def _atr_series(df: pd.DataFrame) -> pd.Series:
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1, skipna=True)
    return true_range.rolling(14, min_periods=14).mean()


def _atr14(df: pd.DataFrame) -> float | None:
    if len(df) < 14:
        return None
    value = float(_atr_series(df).iloc[-1])
    return value if math.isfinite(value) and value > 0 else None


def _date_at(df: pd.DataFrame, idx: int) -> str:
    for column in ("date", "trade_date"):
        if column in df.columns:
            return pd.Timestamp(df[column].iat[idx]).strftime("%Y-%m-%d")
    global_idx = int(df["_global_idx"].iat[idx]) if "_global_idx" in df else idx
    return f"index:{global_idx}"


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _volume_ratio_5(volume: pd.Series | None, idx: int) -> float | None:
    if volume is None:
        return None
    current = _safe_float(volume.iloc[idx])
    if current is None:
        return None
    previous = []
    for item in reversed(volume.iloc[:idx].tolist()):
        number = _safe_float(item)
        if number is not None and number > 0:
            previous.append(number)
            if len(previous) == 5:
                break
    if not previous:
        return None
    mean = sum(previous) / len(previous)
    return current / mean if mean > 0 else None


def _weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    total = sum(weights)
    return sum(value * weight for value, weight in zip(values, weights)) / total


def _score(features: dict[str, float], params: ResistanceZoneParams, role: str, has_volume: bool) -> float:
    names = ["touch", "recency", "volume", "rejection", "tightness"]
    if role == "r1":
        names.append("distance")
        weights = [
            params.r1_touch_weight,
            params.r1_recency_weight,
            params.r1_volume_weight,
            params.r1_rejection_weight,
            params.r1_tightness_weight,
            params.r1_distance_weight,
        ]
    else:
        names.append("height")
        weights = [
            params.r2_touch_weight,
            params.r2_recency_weight,
            params.r2_volume_weight,
            params.r2_rejection_weight,
            params.r2_tightness_weight,
            params.r2_height_weight,
        ]
    selected = [
        (features[name], weight)
        for name, weight in zip(names, weights)
        if has_volume or name != "volume"
    ]
    total = sum(weight for _, weight in selected)
    return sum(value * weight for value, weight in selected) / total


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class ResistanceZoneDetector:
    """Stateless facade for resistance-zone calculation."""

    @classmethod
    def calculate(
        cls,
        df: pd.DataFrame,
        *,
        a_idx: int,
        b_idx: int,
        candidate_version: str,
        params: ResistanceZoneParams = ResistanceZoneParams(),
        metadata: ResistanceZoneMetadata = ResistanceZoneMetadata(),
    ) -> dict[str, Any]:
        frozen = cls.freeze_evidence(
            df,
            a_idx=a_idx,
            b_idx=b_idx,
            candidate_version=candidate_version,
            params=params,
            metadata=metadata,
        )
        return cls.evaluate_frozen_evidence(frozen, params=params)

    @classmethod
    def freeze_evidence(
        cls,
        df: pd.DataFrame,
        *,
        a_idx: int,
        b_idx: int,
        candidate_version: str,
        params: ResistanceZoneParams = ResistanceZoneParams(),
        metadata: ResistanceZoneMetadata = ResistanceZoneMetadata(),
    ) -> FrozenResistanceZoneEvidence:
        if not isinstance(candidate_version, str):
            raise TypeError("candidate_version must be a string")
        if not 0 <= a_idx <= b_idx < len(df):
            raise ValueError("a_idx and b_idx must define a valid inclusive range")
        required = {"high", "low", "close"}
        missing = sorted(required.difference(df.columns))
        if missing:
            raise ValueError(f"missing required columns: {', '.join(missing)}")

        prefix = df.iloc[a_idx:b_idx + 1].copy()
        has_date = "date" in prefix or "trade_date" in prefix
        prefix["_global_idx"] = range(a_idx, b_idx + 1)
        prefix = prefix.reset_index(drop=True)
        local_b = len(prefix) - 1
        degradation: set[str] = set()
        if not has_date:
            degradation.add("missing_date")
        if "open" not in prefix:
            degradation.add("missing_open")
        if "volume" not in prefix:
            degradation.add("missing_volume")

        atr_values = _atr_series(prefix)
        atr_b = _atr14(prefix)
        if atr_b is None:
            degradation.add("atr_unavailable")

        touches = cls._extract_touches(
            prefix,
            original_a_idx=a_idx,
            atr_values=atr_values,
            atr_b=atr_b,
            params=params,
            degradation=degradation,
        )
        for touch in touches:
            touch.pop("tolerance", None)
        local_b = len(prefix) - 1
        payload = {
            "a_idx": a_idx,
            "b_idx": b_idx,
            "local_b": local_b,
            "candidate_version": candidate_version,
            "b_date": _date_at(prefix, local_b),
            "canonical_prefix": cls._canonical_prefix(prefix),
            "metadata": asdict(metadata),
            "degradation_reasons": sorted(degradation),
            "atr_b": atr_b,
            "b_close": float(prefix.iloc[local_b]["close"]),
            "b_low": float(prefix.iloc[local_b]["low"]),
            "max_high": float(
                pd.to_numeric(prefix["high"], errors="coerce").max()
            ),
            "touches": touches,
        }
        payload_json = json.dumps(
            _canonicalize(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        invariant_params_json = _canonical_json({
            "swing_order": params.swing_order,
            "long_wick_ratio": params.long_wick_ratio,
            "rejection_wick_ratio": params.rejection_wick_ratio,
            "rejection_atr_ratio": params.rejection_atr_ratio,
        })
        return FrozenResistanceZoneEvidence(
            payload_json=payload_json,
            content_hash=hashlib.sha256(
                payload_json.encode("utf-8")
            ).hexdigest(),
            invariant_params_json=invariant_params_json,
        )

    @classmethod
    def evaluate_frozen_evidence(
        cls,
        frozen: FrozenResistanceZoneEvidence,
        *,
        params: ResistanceZoneParams = ResistanceZoneParams(),
    ) -> dict[str, Any]:
        if not isinstance(frozen, FrozenResistanceZoneEvidence):
            raise TypeError(
                "frozen must be FrozenResistanceZoneEvidence"
            )
        if hashlib.sha256(
            frozen.payload_json.encode("utf-8")
        ).hexdigest() != frozen.content_hash:
            raise ValueError("frozen resistance evidence hash mismatch")
        expected_invariants = _canonical_json({
            "swing_order": params.swing_order,
            "long_wick_ratio": params.long_wick_ratio,
            "rejection_wick_ratio": params.rejection_wick_ratio,
            "rejection_atr_ratio": params.rejection_atr_ratio,
        })
        if expected_invariants != frozen.invariant_params_json:
            raise ValueError(
                "frozen resistance evidence parameter mismatch"
            )
        payload = json.loads(frozen.payload_json)
        atr_b = payload["atr_b"]
        touches = payload["touches"]
        for touch in touches:
            tolerance = touch["anchor_price"] * params.cluster_pct
            if atr_b is not None:
                tolerance = max(
                    tolerance,
                    atr_b * params.atr_gap_multiplier,
                )
            touch["tolerance"] = tolerance
        components = cls._connected_components(touches)
        components = cls._merge_overlaps(components, atr_b, params)

        b_date = payload["b_date"]
        snapshot = asdict(params)
        metadata_dict = payload["metadata"]
        version_payload = {
            "algorithm_version": ALGORITHM_VERSION,
            "candidate_version": payload["candidate_version"],
            "b_date": b_date,
            "ohlcv_prefix": payload["canonical_prefix"],
            "params": snapshot,
            "data_source": metadata_dict["data_source"],
            "adj_factor_source": metadata_dict["adj_factor_source"],
        }
        zone_version = _sha256(version_payload)
        b_close = payload["b_close"]
        b_low = payload["b_low"]
        max_high = payload["max_high"]
        local_b = payload["local_b"]

        internal_zones = []
        for component in components:
            zone = cls._build_zone(
                component,
                atr_b=atr_b,
                b_idx=local_b,
                b_close=b_close,
                b_low=b_low,
                max_high=max_high,
                zone_version=zone_version,
                params=params,
            )
            is_single_valid = (
                zone["touch_count"] == 1
                and component[0]["volume_ratio_5"] is not None
                and component[0]["volume_ratio_5"] >= 2
                and component[0]["strong_rejection"]
            )
            eligible_r1 = (
                zone["touch_count"] >= 2
                and zone["lower"] > b_close
                and zone["_r1_score"] >= params.score_min
            )
            eligible_r2 = (
                (zone["touch_count"] >= 2 or is_single_valid)
                and zone["_r2_score"] >= params.score_min
            )
            if eligible_r1 or eligible_r2:
                zone["_single_valid"] = is_single_valid
                internal_zones.append(zone)

        r1_internal = cls._select_r1(internal_zones, b_close, params)
        r2_internal = cls._select_r2(internal_zones, r1_internal, params)
        r1_id = r1_internal["zone_id"] if r1_internal else None
        r2_id = r2_internal["zone_id"] if r2_internal else None

        public_zones = []
        public_by_id = {}
        for zone in internal_zones:
            role_score = (
                zone["_r1_score"]
                if zone["zone_id"] == r1_id
                else zone["_r2_score"]
                if zone["zone_id"] == r2_id
                else max(zone["_r1_score"], zone["_r2_score"])
            )
            public = cls._public_zone(zone, role_score)
            public_zones.append(public)
            public_by_id[public["zone_id"]] = public
        public_zones.sort(key=lambda item: item["zone_id"])

        result = {
            "found": bool(public_zones),
            "algorithm_version": ALGORITHM_VERSION,
            "candidate_version": payload["candidate_version"],
            "zone_version": zone_version,
            "parameter_snapshot": snapshot,
            "metadata": metadata_dict,
            "zones": public_zones,
            "r1": dict(public_by_id[r1_id]) if r1_id else None,
            "r2": dict(public_by_id[r2_id]) if r2_id else None,
            "zone_count": int(r1_id is not None) + int(r2_id is not None),
            "degradation_reasons": payload["degradation_reasons"],
        }
        return _canonicalize(result)

    @staticmethod
    def _canonical_prefix(prefix: pd.DataFrame) -> list[dict[str, Any]]:
        size = len(prefix)
        date_column = next(
            (
                column
                for column in ("date", "trade_date")
                if column in prefix.columns
            ),
            None,
        )
        if date_column is not None:
            dates = [
                pd.Timestamp(value).strftime("%Y-%m-%d")
                for value in prefix[date_column].tolist()
            ]
        elif "_global_idx" in prefix:
            dates = [
                f"index:{int(value)}"
                for value in prefix["_global_idx"].tolist()
            ]
        else:
            dates = [f"index:{idx}" for idx in range(size)]
        values = {
            column: (
                prefix[column].tolist()
                if column in prefix
                else [None] * size
            )
            for column in ("open", "high", "low", "close", "volume")
        }
        return [
            {
                "date": dates[idx],
                **{
                    column: values[column][idx]
                    for column in (
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                    )
                },
            }
            for idx in range(size)
        ]

    @staticmethod
    def _extract_touches(
        prefix: pd.DataFrame,
        *,
        original_a_idx: int,
        atr_values: pd.Series,
        atr_b: float | None,
        params: ResistanceZoneParams,
        degradation: set[str],
    ) -> list[dict[str, Any]]:
        high_series = pd.to_numeric(prefix["high"], errors="coerce").reset_index(drop=True)
        swing_indices = set(find_swing_highs(high_series, order=params.swing_order))
        volume = prefix["volume"].reset_index(drop=True) if "volume" in prefix else None
        candidates: dict[str, dict[str, Any]] = {}
        high_values = high_series.tolist()
        low_values = pd.to_numeric(
            prefix["low"],
            errors="coerce",
        ).tolist()
        close_values = pd.to_numeric(
            prefix["close"],
            errors="coerce",
        ).tolist()
        open_values = (
            pd.to_numeric(prefix["open"], errors="coerce").tolist()
            if "open" in prefix
            else [None] * len(prefix)
        )
        atr_list = atr_values.tolist()
        dates = [_date_at(prefix, idx) for idx in range(len(prefix))]

        for idx in range(len(prefix)):
            high = _safe_float(high_values[idx])
            low = _safe_float(low_values[idx])
            close = _safe_float(close_values[idx])
            if high is None or low is None or close is None or high < low:
                continue
            open_price = _safe_float(open_values[idx])
            body_top = max(open_price, close) if open_price is not None else close
            upper_wick = max(0.0, high - body_top)
            full_range = high - low
            upper_wick_ratio = upper_wick / full_range if full_range > 0 else 0.0
            atr_at_bar = _safe_float(atr_list[idx])
            rejection_pct = max(0.0, high - close) / max(close, _EPSILON)
            if atr_at_bar is not None and atr_at_bar > 0:
                rejection_atr_ratio = max(0.0, high - close) / atr_at_bar
                rejection_score_component = rejection_atr_ratio
                is_rejection = (
                    upper_wick_ratio >= params.rejection_wick_ratio
                    and rejection_atr_ratio >= params.rejection_atr_ratio
                )
                strong_rejection = rejection_atr_ratio >= 1.0
            else:
                rejection_atr_ratio = None
                rejection_score_component = rejection_pct / 0.03
                is_rejection = (
                    upper_wick_ratio >= params.rejection_wick_ratio
                    and rejection_pct >= 0.015
                )
                strong_rejection = rejection_pct >= 0.03
            if idx not in swing_indices and not is_rejection:
                continue

            if upper_wick_ratio >= params.long_wick_ratio:
                cap = 0.75 * atr_b if atr_b is not None else body_top * 0.03
                anchor = body_top + min(upper_wick, cap)
            else:
                anchor = high
            tolerance = anchor * params.cluster_pct
            if atr_b is not None:
                tolerance = max(tolerance, atr_b * params.atr_gap_multiplier)
            volume_ratio = _volume_ratio_5(volume, idx)
            if volume_ratio is None:
                degradation.add("missing_volume")
            weight = min(3.0, max(0.5, volume_ratio)) if volume_ratio is not None else 1.0
            touch = {
                "idx": original_a_idx + idx,
                "_local_idx": idx,
                "date": dates[idx],
                "high": high,
                "close": close,
                "body_top": body_top,
                "anchor_price": anchor,
                "volume_ratio_5": volume_ratio,
                "upper_wick_ratio": upper_wick_ratio,
                "rejection_ratio": (
                    rejection_atr_ratio
                    if rejection_atr_ratio is not None
                    else rejection_pct
                ),
                "rejection_atr_ratio": rejection_atr_ratio,
                "rejection_pct": rejection_pct,
                "rejection_score_component": rejection_score_component,
                "_atr_available": atr_at_bar is not None and atr_at_bar > 0,
                "strong_rejection": strong_rejection,
                "weight": weight,
                "tolerance": tolerance,
            }
            existing = candidates.get(touch["date"])
            if existing is None or (touch["anchor_price"], touch["idx"]) > (
                existing["anchor_price"],
                existing["idx"],
            ):
                candidates[touch["date"]] = touch
        return sorted(candidates.values(), key=lambda item: (item["anchor_price"], item["date"]))

    @staticmethod
    def _connected_components(touches: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        if not touches:
            return []
        ordered = sorted(touches, key=lambda item: (item["anchor_price"], item["date"]))
        components = [[ordered[0]]]
        for touch in ordered[1:]:
            previous = components[-1][-1]
            gap = touch["anchor_price"] - previous["anchor_price"]
            if gap <= max(touch["tolerance"], previous["tolerance"]):
                components[-1].append(touch)
            else:
                components.append([touch])
        return components

    @classmethod
    def _bounds(
        cls,
        touches: list[dict[str, Any]],
        atr_b: float | None,
    ) -> tuple[float, float, float, float]:
        weights = [touch["weight"] for touch in touches]
        lower = _weighted_quantile([touch["body_top"] for touch in touches], weights, 0.25)
        center = max(
            lower,
            _weighted_quantile([touch["body_top"] for touch in touches], weights, 0.50),
        )
        upper_body = max(
            center,
            _weighted_quantile([touch["body_top"] for touch in touches], weights, 0.75),
        )
        high_q90 = _weighted_quantile([touch["high"] for touch in touches], weights, 0.90)
        cap = center + 2 * atr_b if atr_b is not None else center * 1.05
        upper = max(upper_body, min(high_q90, cap))
        return lower, center, upper_body, upper

    @classmethod
    def _merge_overlaps(
        cls,
        components: list[list[dict[str, Any]]],
        atr_b: float | None,
        params: ResistanceZoneParams,
    ) -> list[list[dict[str, Any]]]:
        merged = list(components)
        changed = True
        while changed:
            changed = False
            output: list[list[dict[str, Any]]] = []
            for component in merged:
                for index, existing in enumerate(output):
                    if cls._overlaps(existing, component, atr_b, params.overlap_ratio):
                        output[index] = sorted(
                            existing + component,
                            key=lambda item: (item["anchor_price"], item["date"]),
                        )
                        changed = True
                        break
                else:
                    output.append(component)
            merged = output
        return merged

    @classmethod
    def _overlaps(
        cls,
        left: list[dict[str, Any]],
        right: list[dict[str, Any]],
        atr_b: float | None,
        required: float,
    ) -> bool:
        left_lower, _, _, left_upper = cls._bounds(left, atr_b)
        right_lower, _, _, right_upper = cls._bounds(right, atr_b)
        intersection = max(0.0, min(left_upper, right_upper) - max(left_lower, right_lower))
        minimum_width = min(left_upper - left_lower, right_upper - right_lower)
        if minimum_width <= 0:
            return left_lower == right_lower
        return intersection / minimum_width >= required

    @classmethod
    def _build_zone(
        cls,
        touches: list[dict[str, Any]],
        *,
        atr_b: float | None,
        b_idx: int,
        b_close: float,
        b_low: float,
        max_high: float,
        zone_version: str,
        params: ResistanceZoneParams,
    ) -> dict[str, Any]:
        lower, center, upper_body, upper = cls._bounds(touches, atr_b)
        latest = max(touches, key=lambda item: (item["_local_idx"], item["date"]))
        weights = [touch["weight"] for touch in touches]
        available_volume = [
            (touch["volume_ratio_5"], touch["weight"])
            for touch in touches
            if touch["volume_ratio_5"] is not None
        ]
        if available_volume:
            volume = min(
                _weighted_mean(
                    [value for value, _ in available_volume],
                    [weight for _, weight in available_volume],
                )
                / 3,
                1.0,
            )
        else:
            volume = 0.0
        rejection = min(
            _weighted_mean(
                [touch["rejection_score_component"] for touch in touches],
                weights,
            ),
            1.0,
        )
        if atr_b is not None:
            tightness = 1 - min((upper_body - lower) / max(2 * atr_b, _EPSILON), 1.0)
        else:
            tightness = 1 - min(
                (upper_body - lower) / (max(center, _EPSILON) * 0.05),
                1.0,
            )
        distance = 1 - min((lower - b_close) / max(b_close * 0.25, _EPSILON), 1.0)
        height_denominator = max_high - b_low
        height = min(
            max((center - b_low) / max(height_denominator, _EPSILON), 0.0),
            1.0,
        )
        features = {
            "touch": min(len(touches) / 4, 1.0),
            "recency": math.exp(-(b_idx - latest["_local_idx"]) / 20),
            "volume": volume,
            "rejection": rejection,
            "tightness": tightness,
            "distance": distance,
            "height": height,
        }
        has_volume = bool(available_volume)
        touch_dates = sorted({touch["date"] for touch in touches})
        return {
            "zone_id": _sha256(
                {"zone_version": zone_version, "touch_dates": touch_dates}
            ),
            "lower": lower,
            "center": center,
            "upper_body": upper_body,
            "upper": upper,
            "touch_count": len(touches),
            "latest_touch_date": latest["date"],
            "_latest_idx": latest["_local_idx"],
            "touch_points": touches,
            "features": features,
            "_r1_score": _score(features, params, "r1", has_volume),
            "_r2_score": _score(features, params, "r2", has_volume),
        }

    @staticmethod
    def _select_r1(
        zones: list[dict[str, Any]],
        b_close: float,
        params: ResistanceZoneParams,
    ) -> dict[str, Any] | None:
        candidates = [
            zone
            for zone in zones
            if zone["touch_count"] >= 2
            and zone["lower"] > b_close
            and zone["_r1_score"] >= params.score_min
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda zone: (
                1 - zone["features"]["distance"],
                -zone["_r1_score"],
                -zone["_latest_idx"],
                zone["zone_id"],
            ),
        )

    @staticmethod
    def _select_r2(
        zones: list[dict[str, Any]],
        r1: dict[str, Any] | None,
        params: ResistanceZoneParams,
    ) -> dict[str, Any] | None:
        candidates = [
            zone
            for zone in zones
            if (zone["touch_count"] >= 2 or zone.get("_single_valid"))
            and zone["_r2_score"] >= params.score_min
            and (
                r1 is None
                or (
                    zone["zone_id"] != r1["zone_id"]
                    and zone["lower"] > r1["upper"]
                )
            )
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda zone: (
                -zone["_r2_score"],
                -zone["center"],
                -zone["touch_count"],
                -zone["_latest_idx"],
                zone["zone_id"],
            ),
        )

    @staticmethod
    def _public_zone(zone: dict[str, Any], score: float) -> dict[str, Any]:
        confidence = (
            "low"
            if zone["touch_count"] == 1
            else "high"
            if score >= 0.65
            else "medium"
        )
        touch_points = []
        for touch in sorted(
            zone["touch_points"],
            key=lambda item: (item["date"], item["idx"]),
        ):
            touch_points.append(
                {
                    "idx": touch["idx"],
                    "date": touch["date"],
                    "high": touch["high"],
                    "close": touch["close"],
                    "body_top": touch["body_top"],
                    "anchor_price": touch["anchor_price"],
                    "volume_ratio_5": touch["volume_ratio_5"],
                    "upper_wick_ratio": touch["upper_wick_ratio"],
                    "rejection_ratio": touch["rejection_ratio"],
                    "rejection_atr_ratio": touch["rejection_atr_ratio"],
                    "rejection_pct": touch["rejection_pct"],
                    "rejection_score_component": touch[
                        "rejection_score_component"
                    ],
                }
            )
        return _canonicalize(
            {
                "zone_id": zone["zone_id"],
                "lower": zone["lower"],
                "center": zone["center"],
                "upper_body": zone["upper_body"],
                "upper": zone["upper"],
                "score": score,
                "confidence": confidence,
                "touch_count": zone["touch_count"],
                "latest_touch_date": zone["latest_touch_date"],
                "touch_points": touch_points,
                "features": zone["features"],
            }
        )
