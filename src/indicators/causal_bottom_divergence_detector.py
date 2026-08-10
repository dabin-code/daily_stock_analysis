"""Causal bottom-divergence candidates with frozen resistance evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import base64
import hashlib
import json
import zlib
from typing import Any

import numpy as np
import pandas as pd

from src.indicators.causal_bottom_divergence_events import (
    actionability_v2,
    scan_early_v2,
    with_trendline_breakout_v2,
    zone_events_v2,
)
from src.indicators.causal_bottom_divergence_support import (
    VALID_PATTERNS_V2,
    classify_relation_v2,
    date_at_v2,
    event_v2,
    find_swing_highs_v2,
    find_swing_lows_v2,
    has_required_context_v2,
    macd_semantics_v2,
    normalize_visible_market_data,
    validate_detector_params,
)
from src.indicators.divergence_detector import compute_macd
from src.indicators.resistance_zone_detector import (
    FrozenResistanceZoneEvidence,
    ResistanceZoneDetector,
    ResistanceZoneMetadata,
    ResistanceZoneParams,
    _atr_series,
    _canonical_json,
)


ALGORITHM_VERSION = "causal-bottom-divergence-v2"
_STAGE_PRIORITY = {
    "major_actionable": 4,
    "major_unverified": 4,
    "stale": 4,
    "extended": 4,
    "breakout_failed": 4,
    "invalidated": 4,
    "near_cleared": 3,
    "early": 2,
    "forming": 1,
}
_LIFECYCLE_PRIORITY = {"confirmed": 2, "provisional": 1}
_MAJOR_NON_ACTIONABLE_STAGES = {
    "confirmation_too_old": "stale",
    "extension_out_of_range": "extended",
    "candidate_invalidated": "invalidated",
    "structure_floor_broken": "invalidated",
    "structure_broken": "invalidated",
    "adjustment_unknown": "major_unverified",
    "below_r2": "breakout_failed",
    "price_below_major_zone": "breakout_failed",
}
_TRUSTED_ADJUSTMENT_SOURCES = {
    "tushare_native",
    "akshare_qfq_div_raw",
}

_date_at = date_at_v2
_event = event_v2


@dataclass(frozen=True)
class FrozenCausalEvidence:
    """Immutable parameter-independent A/B and resistance input evidence."""

    payload_json: str
    content_hash: str
    invariant_params_json: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    def decode_payload(self) -> dict[str, Any]:
        raw = zlib.decompress(
            base64.b64decode(self.payload_json.encode("ascii"))
        ).decode("utf-8")
        return json.loads(raw)


def _point(df: pd.DataFrame, idx: int, price: float) -> dict[str, Any]:
    return {
        "idx": int(idx),
        "bar_index": int(idx),
        "date": _date_at(df, idx),
        "price": round(float(price), 6),
    }


def _empty_result(
    reason: str | None = None,
    degradation_reasons: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "found": False,
        "algorithm_version": ALGORITHM_VERSION,
        "candidate_records": [],
        "primary_candidate_version": None,
        "candidate_version": None,
        "lifecycle": None,
        "invalidated_at": None,
        "a": None,
        "b": None,
        "macd": None,
        "h": None,
        "pattern": None,
        "zone": None,
        "frozen_trendline": None,
        "early_reversal": None,
        "near_zone_events": None,
        "major_zone_breakout": None,
        "major_zone_actionable_entry": None,
        "actionability_status": reason,
        "stage": None,
        "signal_strength": 0.0,
        "stop_loss_price": None,
        "layered_buy_points": [],
        "degradation_reasons": degradation_reasons or [],
        "rejection_reason": reason,
    }


def _candidate_metadata(
    visible: pd.DataFrame,
    *,
    a_idx: int,
    b_idx: int,
    fallback: ResistanceZoneMetadata,
) -> ResistanceZoneMetadata:
    """Freeze provenance and adjustment health to the candidate A/B prefix."""
    segment = visible.iloc[a_idx:b_idx + 1]

    def unique_nonempty(column: str) -> tuple[list[str], bool]:
        values: list[str] = []
        missing_value = False
        for raw in segment[column].tolist():
            if pd.isna(raw) or not str(raw).strip():
                missing_value = True
                continue
            values.append(str(raw).strip())
        return sorted(set(values)), missing_value

    if "data_source" in segment.columns:
        data_sources, data_missing = unique_nonempty("data_source")
        data_source = "|".join(data_sources) or None
    else:
        data_source = fallback.data_source
        data_missing = not bool((data_source or "").strip())

    if "adj_factor_source" in segment.columns:
        adjustment_sources, adjustment_missing = unique_nonempty(
            "adj_factor_source"
        )
        adjustment_source = "|".join(adjustment_sources) or None
    else:
        adjustment_source = fallback.adj_factor_source
        adjustment_sources = [
            value.strip()
            for value in (adjustment_source or "").split("|")
            if value.strip()
        ]
        adjustment_missing = not adjustment_sources

    adjustment_healthy = (
        not adjustment_missing
        and bool(adjustment_sources)
        and all(
            value.lower() in _TRUSTED_ADJUSTMENT_SOURCES
            for value in adjustment_sources
        )
    )
    if "adj_factor" in segment.columns:
        factors = pd.to_numeric(segment["adj_factor"], errors="coerce")
        adjustment_healthy = adjustment_healthy and bool(
            not factors.isna().any()
            and np.isfinite(factors.to_numpy(dtype=float)).all()
            and (factors > 0).all()
        )
    elif not adjustment_healthy:
        adjustment_healthy = False

    unsafe_data_markers = {
        "unknown",
        "fetcher_unset",
        "legacy_assume_one",
    }
    if (
        data_missing
        or not data_source
        or any(
            marker in value.lower()
            for value in (data_source.split("|") if data_source else [])
            for marker in unsafe_data_markers
        )
    ):
        adjustment_healthy = False

    return ResistanceZoneMetadata(
        data_source=data_source,
        adj_factor_source=(
            adjustment_source if adjustment_healthy else "unknown"
        ),
    )


def _local_min(series: pd.Series, center: int, window: int, cap: int) -> int:
    start = max(0, center - window)
    end = min(cap, center + window) + 1
    return int(series.iloc[start:end].idxmin())


def _macd_point(
    df: pd.DataFrame,
    dif: pd.Series,
    dea: pd.Series,
    *,
    center: int,
    window: int,
    cap: int,
) -> dict[str, Any]:
    dif_idx = _local_min(dif, center, window, cap)
    dea_idx = _local_min(dea, center, window, cap)
    dif_value = float(dif.iloc[dif_idx])
    dea_value = float(dea.iloc[dea_idx])
    return {
        "idx": dif_idx,
        "date": _date_at(df, dif_idx),
        "dif": round(dif_value, 6),
        "dea": round(dea_value, 6),
        "dif_point": {
            "idx": dif_idx,
            "date": _date_at(df, dif_idx),
            "value": round(dif_value, 6),
        },
        "dea_point": {
            "idx": dea_idx,
            "date": _date_at(df, dea_idx),
            "value": round(dea_value, 6),
        },
    }


_macd_semantics = macd_semantics_v2


def _causal_swing_indices(
    series: pd.Series,
    fully_confirmed: list[int],
    *,
    evidence_end: int,
    order: int,
    mode: str,
) -> list[int]:
    confirmed = {
        idx for idx in fully_confirmed if idx + order <= evidence_end
    }
    values = series.to_numpy(dtype=float)
    start = max(order, evidence_end - order + 1)
    for idx in range(start, evidence_end):
        left = values[idx - order:idx]
        right = values[idx + 1:min(evidence_end + 1, idx + order + 1)]
        if len(right) < 1:
            continue
        if mode == "low":
            matches = values[idx] <= np.min(left) and values[idx] <= np.min(right)
        else:
            matches = values[idx] >= np.max(left) and values[idx] >= np.max(right)
        if matches:
            confirmed.add(idx)
    return sorted(confirmed)


def _candidate_version(
    *,
    pattern: dict[str, Any],
    a: dict[str, Any],
    b: dict[str, Any],
    macd: dict[str, Any],
) -> str:
    def identity_price(point: dict[str, Any]) -> dict[str, Any]:
        return {"date": point["date"], "price": point["price"]}

    def identity_macd(point: dict[str, Any]) -> dict[str, Any]:
        return {
            "dif": {
                "date": point["dif_point"]["date"],
                "value": point["dif_point"]["value"],
            },
            "dea": {
                "date": point["dea_point"]["date"],
                "value": point["dea_point"]["value"],
            },
        }

    payload = {
        "algorithm": ALGORITHM_VERSION,
        "pattern": {
            "code": pattern["code"],
            "price_relation": pattern["price_relation"],
            "macd_relation": pattern["macd_relation"],
        },
        "a": identity_price(a),
        "b": identity_price(b),
        "macd_a": identity_macd(macd["a"]),
        "macd_b": identity_macd(macd["b"]),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _frozen_trendline(
    frozen: pd.DataFrame,
    *,
    a_idx: int,
    b_idx: int,
) -> dict[str, Any]:
    empty = {
        "found": False,
        "slope": 0.0,
        "intercept": 0.0,
        "touches": [],
        "touch_points": [],
        "breakout_confirmed": False,
        "breakout_bar_index": None,
        "breakout_date": None,
        "projected_value_at_breakout": None,
    }
    start = max(0, a_idx - 20)
    high = pd.to_numeric(frozen["high"], errors="coerce").reset_index(drop=True)
    segment = high.iloc[start:b_idx + 1].reset_index(drop=True)
    swing_highs = [
        start + idx for idx in find_swing_highs_v2(segment, order=3)
    ]
    pairs: list[tuple[int, float, str, int, int, float, float]] = []
    for left_pos, left_idx in enumerate(swing_highs):
        left_price = float(high.iloc[left_idx])
        for right_idx in swing_highs[left_pos + 1:]:
            right_price = float(high.iloc[right_idx])
            if right_price >= left_price:
                continue
            slope = (right_price - left_price) / (right_idx - left_idx)
            intercept = left_price - slope * left_idx
            if any(
                float(high.iloc[mid]) > (slope * mid + intercept) * 1.025
                for mid in swing_highs
                if left_idx < mid < right_idx
            ):
                continue
            pairs.append(
                (
                    -(right_idx - left_idx),
                    -left_price,
                    _date_at(frozen, left_idx),
                    left_idx,
                    right_idx,
                    slope,
                    intercept,
                )
            )
    if not pairs:
        return empty
    _, _, _, left_idx, right_idx, slope, intercept = min(pairs)
    touches = [
        _point(frozen, left_idx, high.iloc[left_idx]),
        _point(frozen, right_idx, high.iloc[right_idx]),
    ]
    return {
        **empty,
        "found": True,
        "slope": round(float(slope), 6),
        "intercept": round(float(intercept), 6),
        "touches": touches,
        "touch_points": touches,
    }


def _signal_strength(
    *,
    a_price: float,
    b_price: float,
    dif_a: float,
    dif_b: float,
    h_price: float,
    trendline: dict[str, Any],
    zone: dict[str, Any],
) -> float:
    divergence = max(0.0, (dif_b - dif_a) / max(abs(dif_a), 1e-12))
    bounce = max(0.0, (h_price - min(a_price, b_price)) / min(a_price, b_price))
    score = (
        0.30
        + min(divergence, 1.0) * 0.25
        + min(bounce / 0.30, 1.0) * 0.20
        + (0.10 if trendline["found"] else 0.0)
        + (0.15 if zone.get("found") else 0.0)
    )
    return round(min(max(score, 0.0), 1.0), 6)


def _visible_fingerprint(visible: pd.DataFrame) -> str:
    columns = [
        column
        for column in (
            "date",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "pct_chg",
            "data_source",
            "adj_factor",
            "adj_factor_source",
        )
        if column in visible.columns
    ]
    payload = visible[columns].to_dict(orient="records")
    return hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()


def _causal_invariant_params(
    *,
    zone_params: ResistanceZoneParams,
    lookback: int,
    min_ab_gap: int,
    max_ab_gap: int,
    ab_match_window: int,
    flat_tolerance: float,
    macd_flat_tolerance: float,
    break_tolerance: float,
) -> str:
    zone_snapshot = asdict(zone_params)
    for field_name in (
        "cluster_pct",
        "atr_gap_multiplier",
        "score_min",
    ):
        zone_snapshot.pop(field_name)
    return _canonical_json({
        "zone": zone_snapshot,
        "lookback": lookback,
        "min_ab_gap": min_ab_gap,
        "max_ab_gap": max_ab_gap,
        "ab_match_window": ab_match_window,
        "flat_tolerance": flat_tolerance,
        "macd_flat_tolerance": macd_flat_tolerance,
        "break_tolerance": break_tolerance,
    })


class CausalBottomDivergenceDetector:
    """Rebuild every candidate from its original B+1 evidence boundary."""

    @classmethod
    def detect(
        cls,
        df: pd.DataFrame,
        *,
        as_of_index: int | None = None,
        zone_params: ResistanceZoneParams = ResistanceZoneParams(),
        lookback: int = 100,
        min_ab_gap: int = 10,
        max_ab_gap: int = 60,
        ab_match_window: int = 5,
        flat_tolerance: float = 0.05,
        macd_flat_tolerance: float = 0.30,
        break_tolerance: float = 0.0,
        sync_window: int | None = None,
        retention_bars: int | None = None,
        metadata: ResistanceZoneMetadata = ResistanceZoneMetadata(),
    ) -> dict[str, Any]:
        validate_detector_params(
            zone_params=zone_params,
            lookback=lookback,
            min_ab_gap=min_ab_gap,
            max_ab_gap=max_ab_gap,
            ab_match_window=ab_match_window,
            flat_tolerance=flat_tolerance,
            macd_flat_tolerance=macd_flat_tolerance,
            break_tolerance=break_tolerance,
            sync_window=sync_window,
            retention_bars=retention_bars,
            metadata=metadata,
        )
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return _empty_result("insufficient_data")
        overrides = {}
        if sync_window is not None:
            overrides["sync_window"] = sync_window
        if retention_bars is not None:
            overrides["invalidated_retention_bars"] = retention_bars
        effective_zone_params = (
            replace(zone_params, **overrides) if overrides else zone_params
        )
        effective_retention = effective_zone_params.invalidated_retention_bars
        as_of = len(df) - 1 if as_of_index is None else as_of_index
        if type(as_of) is not int or not 0 <= as_of < len(df):
            return _empty_result("as_of_out_of_range")
        visible = df.iloc[:as_of + 1].copy().reset_index(drop=True)
        if len(visible) < 35:
            return _empty_result("insufficient_data")
        normalized, degradation = normalize_visible_market_data(visible)
        if normalized is None:
            return _empty_result("invalid_market_data", degradation)
        visible = normalized
        close = visible["close"].reset_index(drop=True)
        dif, dea, _ = compute_macd(close, 12, 26, 9)
        atr_values = _atr_series(visible)
        low = visible["low"].reset_index(drop=True)
        high = visible["high"].reset_index(drop=True)
        all_swing_lows = find_swing_lows_v2(low, order=5)
        all_swing_highs = find_swing_highs_v2(high, order=5)

        records: dict[str, dict[str, Any]] = {}
        zone_cache: dict[
            tuple[int, int, str, str | None, str | None],
            dict[str, Any],
        ] = {}
        scan_start = max(5, as_of - max(lookback, effective_retention + 5))
        for b_idx in range(scan_start, as_of):
            if not cls._was_provisional(low, b_idx):
                continue
            for evidence in cls._freeze_candidates(
                visible,
                b_idx=b_idx,
                min_ab_gap=min_ab_gap,
                max_ab_gap=max_ab_gap,
                ab_match_window=ab_match_window,
                flat_tolerance=flat_tolerance,
                macd_flat_tolerance=macd_flat_tolerance,
                zone_params=effective_zone_params,
                metadata=metadata,
                dif=dif,
                dea=dea,
                all_swing_lows=all_swing_lows,
                all_swing_highs=all_swing_highs,
                zone_cache=zone_cache,
            ):
                record = cls._replay_candidate(
                    visible,
                    evidence=evidence,
                    break_tolerance=break_tolerance,
                    retention_bars=effective_retention,
                    zone_params=effective_zone_params,
                    metadata=metadata,
                    atr_values=atr_values,
                )
                if record is not None:
                    records[record["candidate_version"]] = record

        ordered = sorted(records.values(), key=cls._sort_key)
        active = [item for item in ordered if item["lifecycle"] != "invalidated"]
        result = _empty_result()
        result["candidate_records"] = ordered
        if not active:
            return result
        primary = active[0]
        result.update(primary)
        result["found"] = True
        result["candidate_records"] = ordered
        result["primary_candidate_version"] = primary["candidate_version"]
        return result

    @classmethod
    def freeze_evidence(
        cls,
        df: pd.DataFrame,
        *,
        as_of_index: int | None = None,
        zone_params: ResistanceZoneParams = ResistanceZoneParams(),
        lookback: int = 100,
        min_ab_gap: int = 10,
        max_ab_gap: int = 60,
        ab_match_window: int = 5,
        flat_tolerance: float = 0.05,
        macd_flat_tolerance: float = 0.30,
        break_tolerance: float = 0.0,
        sync_window: int | None = None,
        retention_bars: int | None = None,
        metadata: ResistanceZoneMetadata = ResistanceZoneMetadata(),
        evaluation_as_of_indices: tuple[int, ...] | None = None,
    ) -> FrozenCausalEvidence:
        """Freeze parameter-independent causal candidates at one as-of date."""
        validate_detector_params(
            zone_params=zone_params,
            lookback=lookback,
            min_ab_gap=min_ab_gap,
            max_ab_gap=max_ab_gap,
            ab_match_window=ab_match_window,
            flat_tolerance=flat_tolerance,
            macd_flat_tolerance=macd_flat_tolerance,
            break_tolerance=break_tolerance,
            sync_window=sync_window,
            retention_bars=retention_bars,
            metadata=metadata,
        )
        overrides = {}
        if sync_window is not None:
            overrides["sync_window"] = sync_window
        if retention_bars is not None:
            overrides["invalidated_retention_bars"] = retention_bars
        effective_zone_params = (
            replace(zone_params, **overrides) if overrides else zone_params
        )
        invariant_params_json = _causal_invariant_params(
            zone_params=effective_zone_params,
            lookback=lookback,
            min_ab_gap=min_ab_gap,
            max_ab_gap=max_ab_gap,
            ab_match_window=ab_match_window,
            flat_tolerance=flat_tolerance,
            macd_flat_tolerance=macd_flat_tolerance,
            break_tolerance=break_tolerance,
        )

        def freeze_payload(payload: dict[str, Any]) -> FrozenCausalEvidence:
            canonical = _canonical_json(payload).encode("utf-8")
            payload_json = base64.b64encode(
                zlib.compress(canonical, level=6)
            ).decode("ascii")
            return FrozenCausalEvidence(
                payload_json=payload_json,
                content_hash=hashlib.sha256(
                    payload_json.encode("utf-8")
                ).hexdigest(),
                invariant_params_json=invariant_params_json,
            )

        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return freeze_payload({
                "empty_result": _empty_result("insufficient_data"),
            })
        as_of = len(df) - 1 if as_of_index is None else as_of_index
        if type(as_of) is not int or not 0 <= as_of < len(df):
            return freeze_payload({
                "empty_result": _empty_result("as_of_out_of_range"),
            })
        visible = df.iloc[:as_of + 1].copy().reset_index(drop=True)
        if len(visible) < 35:
            return freeze_payload({
                "empty_result": _empty_result("insufficient_data"),
            })
        normalized, degradation = normalize_visible_market_data(visible)
        if normalized is None:
            return freeze_payload({
                "empty_result": _empty_result(
                    "invalid_market_data",
                    degradation,
                ),
            })
        visible = normalized
        close = visible["close"].reset_index(drop=True)
        dif, dea, _ = compute_macd(close, 12, 26, 9)
        low = visible["low"].reset_index(drop=True)
        high = visible["high"].reset_index(drop=True)
        all_swing_lows = find_swing_lows_v2(low, order=5)
        all_swing_highs = find_swing_highs_v2(high, order=5)
        evidence_by_version: dict[str, dict[str, Any]] = {}
        zone_cache: dict[
            tuple[int, int, str, str | None, str | None],
            FrozenResistanceZoneEvidence,
        ] = {}
        effective_retention = (
            effective_zone_params.invalidated_retention_bars
        )
        evaluation_indices = tuple(sorted(set(
            evaluation_as_of_indices or (as_of,)
        )))
        if (
            not evaluation_indices
            or any(
                type(item) is not int or not 0 <= item <= as_of
                for item in evaluation_indices
            )
        ):
            raise ValueError(
                "evaluation_as_of_indices must be valid frozen-prefix indices"
            )
        scan_start = min(
            max(5, item - max(lookback, effective_retention + 5))
            for item in evaluation_indices
        )
        for b_idx in range(scan_start, as_of):
            if not cls._was_provisional(low, b_idx):
                continue
            for evidence in cls._freeze_candidates(
                visible,
                b_idx=b_idx,
                min_ab_gap=min_ab_gap,
                max_ab_gap=max_ab_gap,
                ab_match_window=ab_match_window,
                flat_tolerance=flat_tolerance,
                macd_flat_tolerance=macd_flat_tolerance,
                zone_params=effective_zone_params,
                metadata=metadata,
                dif=dif,
                dea=dea,
                all_swing_lows=all_swing_lows,
                all_swing_highs=all_swing_highs,
                zone_cache=zone_cache,
                materialize_zone=False,
                relevant_as_of_indices=evaluation_indices,
                retention_bars=effective_retention,
            ):
                evidence_by_version[evidence["candidate_version"]] = evidence
        return freeze_payload({
            "as_of_index": as_of,
            "evaluation_as_of_indices": list(evaluation_indices),
            "visible_fingerprints": {
                str(item): _visible_fingerprint(
                    normalize_visible_market_data(
                        df.iloc[:item + 1].copy().reset_index(drop=True)
                    )[0]
                )
                for item in evaluation_indices
            },
            "candidate_evidence": [
                evidence_by_version[key]
                for key in sorted(evidence_by_version)
            ],
            "detector": {
                "lookback": lookback,
                "min_ab_gap": min_ab_gap,
                "max_ab_gap": max_ab_gap,
                "ab_match_window": ab_match_window,
                "flat_tolerance": flat_tolerance,
                "macd_flat_tolerance": macd_flat_tolerance,
                "break_tolerance": break_tolerance,
                "retention_bars": effective_retention,
            },
            "metadata": asdict(metadata),
        })

    @classmethod
    def evaluate_frozen_evidence(
        cls,
        df: pd.DataFrame,
        frozen: FrozenCausalEvidence,
        *,
        zone_params: ResistanceZoneParams = ResistanceZoneParams(),
        as_of_index: int | None = None,
    ) -> dict[str, Any]:
        """Evaluate frozen A/B evidence with one registered zone snapshot."""
        if not isinstance(frozen, FrozenCausalEvidence):
            raise TypeError("frozen must be FrozenCausalEvidence")
        if hashlib.sha256(
            frozen.payload_json.encode("utf-8")
        ).hexdigest() != frozen.content_hash:
            raise ValueError("frozen causal evidence hash mismatch")
        payload = frozen.decode_payload()
        if "empty_result" in payload:
            return payload["empty_result"]
        detector = payload["detector"]
        expected_invariants = _causal_invariant_params(
            zone_params=replace(
                zone_params,
                invalidated_retention_bars=detector["retention_bars"],
            ),
            lookback=detector["lookback"],
            min_ab_gap=detector["min_ab_gap"],
            max_ab_gap=detector["max_ab_gap"],
            ab_match_window=detector["ab_match_window"],
            flat_tolerance=detector["flat_tolerance"],
            macd_flat_tolerance=detector["macd_flat_tolerance"],
            break_tolerance=detector["break_tolerance"],
        )
        if expected_invariants != frozen.invariant_params_json:
            raise ValueError("frozen causal evidence parameter mismatch")
        as_of = (
            payload["as_of_index"]
            if as_of_index is None
            else as_of_index
        )
        if as_of not in payload["evaluation_as_of_indices"]:
            raise ValueError(
                "requested as_of_index was not frozen"
            )
        if df is None or not isinstance(df, pd.DataFrame) or as_of >= len(df):
            raise ValueError("frozen causal evidence source is unavailable")
        visible = df.iloc[:as_of + 1].copy().reset_index(drop=True)
        normalized, degradation = normalize_visible_market_data(visible)
        if normalized is None:
            raise ValueError(
                "frozen causal evidence source is no longer valid"
            )
        visible = normalized
        if _visible_fingerprint(visible) != payload[
            "visible_fingerprints"
        ][str(as_of)]:
            raise ValueError("frozen causal evidence source hash mismatch")
        atr_values = _atr_series(visible)
        metadata = ResistanceZoneMetadata(**payload["metadata"])
        records: dict[str, dict[str, Any]] = {}
        low_values = pd.to_numeric(
            visible["low"],
            errors="coerce",
        ).tolist()
        scan_start = max(
            5,
            as_of - max(
                detector["lookback"],
                detector["retention_bars"] + 5,
            ),
        )
        for raw_evidence in payload["candidate_evidence"]:
            evidence = dict(raw_evidence)
            b_idx = int(evidence["b"]["idx"])
            if not scan_start <= b_idx < as_of:
                continue
            b_price = float(evidence["b"]["price"])
            invalid_idx = next(
                (
                    idx
                    for idx in range(
                        b_idx + 1,
                        min(b_idx + 5, as_of) + 1,
                    )
                    if float(low_values[idx]) < b_price
                ),
                None,
            )
            if (
                invalid_idx is not None
                and as_of - invalid_idx > detector["retention_bars"]
            ):
                continue
            zone_frozen = FrozenResistanceZoneEvidence(
                **evidence.pop("zone_evidence")
            )
            zone = ResistanceZoneDetector.evaluate_frozen_evidence(
                zone_frozen,
                params=replace(
                    zone_params,
                    invalidated_retention_bars=detector["retention_bars"],
                ),
            )
            evidence["zone"] = zone
            evidence["degradation_reasons"] = sorted(set(
                evidence.get("degradation_reasons", [])
                + list(zone.get("degradation_reasons", []))
            ))
            record = cls._replay_candidate(
                visible,
                evidence=evidence,
                break_tolerance=detector["break_tolerance"],
                retention_bars=detector["retention_bars"],
                zone_params=replace(
                    zone_params,
                    invalidated_retention_bars=detector["retention_bars"],
                ),
                metadata=metadata,
                atr_values=atr_values,
            )
            if record is not None:
                records[record["candidate_version"]] = record
        ordered = sorted(records.values(), key=cls._sort_key)
        active = [
            item for item in ordered if item["lifecycle"] != "invalidated"
        ]
        result = _empty_result()
        result["candidate_records"] = ordered
        if not active:
            return result
        primary = active[0]
        result.update(primary)
        result["found"] = True
        result["candidate_records"] = ordered
        result["primary_candidate_version"] = primary[
            "candidate_version"
        ]
        return result

    @staticmethod
    def _was_provisional(low: pd.Series, b_idx: int) -> bool:
        if b_idx < 5 or b_idx + 1 >= len(low):
            return False
        values = low.iloc[b_idx - 5:b_idx + 2]
        if values.isna().any():
            return False
        b_low = float(low.iloc[b_idx])
        return (
            b_low <= float(low.iloc[b_idx - 5:b_idx].min())
            and b_low <= float(low.iloc[b_idx + 1])
        )

    @classmethod
    def _freeze_candidates(
        cls,
        visible: pd.DataFrame,
        *,
        b_idx: int,
        min_ab_gap: int,
        max_ab_gap: int,
        ab_match_window: int,
        flat_tolerance: float,
        macd_flat_tolerance: float,
        zone_params: ResistanceZoneParams,
        metadata: ResistanceZoneMetadata,
        dif: pd.Series,
        dea: pd.Series,
        all_swing_lows: list[int],
        all_swing_highs: list[int],
        zone_cache: dict[
            tuple[int, int, str, str | None, str | None],
            Any,
        ],
        materialize_zone: bool = True,
        relevant_as_of_indices: tuple[int, ...] | None = None,
        retention_bars: int = 20,
    ) -> list[dict[str, Any]]:
        frozen = visible.iloc[:b_idx + 2].copy().reset_index(drop=True)
        low = visible["low"].reset_index(drop=True)
        high = visible["high"].reset_index(drop=True)
        evidence_end = b_idx + 1
        swing_lows = [
            idx
            for idx in _causal_swing_indices(
                low,
                all_swing_lows,
                evidence_end=evidence_end,
                order=5,
                mode="low",
            )
            if idx < b_idx and min_ab_gap <= b_idx - idx <= max_ab_gap
        ]
        swing_highs = _causal_swing_indices(
            high,
            all_swing_highs,
            evidence_end=evidence_end,
            order=5,
            mode="high",
        )
        output = []
        for a_idx in swing_lows:
            between = [idx for idx in swing_highs if a_idx < idx < b_idx]
            if not between:
                continue
            h_idx = max(between, key=lambda idx: (float(high.iloc[idx]), -idx))
            a_price = float(low.iloc[a_idx])
            b_price = float(low.iloc[b_idx])
            h_price = float(high.iloc[h_idx])
            floor = min(a_price, b_price)
            if (
                floor <= 0
                or h_price <= max(a_price, b_price)
                or (h_price - floor) / floor < 0.10
            ):
                continue
            macd_a = _macd_point(
                frozen,
                dif,
                dea,
                center=a_idx,
                window=ab_match_window,
                cap=b_idx + 1,
            )
            macd_b = _macd_point(
                frozen,
                dif,
                dea,
                center=b_idx,
                window=ab_match_window,
                cap=b_idx + 1,
            )
            semantics = macd_semantics_v2(
                a_price=a_price,
                dif_a=float(dif.iloc[macd_a["dif_point"]["idx"]]),
                dif_b=float(dif.iloc[macd_b["dif_point"]["idx"]]),
                dea_a=float(dea.iloc[macd_a["dea_point"]["idx"]]),
                dea_b=float(dea.iloc[macd_b["dea_point"]["idx"]]),
                tolerance=macd_flat_tolerance,
            )
            if semantics is None:
                continue
            dif_relation = semantics["dif_relation"]
            dea_relation = semantics["dea_relation"]
            macd_relation = semantics["macd_relation"]
            price_relation = classify_relation_v2(
                a_price, b_price, flat_tolerance
            )
            info = VALID_PATTERNS_V2.get((price_relation, macd_relation))
            if info is None:
                continue
            if not has_required_context_v2(frozen, a_idx, info["family"]):
                continue
            a = _point(frozen, a_idx, a_price)
            b = _point(frozen, b_idx, b_price)
            h = _point(frozen, h_idx, h_price)
            pattern = {
                "code": info["code"],
                "label": info["label"],
                "family": info["family"],
                "price_relation": price_relation,
                "macd_relation": macd_relation,
                "dif_relation": dif_relation,
                "dea_relation": dea_relation,
            }
            macd = {"a": macd_a, "b": macd_b}
            version = _candidate_version(
                pattern=pattern, a=a, b=b, macd=macd
            )
            if relevant_as_of_indices is not None:
                relevant = False
                for target_as_of in relevant_as_of_indices:
                    if b_idx >= target_as_of:
                        continue
                    invalid_idx = next(
                        (
                            idx
                            for idx in range(
                                b_idx + 1,
                                min(b_idx + 5, target_as_of) + 1,
                            )
                            if float(low.iloc[idx]) < b_price
                        ),
                        None,
                    )
                    if (
                        invalid_idx is None
                        or target_as_of - invalid_idx <= retention_bars
                    ):
                        relevant = True
                        break
                if not relevant:
                    continue
            frozen_metadata = _candidate_metadata(
                visible,
                a_idx=a_idx,
                b_idx=b_idx,
                fallback=metadata,
            )
            cache_key = (
                a_idx,
                b_idx,
                version,
                frozen_metadata.data_source,
                frozen_metadata.adj_factor_source,
            )
            frozen_zone = zone_cache.get(cache_key)
            if frozen_zone is None:
                frozen_zone = ResistanceZoneDetector.freeze_evidence(
                    frozen,
                    a_idx=a_idx,
                    b_idx=b_idx,
                    candidate_version=version,
                    params=zone_params,
                    metadata=frozen_metadata,
                )
                zone_cache[cache_key] = frozen_zone
            trendline = _frozen_trendline(
                frozen, a_idx=a_idx, b_idx=b_idx
            )
            frozen_zone_payload = json.loads(frozen_zone.payload_json)
            degradation = list(
                frozen_zone_payload.get("degradation_reasons", [])
            )
            if "date" not in frozen and "trade_date" not in frozen:
                degradation.append("missing_date")
            evidence = {
                "candidate_version": version,
                "a": a,
                "b": b,
                "h": h,
                "macd": macd,
                "pattern": pattern,
                "frozen_trendline": trendline,
                "degradation_reasons": sorted(set(degradation)),
            }
            if materialize_zone:
                evidence["zone"] = (
                    ResistanceZoneDetector.evaluate_frozen_evidence(
                        frozen_zone,
                        params=zone_params,
                    )
                )
            else:
                evidence["zone_evidence"] = frozen_zone.to_dict()
            output.append(evidence)
        return output

    @classmethod
    def _replay_candidate(
        cls,
        visible: pd.DataFrame,
        *,
        evidence: dict[str, Any],
        break_tolerance: float,
        retention_bars: int,
        zone_params: ResistanceZoneParams,
        metadata: ResistanceZoneMetadata,
        atr_values: pd.Series,
    ) -> dict[str, Any] | None:
        b_idx = evidence["b"]["idx"]
        a_price = float(evidence["a"]["price"])
        b_price = float(evidence["b"]["price"])
        low = pd.to_numeric(visible["low"], errors="coerce").reset_index(drop=True)
        invalid_idx = next(
            (
                idx
                for idx in range(b_idx + 1, min(b_idx + 5, len(visible) - 1) + 1)
                if float(low.iloc[idx]) < b_price
            ),
            None,
        )
        if invalid_idx is not None:
            if len(visible) - 1 - invalid_idx > retention_bars:
                return None
            lifecycle = "invalidated"
            invalidated_at = _event(
                visible, invalid_idx, float(low.iloc[invalid_idx])
            )
        elif len(visible) - 1 >= b_idx + 5:
            lifecycle = "confirmed"
            invalidated_at = None
        else:
            lifecycle = "provisional"
            invalidated_at = None

        floor = min(a_price, b_price) * (1 - break_tolerance)
        structure_idx = next(
            (
                idx
                for idx in range(b_idx + 1, len(visible))
                if float(low.iloc[idx]) < floor
            ),
            None,
        )
        structure_break = _event(
            visible,
            structure_idx,
            float(low.iloc[structure_idx]) if structure_idx is not None else None,
            floor=round(floor, 6),
        )
        event_visible = (
            visible
            if invalid_idx is None
            else visible.iloc[:invalid_idx].copy()
        )
        trendline = with_trendline_breakout_v2(
            evidence["frozen_trendline"], event_visible, b_idx=b_idx
        )
        early = scan_early_v2(
            event_visible,
            b_idx=b_idx,
            pattern_code=evidence["pattern"]["code"],
        )
        near, major = zone_events_v2(
            event_visible,
            b_idx=b_idx,
            zone=evidence["zone"],
            params=zone_params,
            atr_values=atr_values,
        )
        trend_idx = trendline.get("breakout_bar_index")
        if major["triggered"] and trend_idx is not None:
            gap = abs(int(major["bar_index"]) - int(trend_idx))
            major["trendline_breakout_bar_index"] = trend_idx
            major["sync_gap"] = gap
            major["confirmed"] = gap <= zone_params.sync_window
        actionable, status = actionability_v2(
            event_visible,
            major=major,
            zone=evidence["zone"],
            structure_break=structure_break,
            metadata=ResistanceZoneMetadata(
                **(evidence["zone"].get("metadata") or {
                    "data_source": metadata.data_source,
                    "adj_factor_source": metadata.adj_factor_source,
                })
            ),
        )
        if lifecycle == "invalidated":
            actionable["actionable"] = False
            status = "candidate_invalidated"
            stage = "invalidated"
        elif actionable["actionable"]:
            stage = "major_actionable"
        elif major.get("confirmed"):
            stage = _MAJOR_NON_ACTIONABLE_STAGES.get(
                status,
                "major_unverified",
            )
        elif near["cleared_confirmed"]["triggered"]:
            stage = "near_cleared"
        elif early["triggered"]:
            stage = "early"
        else:
            stage = "forming"
        stop = round(min(a_price, b_price) * 0.97, 6)
        layered = [
            {
                "level": "early",
                "cumulative_position_pct": 20,
                "triggered": early["triggered"],
                "bar_index": early["bar_index"],
                "price": early["price"],
                "stop": stop,
            },
            {
                "level": "r1",
                "cumulative_position_pct": 50,
                "triggered": near["cleared_confirmed"]["triggered"],
                "bar_index": near["cleared_confirmed"]["bar_index"],
                "price": near["cleared_confirmed"]["price"],
                "stop": stop,
            },
            {
                "level": "r2",
                "cumulative_position_pct": 100,
                "triggered": major["confirmed"],
                "bar_index": major["bar_index"] if major["confirmed"] else None,
                "price": major["price"] if major["confirmed"] else None,
                "stop": stop,
            },
        ]
        signal = _signal_strength(
            a_price=a_price,
            b_price=b_price,
            dif_a=evidence["macd"]["a"]["dif"],
            dif_b=evidence["macd"]["b"]["dif"],
            h_price=evidence["h"]["price"],
            trendline=evidence["frozen_trendline"],
            zone=evidence["zone"],
        )
        return {
            **evidence,
            "lifecycle": lifecycle,
            "invalidated_at": invalidated_at,
            "structure_break": structure_break,
            "frozen_trendline": trendline,
            "early_reversal": early,
            "near_zone_events": near,
            "major_zone_breakout": major,
            "major_zone_actionable_entry": actionable,
            "actionability_status": status,
            "stage": stage,
            "signal_strength": signal,
            "stop_loss_price": stop,
            "layered_buy_points": layered,
        }

    @staticmethod
    def _sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
        active = record["lifecycle"] != "invalidated"
        return (
            0 if active else 1,
            -_LIFECYCLE_PRIORITY.get(record["lifecycle"], 0),
            -_STAGE_PRIORITY.get(record["stage"], 0),
            -float(record["signal_strength"]),
            -pd.Timestamp(record["b"]["date"]).value
            if not str(record["b"]["date"]).startswith("index:")
            else -int(str(record["b"]["date"]).split(":")[1]),
            record["candidate_version"],
        )
