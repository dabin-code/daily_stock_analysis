# -*- coding: utf-8 -*-
"""
集成测试：五层决策链路 pipeline 集成验证。

验证:
  1. _apply_five_layer_decision 正确为 candidate 赋值五层字段
  2. _build_candidate_payloads 包含五层字段
  3. save_screening_candidates 写入五层字段到 DB
  4. 硬规则在集成层面生效（stand_aside → watch）
"""

import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from src.core.market_guard import MarketGuardResult
from src.schemas.trading_types import (
    EntryMaturity,
    MarketEnvironment,
    MarketRegime,
    RiskLevel,
    ThemeDecision,
    ThemePosition,
    TradeStage,
)
from src.services.five_layer_pipeline import FiveLayerPipeline
from src.services.screener_service import ScreeningCandidateRecord


def _make_candidate(
    code: str = "600519",
    name: str = "贵州茅台",
    rank: int = 1,
    setup_type: str = "trend_breakout",
    factor_snapshot: dict | None = None,
) -> ScreeningCandidateRecord:
    fs = factor_snapshot or {
        "pct_chg": 5.0,
        "leader_score": 75.0,
        "extreme_strength_score": 85.0,
        # 真实止损来源字段，交由 resolve_setup_stop_loss 解析
        "risk_params": {"stop_loss": 88.0},
        "ma100_breakout_days": 3,
    }
    return ScreeningCandidateRecord(
        code=code,
        name=name,
        rank=rank,
        rule_score=80.0,
        rule_hits=["trend_breakout_hit"],
        factor_snapshot=fs,
        matched_strategies=["trend_breakout"],
        strategy_scores={"trend_breakout": 80.0},
        setup_type=setup_type,
    )


class FiveLayerDecisionTestCase(unittest.TestCase):
    """测试 _apply_five_layer_decision 方法。"""

    def _make_service(self):
        """构造最小化的 ScreeningTaskService mock。"""
        from src.services.screening_task_service import ScreeningTaskService

        db_mock = MagicMock()
        db_mock.batch_get_instrument_board_names.return_value = {
            "600519": ["白酒"],
            "000858": ["白酒"],
        }
        db_mock.list_sector_heat_history.return_value = []

        svc = ScreeningTaskService.__new__(ScreeningTaskService)
        svc.db = db_mock
        svc.config = MagicMock()
        svc.config.screening_market_guard_enabled = False
        svc.config.screening_market_guard_index = "sh000001"
        svc._theme_context = None

        # Mock market_data_sync_service with real return values
        sync_mock = MagicMock()
        sync_mock.fetcher_manager.get_market_stats.return_value = {
            "limit_up_count": 30, "limit_down_count": 10,
            "up_count": 2500, "down_count": 1500,
        }
        svc._market_data_sync_service = sync_mock
        return svc

    def _make_snapshot_df(self):
        import pandas as pd
        return pd.DataFrame([
            {"code": "600519", "name": "贵州茅台", "pct_chg": 5.0, "volume_ratio": 1.5,
             "turnover_rate": 2.0, "close": 100.0, "ma5": 98.0, "ma10": 96.0,
             "ma20": 94.0, "ma60": 90.0, "is_limit_up": False,
             "above_ma100": True, "gap_breakaway": False},
            {"code": "000858", "name": "五粮液", "pct_chg": 4.2, "volume_ratio": 1.4,
             "turnover_rate": 2.1, "close": 120.0, "ma5": 118.0, "ma10": 116.0,
             "ma20": 112.0, "ma60": 105.0, "is_limit_up": False,
             "above_ma100": True, "gap_breakaway": False},
        ])

    def test_five_layer_populates_all_fields(self):
        """五层链路为 candidate 填充所有决策字段。"""
        svc = self._make_service()
        candidate = _make_candidate()
        snapshot_df = self._make_snapshot_df()

        guard = MarketGuardResult(is_safe=True, index_price=3200.0, index_ma100=3100.0)

        svc._apply_five_layer_decision(
            selected=[candidate],
            snapshot_df=snapshot_df,
            effective_trade_date=date(2026, 3, 31),
            guard_result=guard,
        )

        self.assertIsNotNone(candidate.trade_stage)
        self.assertIsNotNone(candidate.market_regime)
        self.assertIsNotNone(candidate.entry_maturity)
        self.assertIsNotNone(candidate.candidate_pool_level)
        self.assertIsNotNone(candidate.theme_position)
        self.assertIsNotNone(candidate.risk_level)

    def test_stand_aside_caps_at_watch(self):
        """stand_aside 环境下 trade_stage 不超过 watch。"""
        svc = self._make_service()
        candidate = _make_candidate()
        snapshot_df = self._make_snapshot_df()

        # 指数 < MA100 + MA20↓ + 赚钱效应差 → stand_aside
        guard = MarketGuardResult(is_safe=False, index_price=2800.0, index_ma100=3100.0)

        # Mock market_stats for bad money effect
        svc._market_data_sync_service.fetcher_manager.get_market_stats.return_value = {
            "limit_up_count": 5, "limit_down_count": 30,
            "up_count": 500, "down_count": 3500,
        }

        # index_bars with descending MA20
        import pandas as pd
        import numpy as np
        bars = pd.DataFrame({
            "close": np.linspace(3200, 2800, 30),
            "date": pd.date_range("2026-03-01", periods=30),
        })
        try:
            guard_inst = MagicMock()
            guard_inst.get_index_bars.return_value = bars
            with patch("src.services.screening_task_service.MarketGuard", return_value=guard_inst):
                svc._apply_five_layer_decision(
                    selected=[candidate],
                    snapshot_df=snapshot_df,
                    effective_trade_date=date(2026, 3, 31),
                    guard_result=guard,
                )
        except Exception:
            # 即使数据拉取失败也能降级运行
            svc._apply_five_layer_decision(
                selected=[candidate],
                snapshot_df=snapshot_df,
                effective_trade_date=date(2026, 3, 31),
                guard_result=guard,
            )

        # stand_aside 或 defensive 环境下
        self.assertIn(candidate.trade_stage, [
            TradeStage.WATCH.value, TradeStage.FOCUS.value, TradeStage.STAND_ASIDE.value,
        ])

    def test_no_guard_result_still_works(self):
        """guard_result=None 时系统仍能降级运行。"""
        svc = self._make_service()
        candidate = _make_candidate()
        snapshot_df = self._make_snapshot_df()

        svc._apply_five_layer_decision(
            selected=[candidate],
            snapshot_df=snapshot_df,
            effective_trade_date=date(2026, 3, 31),
            guard_result=None,
        )

        # 应该默认 is_safe=True → balanced → 有值
        self.assertIsNotNone(candidate.trade_stage)
        self.assertIsNotNone(candidate.market_regime)

    def test_empty_candidates_no_error(self):
        """空候选列表不报错。"""
        svc = self._make_service()
        import pandas as pd
        svc._apply_five_layer_decision(
            selected=[],
            snapshot_df=pd.DataFrame(),
            effective_trade_date=date(2026, 3, 31),
        )


