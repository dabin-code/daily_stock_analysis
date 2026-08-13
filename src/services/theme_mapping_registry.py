# -*- coding: utf-8 -*-
"""
ThemeMappingRegistry: 板块→题材主数据映射注册表。

从 YAML 配置加载稳定的 board_name → canonical_theme_tag 映射，
支持多板块聚合到统一题材标签，替代原有 1:1 映射。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── 数据结构 ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ThemeMapping:
    """单条板块→题材映射记录。"""

    board_name: str       # "AIGC概念"
    canonical_tag: str    # "AI大模型"
    priority: int         # 越高越优先
    source: str           # "manual" | "fallback"


@dataclass(frozen=True)
class ThemeResolution:
    """多板块归属解析结果。"""

    canonical_tag: str     # 胜出的题材标签
    primary_board: str     # 决定该标签的板块
    priority: int          # 胜出优先级
    all_tags: Tuple[str, ...]    # 该股票所有映射到的题材标签
    source: str            # "manual" | "fallback"


# ── 默认配置路径 ─────────────────────────────────────────────────────────

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "theme_board_mappings.yaml"


# ── YAML 解析缓存 ────────────────────────────────────────────────────────
#
# 回测里每次 FiveLayerPipeline.run() 都会新建一个 ThemeMappingRegistry，
# 同一份 YAML 会被重复解析上百次。缓存键取文件内容摘要而非路径或 mtime：
# 文件一旦被改写，摘要必然变化，因此不存在读到陈旧配置的时间窗口。

_MAX_CACHED_DOCUMENTS = 8
_document_cache: Dict[str, Any] = {}


def clear_theme_mapping_cache() -> None:
    """清空 YAML 解析缓存。测试或需要强制重读配置时使用。"""
    _document_cache.clear()


def _load_yaml_document(path: str) -> Any:
    """读取并解析 YAML；内容未变时复用已解析结果。

    返回值在多个注册表实例间共享，调用方只读不改。
    """
    import yaml

    with open(path, "rb") as f:
        raw = f.read()

    digest = hashlib.sha256(raw).hexdigest()
    if digest in _document_cache:
        return _document_cache[digest]

    data = yaml.safe_load(raw.decode("utf-8"))

    if len(_document_cache) >= _MAX_CACHED_DOCUMENTS:
        oldest = next(iter(_document_cache), None)
        if oldest is not None:
            _document_cache.pop(oldest, None)
    _document_cache[digest] = data
    return data


# ── 注册表 ───────────────────────────────────────────────────────────────

class ThemeMappingRegistry:
    """board_name → canonical_theme_tag 确定性映射注册表。"""

    def __init__(self, config_path: Optional[str] = None) -> None:
        self._board_to_mapping: Dict[str, ThemeMapping] = {}
        self._tag_to_boards: Dict[str, List[str]] = {}
        path = config_path if config_path is not None else str(_DEFAULT_CONFIG_PATH)
        self._load(path)

    # ── public API ──────────────────────────────────────────────────

    def resolve_tag(self, board_name: str) -> str:
        """单板块 → canonical_tag。未映射时 fallback 为 board_name。"""
        mapping = self._board_to_mapping.get(board_name)
        return mapping.canonical_tag if mapping else board_name

    def resolve_boards(self, board_names: List[str]) -> ThemeResolution:
        """多板块归属解析：取最高 priority 的题材组。"""
        if not board_names:
            return ThemeResolution(
                canonical_tag="", primary_board="", priority=0,
                all_tags=(), source="fallback",
            )

        # 分离已映射 vs 未映射
        mapped: List[ThemeMapping] = []
        for b in board_names:
            m = self._board_to_mapping.get(b)
            if m is not None:
                mapped.append(m)

        if not mapped:
            # 全部未映射 → fallback 到第一个 board_name
            first = board_names[0]
            return ThemeResolution(
                canonical_tag=first, primary_board=first, priority=0,
                all_tags=(first,), source="fallback",
            )

        # 按 canonical_tag 分组，取各组最高 priority
        tag_best: Dict[str, ThemeMapping] = {}
        for m in mapped:
            existing = tag_best.get(m.canonical_tag)
            if existing is None or m.priority > existing.priority:
                tag_best[m.canonical_tag] = m

        # 所有题材标签
        all_tags = tuple(sorted(tag_best.keys()))

        # 取最高 priority 的题材组
        winner = max(tag_best.values(), key=lambda m: m.priority)

        return ThemeResolution(
            canonical_tag=winner.canonical_tag,
            primary_board=winner.board_name,
            priority=winner.priority,
            all_tags=all_tags,
            source="manual",
        )

    def get_all_boards_for_tag(self, canonical_tag: str) -> List[str]:
        """反向查询：题材 → 所有关联板块。"""
        return list(self._tag_to_boards.get(canonical_tag, []))

    def is_mapped(self, board_name: str) -> bool:
        """检查板块是否有显式映射。"""
        return board_name in self._board_to_mapping

    @property
    def is_empty(self) -> bool:
        """注册表是否为空（零映射加载）。"""
        return len(self._board_to_mapping) == 0

    # ── private ─────────────────────────────────────────────────────

    def _load(self, path: str) -> None:
        """从 YAML 文件加载映射。失败时退化为空注册表。"""
        try:
            import yaml  # noqa: F401
        except ImportError:
            logger.warning("PyYAML not installed; ThemeMappingRegistry empty.")
            return

        try:
            data = _load_yaml_document(path)
        except FileNotFoundError:
            logger.warning("Theme mapping config not found: %s", path)
            return
        except Exception:
            logger.warning("Failed to load theme mapping config: %s", path, exc_info=True)
            return

        if not isinstance(data, dict):
            return

        themes = data.get("themes")
        if not isinstance(themes, list):
            return

        for theme_entry in themes:
            if not isinstance(theme_entry, dict):
                continue
            canonical_tag = theme_entry.get("canonical_tag", "")
            if not canonical_tag:
                continue

            boards = theme_entry.get("boards", [])
            if not isinstance(boards, list):
                continue

            board_names_for_tag: List[str] = []
            for board_entry in boards:
                try:
                    if not isinstance(board_entry, dict):
                        continue
                    board_name = board_entry.get("board_name", "")
                    if not board_name:
                        continue
                    try:
                        priority = int(board_entry.get("priority", 50))
                    except (TypeError, ValueError):
                        logger.warning(
                            "Invalid priority for board '%s' in tag '%s', defaulting to 50",
                            board_name, canonical_tag,
                        )
                        priority = 50

                    # H1: 重复 board_name 检测
                    existing = self._board_to_mapping.get(board_name)
                    if existing is not None:
                        logger.warning(
                            "Duplicate board_name '%s': overwriting tag '%s' with '%s'",
                            board_name, existing.canonical_tag, canonical_tag,
                        )

                    mapping = ThemeMapping(
                        board_name=board_name,
                        canonical_tag=canonical_tag,
                        priority=priority,
                        source="manual",
                    )
                    self._board_to_mapping[board_name] = mapping
                    board_names_for_tag.append(board_name)
                except Exception:
                    logger.warning("Skipping malformed board entry in tag '%s'", canonical_tag, exc_info=True)

            if board_names_for_tag:
                # H4: 同 canonical_tag 多次出现时追加而非覆盖
                self._tag_to_boards.setdefault(canonical_tag, []).extend(board_names_for_tag)

        logger.info(
            "ThemeMappingRegistry loaded: %d boards → %d themes",
            len(self._board_to_mapping),
            len(self._tag_to_boards),
        )
