# -*- coding: utf-8 -*-
"""Ranking effectiveness calculator for five-layer backtest.

Measures whether the screening system's tiered ranking (pool levels,
theme positions, maturity grades) actually predicts forward performance.

Key questions answered:
  - Does leader_pool outperform watchlist?
  - Does main_theme outperform non_theme?
  - Does HIGH maturity outperform MEDIUM / LOW?

D1 family-correctness:
  Comparisons are anchored on the entry family (forward_return_5d / win
  outcomes) by default, because "leader_pool outperforms watchlist" is a
  question about tradable entry signals — observation samples score
  ``risk_avoided_pct`` (always non-negative), which would inflate any
  tier with a higher observation share. The legacy mixed-family numbers
  are still computed and exposed under ``*_mixed`` fields so analysts
  can audit the magnitude of the family-mix bias on a per-run basis.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.backtest.models.backtest_models import FiveLayerBacktestGroupSummary
from src.backtest.utils.summary_metrics import (
    get_aggregatable_sample_count,
    load_summary_metrics,
)

logger = logging.getLogger(__name__)

# ── Ranking pairs to compare ────────────────────────────────────────────────

_POOL_LEVEL_ORDER = ["leader_pool", "focus_list", "watchlist"]
_THEME_POSITION_ORDER = ["main_theme", "secondary_theme", "follower_theme", "non_theme"]
_MATURITY_ORDER = ["HIGH", "MEDIUM", "LOW"]

# Default family scope for ranking inference. Entry signals carry the
# directional pnl contract that "leader_pool should beat watchlist" is
# really making a claim about; observation signals describe wait-correctness
# whose "win" semantics are non-comparable across pool tiers.
DEFAULT_FAMILY_SCOPE = "entry"


# ── Result dataclasses ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class RankingComparisonResult:
    """Result of comparing two tiers within a dimension.

    ``metric_source`` records whether the metrics actually came from the
    family-correct ``family_breakdown[entry]`` block (``family_entry`` /
    ``family_observation``) or fell back to the mixed-family summary
    columns (``mixed_legacy``). When both tiers had to fall back, the
    comparison is still emitted but reviewers should treat ``is_effective``
    with caution because the mix difference can dominate the signal.
    """

    dimension: str
    tier_high: str
    tier_low: str
    high_avg_return: Optional[float]
    low_avg_return: Optional[float]
    excess_return_pct: Optional[float]
    high_win_rate: Optional[float]
    low_win_rate: Optional[float]
    high_sample_count: int
    low_sample_count: int
    is_effective: bool  # True if higher tier outperforms
    metric_source: str = "mixed_legacy"


@dataclass(frozen=True)
class RankingEffectivenessReport:
    """Full ranking effectiveness analysis.

    Two parallel views are exposed:

    * Family-correct view (``leader_pool_win_share`` / ``excess_return_pct``)
      anchored on ``family_scope`` (default ``"entry"``). This is the
      authoritative number for ranking decisions.
    * Mixed-legacy view (``leader_pool_win_share_mixed`` /
      ``excess_return_pct_mixed``) reproduces the pre-D1 calculation that
      reads the mixed-family summary columns directly. It exists so
      analysts can compare the two and quantify the family-mix bias.

    ``top_k_hit_rate`` is preserved as a deprecated alias of
    ``leader_pool_win_share`` for backward compatibility.
    """

    comparisons: List[RankingComparisonResult]
    overall_effectiveness_ratio: float  # % of comparisons where ranking is effective
    leader_pool_win_share: Optional[float]
    excess_return_pct: Optional[float]
    ranking_consistency: Optional[float]
    family_scope: str = DEFAULT_FAMILY_SCOPE
    leader_pool_win_share_mixed: Optional[float] = None
    excess_return_pct_mixed: Optional[float] = None
    top_k_hit_rate: Optional[float] = None  # DEPRECATED alias of leader_pool_win_share


class RankingEffectivenessCalculator:
    """Evaluates whether the screening system's ranking tiers are predictive."""

    @staticmethod
    def compute(
        summaries: List[FiveLayerBacktestGroupSummary],
        family_scope: str = DEFAULT_FAMILY_SCOPE,
    ) -> RankingEffectivenessReport:
        """Compute ranking effectiveness from group summaries.

        Args:
            summaries: All group summaries for a run, including
                       candidate_pool_level, theme_position, entry_maturity groups.
            family_scope: Which family the comparisons are anchored on. Default
                ``"entry"`` is correct for ranking decisions because entry
                signals carry the directional pnl contract (forward_return_5d).
                ``"observation"`` is exposed for analysts who want to inspect
                wait-correctness rankings separately. ``"mixed"`` reproduces
                the legacy pre-D1 behaviour and is provided for parity audits
                only — do not feed it into recommendation gates.
        """
        index = _build_summary_index(summaries)
        comparisons: List[RankingComparisonResult] = []

        for group_type, tier_order in (
            ("candidate_pool_level", _POOL_LEVEL_ORDER),
            ("theme_position", _THEME_POSITION_ORDER),
            ("entry_maturity", _MATURITY_ORDER),
        ):
            comparisons.extend(
                _compare_tiers(group_type, tier_order, index, family_scope),
            )

        effective_count = sum(1 for c in comparisons if c.is_effective)
        total = len(comparisons) if comparisons else 1
        effectiveness_ratio = effective_count / total

        # Family-correct headline metrics (authoritative for D1).
        leader_pool_win_share = _compute_leader_pool_win_share(
            index, family_scope=family_scope,
        )
        excess_return = _compute_excess_return(
            index, family_scope=family_scope,
        )

        # Mixed-legacy alias values, preserved so analysts can quantify the
        # family-mix bias when family-correct and mixed numbers diverge.
        leader_pool_win_share_mixed = _compute_leader_pool_win_share(
            index, family_scope="mixed",
        )
        excess_return_mixed = _compute_excess_return(
            index, family_scope="mixed",
        )

        consistency = _compute_ranking_consistency(comparisons)

        return RankingEffectivenessReport(
            comparisons=comparisons,
            overall_effectiveness_ratio=round(effectiveness_ratio, 4),
            leader_pool_win_share=leader_pool_win_share,
            excess_return_pct=excess_return,
            ranking_consistency=consistency,
            family_scope=family_scope,
            leader_pool_win_share_mixed=leader_pool_win_share_mixed,
            excess_return_pct_mixed=excess_return_mixed,
            top_k_hit_rate=leader_pool_win_share,
        )


