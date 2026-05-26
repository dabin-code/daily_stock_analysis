from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

SCREENING_MODE_OPTIONS = ("balanced", "aggressive", "quality")

_SCREENING_MODE_PRESETS: Dict[str, Dict[str, Any]] = {
    "balanced": {},
    "aggressive": {
        "candidate_limit": 50,
        "ai_top_k": 25,
        "min_list_days": 60,
        "min_volume_ratio": 1.0,
        "breakout_lookback_days": 15,
        "factor_lookback_days": 60,
    },
    "quality": {
        "candidate_limit": 20,
        "ai_top_k": 10,
        "min_list_days": 180,
        "min_volume_ratio": 1.5,
        "breakout_lookback_days": 30,
        "factor_lookback_days": 120,
    },
}


@dataclass(frozen=True)
class ResolvedScreeningRuntimeConfig:
    mode: str
    candidate_limit: int
    ai_top_k: int
    min_list_days: int
    min_volume_ratio: float
    breakout_lookback_days: int
    factor_lookback_days: int
    min_final_score: float = 0.0

    def to_snapshot(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "candidate_limit": self.candidate_limit,
            "ai_top_k": self.ai_top_k,
            "screening_min_list_days": self.min_list_days,
            "screening_min_volume_ratio": self.min_volume_ratio,
            "screening_breakout_lookback_days": self.breakout_lookback_days,
            "screening_factor_lookback_days": self.factor_lookback_days,
            "screening_min_final_score": self.min_final_score,
        }


def normalize_screening_mode(mode: Optional[str]) -> str:
    normalized = (mode or "").strip().lower()
    if normalized in _SCREENING_MODE_PRESETS:
        return normalized
    return "balanced"


def resolve_screening_runtime_config(
    config: Any,
    mode: Optional[str],
    candidate_limit: Optional[int],
    ai_top_k: Optional[int],
) -> ResolvedScreeningRuntimeConfig:
    resolved_mode = normalize_screening_mode(mode or getattr(config, "screening_default_mode", "balanced"))
    preset = _SCREENING_MODE_PRESETS[resolved_mode]

    base_candidate_limit = int(getattr(config, "screening_candidate_limit"))
    base_ai_top_k = int(getattr(config, "screening_ai_top_k"))
    base_min_list_days = int(getattr(config, "screening_min_list_days"))
    base_min_volume_ratio = float(getattr(config, "screening_min_volume_ratio"))
    base_breakout_lookback_days = int(getattr(config, "screening_breakout_lookback_days"))
    base_factor_lookback_days = int(getattr(config, "screening_factor_lookback_days"))
    base_min_final_score = float(getattr(config, "screening_min_final_score", 0.0) or 0.0)
    if base_min_final_score < 0:
        base_min_final_score = 0.0

    def _resolve_directional_value(base_value: float, preset_key: str, *, prefer_higher: bool) -> float:
        if preset_key not in preset:
            return base_value
        preset_value = float(preset[preset_key])
        if resolved_mode == "aggressive":
            return max(base_value, preset_value) if prefer_higher else min(base_value, preset_value)
        if resolved_mode == "quality":
            return min(base_value, preset_value) if prefer_higher else max(base_value, preset_value)
        return base_value

    resolved_candidate_limit = (
        candidate_limit
        if candidate_limit is not None
        else int(_resolve_directional_value(base_candidate_limit, "candidate_limit", prefer_higher=True))
    )
    resolved_ai_top_k = (
        ai_top_k
        if ai_top_k is not None
        else (
            max(base_ai_top_k, int(preset["ai_top_k"]))
            if "ai_top_k" in preset
            else base_ai_top_k
        )
    )
    resolved_ai_top_k = min(resolved_ai_top_k, resolved_candidate_limit)

    resolved_min_final_score = float(
        _resolve_directional_value(base_min_final_score, "min_final_score", prefer_higher=True)
    )
    if resolved_min_final_score < 0:
        resolved_min_final_score = 0.0

    return ResolvedScreeningRuntimeConfig(
        mode=resolved_mode,
        candidate_limit=resolved_candidate_limit,
        ai_top_k=resolved_ai_top_k,
        min_list_days=int(_resolve_directional_value(base_min_list_days, "min_list_days", prefer_higher=False)),
        min_volume_ratio=float(_resolve_directional_value(base_min_volume_ratio, "min_volume_ratio", prefer_higher=False)),
        breakout_lookback_days=int(
            _resolve_directional_value(base_breakout_lookback_days, "breakout_lookback_days", prefer_higher=False)
        ),
        factor_lookback_days=int(
            _resolve_directional_value(base_factor_lookback_days, "factor_lookback_days", prefer_higher=False)
        ),
        min_final_score=resolved_min_final_score,
    )
