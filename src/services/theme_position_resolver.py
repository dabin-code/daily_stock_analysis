# -*- coding: utf-8 -*-
"""
L2 题材地位解析 — per-stock theme_position 推算 + 双通道融合。

构造函数注入上下文（sector_results / theme_results / theme_context），
resolve(stock_boards) 处理单只股票。

D3 修复: 新增 identify_main_themes() 实现"先找主线，再匹配股票"。
  旧逻辑: 从股票所属板块中找最热的 → 推算 position（挂标签）
  新逻辑: 先全市场识别主线/次线题材 → 检查股票是否属于主线 → 不属于再走兜底

盘面先行原则:
  盘面强 + OpenClaw 强 → MAIN_THEME
  盘面强 + OpenClaw 弱/无 → SECONDARY_THEME（或由盘面独立判定）
  盘面弱 + OpenClaw 强 → NON_THEME
  盘面弱 + OpenClaw 弱 → NON_THEME
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Set

from src.schemas.trading_types import ThemeDecision, ThemePosition
from src.services.sector_heat_engine import SectorHeatResult
from src.services.theme_aggregation_service import ThemeAggregateResult

if TYPE_CHECKING:
    from src.services.theme_mapping_registry import ThemeMappingRegistry

logger = logging.getLogger(__name__)
# 单日识别的主线/次线题材数量上限。
# 这是 L2 的宽度闸门：题材层不设限会把 L3 的候选输入整体放大，
# 让五层筛选形同虚设。fallback 与主路径必须共用同一个上限。
MAX_IDENTIFIED_THEMES = 10
MAX_FALLBACK_THEMES = MAX_IDENTIFIED_THEMES  # 向后兼容既有引用


@dataclass
class IdentifiedTheme:
    """全市场识别出的主线/次线题材。"""
    name: str
    position: ThemePosition
    score: float = 0.0
    stage: str = ""
    leader_codes: List[str] = field(default_factory=list)
    front_codes: List[str] = field(default_factory=list)
    member_boards: List[str] = field(default_factory=list)
    quality_flags: Dict[str, object] = field(default_factory=dict)


class ThemePositionResolver:
    """L2 per-stock 题材地位解析器。"""

    def __init__(
        self,
        sector_results: List[SectorHeatResult],
        theme_results: List[ThemeAggregateResult],
        theme_context: Optional[Dict[str, dict]] = None,
        registry: Optional["ThemeMappingRegistry"] = None,
    ) -> None:
        self._sector_map: Dict[str, SectorHeatResult] = {
            s.board_name: s for s in sector_results
        }
        self._theme_map: Dict[str, ThemeAggregateResult] = {
            t.theme_tag: t for t in theme_results
        }
        self._theme_context = theme_context or {}
        self._registry = registry

        # D3: 构造时即识别全市场主线题材
        self._identified_themes: List[IdentifiedTheme] = self.identify_main_themes()
        # 构建快速查找: board_name → IdentifiedTheme
        self._board_to_theme: Dict[str, IdentifiedTheme] = {}
        for theme in self._identified_themes:
            for board in theme.member_boards:
                self._board_to_theme[board] = theme

    def identify_main_themes(self) -> List[IdentifiedTheme]:
        """从全市场角度识别主线/次线题材（不依赖具体股票）。

        方案要求: "先由市场盘面确认，不是先由资讯定义"
        遍历所有板块，按 status + stage 规则筛选 MAIN_THEME / SECONDARY_THEME。
        有 registry 时合并同 canonical_tag 下的多个板块。
        """
        # 第一遍: 逐板块判定 position
        raw_themes: List[tuple] = []  # (board_name, position, sector)
        for board_name, sector in self._sector_map.items():
            position = self._position_from_sector(sector)
            if position in (ThemePosition.MAIN_THEME, ThemePosition.SECONDARY_THEME):
                raw_themes.append((board_name, position, sector))

        if not raw_themes:
            # 运行证据表明，部分交易日盘面只有 warm+expand 的强势板块，
            # 若此时直接返回空题材，会让 L2 完全丢失热点梳理能力。
            raw_themes = self._build_warm_expand_fallback_themes()
            if not raw_themes:
                return []

        # 有 registry 时按 canonical_tag 合并
        if self._registry is not None:
            tag_groups: Dict[str, List[tuple]] = {}
            for board_name, position, sector in raw_themes:
                tag = self._registry.resolve_tag(board_name)
                tag_groups.setdefault(tag, []).append((board_name, position, sector))

            themes: List[IdentifiedTheme] = []
            for tag, group in tag_groups.items():
                # 取组内最高分的板块作为代表
                best = max(group, key=lambda x: x[2].sector_hot_score)
                _, best_position, best_sector = best
                all_boards = [g[0] for g in group]
                all_leaders = []
                all_fronts = []
                for _, _, s in group:
                    all_leaders.extend(s.leader_codes)
                    all_fronts.extend(s.front_codes)
                themes.append(IdentifiedTheme(
                    name=tag,
                    position=best_position,
                    score=best_sector.sector_hot_score,
                    stage=best_sector.sector_stage,
                    leader_codes=list(set(all_leaders)),
                    front_codes=list(set(all_fronts)),
                    member_boards=all_boards,
                    quality_flags=dict(best_sector.quality_flags or {}),
                ))
            themes.sort(key=self._theme_sort_key, reverse=True)
            return themes[:MAX_IDENTIFIED_THEMES]

        # 无 registry: 每个板块独立为一个 theme
        themes = [
            IdentifiedTheme(
                name=board_name,
                position=position,
                score=sector.sector_hot_score,
                stage=sector.sector_stage,
                leader_codes=list(sector.leader_codes),
                front_codes=list(sector.front_codes),
                member_boards=[board_name],
                quality_flags=dict(sector.quality_flags or {}),
            )
            for board_name, position, sector in raw_themes
        ]
        themes.sort(key=self._theme_sort_key, reverse=True)
        return themes[:MAX_IDENTIFIED_THEMES]

    def _build_warm_expand_fallback_themes(self) -> List[tuple]:
        """当日没有 hot 主/次线时，允许 warm+expand/launch 作为次线兜底。"""
        fallback: List[tuple] = []
        for board_name, sector in self._sector_map.items():
            if sector.sector_status != "warm":
                continue
            if sector.sector_stage not in ("expand", "launch"):
                continue
            fallback.append((board_name, ThemePosition.SECONDARY_THEME, sector))
        fallback.sort(
            key=lambda item: (
                -float(item[2].board_strength_score or item[2].sector_hot_score),
                -float(item[2].sector_hot_score),
                item[0],
            )
        )
        return fallback[:MAX_FALLBACK_THEMES]

    def get_main_theme_boards(self) -> Set[str]:
        """返回所有主线/次线题材涉及的板块名集合（用于 L2 Universe 缩小）。"""
        boards: Set[str] = set()
        for theme in self._identified_themes:
            boards.update(theme.member_boards)
        return boards

    @property
    def identified_themes(self) -> List[IdentifiedTheme]:
        """公开已识别题材，供上游做统计与日志，不暴露可变内部引用。"""
        return list(self._identified_themes)

    def resolve(self, stock_boards: List[str]) -> ThemeDecision:
        if not stock_boards:
            return self._non_theme_decision()

        # ── D3 修复: 主线优先匹配 ──────────────────────────────────────
        # 先检查股票板块是否属于已识别的主线/次线题材
        for board_name in stock_boards:
            matched_theme = self._board_to_theme.get(board_name)
            if matched_theme is not None:
                theme_tag = matched_theme.name
                theme_result = self._theme_map.get(theme_tag)
                theme_score = theme_result.theme_score if theme_result else matched_theme.score
                return ThemeDecision(
                    theme_tag=theme_tag,
                    theme_score=theme_score,
                    theme_position=matched_theme.position,
                    leader_score=0.0,
                    sector_strength=self._sector_map.get(board_name, SectorHeatResult(board_name="")).strength_score,
                    theme_duration=(theme_result.sector_stage_rollup if theme_result else "unknown"),
                    trade_theme_stage=(theme_result.trade_theme_stage if theme_result else "unknown"),
                    leader_stocks=matched_theme.leader_codes,
                    front_stocks=matched_theme.front_codes,
                )

        # ── 兜底: 不在主线中，走原有 warm/follower 逻辑 ─────────────
        primary_board, primary_sector = self._pick_primary_board(stock_boards)
        if primary_sector is None:
            return self._non_theme_decision()

        position = self._position_from_sector(primary_sector)

        # 双源融合: 外部热点上下文调整
        position = self._adjust_with_external_context(position, primary_board)

        # 题材标签
        theme_tag = primary_board
        if self._registry is not None:
            theme_tag = self._registry.resolve_tag(primary_board)

        theme_result = self._theme_map.get(theme_tag)
        if theme_result is None:
            theme_result = self._theme_map.get(primary_board)
        theme_score = theme_result.theme_score if theme_result else primary_sector.sector_hot_score

        return ThemeDecision(
            theme_tag=theme_tag,
            theme_score=theme_score,
            theme_position=position,
            leader_score=0.0,
            sector_strength=primary_sector.strength_score,
            theme_duration=(theme_result.sector_stage_rollup if theme_result else primary_sector.sector_stage),
            trade_theme_stage=(theme_result.trade_theme_stage if theme_result else primary_sector.sector_stage),
            leader_stocks=primary_sector.leader_codes,
            front_stocks=primary_sector.front_codes,
        )

    # ── 内部方法 ─────────────────────────────────────────────────────────────

    def _pick_primary_board(
        self, boards: List[str],
    ) -> tuple[str, Optional[SectorHeatResult]]:
        """选取 sector_hot_score 最高的板块。"""
        best_name = ""
        best_sector: Optional[SectorHeatResult] = None
        best_score = -1.0

        for name in boards:
            sector = self._sector_map.get(name)
            if sector and sector.sector_hot_score > best_score:
                best_score = sector.sector_hot_score
                best_name = name
                best_sector = sector

        return best_name, best_sector

    def _position_from_sector(self, sector: SectorHeatResult) -> ThemePosition:
        """theme_position 推算规则（MVP）。"""
        status = sector.sector_status
        stage = sector.sector_stage

        if status == "hot" and stage in ("launch", "expand"):
            return ThemePosition.MAIN_THEME
        if status == "hot" and stage == "climax":
            return ThemePosition.SECONDARY_THEME
        if status == "warm":
            if stage == "expand":
                return ThemePosition.SECONDARY_THEME
            if stage in ("climax", "fade"):
                return ThemePosition.FADING_THEME
            if stage == "launch":
                return ThemePosition.FOLLOWER_THEME
            return ThemePosition.FOLLOWER_THEME
        if status == "hot" and stage == "fade":
            return ThemePosition.FADING_THEME
        return ThemePosition.NON_THEME

    @staticmethod
    def _theme_sort_key(theme: IdentifiedTheme) -> tuple:
        flags = theme.quality_flags or {}
        return (
            1 if theme.position == ThemePosition.MAIN_THEME else 0,
            1 if flags.get("has_leader_candidate") else 0,
            1 if flags.get("limit_up_cluster") else 0,
            1 if flags.get("persistence_ok") else 0,
            float(theme.score),
        )

    def _adjust_with_external_context(
        self, base_position: ThemePosition, primary_board: str,
    ) -> ThemePosition:
        """外部热点上下文融合：盘面 + OpenClaw 双源校验。"""
        if not self._theme_context:
            return base_position

        external_themes = self._theme_context.get("themes", [])
        match = self._find_matching_theme(primary_board, external_themes)
        if not match:
            return base_position

        ext_score = match.get("heat_score", 0)
        ext_confidence = match.get("confidence", 0)

        if base_position == ThemePosition.NON_THEME and ext_score >= 70 and ext_confidence >= 0.7:
            return ThemePosition.FOLLOWER_THEME
        if base_position == ThemePosition.FOLLOWER_THEME and ext_score >= 80 and ext_confidence >= 0.8:
            return ThemePosition.SECONDARY_THEME

        return base_position

    @staticmethod
    def _find_matching_theme(
        board_name: str, external_themes: List[dict],
    ) -> Optional[dict]:
        """在外部题材列表中查找匹配的题材。"""
        if not external_themes:
            return None
        board_lower = board_name.lower()
        for theme in external_themes:
            theme_name = theme.get("name", "").lower()
            if theme_name and (theme_name in board_lower or board_lower in theme_name):
                return theme
        return None

    def _non_theme_decision(self) -> ThemeDecision:
        return ThemeDecision(
            theme_tag="",
            theme_score=0.0,
            theme_position=ThemePosition.NON_THEME,
        )
