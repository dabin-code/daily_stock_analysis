# -*- coding: utf-8 -*-
"""Tests for ThemeMappingRegistry — TDD RED phase."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from src.services.theme_mapping_registry import (
    ThemeMapping,
    ThemeMappingRegistry,
    ThemeResolution,
    clear_theme_mapping_cache,
)

# ── 测试用 YAML 内容 ────────────────────────────────────────────────────

_TEST_YAML = """\
themes:
  - canonical_tag: "AI大模型"
    boards:
      - board_name: "AIGC概念"
        priority: 100
      - board_name: "多模态AI"
        priority: 90
  - canonical_tag: "半导体"
    boards:
      - board_name: "半导体概念"
        priority: 100
      - board_name: "芯片概念"
        priority: 80
  - canonical_tag: "锂电产业链"
    boards:
      - board_name: "锂电池概念"
        priority: 100
      - board_name: "锂矿概念"
        priority: 90
"""


def _write_temp_yaml(content: str) -> str:
    """写入临时 YAML 文件并返回路径。"""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class TestThemeMappingRegistry(unittest.TestCase):
    """ThemeMappingRegistry 核心逻辑测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._yaml_path = _write_temp_yaml(_TEST_YAML)
        cls.registry = ThemeMappingRegistry(config_path=cls._yaml_path)

    @classmethod
    def tearDownClass(cls) -> None:
        os.unlink(cls._yaml_path)

    # ── 加载 ────────────────────────────────────────────────────────

    def test_load_valid_yaml(self) -> None:
        """加载配置后内部映射非空。"""
        self.assertTrue(self.registry.is_mapped("AIGC概念"))
        self.assertTrue(self.registry.is_mapped("半导体概念"))
        self.assertTrue(self.registry.is_mapped("锂矿概念"))

    # ── resolve_tag ─────────────────────────────────────────────────

    def test_resolve_tag_mapped(self) -> None:
        """已映射板块返回 canonical_tag。"""
        self.assertEqual(self.registry.resolve_tag("AIGC概念"), "AI大模型")
        self.assertEqual(self.registry.resolve_tag("多模态AI"), "AI大模型")
        self.assertEqual(self.registry.resolve_tag("芯片概念"), "半导体")

    def test_resolve_tag_unmapped_fallback(self) -> None:
        """未映射板块 fallback 为 board_name 本身。"""
        self.assertEqual(self.registry.resolve_tag("白酒"), "白酒")
        self.assertEqual(self.registry.resolve_tag("未知板块XYZ"), "未知板块XYZ")

    # ── resolve_boards ──────────────────────────────────────────────

    def test_resolve_boards_single_board(self) -> None:
        """单板块正确解析。"""
        res = self.registry.resolve_boards(["AIGC概念"])
        self.assertEqual(res.canonical_tag, "AI大模型")
        self.assertEqual(res.primary_board, "AIGC概念")
        self.assertEqual(res.source, "manual")

    def test_resolve_boards_same_theme(self) -> None:
        """两板块映射同一题材 → 合并，取高优先级。"""
        res = self.registry.resolve_boards(["多模态AI", "AIGC概念"])
        self.assertEqual(res.canonical_tag, "AI大模型")
        self.assertEqual(res.primary_board, "AIGC概念")  # priority 100 > 90
        self.assertEqual(res.priority, 100)
        self.assertEqual(res.all_tags, ("AI大模型",))  # 去重为单元素 tuple

    def test_resolve_boards_different_themes(self) -> None:
        """多题材取最高 priority 题材组。"""
        res = self.registry.resolve_boards(["AIGC概念", "芯片概念"])
        # AIGC概念 priority=100 (AI大模型), 芯片概念 priority=80 (半导体)
        self.assertEqual(res.canonical_tag, "AI大模型")
        self.assertIn("AI大模型", res.all_tags)
        self.assertIn("半导体", res.all_tags)

    def test_resolve_boards_empty(self) -> None:
        """空列表返回 fallback resolution。"""
        res = self.registry.resolve_boards([])
        self.assertEqual(res.source, "fallback")
        self.assertEqual(res.canonical_tag, "")

    def test_resolve_boards_all_unmapped(self) -> None:
        """全部未映射板块 → fallback，取第一个 board_name。"""
        res = self.registry.resolve_boards(["白酒", "食品饮料"])
        self.assertEqual(res.source, "fallback")
        self.assertEqual(res.canonical_tag, "白酒")
        self.assertEqual(res.primary_board, "白酒")

    def test_resolve_boards_mixed_mapped_unmapped(self) -> None:
        """混合映射/未映射，映射板块优先。"""
        res = self.registry.resolve_boards(["白酒", "AIGC概念"])
        self.assertEqual(res.canonical_tag, "AI大模型")
        self.assertEqual(res.source, "manual")

    # ── get_all_boards_for_tag ──────────────────────────────────────

    def test_get_all_boards_for_tag(self) -> None:
        """反向查询返回所有板块。"""
        boards = self.registry.get_all_boards_for_tag("AI大模型")
        self.assertIn("AIGC概念", boards)
        self.assertIn("多模态AI", boards)
        self.assertEqual(len(boards), 2)

    def test_get_all_boards_for_unknown_tag(self) -> None:
        """未知 tag 返回空列表。"""
        self.assertEqual(self.registry.get_all_boards_for_tag("不存在"), [])

    # ── is_mapped ───────────────────────────────────────────────────

    def test_is_mapped_true(self) -> None:
        self.assertTrue(self.registry.is_mapped("AIGC概念"))

    def test_is_mapped_false(self) -> None:
        self.assertFalse(self.registry.is_mapped("白酒"))