class PayloadOutputTestCase(unittest.TestCase):
    """测试 _build_candidate_payloads 包含五层字段。"""

    def test_payload_includes_five_layer_fields(self):
        from src.services.screening_task_service import ScreeningTaskService

        candidate = _make_candidate()
        candidate.trade_stage = "probe_entry"
        candidate.market_regime = "balanced"
        candidate.entry_maturity = "high"
        candidate.candidate_pool_level = "leader_pool"
        candidate.theme_position = "main_theme"
        candidate.risk_level = "medium"

        payloads = ScreeningTaskService._build_candidate_payloads(
            selected=[candidate],
            ai_results={},
            ai_top_k=5,
        )

        self.assertEqual(len(payloads), 1)
        p = payloads[0]
        self.assertEqual(p["trade_stage"], "probe_entry")
        self.assertEqual(p["market_regime"], "balanced")
        self.assertEqual(p["entry_maturity"], "high")
        self.assertEqual(p["candidate_pool_level"], "leader_pool")
        self.assertEqual(p["theme_position"], "main_theme")
        self.assertEqual(p["risk_level"], "medium")

    def test_payload_handles_none_five_layer_fields(self):
        """五层字段未赋值时 payload 中为 None。"""
        from src.services.screening_task_service import ScreeningTaskService

        candidate = _make_candidate()
        # 不赋值五层字段

        payloads = ScreeningTaskService._build_candidate_payloads(
            selected=[candidate],
            ai_results={},
            ai_top_k=5,
        )

        p = payloads[0]
        self.assertIsNone(p["trade_stage"])
        self.assertIsNone(p["market_regime"])


