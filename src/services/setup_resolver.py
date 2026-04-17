"""Setup resolver — converges multiple strategy hits into one setup_type.

Phase 2B module. When a candidate is matched by multiple entry_core
strategies, this resolver picks the best ``SetupType`` based on
environment + theme context priority rules.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.schemas.trading_types import (
    MarketRegime,
    SetupType,
    StrategyFamily,
    ThemePosition,
)

logger = logging.getLogger(__name__)


# ── Data structures ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _StrategyMeta:
    name: str
    system_role: str
    strategy_family: str
    setup_type: Optional[str]


@dataclass
class SetupResolution:
    """Result of resolving multiple strategy hits into one setup."""
    setup_type: SetupType
    strategy_family: Optional[StrategyFamily]
    primary_strategy: Optional[str]
    contributing_strategies: List[str]
    reason: str


# ── Family priority matrix ──────────────────────────────────────────────────

# Ordered from highest to lowest priority per context.
_PRIORITY_DEFENSIVE_OR_NON_THEME = [
    StrategyFamily.REVERSAL,
    StrategyFamily.TREND,
    StrategyFamily.MOMENTUM,
]

_PRIORITY_BALANCED_THEME = [
    StrategyFamily.TREND,
    StrategyFamily.REVERSAL,
    StrategyFamily.MOMENTUM,
]

_PRIORITY_AGGRESSIVE_THEME = [
    StrategyFamily.TREND,
    StrategyFamily.MOMENTUM,
    StrategyFamily.REVERSAL,
]


# ── Resolver ────────────────────────────────────────────────────────────────

class SetupResolver:
    """Converges multiple matched entry_core strategies into one setup_type.

    Priority depends on market regime + theme position context:
    - Weak (defensive / non-theme): reversal > trend > momentum
    - Balanced + theme: trend > reversal > momentum
    - Aggressive + theme: trend > momentum > reversal
    """

    def __init__(self, strategy_rules: List[Any]) -> None:
        self._meta: Dict[str, _StrategyMeta] = {}
        for rule in strategy_rules:
            self._meta[rule.strategy_name] = _StrategyMeta(
                name=rule.strategy_name,
                system_role=rule.system_role or "",
                strategy_family=rule.strategy_family or "",
                setup_type=rule.setup_type,
            )

    def resolve(
        self,
        allowed_strategies: List[str],
        strategy_scores: Dict[str, float],
        market_regime: MarketRegime,
        theme_position: ThemePosition,
        factor_snapshot: Optional[Dict[str, Any]] = None,
    ) -> SetupResolution:
        """Resolve allowed strategies into one primary setup.

        Parameters
        ----------
        allowed_strategies:
            Strategy names that survived dispatch filtering.
        strategy_scores:
            Per-strategy scores from the screening engine.
        market_regime:
            L1 market regime.
        theme_position:
            L2 theme position for this candidate.

        Returns
        -------
        SetupResolution with the converged setup_type.
        """
        if not allowed_strategies:
            return SetupResolution(
                setup_type=SetupType.NONE,
                strategy_family=None,
                primary_strategy=None,
                contributing_strategies=[],
                reason="no_allowed_strategies",
            )

        # Step 1: extract entry_core strategies
        entry_cores = [
            name for name in allowed_strategies
            if self._meta.get(name, _StrategyMeta(name, "", "", None)).system_role == "entry_core"
        ]

        if not entry_cores:
            return SetupResolution(
                setup_type=SetupType.NONE,
                strategy_family=None,
                primary_strategy=None,
                contributing_strategies=allowed_strategies,
                reason="no_entry_core_in_allowed",
            )

        # Step 2: single entry_core → use directly
        if len(entry_cores) == 1:
            meta = self._meta[entry_cores[0]]
            return self._build_resolution(
                meta,
                entry_cores,
                allowed_strategies,
                "single_entry_core",
                factor_snapshot=factor_snapshot,
            )

        # Step 3: multiple entry_cores → sort by priority matrix
        priority = self._family_priority(market_regime, theme_position)
        best = self._pick_best(entry_cores, strategy_scores, priority)
        return self._build_resolution(
            best, entry_cores, allowed_strategies,
            f"multi_entry_core; priority={[f.value for f in priority]}",
            factor_snapshot=factor_snapshot,
        )

    # ── private helpers ─────────────────────────────────────────────────

    @staticmethod
    def _family_priority(
        regime: MarketRegime, theme: ThemePosition,
    ) -> List[StrategyFamily]:
        """Return family priority list (highest first) for context."""
        if regime == MarketRegime.DEFENSIVE:
            return list(_PRIORITY_DEFENSIVE_OR_NON_THEME)
        if theme in (ThemePosition.NON_THEME, ThemePosition.FADING_THEME, ThemePosition.FOLLOWER_THEME):
            return list(_PRIORITY_DEFENSIVE_OR_NON_THEME)
        if regime == MarketRegime.BALANCED:
            return list(_PRIORITY_BALANCED_THEME)
        # aggressive + has theme
        return list(_PRIORITY_AGGRESSIVE_THEME)

    def _pick_best(
        self,
        entry_cores: List[str],
        strategy_scores: Dict[str, float],
        priority: List[StrategyFamily],
    ) -> _StrategyMeta:
        """Pick the best entry_core strategy using priority + score + name."""
        family_rank = {f: i for i, f in enumerate(priority)}
        # auxiliary and unknown families get lowest priority
        default_rank = len(priority)

        def sort_key(name: str):
            meta = self._meta.get(name, _StrategyMeta(name, "", "", None))
            try:
                fam = StrategyFamily(meta.strategy_family)
            except ValueError:
                fam = None
            rank = family_rank.get(fam, default_rank) if fam else default_rank
            score = strategy_scores.get(name, 0.0)
            # Sort: family rank ASC, score DESC, name ASC
            return (rank, -score, name)

        sorted_cores = sorted(entry_cores, key=sort_key)
        return self._meta[sorted_cores[0]]

    def _build_resolution(
        self,
        meta: _StrategyMeta,
        entry_cores: List[str],
        all_strategies: List[str],
        reason_detail: str,
        factor_snapshot: Optional[Dict[str, Any]] = None,
    ) -> SetupResolution:
        try:
            setup = SetupType(meta.setup_type) if meta.setup_type else SetupType.NONE
        except ValueError:
            setup = SetupType.NONE

        if meta.name == "gap_limitup_breakout":
            snapshot = factor_snapshot or {}
            # Prefer the new structured limit-up flag; fall back to the
            # legacy ``limit_up_breakout`` / ``is_limit_up`` combo so that
            # older snapshots (no sub-semantic fields) still route to
            # LIMITUP_STRUCTURE when the bar is actually a limit-up.
            if (
                snapshot.get("limitup_structure_breakout")
                or snapshot.get("is_limit_up")
                or snapshot.get("limit_up_breakout")
            ):
                setup = SetupType.LIMITUP_STRUCTURE
            else:
                setup = SetupType.GAP_BREAKOUT

        try:
            family = StrategyFamily(meta.strategy_family) if meta.strategy_family else None
        except ValueError:
            family = None

        return SetupResolution(
            setup_type=setup,
            strategy_family=family,
            primary_strategy=meta.name,
            contributing_strategies=all_strategies,
            reason=f"{reason_detail}; primary={meta.name}; setup={setup.value}",
        )