class TestThemeMappingRegistryEdgeCases(unittest.TestCase):
    """异常/边界条件测试。"""

    def test_missing_config_file(self) -> None:
        """YAML 文件不存在 → 空 registry，全部 fallback。"""
        reg = ThemeMappingRegistry(config_path="/nonexistent/path.yaml")
        self.assertFalse(reg.is_mapped("AIGC概念"))
        self.assertEqual(reg.resolve_tag("AIGC概念"), "AIGC概念")

    def test_malformed_yaml(self) -> None:
        """格式错误的 YAML → 空 registry，不崩溃。"""
        path = _write_temp_yaml("not: [valid: yaml: {{{}}")
        try:
            reg = ThemeMappingRegistry(config_path=path)
            self.assertFalse(reg.is_mapped("AIGC概念"))
            self.assertEqual(reg.resolve_tag("AIGC概念"), "AIGC概念")
        finally:
            os.unlink(path)

    def test_empty_yaml(self) -> None:
        """空 YAML → 空 registry。"""
        path = _write_temp_yaml("")
        try:
            reg = ThemeMappingRegistry(config_path=path)
            self.assertFalse(reg.is_mapped("AIGC概念"))
            self.assertTrue(reg.is_empty)
        finally:
            os.unlink(path)

    def test_duplicate_board_name_last_wins(self) -> None:
        """同一 board_name 出现在两个 tag 下 → 后者覆盖。"""
        yaml_content = """\
themes:
  - canonical_tag: "AI大模型"
    boards:
      - board_name: "AIGC概念"
        priority: 100
  - canonical_tag: "算力产业链"
    boards:
      - board_name: "AIGC概念"
        priority: 80
"""
        path = _write_temp_yaml(yaml_content)
        try:
            reg = ThemeMappingRegistry(config_path=path)
            # 后定义的 canonical_tag 覆盖前者
            self.assertEqual(reg.resolve_tag("AIGC概念"), "算力产业链")
        finally:
            os.unlink(path)

    def test_invalid_priority_defaults_to_50(self) -> None:
        """priority 格式错误 → 默认 50，不崩溃。"""
        yaml_content = """\
themes:
  - canonical_tag: "测试"
    boards:
      - board_name: "板块A"
        priority: "invalid"
      - board_name: "板块B"
        priority: 80
"""
        path = _write_temp_yaml(yaml_content)
        try:
            reg = ThemeMappingRegistry(config_path=path)
            self.assertTrue(reg.is_mapped("板块A"))
            self.assertTrue(reg.is_mapped("板块B"))
            # 板块A priority=50 (default), 板块B priority=80
            res = reg.resolve_boards(["板块A", "板块B"])
            self.assertEqual(res.primary_board, "板块B")  # 80 > 50
        finally:
            os.unlink(path)

    def test_is_empty_property(self) -> None:
        """空 registry 的 is_empty 为 True。"""
        reg = ThemeMappingRegistry(config_path="/nonexistent/path.yaml")
        self.assertTrue(reg.is_empty)

    def test_get_all_boards_returns_copy(self) -> None:
        """get_all_boards_for_tag 返回副本，修改不影响注册表。"""
        path = _write_temp_yaml(_TEST_YAML)
        try:
            reg = ThemeMappingRegistry(config_path=path)
            boards = reg.get_all_boards_for_tag("AI大模型")
            boards.clear()
            self.assertEqual(len(reg.get_all_boards_for_tag("AI大模型")), 2)
        finally:
            os.unlink(path)

    def test_frozen_dataclass(self) -> None:
        """ThemeMapping 和 ThemeResolution 不可变。"""
        m = ThemeMapping(board_name="X", canonical_tag="Y", priority=50, source="manual")
        with self.assertRaises(AttributeError):
            m.priority = 999  # type: ignore[misc]

        r = ThemeResolution(
            canonical_tag="Y", primary_board="X", priority=50,
            all_tags=("Y",), source="manual",
        )
        with self.assertRaises(AttributeError):
            r.canonical_tag = "Z"  # type: ignore[misc]