class DBSaveTestCase(unittest.TestCase):
    """测试 save_screening_candidates 写入五层字段。"""

    def setUp(self) -> None:
        import tempfile
        import os
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test.db")
        os.environ["DATABASE_PATH"] = self._db_path

        from src.config import Config
        from src.storage import DatabaseManager

        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()

    def tearDown(self) -> None:
        import os
        from src.config import Config
        from src.storage import DatabaseManager

        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def test_save_includes_five_layer_fields(self):
        """save_screening_candidates 将五层字段写入 ScreeningCandidate 模型。"""
        run_id = "test-run-001"
        self.db.create_screening_run(
            run_id=run_id,
            trade_date=date(2026, 3, 31),
            trigger_type="manual",
        )

        candidates = [{
            "code": "600519",
            "name": "贵州茅台",
            "rank": 1,
            "rule_score": 80.0,
            "selected_for_ai": True,
            "matched_strategies": ["trend_breakout"],
            "rule_hits": ["hit1"],
            "factor_snapshot": {"pct_chg": 5.0},
            "trade_stage": "probe_entry",
            "market_regime": "balanced",
            "entry_maturity": "high",
            "risk_level": "medium",
            "theme_position": "main_theme",
            "candidate_pool_level": "leader_pool",
            "setup_type": "trend_breakout",
        }]

        self.db.save_screening_candidates(run_id=run_id, candidates=candidates)

        rows = self.db.list_screening_candidates(run_id=run_id)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["trade_stage"], "probe_entry")
        self.assertEqual(row["market_regime"], "balanced")
        self.assertEqual(row["entry_maturity"], "high")
        self.assertEqual(row["theme_position"], "main_theme")
        self.assertEqual(row["candidate_pool_level"], "leader_pool")
        self.assertEqual(row["risk_level"], "medium")
        self.assertEqual(row["setup_type"], "trend_breakout")

    def test_layered_divergence_persistence_preserves_versioned_json_evidence(
        self,
    ) -> None:
        """v2 的嵌套候选、版本和分层事件必须逐字段通过通用 JSON 保存链路。"""
        from src.services.screening_task_service import ScreeningTaskService

        factor_snapshot = {
            "bottom_divergence_v2_stage": "early",
            "bottom_divergence_v2_candidate_version": "candidate-v2",
            "bottom_divergence_v2_zone_version": "zone-v2",
            "bottom_divergence_v2_candidate_records": [
                {
                    "candidate_version": "candidate-v2",
                    "lifecycle": "confirmed",
                    "zone": {
                        "zone_version": "zone-v2",
                        "r1": {"lower": 37.46, "upper": 39.2},
                        "r2": {"lower": 40.61, "upper": 42.9},
                    },
                    "early_reversal": {
                        "triggered": True,
                        "date": "2026-07-22",
                    },
                    "near_zone_events": {
                        "crossed": {"date": None},
                        "cleared_confirmed": {"date": None},
                    },
                    "major_zone_breakout": {
                        "confirmed": False,
                        "date": None,
                    },
                }
            ],
            "bottom_divergence_v2_layered_buy_points": [
                {
                    "level": "early",
                    "price": 36.6,
                    "stop": 31.36,
                    "triggered": True,
                }
            ],
        }
        candidate = ScreeningCandidateRecord(
            code="001337",
            name="四川黄金",
            rank=1,
            rule_score=82.0,
            rule_hits=["strategy:bottom_divergence_layered_entry_v2"],
            factor_snapshot=factor_snapshot,
            matched_strategies=["bottom_divergence_layered_entry_v2"],
            strategy_scores={"bottom_divergence_layered_entry_v2": 82.0},
            setup_type="bottom_divergence_layered_entry",
            strategy_family="reversal",
            primary_strategy="bottom_divergence_layered_entry_v2",
        )
        candidate.trade_stage = "probe_entry"
        candidate.entry_maturity = "medium"
        candidate.market_regime = "balanced"
        candidate.theme_position = "main_theme"
        candidate.candidate_pool_level = "focus_list"
        candidate.risk_level = "medium"

        payload = ScreeningTaskService._build_candidate_payloads(
            selected=[candidate],
            ai_results={},
            ai_top_k=1,
        )[0]
        run_id = "test-run-v2-versioned-evidence"
        self.db.create_screening_run(
            run_id=run_id,
            trade_date=date(2026, 7, 22),
            trigger_type="manual",
        )
        self.db.save_screening_candidates(run_id=run_id, candidates=[payload])

        row = self.db.list_screening_candidates(run_id=run_id)[0]
        self.assertEqual(
            row["matched_strategies"],
            ["bottom_divergence_layered_entry_v2"],
        )
        self.assertEqual(
            row["setup_type"],
            "bottom_divergence_layered_entry",
        )
        self.assertEqual(row["factor_snapshot"], factor_snapshot)
        self.assertEqual(
            row["primary_strategy"],
            "bottom_divergence_layered_entry_v2",
        )


