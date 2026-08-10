# -*- coding: utf-8 -*-
"""TradePlanBuilder 单元测试 — Phase 3A TDD red→green。"""

from __future__ import annotations

import json
import unittest

from src.schemas.trading_types import (
    CandidatePoolLevel,
    EntryMaturity,
    RiskLevel,
    SetupType,
    TradePlan,
    TradeStage,
)
from src.services.trade_plan_builder import TradePlanBuilder
from src.services import bottom_divergence_v2_trade_support


class TestTradePlanBuilder(unittest.TestCase):

    def setUp(self) -> None:
        self.builder = TradePlanBuilder()
        self.base_fs: dict = {}

    # ── 非可执行阶段 → None ─────────────────────────────────────────

    def test_watch_returns_none(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.WATCH,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot=self.base_fs,
        )
        self.assertIsNone(result)

    def test_focus_returns_none(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.FOCUS,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot=self.base_fs,
        )
        self.assertIsNone(result)

    def test_stand_aside_returns_none(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.STAND_ASIDE,
            setup_type=SetupType.NONE,
            entry_maturity=EntryMaturity.LOW,
            risk_level=RiskLevel.HIGH,
            pool_level=CandidatePoolLevel.WATCHLIST,
            factor_snapshot=self.base_fs,
        )
        self.assertIsNone(result)

    def test_reject_returns_none(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.REJECT,
            setup_type=SetupType.NONE,
            entry_maturity=EntryMaturity.LOW,
            risk_level=RiskLevel.HIGH,
            pool_level=CandidatePoolLevel.WATCHLIST,
            factor_snapshot=self.base_fs,
        )
        self.assertIsNone(result)

    # ── probe_entry 基本测试 ────────────────────────────────────────

    def test_probe_entry_bottom_divergence_has_stop_loss(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.BOTTOM_DIVERGENCE_BREAKOUT,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot=self.base_fs,
        )
        self.assertIsNotNone(result)
        self.assertIsInstance(result, TradePlan)
        self.assertIsNotNone(result.stop_loss_rule)
        self.assertTrue(len(result.stop_loss_rule) > 0)

    def test_probe_entry_has_no_add_rule(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot=self.base_fs,
        )
        self.assertIsNotNone(result)
        self.assertIsNone(result.add_rule)

    def test_probe_entry_trend_breakout_holding_expectation(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot=self.base_fs,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.holding_expectation, "1~2周波段")

    def test_probe_entry_limitup_holding_expectation(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.LIMITUP_STRUCTURE,
            entry_maturity=EntryMaturity.HIGH,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot=self.base_fs,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.holding_expectation, "3~5日短线")

    def test_probe_entry_has_invalidation_rule(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.LOW123_BREAKOUT,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot=self.base_fs,
        )
        self.assertIsNotNone(result)
        self.assertIn("3个交易日", result.invalidation_rule)

    def test_probe_entry_has_take_profit_plan(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.GAP_BREAKOUT,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot=self.base_fs,
        )
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.take_profit_plan)
        self.assertTrue(len(result.take_profit_plan) > 0)

    # ── risk_level → initial_position 映射 ─────────────────────────

    def test_risk_high_small_position(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.HIGH,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot=self.base_fs,
        )
        self.assertEqual(result.initial_position, "1/10仓")

    def test_risk_medium_moderate_position(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot=self.base_fs,
        )
        self.assertEqual(result.initial_position, "1/5仓")

    def test_risk_low_larger_position(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.LOW,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot=self.base_fs,
        )
        self.assertEqual(result.initial_position, "1/3仓")

    # ── add_on_strength 测试 ────────────────────────────────────────

    def test_add_on_strength_has_add_rule(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.ADD_ON_STRENGTH,
            setup_type=SetupType.LOW123_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            risk_level=RiskLevel.LOW,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            factor_snapshot=self.base_fs,
        )
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.add_rule)
        self.assertIn("加仓", result.add_rule)

    def test_add_on_strength_has_stop_loss(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.ADD_ON_STRENGTH,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            factor_snapshot=self.base_fs,
        )
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.stop_loss_rule)

    def test_add_on_strength_risk_low_larger_position(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.ADD_ON_STRENGTH,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            risk_level=RiskLevel.LOW,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            factor_snapshot=self.base_fs,
        )
        self.assertEqual(result.initial_position, "1/2仓")

    def test_add_on_strength_risk_high_small_position(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.ADD_ON_STRENGTH,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            risk_level=RiskLevel.HIGH,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            factor_snapshot=self.base_fs,
        )
        self.assertEqual(result.initial_position, "1/5仓")

    # ── 底背离双突破：递进三级买点 add_rule ─────────────────────────
    # Notion《各形态买卖点识别手册》要求底背离双突破按"趋势线突破→阻力线
    # 突破→回踩支撑"三级递进加仓。下列三条断言验证 ADD_ON 阶段的 add_rule
    # 能吃到 detector 产出的 buy_points 细节价格，而不是沿用静态模板。

    def test_add_on_bottom_divergence_add_rule_uses_buy_points(self) -> None:
        fs = {
            "bottom_divergence_state": "confirmed",
            "bottom_divergence_exit_plan": {"initial_stop_loss": 9.5},
            "bottom_divergence_buy_points": [
                {
                    "level": 1,
                    "label": "趋势线突破",
                    "trigger_price": 10.2,
                    "triggered": True,
                    "position_ratio": "1/5仓",
                    "stop_loss_price": 9.5,
                },
                {
                    "level": 2,
                    "label": "阻力线突破",
                    "trigger_price": 11.0,
                    "triggered": False,
                    "position_ratio": "1/3仓",
                    "stop_loss_price": 10.5,
                },
                {
                    "level": 3,
                    "label": "回踩支撑",
                    "trigger_price": 11.0,
                    "triggered": False,
                    "position_ratio": "1/3仓",
                    "stop_loss_price": 10.7,
                },
            ],
        }
        result = self.builder.build(
            trade_stage=TradeStage.ADD_ON_STRENGTH,
            setup_type=SetupType.BOTTOM_DIVERGENCE_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            factor_snapshot=fs,
        )
        self.assertIsNotNone(result.add_rule)
        self.assertIn("阻力线突破", result.add_rule)
        self.assertIn("11.00", result.add_rule)
        self.assertIn("1/3仓", result.add_rule)

    def test_add_on_bottom_divergence_after_level2_points_to_pullback(self) -> None:
        """Level 1 + Level 2 都已触发 → add_rule 描述下一级（回踩）。"""
        fs = {
            "bottom_divergence_state": "confirmed",
            "bottom_divergence_buy_points": [
                {"level": 1, "label": "趋势线突破", "trigger_price": 10.2,
                 "triggered": True, "position_ratio": "1/5仓",
                 "stop_loss_price": 9.5},
                {"level": 2, "label": "阻力线突破", "trigger_price": 11.0,
                 "triggered": True, "position_ratio": "1/3仓",
                 "stop_loss_price": 10.5},
                {"level": 3, "label": "回踩支撑", "trigger_price": 11.0,
                 "triggered": False, "position_ratio": "1/3仓",
                 "stop_loss_price": 10.7},
            ],
        }
        result = self.builder.build(
            trade_stage=TradeStage.ADD_ON_STRENGTH,
            setup_type=SetupType.BOTTOM_DIVERGENCE_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            factor_snapshot=fs,
        )
        self.assertIsNotNone(result.add_rule)
        self.assertIn("回踩支撑", result.add_rule)

    def test_add_on_bottom_divergence_falls_back_when_no_buy_points(self) -> None:
        """buy_points 缺失 → 沿用静态模板以向后兼容。"""
        result = self.builder.build(
            trade_stage=TradeStage.ADD_ON_STRENGTH,
            setup_type=SetupType.BOTTOM_DIVERGENCE_BREAKOUT,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            factor_snapshot=self.base_fs,
        )
        self.assertIsNotNone(result.add_rule)
        self.assertIn("加仓", result.add_rule)

    def test_v2_early_plan_targets_twenty_percent_and_references_r1(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot={
                "bottom_divergence_v2_stage": "early",
                "bottom_divergence_v2_actionability_status": "major_not_confirmed",
                "bottom_divergence_v2_stop_loss_price": 8.8,
                "bottom_divergence_v2_layered_buy_points": [
                    {
                        "level": "early",
                        "price": 12.0,
                        "stop": 8.8,
                        "triggered": True,
                    },
                    {
                        "level": "r1",
                        "price": 14.0,
                        "stop": 9.5,
                        "triggered": False,
                    },
                    {
                        "level": "r2",
                        "price": 16.0,
                        "stop": 11.0,
                        "triggered": False,
                    },
                ],
            },
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.initial_position, "目标仓位20%")
        self.assertIn("R1", result.add_rule)
        self.assertEqual(result.entry_price, 12.0)
        self.assertEqual(result.stop_loss_price, 8.8)
        self.assertEqual(result.holding_expectation, "1~2周波段")

    def test_v2_near_plan_targets_fifty_percent_and_references_r2(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.ADD_ON_STRENGTH,
            setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
            entry_maturity=EntryMaturity.HIGH,
            risk_level=RiskLevel.LOW,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            factor_snapshot={
                "bottom_divergence_v2_stage": "near_cleared",
                "bottom_divergence_v2_actionability_status": "major_not_confirmed",
                "bottom_divergence_v2_stop_loss_price": 8.8,
                "bottom_divergence_v2_layered_buy_points": [
                    {
                        "level": "early",
                        "price": 12.0,
                        "stop": 8.8,
                        "triggered": True,
                    },
                    {
                        "level": "r1",
                        "price": 14.0,
                        "stop": 9.5,
                        "triggered": True,
                    },
                    {
                        "level": "r2",
                        "price": 16.0,
                        "stop": 11.0,
                        "triggered": False,
                    },
                ],
            },
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.initial_position, "目标仓位50%")
        self.assertIn("R2", result.add_rule)
        self.assertEqual(result.entry_price, 14.0)
        self.assertEqual(result.stop_loss_price, 8.8)

    def test_v2_major_plan_targets_full_position_without_further_add(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.ADD_ON_STRENGTH,
            setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
            entry_maturity=EntryMaturity.HIGH,
            risk_level=RiskLevel.LOW,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            factor_snapshot={
                "bottom_divergence_v2_stage": "major_actionable",
                "bottom_divergence_v2_actionability_status": "actionable",
                "bottom_divergence_v2_major_breakout": True,
                "bottom_divergence_v2_major_actionable_entry": True,
                "bottom_divergence_v2_stop_loss_price": 8.8,
                "bottom_divergence_v2_layered_buy_points": [
                    {
                        "level": "early",
                        "price": 12.0,
                        "stop": 8.8,
                        "triggered": True,
                    },
                    {
                        "level": "r1",
                        "price": 14.0,
                        "stop": 9.5,
                        "triggered": True,
                    },
                    {
                        "level": "r2",
                        "price": 16.0,
                        "stop": 11.0,
                        "triggered": True,
                    },
                ],
            },
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.initial_position, "目标仓位100%")
        self.assertIsNone(result.add_rule)
        self.assertEqual(result.entry_price, 16.0)

    def test_v2_plan_consumes_only_v2_price_fields(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot={
                "bottom_divergence_v2_stage": "early",
                "bottom_divergence_v2_actionability_status": "major_not_confirmed",
                "bottom_divergence_v2_stop_loss_price": 8.8,
                "bottom_divergence_v2_layered_buy_points": [
                    {
                        "level": "early",
                        "price": 12.0,
                        "stop": 8.8,
                        "triggered": True,
                    },
                ],
                "bottom_divergence_entry_price": 99.0,
                "bottom_divergence_stop_loss": 98.0,
                "bottom_divergence_buy_points": [
                    {"level": 1, "trigger_price": 97.0, "stop_loss_price": 96.0}
                ],
                "close": 95.0,
            },
        )

        self.assertEqual(result.entry_price, 12.0)
        self.assertEqual(result.stop_loss_price, 8.8)
        self.assertNotIn("97.00", result.add_rule)

    def test_v2_global_infinite_stop_falls_back_to_triggered_stage_point(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot={
                "bottom_divergence_v2_stage": "early",
                "bottom_divergence_v2_actionability_status": "major_not_confirmed",
                "bottom_divergence_v2_stop_loss_price": float("inf"),
                "bottom_divergence_v2_layered_buy_points": [
                    {
                        "level": "early",
                        "price": 12.0,
                        "stop": 8.8,
                        "triggered": True,
                    },
                    {
                        "level": "r1",
                        "price": 14.0,
                        "stop": 9.5,
                        "triggered": False,
                    },
                ],
            },
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.stop_loss_price, 8.8)
        self.assertIn("8.80", result.stop_loss_rule)

    def test_v2_all_invalid_stops_produce_none_structured_stop(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot={
                "bottom_divergence_v2_stage": "early",
                "bottom_divergence_v2_stop_loss_price": float("nan"),
                "bottom_divergence_v2_layered_buy_points": [
                    {
                        "level": "early",
                        "price": 12.0,
                        "stop": float("inf"),
                        "triggered": True,
                    },
                    {
                        "level": "r1",
                        "price": 14.0,
                        "stop": 9.5,
                        "triggered": False,
                    },
                ],
            },
        )

        self.assertIsNone(result)

    def test_v2_r1_and_major_cannot_reuse_historical_layer_stop(self) -> None:
        cases = (
            (
                "near_cleared",
                EntryMaturity.HIGH,
                [{"level": "r1", "price": 14.0, "stop": None}],
            ),
            (
                "major_actionable",
                EntryMaturity.HIGH,
                [{"level": "r2", "price": 16.0, "stop": None}],
            ),
        )
        for stage, maturity, current_points in cases:
            with self.subTest(stage=stage):
                result = self.builder.build(
                    trade_stage=TradeStage.ADD_ON_STRENGTH,
                    setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
                    entry_maturity=maturity,
                    risk_level=RiskLevel.LOW,
                    pool_level=CandidatePoolLevel.LEADER_POOL,
                    factor_snapshot={
                        "bottom_divergence_v2_stage": stage,
                        "bottom_divergence_v2_actionability_status": (
                            "actionable"
                            if stage == "major_actionable"
                            else "major_not_confirmed"
                        ),
                        "bottom_divergence_v2_major_actionable_entry": (
                            stage == "major_actionable"
                        ),
                        "bottom_divergence_v2_stop_loss_price": None,
                        "bottom_divergence_v2_layered_buy_points": [
                            {
                                "level": "early",
                                "price": 12.0,
                                "stop": 8.8,
                                "triggered": True,
                            },
                            *[
                                {**point, "triggered": True}
                                for point in current_points
                            ],
                        ],
                    },
                )

                self.assertIsNone(result)

    def test_v2_current_stage_alias_point_drives_entry_and_stop(self) -> None:
        cases = (
            ("near_cleared", "near", 14.0, 9.5, "目标仓位50%"),
            ("major_actionable", "major", 16.0, 11.0, "目标仓位100%"),
        )
        for stage, level, entry, stop, position in cases:
            with self.subTest(stage=stage):
                result = self.builder.build(
                    trade_stage=TradeStage.ADD_ON_STRENGTH,
                    setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
                    entry_maturity=EntryMaturity.HIGH,
                    risk_level=RiskLevel.LOW,
                    pool_level=CandidatePoolLevel.LEADER_POOL,
                    factor_snapshot={
                        "bottom_divergence_v2_stage": stage,
                        "bottom_divergence_v2_actionability_status": (
                            "actionable"
                            if stage == "major_actionable"
                            else "major_not_confirmed"
                        ),
                        "bottom_divergence_v2_major_actionable_entry": (
                            stage == "major_actionable"
                        ),
                        "bottom_divergence_v2_stop_loss_price": None,
                        "bottom_divergence_v2_layered_buy_points": [
                            {
                                "level": level,
                                "price": entry,
                                "stop": stop,
                                "triggered": True,
                            },
                        ],
                    },
                )

                self.assertIsNotNone(result)
                self.assertEqual(result.entry_price, entry)
                self.assertEqual(result.stop_loss_price, stop)
                self.assertEqual(result.initial_position, position)
                self.assertIn(f"{stop:.2f}", result.stop_loss_rule)

    def test_v2_non_actionable_stages_never_build_executable_add_on(self) -> None:
        for stage in (
            "forming",
            "invalidated",
            "stale",
            "extended",
            "major_unverified",
            "breakout_failed",
            "major_confirmed",
            "unknown_stage",
            "rejected",
        ):
            with self.subTest(stage=stage):
                result = self.builder.build(
                    trade_stage=TradeStage.ADD_ON_STRENGTH,
                    setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
                    entry_maturity=EntryMaturity.LOW,
                    risk_level=RiskLevel.MEDIUM,
                    pool_level=CandidatePoolLevel.LEADER_POOL,
                    factor_snapshot={
                        "bottom_divergence_v2_stage": stage,
                        "bottom_divergence_v2_stop_loss_price": 8.8,
                        "bottom_divergence_v2_layered_buy_points": [
                            {"level": "r1", "price": 14.0, "stop": 9.5},
                        ],
                    },
                )
                self.assertIsNone(result)

    def test_v2_stale_major_shape_cannot_fall_back_to_near_plan(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
            entry_maturity=EntryMaturity.HIGH,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot={
                "bottom_divergence_v2_stage": "near_cleared",
                "bottom_divergence_v2_actionability_status": (
                    "confirmation_too_old"
                ),
                "bottom_divergence_v2_major_breakout": True,
                "bottom_divergence_v2_major_actionable_entry": False,
                "bottom_divergence_v2_stop_loss_price": 8.8,
                "bottom_divergence_v2_layered_buy_points": [
                    {"level": "r1", "price": 14.0, "stop": 8.8},
                    {"level": "r2", "price": 16.0, "stop": 8.8},
                ],
            },
        )

        self.assertIsNone(result)

    def test_v2_execution_gate_requires_stage_specific_status(self) -> None:
        execution_allowed = (
            bottom_divergence_v2_trade_support.is_v2_execution_allowed
        )
        self.assertTrue(execution_allowed({
            "bottom_divergence_v2_stage": "early",
            "bottom_divergence_v2_actionability_status": "major_not_confirmed",
        }))
        self.assertTrue(execution_allowed({
            "bottom_divergence_v2_stage": "near_cleared",
            "bottom_divergence_v2_actionability_status": "major_not_confirmed",
        }))
        self.assertTrue(execution_allowed({
            "bottom_divergence_v2_stage": "major_actionable",
            "bottom_divergence_v2_actionability_status": "actionable",
            "bottom_divergence_v2_major_actionable_entry": True,
        }))
        for snapshot in (
            {
                "bottom_divergence_v2_stage": "early",
                "bottom_divergence_v2_actionability_status": "adjustment_unknown",
            },
            {
                "bottom_divergence_v2_stage": "early",
                "bottom_divergence_v2_actionability_status": "unknown",
            },
            {
                "bottom_divergence_v2_stage": "near_cleared",
                "bottom_divergence_v2_actionability_status": "",
            },
            {
                "bottom_divergence_v2_stage": "major_actionable",
                "bottom_divergence_v2_actionability_status": "actionable",
                "bottom_divergence_v2_major_actionable_entry": False,
            },
        ):
            with self.subTest(snapshot=snapshot):
                self.assertFalse(execution_allowed(snapshot))

    def test_v2_unknown_provenance_early_never_builds_plan(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot={
                "bottom_divergence_v2_stage": "early",
                "bottom_divergence_v2_actionability_status": "adjustment_unknown",
                "bottom_divergence_v2_stop_loss_price": 8.8,
                "bottom_divergence_v2_layered_buy_points": [
                    {
                        "level": "early",
                        "price": 12.0,
                        "stop": 8.8,
                        "triggered": True,
                    },
                ],
            },
        )

        self.assertIsNone(result)

    # ── 每种 setup_type 的止损模板覆盖 ─────────────────────────────

    def test_each_setup_type_has_stop_loss_template(self) -> None:
        actionable_setups = [
            SetupType.BOTTOM_DIVERGENCE_BREAKOUT,
            SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
            SetupType.LOW123_BREAKOUT,
            SetupType.TREND_BREAKOUT,
            SetupType.TREND_PULLBACK,
            SetupType.GAP_BREAKOUT,
            SetupType.LIMITUP_STRUCTURE,
        ]
        for st in actionable_setups:
            with self.subTest(setup_type=st):
                factor_snapshot = self.base_fs
                if st == SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY:
                    factor_snapshot = {
                        "bottom_divergence_v2_stage": "early",
                        "bottom_divergence_v2_actionability_status": (
                            "major_not_confirmed"
                        ),
                        "bottom_divergence_v2_stop_loss_price": 8.8,
                        "bottom_divergence_v2_layered_buy_points": [
                            {
                                "level": "early",
                                "price": 9.2,
                                "stop": 8.8,
                                "triggered": True,
                            },
                        ],
                    }
                result = self.builder.build(
                    trade_stage=TradeStage.PROBE_ENTRY,
                    setup_type=st,
                    entry_maturity=EntryMaturity.MEDIUM,
                    risk_level=RiskLevel.MEDIUM,
                    pool_level=CandidatePoolLevel.FOCUS_LIST,
                    factor_snapshot=factor_snapshot,
                )
                self.assertIsNotNone(result)
                self.assertIsNotNone(result.stop_loss_rule, f"{st} missing stop_loss_rule")
                self.assertTrue(len(result.stop_loss_rule) > 0)

    # ── setup_type=NONE 边界 ────────────────────────────────────────

    def test_setup_none_with_probe_entry_uses_default(self) -> None:
        """If somehow trade_stage is probe_entry but setup_type is NONE, still generates a plan."""
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.NONE,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot=self.base_fs,
        )
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.stop_loss_rule)

    # ── risk_level 写入 TradePlan ───────────────────────────────────

    def test_risk_level_propagated(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.HIGH,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot=self.base_fs,
        )
        self.assertEqual(result.risk_level, RiskLevel.HIGH)

    def test_execution_note_handles_non_numeric_anchor_values(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot={"close": "not-a-number", "ma20": object(), "ma100": None},
        )

        self.assertIsNotNone(result)
        self.assertIn("执行锚点", result.execution_note)

    def test_execution_note_keeps_existing_close_anchor_copy(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot={"close": 25.5, "ma20": 24.8, "ma100": 22.0},
        )

        self.assertIsNotNone(result)
        self.assertIn("现价25.50", result.execution_note)

    # ── 真实交易回放所需结构化字段 ─────────────────────────────────

    def test_low123_plan_carries_structured_entry_and_exit_prices(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.LOW123_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            factor_snapshot={
                "pattern_123_entry_price": 12.5,
                "pattern_123_stop_loss": 11.8,
            },
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.entry_price, 12.5)
        self.assertEqual(result.entry_rule, "触达低位123结构买点")
        self.assertEqual(result.stop_loss_price, 11.8)
        self.assertEqual(result.time_stop_days, 3)
        self.assertEqual(result.entry_valid_days, 1)

    def test_hot_theme_risk_params_drive_structured_trade_plan(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            factor_snapshot={
                "close": 20.0,
                "risk_params": {
                    "stop_loss": 18.8,
                    "take_profit_ratio": 0.1,
                },
            },
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.entry_price, 20.0)
        self.assertEqual(result.stop_loss_price, 18.8)
        self.assertEqual(result.take_profit_price, 22.0)
        self.assertTrue(any("止损" in rule for rule in result.exit_rules))
        self.assertTrue(any("止盈" in rule for rule in result.exit_rules))

    def test_trade_plan_json_round_trips_structured_fields(self) -> None:
        result = self.builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.BOTTOM_DIVERGENCE_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            factor_snapshot={
                "bottom_divergence_entry_price": 8.8,
                "bottom_divergence_stop_loss": 8.2,
                "bottom_divergence_exit_plan": {
                    "take_profit_targets": [
                        {"target_price": 9.7, "label": "第一目标", "action": "减半"}
                    ]
                },
            },
        )

        payload = json.loads(json.dumps(result, default=lambda item: getattr(item, "value", item.__dict__)))
        self.assertEqual(payload["entry_price"], 8.8)
        self.assertEqual(payload["stop_loss_price"], 8.2)
        self.assertEqual(payload["take_profit_price"], 9.7)


if __name__ == "__main__":
    unittest.main()