# ── Internal helpers ────────────────────────────────────────────────────────

def _build_summary_index(
    summaries: List[FiveLayerBacktestGroupSummary],
) -> Dict[str, Dict[str, FiveLayerBacktestGroupSummary]]:
    """Build {group_type: {group_key: summary}} lookup."""
    index: Dict[str, Dict[str, FiveLayerBacktestGroupSummary]] = {}
    for s in summaries:
        index.setdefault(s.group_type, {})[s.group_key] = s
    return index


def _extract_tier_metrics(
    summary: FiveLayerBacktestGroupSummary,
    family_scope: str,
) -> Tuple[Optional[float], Optional[float], int, str]:
    """Pull family-correct (avg_return, win_rate, agg_sample_count, metric_source).

    Resolution order:

    1. ``family_scope == "mixed"`` always returns the mixed summary columns
       and tags the result ``mixed_legacy``. This is intentional — the mixed
       view is an audit artifact.
    2. Otherwise prefer ``metrics_json.family_breakdown[family_scope]`` so
       the returned metrics use the correct return source (forward_return_5d
       for entry, risk_avoided_pct for observation) and outcome set.
    3. If the family block is missing or has no data, fall back to the mixed
       summary columns and tag the result ``mixed_fallback`` so callers can
       see that family-correctness was not achievable for this tier.

    The returned sample count is always the *family-correct* aggregatable
    count when ``family_breakdown`` is used, otherwise it falls back to the
    summary-level aggregatable count. This is critical: comparing
    ``family_breakdown[entry].sample_count`` of one tier with the mixed
    sample count of another would itself be a source of bias.
    """
    if family_scope == "mixed":
        return (
            summary.avg_return_pct,
            summary.win_rate_pct,
            get_aggregatable_sample_count(summary),
            "mixed_legacy",
        )

    metrics_payload = load_summary_metrics(summary)
    family_breakdown = metrics_payload.get("family_breakdown")
    if isinstance(family_breakdown, dict):
        family_block = family_breakdown.get(family_scope)
        if isinstance(family_block, dict):
            avg_return = family_block.get("avg_return_pct")
            win_rate = family_block.get("win_rate_pct")
            # Prefer the family-correct aggregatable count; only fall back to
            # the raw sample_count when the per-family aggregatable count is
            # missing (older summaries written before D1).
            agg = family_block.get("aggregatable_sample_count")
            if not isinstance(agg, int):
                agg = family_block.get("sample_count")
            if not isinstance(agg, int):
                agg = 0
            if avg_return is not None or win_rate is not None or agg > 0:
                metric_source = (
                    "family_entry" if family_scope == "entry"
                    else "family_observation" if family_scope == "observation"
                    else f"family_{family_scope}"
                )
                return avg_return, win_rate, agg, metric_source

    # Fall back to mixed columns and tag accordingly so reviewers can see
    # that the requested family was not available for this tier.
    return (
        summary.avg_return_pct,
        summary.win_rate_pct,
        get_aggregatable_sample_count(summary),
        "mixed_fallback",
    )