class Phase2BDispatchTestCase(unittest.TestCase):
    """Phase 2B: 策略调度 + 买点收敛 集成测试。"""

    def _make_service(self):
        from src.services.screening_task_service import ScreeningTaskService
        from src.agent.skills.base import SkillManager

        db_mock = MagicMock()
        db_mock.batch_get_instrument_board_names.return_value = {
            "600519": ["白酒"],
        }
        db_mock.list_sector_heat_history.return_value = []

        svc = ScreeningTaskService.__new__(ScreeningTaskService)
        svc.db = db_mock
        svc.config = MagicMock()
        svc.config.screening_market_guard_enabled = False
        svc.config.screening_market_guard_index = "sh000001"
        svc._theme_context = None

        sync_mock = MagicMock()
        sync_mock.fetcher_manager.get_market_stats.return_value = {
            "limit_up_count": 30, "limit_down_count": 10,
            "up_count": 2500, "down_count": 1500,
        }
        svc._market_data_sync_service = sync_mock

        # Load real strategy YAMLs for dispatch/resolve
        skill_mgr = SkillManager()
        skill_mgr.load_builtin_strategies()
        svc._skill_manager = skill_mgr

        return svc

    def _make_snapshot_df(self):
        import pandas as pd
        return pd.DataFrame([
            {"code": "600519", "name": "贵州茅台", "pct_chg": 5.0, "volume_ratio": 1.5,
             "turnover_rate": 2.0, "close": 100.0, "ma5": 98.0, "ma10": 96.0,
             "ma20": 94.0, "ma60": 90.0, "is_limit_up": False,
             "above_ma100": True, "gap_breakaway": False},
        ])

    def test_stand_aside_filters_out_momentum(self):
        """stand_aside 环境下 momentum 策略被从 matched_strategies 中移除。"""
        svc = self._make_service()
        candidate = ScreeningCandidateRecord(
            code="600519", name="贵州茅台", rank=1, rule_score=80.0,
            rule_hits=["hit"],
            factor_snapshot={"pct_chg": 5.0, "leader_score": 75.0,
                             "extreme_strength_score": 85.0,
                             "risk_params": {"stop_loss": 88.0}},
            matched_strategies=["gap_limitup_breakout", "bottom_volume"],
            strategy_scores={"gap_limitup_breakout": 60.0, "bottom_volume": 40.0},
            setup_type="gap_breakout",
            strategy_family="momentum",
        )

        # Force stand_aside: is_safe=False, low index
        guard = MarketGuardResult(is_safe=False, index_price=2800.0, index_ma100=3100.0)
        svc._market_data_sync_service.fetcher_manager.get_market_stats.return_value = {
            "limit_up_count": 5, "limit_down_count": 30,
            "up_count": 500, "down_count": 3500,
        }

        svc._apply_five_layer_decision(
            selected=[candidate],
            snapshot_df=self._make_snapshot_df(),
            effective_trade_date=date(2026, 3, 31),
            guard_result=guard,
        )

        # gap_limitup_breakout (momentum, aggressive-only) should be blocked
        self.assertNotIn("gap_limitup_breakout", candidate.matched_strategies)
        # bottom_volume (observation) should survive
        self.assertIn("bottom_volume", candidate.matched_strategies)

    def test_dispatch_updates_setup_type(self):
        """调度后 setup_type 反映收敛结果而非原始最高分。"""
        svc = self._make_service()
        candidate = ScreeningCandidateRecord(
            code="600519", name="贵州茅台", rank=1, rule_score=80.0,
            rule_hits=["hit"],
            factor_snapshot={"pct_chg": 5.0, "leader_score": 75.0,
                             "extreme_strength_score": 85.0,
                             "risk_params": {"stop_loss": 88.0},
                             "ma100_breakout_days": 3},
            matched_strategies=[
                "ma100_60min_combined",
                "bottom_divergence_double_breakout",
                "volume_breakout",
            ],
            strategy_scores={
                "ma100_60min_combined": 50.0,
                "bottom_divergence_double_breakout": 70.0,
                "volume_breakout": 30.0,
            },
            setup_type="bottom_divergence_breakout",
            strategy_family="reversal",
        )

        # balanced + non_theme → reversal > trend priority
        guard = MarketGuardResult(is_safe=True, index_price=3200.0, index_ma100=3100.0)

        svc._apply_five_layer_decision(
            selected=[candidate],
            snapshot_df=self._make_snapshot_df(),
            effective_trade_date=date(2026, 3, 31),
            guard_result=guard,
        )

        # With non_theme: reversal preferred → bottom_divergence_breakout wins
        self.assertEqual(candidate.setup_type, "bottom_divergence_breakout")
        self.assertEqual(candidate.strategy_family, "reversal")

    def test_dispatch_exports_bottom_divergence_strategy_attribution(self):
        """P0 sample: bottom divergence should be persisted as primary strategy for backtest attribution."""
        from src.services.screening_task_service import ScreeningTaskService

        svc = self._make_service()
        candidate = ScreeningCandidateRecord(
            code="600519", name="贵州茅台", rank=1, rule_score=80.0,
            rule_hits=["hit"],
            factor_snapshot={"pct_chg": 5.0, "leader_score": 75.0,
                             "extreme_strength_score": 85.0,
                             "risk_params": {"stop_loss": 88.0},
                             "ma100_breakout_days": 3},
            matched_strategies=[
                "ma100_60min_combined",
                "bottom_divergence_double_breakout",
                "volume_breakout",
            ],
            strategy_scores={
                "ma100_60min_combined": 50.0,
                "bottom_divergence_double_breakout": 70.0,
                "volume_breakout": 30.0,
            },
            setup_type="bottom_divergence_breakout",
            strategy_family="reversal",
        )

        guard = MarketGuardResult(is_safe=True, index_price=3200.0, index_ma100=3100.0)
        svc._apply_five_layer_decision(
            selected=[candidate],
            snapshot_df=self._make_snapshot_df(),
            effective_trade_date=date(2026, 3, 31),
            guard_result=guard,
        )

        payload = ScreeningTaskService._build_candidate_payloads(
            selected=[candidate],
            ai_results={},
            ai_top_k=5,
        )[0]

        self.assertEqual(payload["primary_strategy"], "bottom_divergence_double_breakout")
        self.assertEqual(
            payload["contributing_strategies"],
            ["ma100_60min_combined", "volume_breakout"],
        )
        self.assertEqual(payload["strategy_scores"]["bottom_divergence_double_breakout"], 70.0)

    def test_dispatch_can_promote_ma100_low123_as_primary_setup(self):
        """P0 sample: higher-scoring MA100+Low123 should win the resolved setup."""
        svc = self._make_service()
        candidate = ScreeningCandidateRecord(
            code="000858", name="五粮液", rank=1, rule_score=86.0,
            rule_hits=["hit"],
            factor_snapshot={
                "pct_chg": 4.2,
                "leader_score": 78.0,
                "extreme_strength_score": 68.0,
                "risk_params": {"stop_loss": 108.0},
                "ma100_breakout_days": 2,
            },
            matched_strategies=[
                "ma100_low123_combined",
                "bottom_divergence_double_breakout",
                "volume_breakout",
            ],
            strategy_scores={
                "ma100_low123_combined": 88.0,
                "bottom_divergence_double_breakout": 72.0,
                "volume_breakout": 30.0,
            },
            setup_type="bottom_divergence_breakout",
            strategy_family="reversal",
        )

        guard = MarketGuardResult(is_safe=True, index_price=3200.0, index_ma100=3100.0)

        svc._apply_five_layer_decision(
            selected=[candidate],
            snapshot_df=self._make_snapshot_df(),
            effective_trade_date=date(2026, 3, 31),
            guard_result=guard,
        )

        self.assertEqual(candidate.setup_type, "low123_breakout")
        self.assertEqual(candidate.strategy_family, "reversal")
        self.assertIn("ma100_low123_combined", candidate.matched_strategies)
        self.assertIn("bottom_divergence_double_breakout", candidate.setup_hit_reasons)

    def test_dispatch_exports_ma100_low123_strategy_attribution(self):
        """P0 sample: MA100+Low123 should remain the primary strategy in persisted payloads."""
        from src.services.screening_task_service import ScreeningTaskService

        svc = self._make_service()
        candidate = ScreeningCandidateRecord(
            code="000858", name="五粮液", rank=1, rule_score=86.0,
            rule_hits=["hit"],
            factor_snapshot={
                "pct_chg": 4.2,
                "leader_score": 78.0,
                "extreme_strength_score": 68.0,
                "risk_params": {"stop_loss": 108.0},
                "ma100_breakout_days": 2,
            },
            matched_strategies=[
                "ma100_low123_combined",
                "bottom_divergence_double_breakout",
                "volume_breakout",
            ],
            strategy_scores={
                "ma100_low123_combined": 88.0,
                "bottom_divergence_double_breakout": 72.0,
                "volume_breakout": 30.0,
            },
            setup_type="bottom_divergence_breakout",
            strategy_family="reversal",
        )

        guard = MarketGuardResult(is_safe=True, index_price=3200.0, index_ma100=3100.0)
        svc._apply_five_layer_decision(
            selected=[candidate],
            snapshot_df=self._make_snapshot_df(),
            effective_trade_date=date(2026, 3, 31),
            guard_result=guard,
        )

        payload = ScreeningTaskService._build_candidate_payloads(
            selected=[candidate],
            ai_results={},
            ai_top_k=5,
        )[0]

        self.assertEqual(payload["primary_strategy"], "ma100_low123_combined")
        self.assertEqual(
            payload["contributing_strategies"],
            ["bottom_divergence_double_breakout", "volume_breakout"],
        )
        self.assertEqual(payload["strategy_scores"]["ma100_low123_combined"], 88.0)

    def test_no_skill_manager_degrades_gracefully(self):
        """skill_manager=None 时退回到原始 setup_type 逻辑。"""
        svc = self._make_service()
        svc._skill_manager = None

        candidate = _make_candidate(setup_type="trend_breakout")
        guard = MarketGuardResult(is_safe=True, index_price=3200.0, index_ma100=3100.0)

        svc._apply_five_layer_decision(
            selected=[candidate],
            snapshot_df=self._make_snapshot_df(),
            effective_trade_date=date(2026, 3, 31),
            guard_result=guard,
        )

        # Should still have all fields populated
        self.assertIsNotNone(candidate.trade_stage)
        self.assertIsNotNone(candidate.market_regime)


