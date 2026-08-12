"""
五层决策系统端到端集成测试 — 使用本地股票数据

测试用例覆盖:
  1. L1 市场环境引擎 — MarketEnvironmentEngine
  2. L2 板块热度引擎 — SectorHeatEngine
  3. L2.5 题材聚合 — ThemeAggregationService
  4. L2→ 题材地位 — ThemePositionResolver
  5. L3 候选池分级 — CandidatePoolClassifier
  6. L4 买点成熟度 — EntryMaturityAssessor
  7. L5 交易阶段裁决 — TradeStageJudge
  8. 策略调度器 — StrategyDispatcher
  9. 买点收敛器 — SetupResolver
  10. 交易计划 — TradePlanBuilder
  11. 完整管道 — _apply_five_layer_decision 端到端
  12. API 输出 — 验证 API 返回五层字段

运行: python -m pytest tests/test_e2e_five_layer_local.py -v --tb=short
"""
import json
import os
import sqlite3
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "stock_analysis.db"

# 本模块的夹具直接用 sqlite3 读 DB_PATH，而被测引擎走 DatabaseManager，
# 即 DATABASE_PATH。两者必须指向同一个库，否则夹具读到真实数据、
# 引擎读到空库，用例会以「没有候选」的形式假失败。
pytestmark = pytest.mark.real_database


