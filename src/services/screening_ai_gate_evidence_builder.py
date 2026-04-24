"""Strategy-specific evidence package builder for the screening AI gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from src.schemas.screening_ai_gate import ScreeningAiGateConfig

# Fields that identify a candidate regardless of strategy
_CANDIDATE_META_FIELDS = ["code", "name", "close"]

# Fields that describe general market/filter context
_MARKET_FILTER_FIELDS = ["volume_ratio", "trend_score", "is_st"]

# Per-strategy prefix → raw evidence fields needed by the AI gate
_STRATEGY_RAW_EVIDENCE_PREFIXES: Dict[str, str] = {
    "bottom_divergence_double_breakout": "bottom_divergence_",
}

# Key evidence fields that must be non-None for a package to be "sufficient"
_REQUIRED_EVIDENCE_FIELDS: Dict[str, List[str]] = {
    "bottom_divergence_double_breakout": [
        "bottom_divergence_price_low_a",
        "bottom_divergence_price_low_b",
        "bottom_divergence_rebound_high",
        "bottom_divergence_horizontal_resistance",
    ],
}


@dataclass
class EvidencePackage:
    """Compact, strategy-specific evidence for one candidate."""

    candidate_meta: Dict[str, Any]
    strategy_snapshot: Dict[str, Any]
    strategy_raw_evidence: Dict[str, Any]
    market_filter_snapshot: Dict[str, Any]
    data_quality: str  # "sufficient" | "insufficient"
    missing_fields: List[str] = field(default_factory=list)


class ScreeningAiGateEvidenceBuilder:
    """Builds a compact evidence package from factor_snapshot + strategy config."""

    def build(
        self,
        factor_snapshot: Dict[str, Any],
        strategy_config: ScreeningAiGateConfig,
    ) -> EvidencePackage:
        candidate_meta = {
            k: factor_snapshot.get(k) for k in _CANDIDATE_META_FIELDS
        }
        market_filter = {
            k: factor_snapshot.get(k) for k in _MARKET_FILTER_FIELDS
        }

        prefix = _STRATEGY_RAW_EVIDENCE_PREFIXES.get(
            strategy_config.strategy_name, ""
        )
        strategy_snapshot: Dict[str, Any] = {}
        strategy_raw_evidence: Dict[str, Any] = {}

        if prefix:
            for key, value in factor_snapshot.items():
                if key.startswith(prefix):
                    strategy_raw_evidence[key] = value
            # Extract summary fields into strategy_snapshot
            strategy_snapshot = {
                "state": factor_snapshot.get(f"{prefix}state"),
                "signal_strength": factor_snapshot.get(f"{prefix}signal_strength"),
                "pattern_code": factor_snapshot.get(f"{prefix}pattern_code"),
                "sync_breakout": factor_snapshot.get(f"{prefix}sync_breakout"),
            }

        required = _REQUIRED_EVIDENCE_FIELDS.get(
            strategy_config.strategy_name, []
        )
        missing = [
            f for f in required if factor_snapshot.get(f) is None
        ]
        data_quality = "insufficient" if missing else "sufficient"

        return EvidencePackage(
            candidate_meta=candidate_meta,
            strategy_snapshot=strategy_snapshot,
            strategy_raw_evidence=strategy_raw_evidence,
            market_filter_snapshot=market_filter,
            data_quality=data_quality,
            missing_fields=missing,
        )
