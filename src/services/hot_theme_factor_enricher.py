# -*- coding: utf-8 -*-
"""Hot theme factor enrichment for FactorService."""

from typing import Dict, Any, List, Optional

from src.services.theme_matching_service import ThemeMatchingService
from src.services.leader_score_calculator import LeaderScoreCalculator
from src.services.extreme_strength_scorer import ExtremeStrengthScorer
from src.services.theme_context_ingest_service import OpenClawThemeContext
from src.services.core_signal_identifier import CoreSignalIdentifier


class HotThemeFactorEnricher:
    """Enrich factor snapshots with hot theme context."""

    PHASE_LABELS = {
        "phase1_market_and_theme": "阶段1: 市场与题材",
        "phase2_leader_screen": "阶段2: 龙头筛选",
        "phase3_core_signal": "阶段3: 核心信号",
        "phase4_entry_readiness": "阶段4: 入场准备",
        "phase5_risk_controls": "阶段5: 风险控制",
    }

    def __init__(self) -> None:
        """Initialize enricher with scoring services."""
        self.theme_matcher = ThemeMatchingService()
        self.leader_calculator = LeaderScoreCalculator()
        self.strength_scorer = ExtremeStrengthScorer()
        self.signal_identifier = CoreSignalIdentifier()

    def enrich_snapshot(
        self,
        snapshot: Dict[str, Any],
        theme_context: Optional[OpenClawThemeContext],
        boards: Optional[List[str]] = None,
        normalized_themes: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Enrich factor snapshot with hot theme fields.
        Returns enriched snapshot with theme-related fields.
        """
        base_leader_score = float(snapshot.get("base_leader_score", snapshot.get("leader_score", 0.0)) or 0.0)
        base_extreme_strength_score = float(
            snapshot.get(
                "base_extreme_strength_score",
                snapshot.get("extreme_strength_score", 0.0),
            )
            or 0.0
        )
        snapshot["base_leader_score"] = base_leader_score
        snapshot["base_extreme_strength_score"] = base_extreme_strength_score

        if theme_context is None or not theme_context.themes:
            # No theme context, add default values
            snapshot["is_hot_theme_stock"] = False
            snapshot["primary_theme"] = None
            snapshot["theme_tags"] = []
            snapshot["theme_heat_score"] = 0.0
            snapshot["theme_match_score"] = 0.0
            snapshot["theme_leader_score"] = 0.0
            snapshot["theme_extreme_strength_score"] = 0.0
            snapshot["leader_score"] = base_leader_score
            snapshot["extreme_strength_score"] = base_extreme_strength_score
            snapshot["leader_score_source"] = "base"
            snapshot["extreme_strength_score_source"] = "base"
            snapshot["extreme_strength_reasons"] = []
            snapshot["entry_reason"] = None
            snapshot["core_signal"] = None
            snapshot["bonus_signals"] = []
            snapshot["theme_catalyst_summary"] = None
            snapshot["theme_catalyst_news"] = []
            snapshot["phase_results"] = self._build_phase_results(
                market_and_theme=False,
                leader_screen=False,
                core_signal=False,
                entry_readiness=False,
                risk_controls=False,
            )
            snapshot["risk_params"] = {"stop_loss": 0, "position_size": "无", "take_profit_ratio": 0}
            snapshot["phase_explanations"] = self._build_phase_explanations(
                phase_results=snapshot["phase_results"],
                primary_theme=None,
                theme_match_score=0.0,
                theme_heat_score=0.0,
                leader_score=base_leader_score,
                core_signal=None,
                entry_reason=None,
                risk_params=snapshot["risk_params"],
                extreme_strength_score=base_extreme_strength_score,
            )
            return snapshot

        stock_name = snapshot.get("name", "")
        boards = boards or []

        # Build normalized board lookup for each theme
        norm_board_map: Dict[str, List[str]] = {}
        if normalized_themes:
            for nt in normalized_themes:
                raw = nt.get("raw_theme", "")
                if raw and nt.get("matched_boards"):
                    norm_board_map[raw] = nt["matched_boards"]

        # Find best matching theme
        best_theme = None
        best_match_score = 0.0
        best_theme_heat = 0.0

        for theme in theme_context.themes:
            normalized_boards = norm_board_map.get(theme.name)

            if normalized_boards:
                # Use normalized boards: match stock boards against each
                # normalized board as the theme target.
                theme_score = 0.0
                for norm_board in normalized_boards:
                    score = self.theme_matcher.calculate_theme_match_score(
                        boards=boards,
                        stock_name=stock_name,
                        theme_name=norm_board,
                        keywords=theme.keywords,
                    )
                    theme_score = max(theme_score, score)
            else:
                # Fallback: use raw theme name (original behavior)
                theme_score = self.theme_matcher.calculate_theme_match_score(
                    boards=boards,
                    stock_name=stock_name,
                    theme_name=theme.name,
                    keywords=theme.keywords,
                )

            if theme_score > best_match_score:
                best_match_score = theme_score
                best_theme = theme
                best_theme_heat = theme.heat_score

        # Check if hot theme stock
        is_hot = best_match_score >= self.theme_matcher.THEME_MATCH_THRESHOLD

        # Calculate leader score
        theme_leader_score = 0.0
        if is_hot:
            circ_mv = snapshot.get("circ_mv")
            turnover_rate = snapshot.get("turnover_rate")
            theme_leader_score = self.leader_calculator.calculate_leader_score(
                theme_match_score=best_match_score,
                circ_mv=circ_mv,
                turnover_rate=turnover_rate,
                is_limit_up=snapshot.get("is_limit_up", False),
                gap_breakaway=snapshot.get("gap_breakaway", False),
                above_ma100=snapshot.get("above_ma100", False),
                ma100_breakout_days=snapshot.get("ma100_breakout_days", 0),
            )

        # Calculate extreme strength score using corrected strategy
        theme_extreme_strength_score = 0.0
        extreme_strength_reasons = []
        entry_reason = None
        core_signal = None
        bonus_signals = []

        if is_hot:
            # Identify core signal
            core_result = self.signal_identifier.identify_core_signal(
                has_gap=snapshot.get("gap_breakaway", False),
                has_limit_up=snapshot.get("is_limit_up", False),
                has_gap_breakout_ma100=snapshot.get("gap_breakaway", False) and snapshot.get("above_ma100", False),
            )

            # Identify bonus signals
            bonus_result = self.signal_identifier.identify_bonus_signals(
                has_low_123_breakout=snapshot.get("pattern_123_low_trendline", False),
                has_bottom_divergence=snapshot.get("bottom_divergence_double_breakout", False),
            )

            # Calculate total score using the main strategy scorer.
            theme_extreme_strength_score = self.strength_scorer.calculate_extreme_strength_score(
                above_ma100=snapshot.get("above_ma100", False),
                gap_breakaway=snapshot.get("gap_breakaway", False),
                pattern_123_low_trendline=snapshot.get("pattern_123_low_trendline", False),
                pattern_123_watchlist=snapshot.get("pattern_123_watchlist", False),
                is_limit_up=snapshot.get("is_limit_up", False),
                bottom_divergence_double_breakout=snapshot.get("bottom_divergence_double_breakout", False),
                theme_heat_score=best_theme_heat,
                leader_score=theme_leader_score,
                volume_ratio=snapshot.get("volume_ratio", 0.0) or 0.0,
                turnover_rate=snapshot.get("turnover_rate"),
                circ_mv=snapshot.get("circ_mv"),
                breakout_ratio=snapshot.get("breakout_ratio", 0.0) or 0.0,
            )

            core_signal = core_result["core_signal"]
            bonus_signals = bonus_result["bonus_signals"]
            extreme_strength_reasons = core_result["hit_reasons"] + bonus_result["hit_reasons"]
            if snapshot.get("above_ma100"):
                extreme_strength_reasons.append("MA100之上")
            if snapshot.get("gap_breakaway"):
                extreme_strength_reasons.append("跳空突破")
            if snapshot.get("is_limit_up"):
                extreme_strength_reasons.append("涨停")
            extreme_strength_reasons = list(dict.fromkeys(extreme_strength_reasons))

            # Determine entry reason (龙头选出原因)
            intraday_minutes = snapshot.get("intraday_minutes_since_open")
            if (
                snapshot.get("is_limit_up")
                and isinstance(intraday_minutes, (int, float))
                and intraday_minutes <= 30
            ):
                entry_reason = "开盘半小时内涨停"
            elif snapshot.get("above_ma100"):
                entry_reason = "站上/刚突破MA100"

        # Build phase results
        phase_results = self._build_phase_results(
            market_and_theme=is_hot,
            leader_screen=is_hot and theme_leader_score >= 50,
            core_signal=is_hot and core_signal is not None,
            entry_readiness=is_hot and theme_extreme_strength_score >= 60 and entry_reason is not None,
            risk_controls=is_hot,
        )

        # Build risk params
        risk_params = {
            "stop_loss": snapshot.get("ma100", 0) * 0.95 if snapshot.get("above_ma100") else 0,
            "position_size": "轻仓试错" if theme_extreme_strength_score < 80 else "可加仓",
            "take_profit_ratio": 0.15 if theme_extreme_strength_score >= 80 else 0.10,
        }

        # Extract news from theme evidence
        theme_catalyst_news = []
        if best_theme and best_theme.evidence:
            theme_catalyst_news = [
                {
                    "title": e.get("title", ""),
                    "source": e.get("source", ""),
                    "url": e.get("url"),
                    "published_at": e.get("published_at"),
                    "heat_score": best_theme_heat,
                }
                for e in best_theme.evidence
            ]

        # Enrich snapshot
        snapshot["is_hot_theme_stock"] = is_hot
        snapshot["primary_theme"] = best_theme.name if best_theme else None
        snapshot["theme_tags"] = [best_theme.name] if best_theme else []
        snapshot["theme_heat_score"] = best_theme_heat
        snapshot["theme_match_score"] = best_match_score
        effective_leader_score, effective_extreme_strength_score = self._resolve_effective_scores(
            base_leader_score=base_leader_score,
            base_extreme_strength_score=base_extreme_strength_score,
            theme_leader_score=theme_leader_score if is_hot else 0.0,
            theme_extreme_strength_score=theme_extreme_strength_score if is_hot else 0.0,
        )
        snapshot["theme_leader_score"] = theme_leader_score
        snapshot["theme_extreme_strength_score"] = theme_extreme_strength_score
        snapshot["leader_score"] = effective_leader_score
        snapshot["extreme_strength_score"] = effective_extreme_strength_score
        snapshot["leader_score_source"] = "theme" if theme_leader_score > 0.0 and is_hot else "base"
        snapshot["extreme_strength_score_source"] = (
            "theme" if theme_extreme_strength_score > 0.0 and is_hot else "base"
        )
        snapshot["extreme_strength_reasons"] = extreme_strength_reasons
        snapshot["theme_catalyst_summary"] = best_theme.catalyst_summary if best_theme else None
        snapshot["theme_catalyst_news"] = theme_catalyst_news
        snapshot["entry_reason"] = entry_reason
        snapshot["core_signal"] = core_signal
        snapshot["bonus_signals"] = bonus_signals
        snapshot["phase_results"] = phase_results
        snapshot["phase_explanations"] = self._build_phase_explanations(
            phase_results=phase_results,
            primary_theme=best_theme.name if best_theme else None,
            theme_match_score=best_match_score,
            theme_heat_score=best_theme_heat,
            leader_score=effective_leader_score,
            core_signal=core_signal,
            entry_reason=entry_reason,
            risk_params=risk_params,
            extreme_strength_score=effective_extreme_strength_score,
        )
        snapshot["risk_params"] = risk_params

        return snapshot

    @staticmethod
    def _resolve_effective_scores(
        base_leader_score: float,
        base_extreme_strength_score: float,
        theme_leader_score: float,
        theme_extreme_strength_score: float,
    ) -> tuple[float, float]:
        effective_leader_score = (
            theme_leader_score if theme_leader_score > 0.0 else base_leader_score
        )
        effective_extreme_strength_score = (
            theme_extreme_strength_score
            if theme_extreme_strength_score > 0.0
            else base_extreme_strength_score
        )
        return effective_leader_score, effective_extreme_strength_score

    @staticmethod
    def _build_phase_results(
        market_and_theme: bool,
        leader_screen: bool,
        core_signal: bool,
        entry_readiness: bool,
        risk_controls: bool,
    ) -> Dict[str, bool]:
        return {
            "phase1_market_and_theme": market_and_theme,
            "phase2_leader_screen": leader_screen,
            "phase3_core_signal": core_signal,
            "phase4_entry_readiness": entry_readiness,
            "phase5_risk_controls": risk_controls,
        }

    def _build_phase_explanations(
        self,
        phase_results: Dict[str, bool],
        primary_theme: Optional[str],
        theme_match_score: float,
        theme_heat_score: float,
        leader_score: float,
        core_signal: Optional[str],
        entry_reason: Optional[str],
        risk_params: Dict[str, Any],
        extreme_strength_score: float,
    ) -> List[Dict[str, Any]]:
        stop_loss = float(risk_params.get("stop_loss", 0) or 0)
        position_size = risk_params.get("position_size", "-")
        take_profit_ratio = float(risk_params.get("take_profit_ratio", 0) or 0)

        summaries = {
            "phase1_market_and_theme": (
                f"theme={primary_theme or '-'}; "
                f"theme_match_score={theme_match_score:.2f}; "
                f"theme_heat_score={theme_heat_score:.1f}"
                if phase_results["phase1_market_and_theme"]
                else "未通过热点题材匹配门槛"
            ),
            "phase2_leader_screen": (
                f"leader_score={leader_score}"
                if phase_results["phase2_leader_screen"]
                else f"leader_score={leader_score}; 仍未达到龙头筛选阈值"
            ),
            "phase3_core_signal": (
                f"core_signal={core_signal or '-'}"
                if phase_results["phase3_core_signal"]
                else "缺少关键缺口/涨停共振信号"
            ),
            "phase4_entry_readiness": (
                f"entry_reason={entry_reason or '-'}; extreme_strength_score={extreme_strength_score:.1f}"
                if phase_results["phase4_entry_readiness"]
                else f"等待入场确认; extreme_strength_score={extreme_strength_score:.1f}"
            ),
            "phase5_risk_controls": (
                f"stop_loss={stop_loss:.2f}; position_size={position_size}; "
                f"take_profit_ratio={take_profit_ratio:.2f}"
                if phase_results["phase5_risk_controls"]
                else "尚未形成可执行的风险控制参数"
            ),
        }

        return [
            {
                "phase_key": phase_key,
                "label": label,
                "hit": phase_results.get(phase_key, False),
                "summary": summaries[phase_key],
            }
            for phase_key, label in self.PHASE_LABELS.items()
        ]