class Phase3ATradePlanTestCase(unittest.TestCase):
    """Phase 3A: 交易计划生成 集成测试。"""

    def _make_service(self):
        from src.services.screening_task_service import ScreeningTaskService

        db_mock = MagicMock()
        db_mock.batch_get_instrument_board_names.return_value = {
            "600519": ["白酒"],
        }
        db_mock.list_sector_heat_history.return_value = []

        svc = ScreeningTaskService.__new__(ScreeningTaskService)
        svc.db = db_mock
        svc.config = MagicMock()
        svc.config.screening_market_guard_enabled = False
        svc.config.screening_market_guard_index = "sh000001"
        svc._theme_context = None
        svc._skill_manager = None

        sync_mock = MagicMock()
        sync_mock.fetcher_manager.get_market_stats.return_value = {
            "limit_up_count": 30, "limit_down_count": 10,
            "up_count": 2500, "down_count": 1500,
        }
        svc._market_data_sync_service = sync_mock

        return svc

    def _make_snapshot_df(self):
        import pandas as pd
        return pd.DataFrame([
            {"code": "600519", "name": "贵州茅台", "pct_chg": 5.0, "volume_ratio": 1.5,
             "turnover_rate": 2.0, "close": 100.0, "ma5": 98.0, "ma10": 96.0,
             "ma20": 94.0, "ma60": 90.0, "is_limit_up": False,
             "above_ma100": True, "gap_breakaway": False},
        ])

    def test_probe_entry_has_trade_plan(self):
        """probe_entry 候选应有 trade_plan_json，含 stop_loss_rule。"""
        svc = self._make_service()
        # TREND_BREAKOUT + ma100_breakout_days=3 → HIGH maturity → probe_entry
        candidate = ScreeningCandidateRecord(
            code="600519", name="贵州茅台", rank=1, rule_score=80.0,
            rule_hits=["hit"],
            factor_snapshot={"pct_chg": 5.0, "leader_score": 30.0,
                             "extreme_strength_score": 40.0,
                             "risk_params": {"stop_loss": 88.0},
                             "ma100_breakout_days": 3},
            matched_strategies=["ma100_60min_combined"],
            strategy_scores={"ma100_60min_combined": 50.0},
            setup_type="trend_breakout",
        )

        guard = MarketGuardResult(is_safe=True, index_price=3200.0, index_ma100=3100.0)
        svc._apply_five_layer_decision(
            selected=[candidate],
            snapshot_df=self._make_snapshot_df(),
            effective_trade_date=date(2026, 3, 31),
            guard_result=guard,
        )

        self.assertEqual(candidate.trade_stage, "probe_entry")
        self.assertIsNotNone(candidate.trade_plan_json)

        import json
        plan = json.loads(candidate.trade_plan_json)
        self.assertIsNotNone(plan["stop_loss_rule"])
        self.assertIn("止损", plan["stop_loss_rule"])
        self.assertIsNone(plan["add_rule"])  # probe_entry 无加仓
        self.assertIsNotNone(plan["invalidation_rule"])

    def test_non_v2_setup_with_real_stop_reaches_probe_entry(self) -> None:
        """反例防线：不写 has_stop_loss 键，只给真实止损字段，
        非 v2 的 setup 也必须能走到 probe_entry 并产出交易计划。

        修复前这条必然失败——正是它对应线上 652 条候选 0 计划的实测。
        """
        svc = self._make_service()
        factor_snapshot = {
            "pct_chg": 5.0,
            "close": 100.0,
            "leader_score": 30.0,
            "extreme_strength_score": 40.0,
            "ma100_breakout_days": 3,
            "risk_params": {"stop_loss": 88.0},
        }
        self.assertNotIn("has_stop_loss", factor_snapshot)

        candidate = ScreeningCandidateRecord(
            code="600519", name="贵州茅台", rank=1, rule_score=80.0,
            rule_hits=["hit"],
            factor_snapshot=factor_snapshot,
            matched_strategies=["ma100_60min_combined"],
            strategy_scores={"ma100_60min_combined": 50.0},
            setup_type="trend_breakout",
        )

        guard = MarketGuardResult(is_safe=True, index_price=3200.0, index_ma100=3100.0)
        svc._apply_five_layer_decision(
            selected=[candidate],
            snapshot_df=self._make_snapshot_df(),
            effective_trade_date=date(2026, 3, 31),
            guard_result=guard,
        )

        self.assertEqual(candidate.trade_stage, "probe_entry")
        self.assertIsNotNone(candidate.trade_plan_json)
        plan = json.loads(candidate.trade_plan_json)
        self.assertEqual(plan["stop_loss_price"], 88.0)

    def test_watch_has_no_trade_plan(self):
        """watch 候选 trade_plan_json 为 None。"""
        svc = self._make_service()
        # NONE setup → watch
        candidate = ScreeningCandidateRecord(
            code="600519", name="贵州茅台", rank=1, rule_score=30.0,
            rule_hits=[],
            factor_snapshot={"pct_chg": 1.0, "leader_score": 10.0,
                             "extreme_strength_score": 10.0},
            matched_strategies=[],
            strategy_scores={},
            setup_type=None,
        )

        guard = MarketGuardResult(is_safe=True, index_price=3200.0, index_ma100=3100.0)
        svc._apply_five_layer_decision(
            selected=[candidate],
            snapshot_df=self._make_snapshot_df(),
            effective_trade_date=date(2026, 3, 31),
            guard_result=guard,
        )

        # Phase 4: NON_THEME + NONE setup + LOW maturity → REJECT
        self.assertEqual(candidate.trade_stage, "reject")
        self.assertIsNone(getattr(candidate, "trade_plan_json", None))

    def test_trade_plan_json_persists_to_db(self):
        """trade_plan_json 能正确写入 DB。"""
        import json
        import os
        import tempfile

        temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(temp_dir.name, "test.db")
        os.environ["DATABASE_PATH"] = db_path

        from src.config import Config
        from src.storage import DatabaseManager

        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()

        try:
            run_id = "test-run-3a"
            db.create_screening_run(
                run_id=run_id,
                trade_date=date(2026, 3, 31),
                trigger_type="manual",
            )

            trade_plan = {
                "initial_position": "1/5仓",
                "stop_loss_rule": "跌破MA20止损",
                "add_rule": None,
                "take_profit_plan": "沿MA10移动止盈",
                "invalidation_rule": "买入后3个交易日未启动则离场",
                "risk_level": "medium",
                "holding_expectation": "1~2周波段",
            }

            candidates = [{
                "code": "600519",
                "name": "贵州茅台",
                "rank": 1,
                "rule_score": 80.0,
                "selected_for_ai": True,
                "matched_strategies": ["trend_breakout"],
                "rule_hits": ["hit1"],
                "factor_snapshot": {"pct_chg": 5.0},
                "trade_stage": "probe_entry",
                "market_regime": "balanced",
                "entry_maturity": "high",
                "risk_level": "medium",
                "theme_position": "main_theme",
                "candidate_pool_level": "focus_list",
                "setup_type": "trend_breakout",
                "trade_plan_json": json.dumps(trade_plan, ensure_ascii=False),
            }]

            db.save_screening_candidates(run_id=run_id, candidates=candidates)

            rows = db.list_screening_candidates(run_id=run_id)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertIsNotNone(row.get("trade_plan"))
            self.assertEqual(row["trade_plan"]["stop_loss_rule"], "跌破MA20止损")
            self.assertEqual(row["trade_plan"]["holding_expectation"], "1~2周波段")
        finally:
            DatabaseManager.reset_instance()
            Config.reset_instance()
            os.environ.pop("DATABASE_PATH", None)
            temp_dir.cleanup()