def _compare_tiers(
    group_type: str,
    tier_order: List[str],
    index: Dict[str, Dict[str, FiveLayerBacktestGroupSummary]],
    family_scope: str = DEFAULT_FAMILY_SCOPE,
) -> List[RankingComparisonResult]:
    """Compare all available tier pairs within a dimension.

    Compares every pair (higher, lower) where both tiers have data,
    not just adjacent pairs — so leader_pool vs watchlist is compared
    even when focus_list is absent.

    When ``family_scope`` is set, comparisons are anchored on the family-
    correct numbers from ``metrics_json.family_breakdown`` whenever possible,
    falling back to the mixed summary columns per-tier. The
    ``metric_source`` field on each result records the resolution outcome
    so reviewers can spot mixed-fallback comparisons explicitly.
    """
    results: List[RankingComparisonResult] = []
    type_summaries = index.get(group_type, {})
    if not type_summaries:
        return results

    available = [t for t in tier_order if t in type_summaries]

    for i in range(len(available)):
        for j in range(i + 1, len(available)):
            high_key = available[i]
            low_key = available[j]
            high = type_summaries[high_key]
            low = type_summaries[low_key]

            h_ret, h_wr, h_agg, h_src = _extract_tier_metrics(high, family_scope)
            l_ret, l_wr, l_agg, l_src = _extract_tier_metrics(low, family_scope)

            excess: Optional[float] = None
            is_effective = False
            if h_ret is not None and l_ret is not None:
                excess = round(h_ret - l_ret, 4)
                is_effective = h_ret > l_ret

            # Prefer the more specific source. If either tier had to fall
            # back to mixed, the comparison is at best mixed-quality; tag
            # accordingly so consumers don't over-trust it.
            if h_src == l_src:
                metric_source = h_src
            elif "mixed" in {h_src, l_src}:
                metric_source = "mixed_fallback"
            else:
                metric_source = "mixed_fallback"

            results.append(RankingComparisonResult(
                dimension=group_type,
                tier_high=high_key,
                tier_low=low_key,
                high_avg_return=h_ret,
                low_avg_return=l_ret,
                excess_return_pct=excess,
                high_win_rate=h_wr,
                low_win_rate=l_wr,
                high_sample_count=h_agg,
                low_sample_count=l_agg,
                is_effective=is_effective,
                metric_source=metric_source,
            ))
    return results


def _compute_leader_pool_win_share(
    index: Dict[str, Dict[str, FiveLayerBacktestGroupSummary]],
    family_scope: str = DEFAULT_FAMILY_SCOPE,
) -> Optional[float]:
    """Share of pool-level wins attributable to ``leader_pool``.

    Computed as ``leader_pool_wins / total_pool_wins`` where each tier's
    win count is reconstructed from ``win_rate_pct * aggregatable_sample_count``.
    Equals 1.0 when every win came from leader_pool, drops toward
    ``leader_share_of_samples`` when win rates are uniform across tiers.

    NOTE: This is a *share* metric, not a precision@k or recall metric.
    The legacy name ``top_k_hit_rate`` was a misnomer — kept as alias on
    :class:`RankingEffectivenessReport` only for backward compatibility.

    D1: When ``family_scope`` is provided (default ``"entry"``), the win
    rates and sample counts are read from ``family_breakdown[family_scope]``
    instead of the mixed summary columns. Pool tiers that lack the requested
    family fall back to mixed columns silently — this is the closest we can
    get to family-correctness without dropping tiers entirely. Pass
    ``family_scope="mixed"`` to reproduce the legacy pre-D1 calculation
    (used internally to populate ``leader_pool_win_share_mixed``).
    """
    pool_sums = index.get("candidate_pool_level", {})
    leader = pool_sums.get("leader_pool")
    if leader is None:
        return None

    leader_avg, leader_wr, leader_agg, _ = _extract_tier_metrics(leader, family_scope)
    if leader_wr is None or leader_agg <= 0:
        return None

    leader_wins = (leader_wr / 100) * leader_agg

    total_wins = 0.0
    total_samples = 0
    for tier in pool_sums.values():
        _, wr, agg, _ = _extract_tier_metrics(tier, family_scope)
        if wr is None or agg <= 0:
            continue
        total_wins += (wr / 100) * agg
        total_samples += agg

    if total_samples == 0 or total_wins == 0:
        return None

    return round(leader_wins / total_wins, 4)


# Backward-compatible alias so any in-repo / external caller that imported
# ``_compute_top_k_hit_rate`` keeps working through the deprecation window.
_compute_top_k_hit_rate = _compute_leader_pool_win_share


def _compute_excess_return(
    index: Dict[str, Dict[str, FiveLayerBacktestGroupSummary]],
    family_scope: str = DEFAULT_FAMILY_SCOPE,
) -> Optional[float]:
    """leader_pool avg return - watchlist avg return.

    Same family-correct semantics as :func:`_compute_leader_pool_win_share`.
    Pass ``family_scope="mixed"`` to reproduce the legacy pre-D1 number.
    """
    pool_sums = index.get("candidate_pool_level", {})
    leader = pool_sums.get("leader_pool")
    watchlist = pool_sums.get("watchlist")
    if leader is None or watchlist is None:
        return None

    leader_avg, _, _, _ = _extract_tier_metrics(leader, family_scope)
    watch_avg, _, _, _ = _extract_tier_metrics(watchlist, family_scope)
    if leader_avg is None or watch_avg is None:
        return None
    return round(leader_avg - watch_avg, 4)


def _compute_ranking_consistency(
    comparisons: List[RankingComparisonResult],
) -> Optional[float]:
    """Fraction of tier comparisons where higher tier outperforms."""
    if not comparisons:
        return None
    effective = sum(1 for c in comparisons if c.is_effective)
    return round(effective / len(comparisons), 4)