class TestThemeMappingRegistryParseCache(unittest.TestCase):
    """YAML 解析缓存：重复构造不重复解析，且不返回陈旧配置。"""

    # 两份内容不同但字节数完全相同的配置，用于验证缓存不是按
    # (路径, mtime, size) 判定新鲜度——那样在同尺寸快速改写时会读到旧值。
    _YAML_TAG_A = """\
themes:
  - canonical_tag: "TAG_A"
    boards:
      - board_name: "BOARD_X"
        priority: 100
"""
    _YAML_TAG_B = """\
themes:
  - canonical_tag: "TAG_B"
    boards:
      - board_name: "BOARD_X"
        priority: 100
"""

    def setUp(self) -> None:
        clear_theme_mapping_cache()

    def tearDown(self) -> None:
        clear_theme_mapping_cache()

    def test_repeated_construction_parses_yaml_once(self) -> None:
        """同一份配置重复构造注册表，只解析一次 YAML。"""
        path = _write_temp_yaml(_TEST_YAML)
        try:
            with patch("yaml.safe_load", wraps=yaml.safe_load) as safe_load:
                registries = [
                    ThemeMappingRegistry(config_path=path) for _ in range(5)
                ]

            self.assertEqual(safe_load.call_count, 1)
            for reg in registries:
                self.assertEqual(reg.resolve_tag("AIGC概念"), "AI大模型")
                self.assertEqual(len(reg.get_all_boards_for_tag("AI大模型")), 2)
        finally:
            os.unlink(path)

    def test_rewritten_config_is_reparsed(self) -> None:
        """配置被改写后重新解析，不返回缓存中的旧映射。"""
        path = _write_temp_yaml(self._YAML_TAG_A)
        try:
            before = ThemeMappingRegistry(config_path=path)
            self.assertEqual(before.resolve_tag("BOARD_X"), "TAG_A")

            # 同尺寸、同一路径、紧接着改写：mtime 可能落在同一时间戳刻度内
            self.assertEqual(len(self._YAML_TAG_A), len(self._YAML_TAG_B))
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._YAML_TAG_B)

            after = ThemeMappingRegistry(config_path=path)
            self.assertEqual(after.resolve_tag("BOARD_X"), "TAG_B")
            # 已构造的实例保持自身快照，不被缓存刷新影响
            self.assertEqual(before.resolve_tag("BOARD_X"), "TAG_A")
        finally:
            os.unlink(path)

    def test_distinct_configs_do_not_share_cache_entry(self) -> None:
        """不同内容的配置各自解析，互不串味。"""
        path_a = _write_temp_yaml(self._YAML_TAG_A)
        path_b = _write_temp_yaml(self._YAML_TAG_B)
        try:
            self.assertEqual(
                ThemeMappingRegistry(config_path=path_a).resolve_tag("BOARD_X"),
                "TAG_A",
            )
            self.assertEqual(
                ThemeMappingRegistry(config_path=path_b).resolve_tag("BOARD_X"),
                "TAG_B",
            )
        finally:
            os.unlink(path_a)
            os.unlink(path_b)

    def test_missing_file_still_degrades_to_empty_registry(self) -> None:
        """缓存不改变文件缺失时的降级行为。"""
        reg = ThemeMappingRegistry(config_path="/nonexistent/cached/path.yaml")
        self.assertTrue(reg.is_empty)


class TestThemeMappingRegistryDefaultPath(unittest.TestCase):
    """默认路径加载测试。"""

    def test_default_path_loads_project_yaml(self) -> None:
        """无参构造使用项目 data/theme_board_mappings.yaml。"""
        reg = ThemeMappingRegistry()
        # 至少能加载项目配置中的某个映射
        self.assertTrue(reg.is_mapped("AIGC概念"))


if __name__ == "__main__":
    unittest.main()