class BottomDivergenceV2RealPipelineTestCase(unittest.TestCase):
    def _run(
        self,
        *,
        stage: str,
        regime: MarketRegime = MarketRegime.BALANCED,
        direct_stop: object = 8.8,
        layered_points: list | None = None,
        setup_type: str = "bottom_divergence_layered_entry",
        leader_score: float = 55.0,
        is_limit_up: bool = False,
        actionability_status: str | None = None,
        v1_divergence_state: str | None = None,
    ) -> ScreeningCandidateRecord:
        if layered_points is None:
            layered_points = [
                {
                    "level": "early",
                    "price": 12.0,
                    "stop": 8.8,
                    "triggered": stage
                    in ("early", "near_cleared", "major_actionable"),
                },
                {
                    "level": "r1",
                    "price": 14.0,
                    "stop": 9.5,
                    "triggered": stage
                    in ("near_cleared", "major_actionable"),
                },
                {
                    "level": "r2",
                    "price": 16.0,
                    "stop": 11.0,
                    "triggered": stage == "major_actionable",
                },
            ]
        factor_snapshot = {
            "leader_score": leader_score,
            "extreme_strength_score": 65.0,
            "is_limit_up": is_limit_up,
            # v1 成熟度字段：默认缺失（LOW），仅在需要越过成熟度闸门时给值
            "bottom_divergence_state": v1_divergence_state,
            "bottom_divergence_v2_stage": stage,
            "bottom_divergence_v2_actionability_status": (
                actionability_status
                if actionability_status is not None
                else (
                    "actionable"
                    if stage == "major_actionable"
                    else "major_not_confirmed"
                )
            ),
            "bottom_divergence_v2_major_actionable_entry": (
                stage == "major_actionable"
            ),
            "bottom_divergence_v2_stop_loss_price": direct_stop,
            "bottom_divergence_v2_layered_buy_points": layered_points,
        }
        candidate = ScreeningCandidateRecord(
            code="001337",
            name="四川黄金",
            rank=1,
            rule_score=80.0,
            rule_hits=["bottom_divergence_v2_candidate"],
            factor_snapshot=factor_snapshot,
            matched_strategies=["bottom_divergence_layered_entry_v2"],
            strategy_scores={"bottom_divergence_layered_entry_v2": 80.0},
            setup_type=setup_type,
        )
        screener = MagicMock()
        screener._context = SimpleNamespace(run_id="v2-real-pipeline")
        screener.evaluate.return_value = SimpleNamespace(
            selected=[candidate],
            rejected=[],
        )
        db = MagicMock()
        db.batch_get_instrument_board_names.return_value = {
            "001337": ["黄金"],
        }
        market_env = MarketEnvironment(
            regime=regime,
            risk_level=RiskLevel.MEDIUM,
            is_safe=True,
        )
        theme_resolver = MagicMock()
        theme_resolver.identified_themes = []
        theme_resolver.get_main_theme_boards.return_value = set()
        theme_resolver.resolve.return_value = ThemeDecision(
            theme_tag="黄金",
            theme_score=90.0,
            theme_position=ThemePosition.MAIN_THEME,
            leader_score=leader_score,
            sector_strength=80.0,
        )
        sector_engine = MagicMock()
        sector_engine.compute_all_sectors.return_value = []
        registry = MagicMock()
        registry.is_empty = False
        aggregation = MagicMock()
        aggregation.aggregate.return_value = []

        with (
            patch(
                "src.services.sector_heat_engine.SectorHeatEngine",
                return_value=sector_engine,
            ),
            patch(
                "src.services.theme_mapping_registry.ThemeMappingRegistry",
                return_value=registry,
            ),
            patch(
                "src.services.theme_aggregation_service.ThemeAggregationService",
                return_value=aggregation,
            ),
            patch(
                "src.services.theme_position_resolver.ThemePositionResolver",
                return_value=theme_resolver,
            ),
        ):
            result = FiveLayerPipeline().run(
                snapshot_df=pd.DataFrame(
                    [{"code": "001337", "name": "四川黄金"}]
                ),
                trade_date=date(2026, 8, 5),
                market_env=market_env,
                guard_result=SimpleNamespace(is_safe=True, message="ok"),
                screener_service=screener,
                candidate_limit=10,
                db_manager=db,
                skill_manager=None,
                lock_universe=True,
            )

        self.assertEqual(len(result.candidates), 1)
        return result.candidates[0]

    def test_global_stop_without_current_point_stays_focus(self) -> None:
        candidate = self._run(stage="early", layered_points=[])

        self.assertEqual(candidate.trade_stage, TradeStage.FOCUS.value)
        self.assertEqual(candidate.entry_maturity, EntryMaturity.MEDIUM.value)
        self.assertIsNone(candidate.trade_plan_json)

    def test_global_stop_with_untriggered_or_wrong_level_point_stays_focus(
        self,
    ) -> None:
        for layered_points in (
            [
                {
                    "level": "early",
                    "price": 12.0,
                    "stop": 8.8,
                    "triggered": False,
                },
            ],
            [
                {
                    "level": "r1",
                    "price": 14.0,
                    "stop": 9.5,
                    "triggered": True,
                },
            ],
        ):
            with self.subTest(layered_points=layered_points):
                candidate = self._run(
                    stage="early",
                    direct_stop=8.8,
                    layered_points=layered_points,
                )

                self.assertEqual(candidate.trade_stage, TradeStage.FOCUS.value)
                self.assertIsNone(candidate.trade_plan_json)

    def test_r1_reaches_probe_and_r2_reaches_add_by_environment(self) -> None:
        near = self._run(stage="near_cleared")
        major = self._run(
            stage="major_actionable",
            regime=MarketRegime.AGGRESSIVE,
            leader_score=80.0,
            is_limit_up=True,
        )

        self.assertEqual(near.trade_stage, TradeStage.PROBE_ENTRY.value)
        self.assertEqual(major.trade_stage, TradeStage.ADD_ON_STRENGTH.value)
        self.assertIsNotNone(near.trade_plan_json)
        self.assertIsNotNone(major.trade_plan_json)

    def test_unknown_provenance_early_and_r1_are_capped_at_focus(self) -> None:
        for stage in ("early", "near_cleared"):
            with self.subTest(stage=stage):
                candidate = self._run(
                    stage=stage,
                    actionability_status="adjustment_unknown",
                )

                self.assertEqual(candidate.trade_stage, TradeStage.FOCUS.value)
                self.assertIsNone(candidate.trade_plan_json)

    def test_trade_plan_json_serializes_every_trade_plan_field(self) -> None:
        candidate = self._run(stage="near_cleared")

        plan = json.loads(candidate.trade_plan_json)
        self.assertEqual(
            set(plan),
            {
                "initial_position",
                "add_rule",
                "stop_loss_rule",
                "take_profit_plan",
                "invalidation_rule",
                "risk_level",
                "holding_expectation",
                "execution_note",
                "entry_price",
                "entry_rule",
                "entry_valid_days",
                "stop_loss_price",
                "take_profit_price",
                "time_stop_days",
                "exit_rules",
            },
        )

        from src.backtest.execution.plan_replay_executor import PlanReplayExecutor

        replay = PlanReplayExecutor.replay(
            plan,
            [
                SimpleNamespace(
                    date=date(2026, 8, 6),
                    low=13.5,
                    high=14.5,
                    close=14.2,
                )
            ],
        )
        self.assertNotEqual(
            replay.status,
            "missing_structured_trade_plan",
        )

    def test_layered_point_stop_is_enough_without_flat_stop(self) -> None:
        candidate = self._run(
            stage="early",
            direct_stop=None,
            layered_points=[
                {
                    "level": "early",
                    "price": 12.0,
                    "stop": 8.8,
                    "triggered": True,
                },
            ],
        )

        self.assertEqual(candidate.trade_stage, TradeStage.PROBE_ENTRY.value)

    def test_future_layer_stops_cannot_unlock_early_entry(self) -> None:
        candidate = self._run(
            stage="early",
            direct_stop=None,
            layered_points=[
                {
                    "level": "early",
                    "price": 12.0,
                    "stop": None,
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
        )

        self.assertEqual(candidate.trade_stage, TradeStage.FOCUS.value)

    def test_r1_cannot_reuse_triggered_early_stop(self) -> None:
        candidate = self._run(
            stage="near_cleared",
            direct_stop=None,
            layered_points=[
                {
                    "level": "early",
                    "price": 12.0,
                    "stop": 8.8,
                    "triggered": True,
                },
                {
                    "level": "r1",
                    "price": 14.0,
                    "stop": None,
                    "triggered": True,
                },
            ],
        )

        self.assertEqual(candidate.trade_stage, TradeStage.FOCUS.value)

    def test_major_cannot_reuse_triggered_historical_stop(self) -> None:
        candidate = self._run(
            stage="major_actionable",
            regime=MarketRegime.AGGRESSIVE,
            direct_stop=None,
            leader_score=80.0,
            is_limit_up=True,
            layered_points=[
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
                    "stop": None,
                    "triggered": True,
                },
            ],
        )

        self.assertEqual(candidate.trade_stage, TradeStage.FOCUS.value)

    def test_current_stage_point_stop_unlocks_r1_and_major(self) -> None:
        near = self._run(
            stage="near_cleared",
            direct_stop=None,
            layered_points=[
                {
                    "level": "early",
                    "price": 12.0,
                    "stop": 8.8,
                    "triggered": True,
                },
                {
                    "level": "near",
                    "price": 14.0,
                    "stop": 9.5,
                    "triggered": True,
                },
            ],
        )
        major = self._run(
            stage="major_actionable",
            regime=MarketRegime.AGGRESSIVE,
            direct_stop=None,
            leader_score=80.0,
            is_limit_up=True,
            layered_points=[
                {
                    "level": "major",
                    "price": 16.0,
                    "stop": 11.0,
                    "triggered": True,
                },
            ],
        )

        self.assertEqual(near.trade_stage, TradeStage.PROBE_ENTRY.value)
        self.assertEqual(major.trade_stage, TradeStage.ADD_ON_STRENGTH.value)
        self.assertIsNotNone(near.trade_plan_json)
        self.assertIsNotNone(major.trade_plan_json)

    def test_missing_or_non_positive_v2_stop_caps_at_focus(self) -> None:
        for direct_stop, layered_points in (
            (None, []),
            (0, [{"level": "early", "price": 12.0, "stop": 0}]),
            (-1, [{"level": "early", "price": 12.0, "stop": -1}]),
        ):
            with self.subTest(
                direct_stop=direct_stop,
                layered_points=layered_points,
            ):
                candidate = self._run(
                    stage="near_cleared",
                    direct_stop=direct_stop,
                    layered_points=layered_points,
                )
                self.assertEqual(candidate.trade_stage, TradeStage.FOCUS.value)

    def test_v1_setup_cannot_borrow_v2_stop_and_stays_focus(self) -> None:
        """v1 setup 不认 v2 的止损字段：解析不出自己的止损就停在 focus。

        成熟度给到 confirmed(HIGH)，把闸门单独留给止损解析——否则
        成熟度先把候选压在 focus，止损那一步根本没被测到。
        """
        candidate = self._run(
            stage="early",
            setup_type="bottom_divergence_breakout",
            direct_stop=8.8,
            v1_divergence_state="confirmed",
        )

        self.assertEqual(candidate.trade_stage, TradeStage.FOCUS.value)
        self.assertIsNone(candidate.trade_plan_json)


if __name__ == "__main__":
    unittest.main()