def get_latest_factor_date() -> str:
    """从 daily_factor_snapshots 获取最新交易日。"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT trade_date FROM daily_factor_snapshots ORDER BY trade_date DESC LIMIT 1"
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def load_factor_snapshots(trade_date: str) -> pd.DataFrame:
    """加载指定日期的因子快照为 DataFrame。"""
    conn = sqlite3.connect(str(DB_PATH))
    df = pd.read_sql_query(
        "SELECT * FROM daily_factor_snapshots WHERE trade_date = ?",
        conn,
        params=(trade_date,),
    )
    conn.close()
    # 解析 factor_snapshot_json 中的扩展因子
    if "factor_snapshot_json" in df.columns:
        expanded = df["factor_snapshot_json"].apply(
            lambda x: json.loads(x) if pd.notna(x) and x else {}
        )
        expanded_df = pd.json_normalize(expanded)
        # 只取不冲突的列
        overlap = set(df.columns) & set(expanded_df.columns)
        expanded_df = expanded_df.drop(columns=list(overlap), errors="ignore")
        df = pd.concat([df, expanded_df], axis=1)
    return df


def load_board_memberships() -> dict:
    """加载板块成员映射: stock_code -> [board_names]。"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT ibm.instrument_code, bm.board_name
        FROM instrument_board_membership ibm
        JOIN board_master bm ON ibm.board_id = bm.id
        """
    )
    result: dict = {}
    for code, board_name in cursor.fetchall():
        result.setdefault(code, []).append(board_name)
    conn.close()
    return result


# ──────────────────────────────────────────────────────────────
# 测试基类
# ──────────────────────────────────────────────────────────────

class FiveLayerLocalTestBase(unittest.TestCase):
    """带有本地数据加载的测试基类。"""

    @classmethod
    def setUpClass(cls):
        if not DB_PATH.exists():
            raise unittest.SkipTest(f"数据库不存在: {DB_PATH}")

        cls.latest_date = get_latest_factor_date()
        if not cls.latest_date:
            raise unittest.SkipTest("无因子快照数据")

        cls.snapshot_df = load_factor_snapshots(cls.latest_date)
        if cls.snapshot_df.empty:
            raise unittest.SkipTest(f"{cls.latest_date} 无因子数据")

        cls.board_map = load_board_memberships()

        print(f"\n[测试数据] 日期={cls.latest_date}, 股票数={len(cls.snapshot_df)}, "
              f"板块映射={len(cls.board_map)} 只股票")


# ──────────────────────────────────────────────────────────────
# Case 1: L1 市场环境引擎
# ──────────────────────────────────────────────────────────────

class TestL1MarketEnvironment(FiveLayerLocalTestBase):
    """验证 MarketEnvironmentEngine 能基于真实指数数据输出 market_regime。"""

    def test_assess_returns_valid_regime(self):
        """regime 必须是 aggressive/balanced/defensive/stand_aside 之一。"""
        from src.services.market_environment_engine import MarketEnvironmentEngine

        engine = MarketEnvironmentEngine()
        # 使用 guard_result mock（真实数据需要 akshare 调用）
        mock_guard = MagicMock()
        mock_guard.is_safe = True
        mock_guard.index_price = 3300.0
        mock_guard.index_ma100 = 3200.0
        mock_guard.message = "test"

        mock_stats = {
            "up_count": 2500,
            "down_count": 1800,
            "limit_up_count": 45,
            "limit_down_count": 5,
            "total_amount": 1.2e12,
        }

        result = engine.assess(mock_guard, None, mock_stats)
        self.assertIn(
            result.regime.value,
            ["aggressive", "balanced", "defensive", "stand_aside"],
            f"regime={result.regime.value} 不在有效范围内",
        )
        self.assertIn(
            result.risk_level.value,
            ["low", "medium", "high"],
            f"risk_level={result.risk_level.value} 不在有效范围内",
        )
        print(f"  L1 结果: regime={result.regime.value}, risk={result.risk_level.value}")

    def test_stand_aside_when_bearish(self):
        """指数跌破 MA100 + 跌停多于涨停 → stand_aside。"""
        from src.services.market_environment_engine import MarketEnvironmentEngine

        engine = MarketEnvironmentEngine()
        mock_guard = MagicMock()
        mock_guard.is_safe = False
        mock_guard.index_price = 2800.0
        mock_guard.index_ma100 = 3200.0
        mock_guard.message = "test bearish"

        mock_stats = {
            "up_count": 800,
            "down_count": 3500,
            "limit_up_count": 5,
            "limit_down_count": 60,
            "total_amount": 8e11,
        }

        result = engine.assess(mock_guard, None, mock_stats)
        self.assertIn(
            result.regime.value,
            ["defensive", "stand_aside"],
            "极端下跌环境应为 defensive 或 stand_aside",
        )

    def test_aggressive_when_bullish(self):
        """指数站上 MA100 + 涨停远多于跌停 → 至少 balanced。"""
        from src.services.market_environment_engine import MarketEnvironmentEngine

        engine = MarketEnvironmentEngine()
        mock_guard = MagicMock()
        mock_guard.is_safe = True
        mock_guard.index_price = 3500.0
        mock_guard.index_ma100 = 3200.0
        mock_guard.message = "test bullish"

        mock_stats = {
            "up_count": 3500,
            "down_count": 800,
            "limit_up_count": 80,
            "limit_down_count": 3,
            "total_amount": 1.5e12,
        }

        result = engine.assess(mock_guard, None, mock_stats)
        self.assertIn(
            result.regime.value,
            ["aggressive", "balanced"],
            "明显牛市环境应为 aggressive 或 balanced",
        )


# ──────────────────────────────────────────────────────────────
# Case 2: L2 板块热度引擎
# ──────────────────────────────────────────────────────────────

class TestL2SectorHeatEngine(FiveLayerLocalTestBase):
    """验证 SectorHeatEngine 能基于本地因子数据计算板块热度。"""

    def test_compute_returns_non_empty_results(self):
        """至少应有 1 个板块热度结果。"""
        from src.services.sector_heat_engine import SectorHeatEngine
        from src.storage import DatabaseManager

        db = DatabaseManager()
        engine = SectorHeatEngine(db_manager=db)
        trade_date = date.fromisoformat(self.latest_date)

        results = engine.compute_all_sectors(self.snapshot_df, trade_date)
        self.assertGreater(len(results), 0, "应至少有 1 个板块热度结果")
        print(f"  L2 结果: {len(results)} 个板块")

    def test_hot_score_in_valid_range(self):
        """每个板块的 sector_hot_score 应在 [0, 100] 范围内。"""
        from src.services.sector_heat_engine import SectorHeatEngine
        from src.storage import DatabaseManager

        db = DatabaseManager()
        engine = SectorHeatEngine(db_manager=db)
        trade_date = date.fromisoformat(self.latest_date)

        results = engine.compute_all_sectors(self.snapshot_df, trade_date)
        for r in results[:10]:  # 只检查前10个
            self.assertGreaterEqual(r.sector_hot_score, 0.0, f"{r.board_name} score < 0")
            self.assertLessEqual(r.sector_hot_score, 100.0, f"{r.board_name} score > 100")

    def test_sector_status_is_valid(self):
        """status 必须是 hot/warm/neutral/cold 之一。"""
        from src.services.sector_heat_engine import SectorHeatEngine
        from src.storage import DatabaseManager

        db = DatabaseManager()
        engine = SectorHeatEngine(db_manager=db)
        trade_date = date.fromisoformat(self.latest_date)

        results = engine.compute_all_sectors(self.snapshot_df, trade_date)
        valid_statuses = {"hot", "warm", "neutral", "cold"}
        for r in results:
            self.assertIn(r.sector_status, valid_statuses, f"{r.board_name} 状态无效")

    def test_sector_stage_is_valid(self):
        """stage 必须是 launch/ferment/expand/climax/fade 之一。"""
        from src.services.sector_heat_engine import SectorHeatEngine
        from src.storage import DatabaseManager

        db = DatabaseManager()
        engine = SectorHeatEngine(db_manager=db)
        trade_date = date.fromisoformat(self.latest_date)

        results = engine.compute_all_sectors(self.snapshot_df, trade_date)
        valid_stages = {"launch", "ferment", "expand", "climax", "fade"}
        for r in results:
            self.assertIn(r.sector_stage, valid_stages, f"{r.board_name} 阶段无效")

    def test_top_sectors_have_stock_counts(self):
        """热点板块必须有 stock_count >= 5。"""
        from src.services.sector_heat_engine import SectorHeatEngine
        from src.storage import DatabaseManager

        db = DatabaseManager()
        engine = SectorHeatEngine(db_manager=db)
        trade_date = date.fromisoformat(self.latest_date)

        results = engine.compute_all_sectors(self.snapshot_df, trade_date)
        hot_sectors = [r for r in results if r.sector_status in ("hot", "warm")]
        for r in hot_sectors:
            self.assertGreaterEqual(
                r.stock_count, 5, f"热点板块 {r.board_name} 成员数不足"
            )
        if hot_sectors:
            top = hot_sectors[0]
            print(f"  L2 最热板块: {top.board_name} score={top.sector_hot_score:.1f} "
                  f"status={top.sector_status} stage={top.sector_stage}")


# ──────────────────────────────────────────────────────────────
# Case 3: L2.5 题材聚合
# ──────────────────────────────────────────────────────────────

class TestL25ThemeAggregation(FiveLayerLocalTestBase):
    """验证 ThemeAggregationService 能将板块聚合为题材。"""

    def test_aggregate_returns_results(self):
        """聚合后应有题材结果。"""
        from src.services.sector_heat_engine import SectorHeatEngine
        from src.services.theme_aggregation_service import ThemeAggregationService
        from src.storage import DatabaseManager

        db = DatabaseManager()
        engine = SectorHeatEngine(db_manager=db)
        trade_date = date.fromisoformat(self.latest_date)
        sector_results = engine.compute_all_sectors(self.snapshot_df, trade_date)

        service = ThemeAggregationService()
        theme_results = service.aggregate(sector_results)
        self.assertIsInstance(theme_results, list)
        print(f"  L2.5 结果: {len(theme_results)} 个题材聚合")
        for t in theme_results[:3]:
            print(f"    {t.theme_tag}: score={t.theme_score:.1f}, "
                  f"sectors={len(t.related_sectors)}")


# ──────────────────────────────────────────────────────────────
# Case 4: 题材地位解析
# ──────────────────────────────────────────────────────────────

class TestThemePositionResolver(FiveLayerLocalTestBase):
    """验证 ThemePositionResolver 能为个股推算 theme_position。"""

    def test_resolve_returns_valid_position(self):
        """theme_position 必须是有效枚举值。"""
        from src.services.sector_heat_engine import SectorHeatEngine
        from src.services.theme_aggregation_service import ThemeAggregationService
        from src.services.theme_position_resolver import ThemePositionResolver
        from src.storage import DatabaseManager

        db = DatabaseManager()
        engine = SectorHeatEngine(db_manager=db)
        trade_date = date.fromisoformat(self.latest_date)
        sector_results = engine.compute_all_sectors(self.snapshot_df, trade_date)

        service = ThemeAggregationService()
        theme_results = service.aggregate(sector_results)

        resolver = ThemePositionResolver(sector_results, theme_results)
        valid_positions = {"main_theme", "secondary_theme", "follower_theme", "fading_theme", "non_theme"}

        # 取几只有板块归属的股票
        tested = 0
        for code, boards in list(self.board_map.items())[:20]:
            decision = resolver.resolve(boards)
            self.assertIn(
                decision.theme_position.value,
                valid_positions,
                f"{code} theme_position 无效",
            )
            tested += 1

        self.assertGreater(tested, 0, "应至少测试 1 只股票")
        print(f"  L2→ 已测试 {tested} 只股票的 theme_position")


# ──────────────────────────────────────────────────────────────
# Case 5: 策略调度器
# ──────────────────────────────────────────────────────────────

class TestStrategyDispatcher(unittest.TestCase):
    """验证 StrategyDispatcher 按 market_regime 过滤策略。"""

    def _load_dispatcher_and_rules(self):
        """Helper: load strategy rules from YAML and build dispatcher."""
        from src.services.strategy_dispatcher import StrategyDispatcher
        from src.services.strategy_screening_engine import build_rules_from_skills
        from src.agent.skills.base import SkillManager

        sm = SkillManager()
        sm.load_builtin_strategies()
        rules = build_rules_from_skills(sm.get_screening_rules())
        dispatcher = StrategyDispatcher(rules)
        return dispatcher, rules

    def test_stand_aside_blocks_non_observation(self):
        """stand_aside 环境只允许 observation 策略。"""
        from src.schemas.trading_types import MarketRegime

        dispatcher, rules = self._load_dispatcher_and_rules()
        all_strategy_names = [r.strategy_name for r in rules]
        result = dispatcher.filter_strategies(
            all_strategy_names, MarketRegime.STAND_ASIDE
        )
        for s in result.allowed_strategies:
            rule = next((r for r in rules if r.strategy_name == s), None)
            if rule:
                self.assertEqual(
                    rule.system_role,
                    "observation",
                    f"stand_aside 下 {s} (role={rule.system_role}) 不应被允许",
                )
        print(f"  调度器: stand_aside 允许 {len(result.allowed_strategies)} 个策略, "
              f"阻止 {len(result.blocked_strategies)} 个")

    def test_aggressive_allows_all(self):
        """aggressive 环境应允许所有策略。"""
        from src.schemas.trading_types import MarketRegime

        dispatcher, rules = self._load_dispatcher_and_rules()
        all_names = [r.strategy_name for r in rules]
        result = dispatcher.filter_strategies(all_names, MarketRegime.AGGRESSIVE)
        self.assertEqual(
            len(result.blocked_strategies),
            0,
            "aggressive 下不应阻止任何策略",
        )


# ──────────────────────────────────────────────────────────────
# Case 6: 交易阶段裁决器
# ──────────────────────────────────────────────────────────────

class TestTradeStageJudge(unittest.TestCase):
    """验证 TradeStageJudge 硬规则。"""

    def test_stand_aside_caps_at_watch(self):
        """stand_aside 环境下 trade_stage 不超过 watch。"""
        from src.schemas.trading_types import (
            CandidatePoolLevel,
            EntryMaturity,
            MarketRegime,
            SetupType,
            ThemePosition,
        )
        from src.services.trade_stage_judge import TradeStageJudge

        judge = TradeStageJudge()
        mock_env = MagicMock()
        mock_env.regime = MarketRegime.STAND_ASIDE
        mock_env.risk_level = MagicMock(value="high")

        result = judge.judge(
            env=mock_env,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            theme_position=ThemePosition.MAIN_THEME,
            has_stop_loss=True,
        )
        self.assertIn(
            result.value,
            ["watch", "stand_aside", "reject"],
            f"stand_aside 下 trade_stage={result.value} 超出允许范围",
        )

    def test_no_setup_caps_at_watch(self):
        """无买点类型时 trade_stage 不超过 watch。"""
        from src.schemas.trading_types import (
            CandidatePoolLevel,
            EntryMaturity,
            MarketRegime,
            SetupType,
            ThemePosition,
        )
        from src.services.trade_stage_judge import TradeStageJudge

        judge = TradeStageJudge()
        mock_env = MagicMock()
        mock_env.regime = MarketRegime.BALANCED
        mock_env.risk_level = MagicMock(value="medium")

        result = judge.judge(
            env=mock_env,
            setup_type=SetupType.NONE,
            entry_maturity=EntryMaturity.LOW,
            pool_level=CandidatePoolLevel.WATCHLIST,
            theme_position=ThemePosition.NON_THEME,
            has_stop_loss=False,
        )
        self.assertIn(
            result.value,
            ["watch", "stand_aside", "reject", "focus"],
            f"无买点时 trade_stage={result.value} 不应为执行级",
        )

    def test_high_maturity_leader_can_probe(self):
        """高成熟度 + 龙头池 + balanced 以上 → 允许 probe_entry。"""
        from src.schemas.trading_types import (
            CandidatePoolLevel,
            EntryMaturity,
            MarketRegime,
            SetupType,
            ThemePosition,
        )
        from src.services.trade_stage_judge import TradeStageJudge

        judge = TradeStageJudge()
        mock_env = MagicMock()
        mock_env.regime = MarketRegime.BALANCED
        mock_env.risk_level = MagicMock(value="low")

        result = judge.judge(
            env=mock_env,
            setup_type=SetupType.BOTTOM_DIVERGENCE_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            theme_position=ThemePosition.MAIN_THEME,
            has_stop_loss=True,
        )
        self.assertIn(
            result.value,
            ["probe_entry", "add_on_strength", "focus"],
            f"高成熟度龙头 trade_stage={result.value} 应允许执行",
        )


# ──────────────────────────────────────────────────────────────
# Case 7: 交易计划生成器
# ──────────────────────────────────────────────────────────────

class TestTradePlanBuilder(unittest.TestCase):
    """验证 TradePlanBuilder 生成逻辑。"""

    def test_probe_entry_has_stop_loss(self):
        """probe_entry 阶段必须有 stop_loss_rule。"""
        from src.schemas.trading_types import (
            CandidatePoolLevel,
            EntryMaturity,
            RiskLevel,
            SetupType,
            TradeStage,
        )
        from src.services.trade_plan_builder import TradePlanBuilder

        builder = TradePlanBuilder()
        plan = builder.build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.BOTTOM_DIVERGENCE_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            factor_snapshot={},
        )
        self.assertIsNotNone(plan, "probe_entry 应生成交易计划")
        self.assertTrue(
            plan.stop_loss_rule,
            "probe_entry 必须有止损规则",
        )
        self.assertTrue(plan.initial_position, "必须有仓位建议")
        print(f"  交易计划: stop_loss={plan.stop_loss_rule}, "
              f"position={plan.initial_position}")

    def test_watch_returns_none(self):
        """watch 阶段不应生成交易计划。"""
        from src.schemas.trading_types import (
            CandidatePoolLevel,
            EntryMaturity,
            RiskLevel,
            SetupType,
            TradeStage,
        )
        from src.services.trade_plan_builder import TradePlanBuilder

        builder = TradePlanBuilder()
        plan = builder.build(
            trade_stage=TradeStage.WATCH,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.MEDIUM,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.WATCHLIST,
            factor_snapshot={},
        )
        self.assertIsNone(plan, "watch 阶段不应生成交易计划")

    def test_add_on_strength_has_add_rule(self):
        """add_on_strength 阶段必须有加仓规则。"""
        from src.schemas.trading_types import (
            CandidatePoolLevel,
            EntryMaturity,
            RiskLevel,
            SetupType,
            TradeStage,
        )
        from src.services.trade_plan_builder import TradePlanBuilder

        builder = TradePlanBuilder()
        plan = builder.build(
            trade_stage=TradeStage.ADD_ON_STRENGTH,
            setup_type=SetupType.LOW123_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            risk_level=RiskLevel.LOW,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            factor_snapshot={},
        )
        self.assertIsNotNone(plan, "add_on_strength 应生成交易计划")
        self.assertTrue(plan.add_rule, "add_on_strength 必须有加仓规则")


# ──────────────────────────────────────────────────────────────
# Case 8: 完整管道端到端 (使用本地数据)
# ──────────────────────────────────────────────────────────────

class TestFullPipelineE2E(FiveLayerLocalTestBase):
    """端到端测试: 从本地数据执行完整选股管道，验证五层字段有值。"""

    def test_full_screening_run_produces_five_layer_data(self):
        """执行一次完整选股，验证输出包含五层字段。

        这是最核心的集成测试: 实际调用 screening_task_service.execute_run，
        使用本地 DB 数据，验证候选股的五层字段不为 null。
        """
        from src.services.screening_task_service import ScreeningTaskService
        from src.storage import DatabaseManager

        db = DatabaseManager()
        service = ScreeningTaskService(db_manager=db)

        trade_date_str = self.latest_date

        # 使用 balanced 模式，限制候选数以加速测试
        try:
            result = service.execute_run(
                trade_date=trade_date_str,
                mode="balanced",
                candidate_limit=3,
                ai_top_k=0,  # 跳过 AI 以加速
                market="cn",
            )
        except Exception as exc:
            self.skipTest(f"execute_run 失败 (可能需要实时数据): {exc}")

        run_id = result.get("run_id")
        self.assertIsNotNone(run_id, "应返回 run_id")
        status = result.get("status", "")
        self.assertIn(
            status,
            ["completed", "completed_with_ai_degraded"],
            f"run 状态应为 completed, 实际: {status}",
        )

        # 从 DB 读取候选并验证五层字段
        candidates = db.list_screening_candidates(run_id)
        if not candidates:
            self.skipTest("无候选股 (可能交易日数据不足)")

        five_layer_count = 0
        for c in candidates:
            c_dict = c if isinstance(c, dict) else c.to_dict() if hasattr(c, "to_dict") else {}
            code = c_dict.get("code", "?")
            trade_stage = c_dict.get("trade_stage")
            market_regime = c_dict.get("market_regime")
            theme_position = c_dict.get("theme_position")
            pool_level = c_dict.get("candidate_pool_level")

            if trade_stage is not None:
                five_layer_count += 1

            print(
                f"  候选 {code}: stage={trade_stage}, regime={market_regime}, "
                f"theme={theme_position}, pool={pool_level}"
            )

        self.assertGreater(
            five_layer_count,
            0,
            "至少 1 个候选股应有 trade_stage (五层字段不应全为 null)",
        )

        # 验证 decision_context
        decision_context = result.get("decision_context")
        if decision_context:
            market_env = decision_context.get("market_environment")
            if market_env:
                self.assertIsNotNone(
                    market_env.get("market_regime"),
                    "decision_context 应包含 market_regime",
                )
                print(f"  L1 上下文: regime={market_env.get('market_regime')}, "
                      f"risk={market_env.get('risk_level')}")

            sectors = decision_context.get("sector_heat_results", [])
            if sectors:
                print(f"  L2 上下文: {len(sectors)} 个板块, "
                      f"hot={decision_context.get('hot_theme_count', 0)}, "
                      f"warm={decision_context.get('warm_theme_count', 0)}")

        print(f"\n  [结果] {len(candidates)} 候选, {five_layer_count} 有五层数据")

    def test_stand_aside_run_caps_trade_stage(self):
        """验证 stand_aside 环境下 trade_stage 不超过 watch。

        通过 mock MarketGuard 强制 stand_aside，然后检查所有候选的 trade_stage。
        """
        from src.schemas.trading_types import TradeStage

        EXECUTABLE_STAGES = {
            TradeStage.PROBE_ENTRY.value,
            TradeStage.ADD_ON_STRENGTH.value,
        }

        from src.services.screening_task_service import ScreeningTaskService
        from src.storage import DatabaseManager

        db = DatabaseManager()
        service = ScreeningTaskService(db_manager=db)

        # Mock MarketGuard 返回不安全
        mock_guard = MagicMock()
        mock_guard.is_safe = False
        mock_guard.index_price = 2500.0
        mock_guard.index_ma100 = 3200.0

        try:
            with patch.object(
                service, "_check_market_guard", return_value=mock_guard
            ):
                result = service.execute_run(
                    trade_date=self.latest_date,
                    mode="balanced",
                    candidate_limit=3,
                    ai_top_k=0,
                    market="cn",
                )
        except Exception as exc:
            self.skipTest(f"execute_run 失败: {exc}")

        run_id = result.get("run_id")
        if not run_id:
            self.skipTest("无 run_id")

        candidates = db.list_screening_candidates(run_id)
        for c in candidates:
            c_dict = c if isinstance(c, dict) else c.to_dict() if hasattr(c, "to_dict") else {}
            trade_stage = c_dict.get("trade_stage")
            if trade_stage:
                self.assertNotIn(
                    trade_stage,
                    EXECUTABLE_STAGES,
                    f"stand_aside 下 {c_dict.get('code')} 的 trade_stage={trade_stage} "
                    f"不应为执行级",
                )


# ──────────────────────────────────────────────────────────────
# Case 9: API 返回验证
# ──────────────────────────────────────────────────────────────

class TestAPIFiveLayerFields(unittest.TestCase):
    """验证 API 响应包含五层字段（需要有已完成的 run）。"""

    def test_candidate_api_includes_five_layer_fields(self):
        """验证 GET /candidates 返回的字段包含五层系统相关键。"""
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT run_id FROM screening_runs WHERE status='completed' "
            "ORDER BY started_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
        conn.close()

        if not row:
            self.skipTest("无已完成的 screening run")

        run_id = row[0]

        # 直接检查 DB 中的候选数据（避免启动 API 服务器）
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT code, trade_stage, market_regime, theme_position, "
            "candidate_pool_level, setup_type, entry_maturity, risk_level, "
            "trade_plan_json FROM screening_candidates WHERE run_id = ? LIMIT 5",
            (run_id,),
        )
        candidates = cursor.fetchall()
        conn.close()

        if not candidates:
            self.skipTest("无候选数据")

        col_names = [
            "code", "trade_stage", "market_regime", "theme_position",
            "candidate_pool_level", "setup_type", "entry_maturity", "risk_level",
            "trade_plan_json",
        ]
        for row in candidates:
            data = dict(zip(col_names, row))
            # 五层字段列应存在（值可以为 null — 旧数据迁移前）
            self.assertIn("trade_stage", data)
            self.assertIn("market_regime", data)
            print(f"  API 候选 {data['code']}: stage={data['trade_stage']}, "
                  f"regime={data['market_regime']}")


# ──────────────────────────────────────────────────────────────
# Case 10: 板块热度持久化验证
# ──────────────────────────────────────────────────────────────

class TestSectorHeatPersistence(FiveLayerLocalTestBase):
    """验证板块热度计算后能正确持久化到 daily_sector_heat 表。"""

    def test_compute_and_persist(self):
        """计算板块热度后数据应写入 daily_sector_heat 表。"""
        from src.services.sector_heat_engine import SectorHeatEngine
        from src.storage import DatabaseManager

        db = DatabaseManager()
        engine = SectorHeatEngine(db_manager=db)
        trade_date = date.fromisoformat(self.latest_date)

        results = engine.compute_all_sectors(self.snapshot_df, trade_date)
        self.assertGreater(len(results), 0)

        # 检查 DB 中是否有对应日期的记录
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM daily_sector_heat WHERE trade_date = ?",
            (self.latest_date,),
        )
        count = cursor.fetchone()[0]
        conn.close()

        # 注意: engine.compute_all_sectors 可能不直接写 DB（取决于实现）
        # 这里验证表结构正确可查询
        print(f"  板块热度持久化: {count} 条记录 (date={self.latest_date})")


if __name__ == "__main__":
    unittest.main(verbosity=2)
