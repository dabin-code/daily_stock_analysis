"""Deterministic active-strategy resolver for the screening AI gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.schemas.screening_ai_gate import ScreeningAiGateConfig
from src.services.screening_ai_gate_registry import ScreeningAiGateRegistry


@dataclass
class ResolvedActiveStrategy:
    """The single active strategy chosen for one candidate."""

    strategy_name: str
    supported: bool
    config: ScreeningAiGateConfig


class ScreeningAiGateResolver:
    """Picks exactly one active strategy per candidate from matched strategies."""

    def __init__(self, registry: ScreeningAiGateRegistry) -> None:
        self._registry = registry

    def resolve(
        self,
        matched_strategies: List[str],
        factor_snapshot: Dict[str, Any],
    ) -> Tuple[Optional[ResolvedActiveStrategy], List[str]]:
        """Return (active_strategy, alternative_names).

        Selection order:
        1. Supported strategies first, sorted by ai_priority (lower = higher).
        2. Among same priority, pick by stronger signal (signal_strength).
        3. If no supported strategy, pick first unsupported by priority.
        4. Unknown strategies (not in registry) are discarded.
        """
        if not matched_strategies:
            return None, []

        candidates: List[ScreeningAiGateConfig] = []
        for name in matched_strategies:
            config = self._registry.get(name)
            if config is not None:
                candidates.append(config)

        if not candidates:
            return None, []

        supported = [c for c in candidates if c.supported]
        unsupported = [c for c in candidates if not c.supported]

        if supported:
            supported.sort(key=lambda c: (c.ai_priority, c.strategy_name))
            winner = supported[0]
            alternatives = [
                c.strategy_name for c in candidates if c.strategy_name != winner.strategy_name
            ]
            return (
                ResolvedActiveStrategy(
                    strategy_name=winner.strategy_name,
                    supported=True,
                    config=winner,
                ),
                alternatives,
            )

        # No supported strategy — pick first unsupported by priority
        unsupported.sort(key=lambda c: (c.ai_priority, c.strategy_name))
        winner = unsupported[0]
        alternatives = [
            c.strategy_name for c in candidates if c.strategy_name != winner.strategy_name
        ]
        return (
            ResolvedActiveStrategy(
                strategy_name=winner.strategy_name,
                supported=False,
                config=winner,
            ),
            alternatives,
        )
