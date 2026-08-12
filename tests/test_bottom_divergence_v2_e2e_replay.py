# -*- coding: utf-8 -*-
"""001337 因果底背离 v2 的真实生产链路 point-in-time 回放。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from api.v1.schemas.screening import ScreeningCandidateItem
from src.agent.skills.base import (
    SkillManager,
    load_skill_from_yaml,
)
from src.config import Config
from src.schemas.trading_types import (
    CandidateDecision,
    MarketEnvironment,
    MarketRegime,
    RiskLevel,
    ThemeDecision,
    ThemePosition,
)
from src.services.candidate_analysis_service import CandidateAnalysisService
from src.services.entry_maturity_assessor import EntryMaturityAssessor
from src.services.factor_service import FactorService
from src.services.five_layer_pipeline import FiveLayerPipeline
from src.services.screener_service import ScreenerService
from src.services.screening_ai_review_service import ScreeningAiReviewService
from src.services.setup_freshness_assessor import SetupFreshnessAssessor
from src.services.setup_resolver import SetupResolver
from src.services.strategy_screening_engine import (
    CommonFilterConfig,
    StrategyScreeningEngine,
    build_rules_from_skills,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = (
    ROOT
    / "tests"
    / "fixtures"
    / "001337_bottom_divergence_20251201_20260805.csv"
)
STRATEGY_PATH = (
    ROOT
    / "strategies"
    / "bottom_divergence_layered_entry_v2.yaml"
)


class _OfflineSearch:
    is_available = False


class _CapturingLlm:
    model_name = "deterministic-local-fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate_text(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
    ) -> str:
        del max_tokens, temperature
        self.prompts.append(prompt)
        prompt_input = _prompt_input(prompt)
        trade_plan = prompt_input.get("trade_plan") or {}
        return json.dumps(
            {
                "environment_ok": True,
                "trade_stage": prompt_input["market"]["rule_trade_stage"],
                "entry_maturity": prompt_input["setup"]["entry_maturity"],
                "setup_type": "bottom_divergence_layered_entry",
                "risk_level": prompt_input["market"]["risk_level"],
                "initial_position": trade_plan.get("initial_position"),
                "stop_loss_rule": trade_plan.get("stop_loss_rule"),
                "take_profit_plan": trade_plan.get("take_profit_plan"),
                "invalidation_rule": trade_plan.get("invalidation_rule"),
                "reasoning_summary": "仅依据冻结的v2证据维持规则阶段。",
                "confidence": 0.8,
            },
            ensure_ascii=False,
        )


def _prompt_input(prompt: str) -> dict:
    return json.loads(
        prompt.split("Input:\n", 1)[1].split("\nOutput schema:", 1)[0]
    )


def _optional_text(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    return str(value)


class BottomDivergenceV2E2EReplayTestCase(unittest.TestCase):
    def setUp(self) -> None:
        from src.storage import DatabaseManager

        self._database_path_was_set = "DATABASE_PATH" in os.environ
        self._old_database_path = os.environ.get("DATABASE_PATH")
        self._old_db_instance = DatabaseManager._instance
        self._old_config_instance = Config._instance
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup_resources)
        self._db_path = os.path.join(self._temp_dir.name, "v2-e2e.db")
        os.environ["DATABASE_PATH"] = self._db_path

        Config._instance = None
        DatabaseManager._instance = None
        self.db = DatabaseManager.get_instance()
        self._seed_fixture()
        self.universe = pd.DataFrame(
            [
                {
                    "code": "001337",
                    "name": "四川黄金",
                    "market": "cn",
                    "is_st": False,
                    "list_date": date(2023, 3, 3),
                }
            ]
        )
        self.isolated_config = Config(
            bottom_divergence_v2_enabled=True,
            screening_factor_lookback_days=365,
            screening_breakout_lookback_days=20,
            screening_min_list_days=120,
        )
        self.skill = load_skill_from_yaml(STRATEGY_PATH)
        self.rule = build_rules_from_skills([self.skill])[0]
        self.skill_manager = SkillManager()
        self.skill_manager.register(self.skill)

    def _cleanup_resources(self) -> None:
        from src.storage import DatabaseManager

        DatabaseManager.reset_instance()
        DatabaseManager._instance = self._old_db_instance
        Config._instance = self._old_config_instance
        if self._database_path_was_set:
            os.environ["DATABASE_PATH"] = self._old_database_path
        else:
            os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def _seed_fixture(self) -> None:
        from src.storage import InstrumentMaster, StockDaily

        fixture = pd.read_csv(FIXTURE_PATH, parse_dates=["date"])
        self.assertEqual(len(fixture), 165)
        with self.db.get_session() as session:
            session.add(
                InstrumentMaster(
                    code="001337",
                    name="四川黄金",
                    market="cn",
                    exchange="SZSE",
                    listing_status="active",
                    is_st=False,
                    industry="贵金属",
                    list_date=date(2023, 3, 3),
                )
            )
            for row in fixture.to_dict("records"):
                raw_adj_factor = row.get("adj_factor")
                adj_factor = (
                    None
                    if raw_adj_factor is None or pd.isna(raw_adj_factor)
                    else float(raw_adj_factor)
                )
                session.add(
                    StockDaily(
                        code="001337",
                        date=pd.Timestamp(row["date"]).date(),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        amount=float(row["close"]) * float(row["volume"]),
                        pct_chg=float(row["pct_chg"]),
                        data_source=_optional_text(row.get("data_source")),
                        adj_factor=adj_factor,
                        adj_factor_source=_optional_text(
                            row.get("adj_factor_source")
                        ),
                    )
                )
            session.commit()

    def _build_factor_snapshot(self, trade_date: date) -> pd.DataFrame:
        return FactorService(
            db_manager=self.db,
            config=self.isolated_config,
        ).build_factor_snapshot(
            self.universe,
            trade_date=trade_date,
            persist=False,
        )

    @staticmethod
    def _fake_layer_collaborators() -> tuple:
        sector_engine = MagicMock()
        sector_engine.compute_all_sectors.return_value = []
        registry = MagicMock()
        registry.is_empty = False
        aggregation = MagicMock()
        aggregation.aggregate.return_value = []
        resolver = MagicMock()
        resolver.identified_themes = []
        resolver.get_main_theme_boards.return_value = set()
        resolver.resolve.return_value = ThemeDecision(
            theme_tag="黄金",
            theme_score=90.0,
            theme_position=ThemePosition.MAIN_THEME,
            leader_score=80.0,
            sector_strength=85.0,
        )
        return sector_engine, registry, aggregation, resolver

    def _run_five_layer(
        self,
        snapshot_df: pd.DataFrame,
        trade_date: date,
    ):
        screener = ScreenerService(
            skill_manager=self.skill_manager,
            strategy_names=["bottom_divergence_layered_entry_v2"],
        )
        collaborators = self._fake_layer_collaborators()
        with (
            patch(
                "src.services.sector_heat_engine.SectorHeatEngine",
                return_value=collaborators[0],
            ),
            patch(
                "src.services.theme_mapping_registry.ThemeMappingRegistry",
                return_value=collaborators[1],
            ),
            patch(
                "src.services.theme_aggregation_service.ThemeAggregationService",
                return_value=collaborators[2],
            ),
            patch(
                "src.services.theme_position_resolver.ThemePositionResolver",
                return_value=collaborators[3],
            ),
            patch("src.services.five_layer_pipeline.write_debug_log"),
        ):
            return FiveLayerPipeline().run(
                snapshot_df=snapshot_df,
                trade_date=trade_date,
                market_env=MarketEnvironment(
                    regime=MarketRegime.BALANCED,
                    risk_level=RiskLevel.MEDIUM,
                    is_safe=True,
                ),
                guard_result=SimpleNamespace(is_safe=True, message="deterministic"),
                screener_service=screener,
                candidate_limit=1,
                db_manager=self.db,
                skill_manager=self.skill_manager,
                lock_universe=True,
            )

    def _run_full_chain(self, trade_date: date) -> dict:
        snapshot_df = self._build_factor_snapshot(trade_date)
        self.assertEqual(snapshot_df["code"].tolist(), ["001337"])
        snapshot = snapshot_df.iloc[0].to_dict()
        factor_snapshot = dict(snapshot)
        primary_record = next(
            record
            for record in factor_snapshot[
                "bottom_divergence_v2_candidate_records"
            ]
            if record["candidate_version"]
            == factor_snapshot["bottom_divergence_v2_candidate_version"]
        )

        engine_result = StrategyScreeningEngine().evaluate(
            snapshot_df=snapshot_df,
            rules=[self.rule],
            common_filters=CommonFilterConfig(min_list_days=120),
        )
        pipeline_result = self._run_five_layer(snapshot_df, trade_date)
        if engine_result.selected:
            self.assertEqual(len(engine_result.selected), 1)
            self.assertEqual(len(pipeline_result.candidates), 1)
            engine_candidate = engine_result.selected[0]
            setup = SetupResolver([self.rule]).resolve(
                allowed_strategies=engine_candidate.matched_strategies,
                strategy_scores=engine_candidate.strategy_scores,
                market_regime=MarketRegime.BALANCED,
                theme_position=ThemePosition.MAIN_THEME,
                factor_snapshot=engine_candidate.factor_snapshot,
            )
            maturity = EntryMaturityAssessor().assess(
                setup.setup_type,
                engine_candidate.factor_snapshot,
            )
            freshness = SetupFreshnessAssessor().assess(
                setup.setup_type,
                engine_candidate.factor_snapshot,
            )
            pipeline_candidate = pipeline_result.candidates[0]
            decision = CandidateDecision.from_record(pipeline_candidate)
            decision.selected_for_ai = True

            llm = _CapturingLlm()
            with patch(
                "src.services.candidate_analysis_service.write_debug_log_ea8dae"
            ):
                analysis = CandidateAnalysisService(
                    analysis_service=MagicMock(),
                    search_service=_OfflineSearch(),
                    db_manager=self.db,
                    screening_ai_review_service=ScreeningAiReviewService(
                        llm_client=llm
                    ),
                ).analyze_top_k(
                    [decision],
                    top_k=1,
                    news_top_m=0,
                )
            self.assertEqual(analysis.failed_codes, [])
            self.assertEqual(len(llm.prompts), 1)
            self.assertIn("001337", analysis.results)
            ai_result = analysis.results["001337"]
            api_payload = decision.to_payload()
            api_payload.update(ai_result)
            api_item = ScreeningCandidateItem(**api_payload).model_dump(
                mode="json"
            )
            prompt_evidence = _prompt_input(llm.prompts[0])["setup"][
                "bottom_divergence_v2_evidence"
            ]
            matched_strategies = engine_candidate.matched_strategies
            rule_hits = engine_candidate.rule_hits
            setup_artifact = {
                "resolved": setup.setup_type.value,
                "pipeline": pipeline_candidate.setup_type,
                "maturity": maturity.value,
                "freshness": freshness,
            }
            trade_artifact = {
                "stage": pipeline_candidate.trade_stage,
                "plan": (
                    json.loads(pipeline_candidate.trade_plan_json)
                    if pipeline_candidate.trade_plan_json
                    else None
                ),
            }
            ai_artifact = {
                "executed": True,
                "trade_stage": ai_result["ai_trade_stage"],
                "initial_position": ai_result["initial_position"],
                "downgrade_reasons": ai_result["downgrade_reasons"],
            }
        else:
            self.assertEqual(pipeline_result.candidates, [])
            api_item = ScreeningCandidateItem(
                code="001337",
                name="四川黄金",
                rank=1,
                rule_score=0.0,
                selected_for_ai=False,
                matched_strategies=[],
                rule_hits=[],
                setup_type="bottom_divergence_layered_entry",
                factor_snapshot=factor_snapshot,
            ).model_dump(mode="json")
            prompt_evidence = None
            matched_strategies = []
            rule_hits = []
            setup_artifact = {
                "resolved": None,
                "pipeline": None,
                "maturity": None,
                "freshness": None,
            }
            trade_artifact = {"stage": None, "plan": None}
            ai_artifact = {
                "executed": False,
                "trade_stage": None,
                "initial_position": None,
                "downgrade_reasons": [],
            }

        return {
            "factor": {
                key: factor_snapshot[key]
                for key in (
                    "bottom_divergence_v2_stage",
                    "bottom_divergence_v2_pattern_code",
                    "bottom_divergence_v2_early_reversal",
                    "bottom_divergence_v2_early_strength",
                    "bottom_divergence_v2_near_zone_lower",
                    "bottom_divergence_v2_near_zone_upper",
                    "bottom_divergence_v2_major_zone_lower",
                    "bottom_divergence_v2_major_zone_upper",
                    "bottom_divergence_v2_near_cleared",
                    "bottom_divergence_v2_major_breakout",
                    "bottom_divergence_v2_major_actionable_entry",
                    "bottom_divergence_v2_candidate",
                    "bottom_divergence_v2_actionability_status",
                    "bottom_divergence_v2_candidate_version",
                    "bottom_divergence_v2_zone_version",
                    "bottom_divergence_v2_candidate_records",
                    "bottom_divergence_v2_layered_buy_points",
                )
            },
            "historical_events": {
                "early": primary_record["early_reversal"],
                "near": primary_record["near_zone_events"],
                "major": primary_record["major_zone_breakout"],
            },
            "rule": {
                "matched_strategies": matched_strategies,
                "rule_hits": rule_hits,
            },
            "setup": setup_artifact,
            "trade": trade_artifact,
            "ai": ai_artifact,
            "prompt_evidence": prompt_evidence,
            "api": {
                "setup_type": api_item["setup_type"],
                "matched_strategies": api_item["matched_strategies"],
                "factor_snapshot": api_item["factor_snapshot"],
            },
            "v1": {
                key: snapshot[key]
                for key in (
                    "bottom_divergence_state",
                    "bottom_divergence_double_breakout",
                    "bottom_divergence_pattern_code",
                    "bottom_divergence_entry_price",
                    "bottom_divergence_stop_loss",
                    "bottom_divergence_buy_points",
                )
            },
        }

    def _mutate_future_rows(self, cutoff: date) -> None:
        from src.storage import StockDaily

        with self.db.get_session() as session:
            rows = (
                session.query(StockDaily)
                .filter(
                    StockDaily.code == "001337",
                    StockDaily.date > cutoff,
                )
                .all()
            )
            for index, row in enumerate(rows, start=1):
                row.open = 80.0 + index
                row.high = 90.0 + index
                row.low = 70.0 + index
                row.close = 85.0 + index
                row.pct_chg = 20.0
                row.data_source = "mutated-future-source"
                row.adj_factor = 9.0
                row.adj_factor_source = "mutated_future_adjustment"
            session.add(
                StockDaily(
                    code="001337",
                    date=date(2026, 8, 6),
                    open=101.0,
                    high=110.0,
                    low=99.0,
                    close=108.0,
                    volume=99999999.0,
                    amount=108.0 * 99999999.0,
                    pct_chg=20.0,
                    data_source="future-appended-source",
                    adj_factor=10.0,
                    adj_factor_source="future_appended_adjustment",
                )
            )
            session.commit()

    def _assert_replay_artifact_unchanged(
        self,
        before: dict,
        after: dict,
    ) -> None:
        for key, value in before["factor"].items():
            self.assertEqual(after["factor"][key], value, key)
        for section in (
            "historical_events",
            "rule",
            "setup",
            "trade",
            "ai",
            "prompt_evidence",
            "api",
            "v1",
        ):
            self.assertEqual(after[section], before[section], section)

    def test_real_unknown_fixture_preserves_structure_and_fails_closed(
        self,
    ) -> None:
        expected = {
            date(2026, 7, 22): ("early", "medium", True),
            date(2026, 7, 23): (
                "near_cleared",
                "high",
                True,
            ),
            date(2026, 8, 5): (
                "major_unverified",
                None,
                False,
            ),
        }
        artifacts = {}

        for trade_date, expectation in expected.items():
            # date 与枚举一样过不了 xdist 的报告序列化通道，并行下会整条变红。
            with self.subTest(trade_date=str(trade_date)):
                artifact = self._run_full_chain(trade_date)
                artifacts[trade_date] = artifact
                stage, maturity, structurally_selected = expectation
                self.assertEqual(
                    artifact["factor"]["bottom_divergence_v2_stage"],
                    stage,
                )
                self.assertEqual(
                    artifact["factor"][
                        "bottom_divergence_v2_actionability_status"
                    ],
                    "adjustment_unknown",
                )
                self.assertIsNone(artifact["trade"]["plan"])
                self.assertIsNone(artifact["ai"]["initial_position"])
                if structurally_selected:
                    self.assertEqual(
                        artifact["rule"]["matched_strategies"],
                        ["bottom_divergence_layered_entry_v2"],
                    )
                    self.assertIn(
                        "strategy:bottom_divergence_layered_entry_v2",
                        artifact["rule"]["rule_hits"],
                    )
                    self.assertEqual(
                        artifact["setup"],
                        {
                            "resolved": "bottom_divergence_layered_entry",
                            "pipeline": "bottom_divergence_layered_entry",
                            "maturity": maturity,
                            "freshness": 1.0,
                        },
                    )
                    self.assertTrue(artifact["ai"]["executed"])
                    self.assertEqual(artifact["ai"]["trade_stage"], "focus")
                    self.assertNotIn(
                        "missing_stop_anchor",
                        artifact["ai"]["downgrade_reasons"],
                    )
                    self.assertEqual(
                        artifact["prompt_evidence"]["stage"],
                        stage,
                    )
                else:
                    self.assertEqual(artifact["rule"]["matched_strategies"], [])
                    self.assertFalse(artifact["ai"]["executed"])
                    self.assertIsNone(artifact["prompt_evidence"])
                    self.assertEqual(
                        artifact["setup"],
                        {
                            "resolved": None,
                            "pipeline": None,
                            "maturity": None,
                            "freshness": None,
                        },
                    )
                for key, value in artifact["factor"].items():
                    self.assertEqual(
                        artifact["api"]["factor_snapshot"][key],
                        value,
                    )

        early = artifacts[date(2026, 7, 22)]
        major = artifacts[date(2026, 8, 5)]
        for version_key in (
            "bottom_divergence_v2_candidate_version",
            "bottom_divergence_v2_zone_version",
        ):
            versions = {
                artifact["factor"][version_key]
                for artifact in artifacts.values()
            }
            self.assertEqual(
                versions,
                {early["factor"][version_key]},
            )
        primary_records = [
            next(
                record
                for record in artifact["factor"][
                    "bottom_divergence_v2_candidate_records"
                ]
                if record["candidate_version"]
                == artifact["factor"][
                    "bottom_divergence_v2_candidate_version"
                ]
            )
            for artifact in artifacts.values()
        ]
        frozen_zone = primary_records[0]["zone"]
        for record in primary_records[1:]:
            self.assertEqual(record["zone"], frozen_zone)
        self.assertEqual(
            major["factor"]["bottom_divergence_v2_near_zone_lower"],
            37.46,
        )
        self.assertEqual(
            major["factor"]["bottom_divergence_v2_near_zone_upper"],
            39.2,
        )
        self.assertEqual(
            major["factor"]["bottom_divergence_v2_major_zone_lower"],
            40.61,
        )
        self.assertEqual(
            major["factor"]["bottom_divergence_v2_major_zone_upper"],
            42.9,
        )
        self.assertEqual(
            [record["early_reversal"] for record in primary_records],
            [primary_records[0]["early_reversal"]] * 3,
        )
        self.assertEqual(
            primary_records[0]["early_reversal"]["date"],
            "2026-07-22",
        )
        self.assertIsNone(
            primary_records[0]["near_zone_events"]["crossed"]["date"]
        )
        for event_name in ("crossed", "cleared_confirmed"):
            self.assertEqual(
                primary_records[1]["near_zone_events"][event_name],
                primary_records[2]["near_zone_events"][event_name],
            )
            self.assertEqual(
                primary_records[1]["near_zone_events"][event_name]["date"],
                "2026-07-23",
            )
        self.assertFalse(primary_records[0]["major_zone_breakout"]["confirmed"])
        self.assertFalse(primary_records[1]["major_zone_breakout"]["confirmed"])
        self.assertTrue(primary_records[2]["major_zone_breakout"]["confirmed"])
        self.assertEqual(
            primary_records[2]["major_zone_breakout"]["date"],
            "2026-08-05",
        )
        self.assertFalse(
            major["factor"]["bottom_divergence_v2_major_actionable_entry"]
        )

    def test_future_database_mutations_do_not_repaint_early_chain(self) -> None:
        trade_date = date(2026, 7, 22)
        before = self._run_full_chain(trade_date)
        self._mutate_future_rows(trade_date)
        after = self._run_full_chain(trade_date)

        self._assert_replay_artifact_unchanged(before, after)

    def test_future_database_mutations_do_not_repaint_near_chain(self) -> None:
        trade_date = date(2026, 7, 23)
        before = self._run_full_chain(trade_date)
        self._mutate_future_rows(trade_date)
        after = self._run_full_chain(trade_date)

        self._assert_replay_artifact_unchanged(before, after)

    def test_v1_replay_contract_is_unchanged_on_same_database_rows(self) -> None:
        early = self._build_factor_snapshot(date(2026, 7, 22)).iloc[0]
        major = self._build_factor_snapshot(date(2026, 8, 5)).iloc[0]

        self.assertEqual(early["bottom_divergence_state"], "divergence_only")
        self.assertFalse(early["bottom_divergence_double_breakout"])
        self.assertEqual(major["bottom_divergence_state"], "confirmed")
        self.assertTrue(major["bottom_divergence_double_breakout"])


if __name__ == "__main__":
    unittest.main()
