# -*- coding: utf-8 -*-
"""
Tests for FactorService integration with BottomDivergenceBreakoutDetector.
"""

import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from src.config import Config
from src.schemas.trading_types import SetupType
from src.services.factor_service import FactorService
from src.services.setup_freshness_assessor import SetupFreshnessAssessor


class TestFactorServiceBottomDivergence(unittest.TestCase):
    """FactorService 与底背离检测器的集成测试。"""

    @staticmethod
    def _make_test_df(n: int = 150) -> pd.DataFrame:
        """构造测试用 OHLCV DataFrame。"""
        rng = np.random.RandomState(42)
        prices = np.linspace(10, 20, n) + rng.randn(n) * 0.5
        return pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=n),
            "code": "TEST001",
            "open": prices,
            "high": prices + 0.5,
            "low": prices - 0.5,
            "close": prices,
            "volume": rng.randint(100_000, 500_000, n),
            "amount": prices * rng.randint(100_000, 500_000, n),
            "pct_chg": rng.randn(n) * 2,
        })

    def test_reuses_frozen_v2_evidence_across_parameters(self):
        from src.indicators.causal_bottom_divergence_detector import (
            CausalBottomDivergenceDetector,
        )

        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "001337_bottom_divergence_20251201_20260805.csv"
        )
        group = pd.read_csv(fixture_path, parse_dates=["date"])
        group["code"] = "001337"
        group["data_source"] = "fixture_source"
        group["adj_factor"] = 1.0
        group["adj_factor_source"] = "tushare_native"
        service = FactorService(
            db_manager=MagicMock(),
            config=Config(bottom_divergence_v2_enabled=True),
        )
        frozen = CausalBottomDivergenceDetector.freeze_evidence(group)

        expected = service._compute_bottom_divergence_v2_factors(group)
        actual = service.compute_bottom_divergence_v2_factors(
            group,
            frozen_evidence=frozen,
        )

        self.assertEqual(actual, expected)

    @staticmethod
    def _v2_keys() -> set[str]:
        return {
            "bottom_divergence_v2_candidate",
            "bottom_divergence_v2_stage",
            "bottom_divergence_v2_pattern_code",
            "bottom_divergence_v2_pattern_label",
            "bottom_divergence_v2_early_reversal",
            "bottom_divergence_v2_early_strength",
            "bottom_divergence_v2_near_zone_lower",
            "bottom_divergence_v2_near_zone_upper",
            "bottom_divergence_v2_near_zone_score",
            "bottom_divergence_v2_near_entered",
            "bottom_divergence_v2_near_accepted",
            "bottom_divergence_v2_near_crossed",
            "bottom_divergence_v2_near_cleared",
            "bottom_divergence_v2_major_zone_lower",
            "bottom_divergence_v2_major_zone_upper",
            "bottom_divergence_v2_major_zone_score",
            "bottom_divergence_v2_major_breakout",
            "bottom_divergence_v2_major_actionable_entry",
            "bottom_divergence_v2_actionability_status",
            "bottom_divergence_v2_confirmation_days",
            "bottom_divergence_v2_extended_pct",
            "bottom_divergence_v2_extended_pct_raw",
            "bottom_divergence_v2_stop_loss_price",
            "bottom_divergence_v2_candidate_version",
            "bottom_divergence_v2_zone_version",
            "bottom_divergence_v2_candidate_records",
            "bottom_divergence_v2_layered_buy_points",
            "bottom_divergence_v2_as_of_index",
            "bottom_divergence_v2_early_event_index",
            "bottom_divergence_v2_near_event_index",
            "bottom_divergence_v2_major_event_index",
            "bottom_divergence_v2_active_event_index",
            "bottom_divergence_v2_event_days",
            "bottom_divergence_v2_degradation_reasons",
            "bottom_divergence_v2_hit_reasons",
        }

    def test_config_injection_does_not_touch_global_singleton(self):
        injected = Config(
            bottom_divergence_v2_enabled=True,
            screening_factor_lookback_days=123,
            screening_min_list_days=234,
            screening_breakout_lookback_days=45,
        )

        with patch(
            "src.services.factor_service.get_config",
            side_effect=AssertionError("global config must not be read"),
        ):
            service = FactorService(db_manager=MagicMock(), config=injected)

        self.assertIs(service.config, injected)
        self.assertEqual(service.lookback_days, 123)
        self.assertEqual(service.min_list_days, 234)
        self.assertEqual(service.breakout_lookback_days, 45)

    @staticmethod
    def _adjustment_metadata_rows(trade_date: date) -> list:
        """20 根含除权的日线：第 10 根 10 送 10，pre_close 减半。"""
        rows = []
        close = 10.0
        for index in range(20):
            previous_close = close
            if index == 0:
                pre_close = None
            elif index == 10:
                pre_close = previous_close / 2.0
            else:
                pre_close = previous_close
            if index > 0:
                close = (pre_close or previous_close) + 0.1
            rows.append(SimpleNamespace(
                code="TEST001",
                date=trade_date - timedelta(days=19 - index),
                open=close - 0.1,
                high=close + 0.2,
                low=close - 0.2,
                close=close,
                pre_close=pre_close,
                volume=1000.0,
                amount=close * 1000.0,
                pct_chg=1.0,
                data_source="source_b" if index % 2 else "source_a",
                adj_factor=1.25,
                adj_factor_source="known_b" if index % 2 else "known_a",
                # 口径守卫的输入。非 raw（含缺列）时整窗 fail-closed，
                # 这批夹具测的是复权链本身，必须声明是原始价。
                adj_convention="raw",
            ))
        return rows

    def _capture_snapshot_group(self, *, trade_date, rows, config):
        scalars = MagicMock()
        scalars.all.return_value = rows
        execution = MagicMock()
        execution.scalars.return_value = scalars
        session = MagicMock()
        session.execute.return_value = execution

        class FakeDb:
            @contextmanager
            def get_session(self):
                yield session

        captured = {}

        def capture_group(group, latest, close_series):
            captured["group"] = group.copy()
            return {}

        service = FactorService(db_manager=FakeDb(), config=config)
        universe = pd.DataFrame([{
            "code": "TEST001",
            "name": "Test",
            "is_st": False,
            "list_date": date(2020, 1, 1),
        }])
        with patch.object(
            service,
            "_compute_extended_factors",
            side_effect=capture_group,
        ), patch.object(service, "_enrich_base_scores"):
            service.build_factor_snapshot(universe, trade_date=trade_date)
        return captured["group"]

    def test_stock_daily_adjustment_metadata_reaches_group_when_switch_off(self):
        """`ADJ_APPLY_ON_READ=false` 时取数元数据必须逐字到达 group。

        这是回滚路径的判据：关掉开关后 group 必须与接入复权之前完全一致。
        """
        trade_date = date(2026, 8, 5)
        group = self._capture_snapshot_group(
            trade_date=trade_date,
            rows=self._adjustment_metadata_rows(trade_date),
            config=Config(adj_apply_on_read=False),
        )

        self.assertEqual(
            {"data_source", "adj_factor", "adj_factor_source"}.difference(group),
            set(),
        )
        self.assertEqual(set(group["data_source"]), {"source_a", "source_b"})
        self.assertEqual(set(group["adj_factor_source"]), {"known_a", "known_b"})
        self.assertTrue((group["adj_factor"] == 1.25).all())

    def test_production_group_carries_the_recomputed_adjustment_chain(self):
        """开关默认打开时，group 拿到的是现算的复权链而不是取数时的旧标记。

        取数时的 `adj_factor_source`（这里是 known_a/known_b）必须被换掉：
        留着它等于让一段刚被复权过的价格顶着旧口径的来源标记进下游门禁。
        """
        trade_date = date(2026, 8, 5)
        group = self._capture_snapshot_group(
            trade_date=trade_date,
            rows=self._adjustment_metadata_rows(trade_date),
            config=Config(),
        )

        self.assertEqual(set(group["data_source"]), {"source_a", "source_b"})
        self.assertEqual(set(group["adj_factor_source"]), {"pre_close_chain"})
        # 末行即 D，因子恒为 1；除权（第 10 根 10 送 10）之前的 bar 被折半。
        self.assertAlmostEqual(float(group["adj_factor"].iloc[-1]), 1.0)
        self.assertAlmostEqual(float(group["adj_factor"].iloc[0]), 0.5)
        self.assertAlmostEqual(float(group["adj_factor"].iloc[10]), 1.0)

    @patch("src.services.factor_service.CausalBottomDivergenceDetector.detect")
    def test_v2_disabled_returns_stable_schema_without_detector_call(self, detect_mock):
        service = FactorService(
            db_manager=MagicMock(),
            config=Config(bottom_divergence_v2_enabled=False),
        )

        result = service._compute_bottom_divergence_v2_factors(
            self._make_test_df(80)
        )

        detect_mock.assert_not_called()
        self.assertEqual(set(result), self._v2_keys())
        self.assertFalse(result["bottom_divergence_v2_candidate"])
        self.assertEqual(result["bottom_divergence_v2_stage"], "rejected")
        self.assertEqual(
            result["bottom_divergence_v2_actionability_status"],
            "disabled",
        )
        self.assertEqual(result["bottom_divergence_v2_candidate_records"], [])
        self.assertEqual(result["bottom_divergence_v2_as_of_index"], 79)
        self.assertIsNone(result["bottom_divergence_v2_early_event_index"])
        self.assertIsNone(result["bottom_divergence_v2_near_event_index"])
        self.assertIsNone(result["bottom_divergence_v2_major_event_index"])
        self.assertIsNone(result["bottom_divergence_v2_active_event_index"])
        self.assertIsNone(result["bottom_divergence_v2_event_days"])

    @patch("src.services.factor_service.CausalBottomDivergenceDetector.detect")
    def test_v2_mocked_primary_is_fully_flattened(self, detect_mock):
        group = self._make_test_df(80)
        group["data_source"] = ["source_b", "source_a"] * 40
        group["adj_factor"] = 1.25
        group["adj_factor_source"] = [
            "tushare_native",
            "akshare_qfq_div_raw",
        ] * 40
        candidate_records = [{"candidate_version": "candidate-1"}]
        layered = [{"level": "early", "triggered": True}]
        detect_mock.return_value = {
            "found": True,
            "stage": "major_actionable",
            "pattern": {"code": "price_down_macd_up", "label": "经典底背离"},
            "early_reversal": {
                "bar_index": 70,
                "triggered": True,
                "strength": 0.83,
            },
            "near_zone_events": {
                "entered": {"bar_index": 71},
                "accepted": {"bar_index": 72},
                "crossed": {"bar_index": 73},
                "cleared_confirmed": {"bar_index": 74},
            },
            "major_zone_breakout": {
                "bar_index": 78,
                "confirmed": True,
            },
            "major_zone_actionable_entry": {
                "actionable": True,
                "confirmation_days": 1,
                "extended_pct": 4.5,
                "extended_pct_raw": 4.5000001,
            },
            "actionability_status": "actionable",
            "zone": {
                "zone_version": "zone-1",
                "r1": {"lower": 12.0, "upper": 12.5, "score": 0.61},
                "r2": {"lower": 14.0, "upper": 14.5, "score": 0.72},
            },
            "candidate_version": "candidate-1",
            "candidate_records": candidate_records,
            "stop_loss_price": 9.5,
            "layered_buy_points": layered,
            "degradation_reasons": ["missing_volume"],
            "hit_reasons": ["mock hit"],
        }
        config = Config(
            bottom_divergence_v2_enabled=True,
            bottom_divergence_v2_cluster_pct=0.02,
            bottom_divergence_v2_atr_gap_multiplier=0.7,
            bottom_divergence_v2_zone_score_min=0.6,
            bottom_divergence_v2_breakout_buffer_pct=0.004,
            bottom_divergence_v2_sync_window=4,
            bottom_divergence_v2_retention_bars=25,
            bottom_divergence_v2_r1_weights=(0.2, 0.2, 0.2, 0.2, 0.1, 0.1),
            bottom_divergence_v2_r2_weights=(0.1, 0.2, 0.2, 0.2, 0.2, 0.1),
        )

        result = FactorService(
            db_manager=MagicMock(),
            config=config,
        )._compute_bottom_divergence_v2_factors(group)

        _, kwargs = detect_mock.call_args
        self.assertEqual(kwargs["as_of_index"], 79)
        self.assertNotIn("sync_window", kwargs)
        self.assertNotIn("retention_bars", kwargs)
        params = kwargs["zone_params"]
        self.assertEqual(params.swing_order, 5)
        self.assertEqual(params.cluster_pct, 0.02)
        self.assertEqual(params.atr_gap_multiplier, 0.7)
        self.assertEqual(params.score_min, 0.6)
        self.assertEqual(params.breakout_buffer_pct, 0.004)
        self.assertEqual(params.sync_window, 4)
        self.assertEqual(params.invalidated_retention_bars, 25)
        self.assertEqual(
            (
                params.r1_touch_weight,
                params.r1_recency_weight,
                params.r1_volume_weight,
                params.r1_rejection_weight,
                params.r1_tightness_weight,
                params.r1_distance_weight,
            ),
            config.bottom_divergence_v2_r1_weights,
        )
        self.assertEqual(kwargs["metadata"].data_source, "source_a|source_b")
        self.assertEqual(
            kwargs["metadata"].adj_factor_source,
            "akshare_qfq_div_raw|tushare_native",
        )
        self.assertEqual(set(result), self._v2_keys())
        self.assertTrue(result["bottom_divergence_v2_candidate"])
        self.assertEqual(result["bottom_divergence_v2_stage"], "major_actionable")
        self.assertEqual(result["bottom_divergence_v2_pattern_code"], "price_down_macd_up")
        self.assertEqual(result["bottom_divergence_v2_early_strength"], 0.83)
        self.assertEqual(result["bottom_divergence_v2_near_zone_lower"], 12.0)
        self.assertEqual(result["bottom_divergence_v2_major_zone_upper"], 14.5)
        self.assertTrue(result["bottom_divergence_v2_near_entered"])
        self.assertTrue(result["bottom_divergence_v2_near_accepted"])
        self.assertTrue(result["bottom_divergence_v2_near_crossed"])
        self.assertTrue(result["bottom_divergence_v2_near_cleared"])
        self.assertTrue(result["bottom_divergence_v2_major_breakout"])
        self.assertTrue(result["bottom_divergence_v2_major_actionable_entry"])
        self.assertEqual(result["bottom_divergence_v2_confirmation_days"], 1)
        self.assertEqual(result["bottom_divergence_v2_extended_pct"], 4.5)
        self.assertEqual(result["bottom_divergence_v2_extended_pct_raw"], 4.5000001)
        self.assertEqual(result["bottom_divergence_v2_candidate_records"], candidate_records)
        self.assertEqual(result["bottom_divergence_v2_layered_buy_points"], layered)
        self.assertEqual(result["bottom_divergence_v2_as_of_index"], 79)
        self.assertEqual(result["bottom_divergence_v2_early_event_index"], 70)
        self.assertEqual(result["bottom_divergence_v2_near_event_index"], 74)
        self.assertEqual(result["bottom_divergence_v2_major_event_index"], 78)
        self.assertEqual(result["bottom_divergence_v2_active_event_index"], 78)
        self.assertEqual(result["bottom_divergence_v2_event_days"], 1)
        self.assertEqual(result["bottom_divergence_v2_hit_reasons"], ["mock hit"])

    @patch("src.services.factor_service.CausalBottomDivergenceDetector.detect")
    def test_v2_unknown_adjustment_defensively_blocks_major_actionability(
        self,
        detect_mock,
    ):
        group = self._make_test_df(80)
        group["data_source"] = "known_source"
        group["adj_factor"] = 1.0
        group["adj_factor_source"] = "akshare_qfq_div_raw_fallback"
        detect_mock.return_value = {
            "found": True,
            "stage": "major_actionable",
            "pattern": {"code": "price_down_macd_up", "label": "经典底背离"},
            "early_reversal": {"bar_index": 70, "strength": 0.8},
            "near_zone_events": {
                "entered": {"bar_index": 71},
                "accepted": {"bar_index": 72},
                "crossed": {"bar_index": 73},
                "cleared_confirmed": {"bar_index": 74},
            },
            "major_zone_breakout": {"bar_index": 78, "confirmed": True},
            "major_zone_actionable_entry": {
                "actionable": True,
                "confirmation_days": 1,
                "extended_pct": 2.0,
                "extended_pct_raw": 2.0,
            },
            "actionability_status": "actionable",
            "zone": {
                "zone_version": "zone-1",
                "r1": {"lower": 12.0, "upper": 12.5, "score": 0.61},
                "r2": {"lower": 14.0, "upper": 14.5, "score": 0.72},
            },
            "candidate_version": "candidate-1",
            "candidate_records": [{"candidate_version": "candidate-1"}],
            "stop_loss_price": 9.5,
            "layered_buy_points": [{"level": "early"}],
            "degradation_reasons": [],
        }
        service = FactorService(
            db_manager=MagicMock(),
            config=Config(bottom_divergence_v2_enabled=True),
        )

        result = service._compute_bottom_divergence_v2_factors(group)

        self.assertEqual(
            detect_mock.call_args.kwargs["metadata"].adj_factor_source,
            "unknown",
        )
        self.assertEqual(result["bottom_divergence_v2_stage"], "major_unverified")
        self.assertTrue(result["bottom_divergence_v2_early_reversal"])
        self.assertTrue(result["bottom_divergence_v2_near_cleared"])
        self.assertTrue(result["bottom_divergence_v2_major_breakout"])
        self.assertFalse(result["bottom_divergence_v2_major_actionable_entry"])
        self.assertEqual(result["bottom_divergence_v2_early_event_index"], 70)
        self.assertEqual(result["bottom_divergence_v2_near_event_index"], 74)
        self.assertEqual(result["bottom_divergence_v2_major_event_index"], 78)
        self.assertIsNone(result["bottom_divergence_v2_active_event_index"])
        self.assertIsNone(result["bottom_divergence_v2_event_days"])
        self.assertEqual(
            result["bottom_divergence_v2_actionability_status"],
            "adjustment_unknown",
        )

    def test_real_v2_event_days_feed_freshness_from_active_event(self):
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "001337_bottom_divergence_20251201_20260805.csv"
        )
        fixture = pd.read_csv(fixture_path, parse_dates=["date"])
        fixture["data_source"] = "fixture_source"
        fixture["adj_factor"] = 1.0
        fixture["adj_factor_source"] = "tushare_native"
        service = FactorService(
            db_manager=MagicMock(),
            config=Config(bottom_divergence_v2_enabled=True),
        )
        assessor = SetupFreshnessAssessor()

        snapshots = []
        for as_of in (154, 155, 156):
            factors = service._compute_bottom_divergence_v2_factors(
                fixture.iloc[:as_of + 1].copy()
            )
            freshness = assessor.assess(
                SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
                factors,
            )
            snapshots.append((factors, freshness))

        early, near, later = snapshots
        self.assertEqual(early[0]["bottom_divergence_v2_stage"], "early")
        self.assertEqual(
            fixture.iloc[
                early[0]["bottom_divergence_v2_active_event_index"]
            ]["date"].strftime("%Y-%m-%d"),
            "2026-07-22",
        )
        self.assertEqual(early[0]["bottom_divergence_v2_event_days"], 0)
        self.assertEqual(
            early[0]["bottom_divergence_v2_early_event_index"],
            early[0]["bottom_divergence_v2_active_event_index"],
        )
        self.assertIsNone(early[0]["bottom_divergence_v2_near_event_index"])
        self.assertIsNone(early[0]["bottom_divergence_v2_major_event_index"])
        self.assertEqual(early[1], 1.0)

        self.assertEqual(near[0]["bottom_divergence_v2_stage"], "near_cleared")
        self.assertEqual(
            fixture.iloc[
                near[0]["bottom_divergence_v2_active_event_index"]
            ]["date"].strftime("%Y-%m-%d"),
            "2026-07-23",
        )
        self.assertEqual(near[0]["bottom_divergence_v2_event_days"], 0)
        self.assertEqual(near[0]["bottom_divergence_v2_early_event_index"], 154)
        self.assertEqual(
            near[0]["bottom_divergence_v2_near_event_index"],
            near[0]["bottom_divergence_v2_active_event_index"],
        )
        self.assertIsNone(near[0]["bottom_divergence_v2_major_event_index"])
        self.assertEqual(near[1], 1.0)

        self.assertEqual(later[0]["bottom_divergence_v2_stage"], "near_cleared")
        self.assertEqual(later[0]["bottom_divergence_v2_event_days"], 1)
        self.assertEqual(later[1], 0.9)

    @patch("src.services.factor_service.CausalBottomDivergenceDetector.detect")
    def test_v2_short_data_and_no_primary_return_stable_defaults(self, detect_mock):
        service = FactorService(
            db_manager=MagicMock(),
            config=Config(bottom_divergence_v2_enabled=True),
        )

        short = service._compute_bottom_divergence_v2_factors(
            self._make_test_df(59)
        )
        detect_mock.assert_not_called()
        self.assertEqual(set(short), self._v2_keys())
        self.assertEqual(short["bottom_divergence_v2_actionability_status"], "insufficient_data")

        detect_mock.return_value = {
            "found": False,
            "stage": None,
            "candidate_records": [{"lifecycle": "invalidated"}],
            "degradation_reasons": ["missing_volume"],
            "actionability_status": None,
        }
        no_primary = service._compute_bottom_divergence_v2_factors(
            self._make_test_df(80).assign(
                data_source="known_source",
                adj_factor=1.0,
                adj_factor_source="known_adjustment",
            )
        )
        self.assertEqual(set(no_primary), self._v2_keys())
        self.assertEqual(no_primary["bottom_divergence_v2_stage"], "rejected")
        self.assertFalse(no_primary["bottom_divergence_v2_candidate"])
        self.assertEqual(
            no_primary["bottom_divergence_v2_candidate_records"],
            [{"lifecycle": "invalidated"}],
        )
        self.assertEqual(
            no_primary["bottom_divergence_v2_degradation_reasons"],
            ["missing_volume"],
        )

    @patch("src.services.factor_service.CausalBottomDivergenceDetector.detect")
    def test_v2_invalid_injected_config_rejects_without_crashing(
        self,
        detect_mock,
    ):
        service = FactorService(
            db_manager=MagicMock(),
            config=Config(
                bottom_divergence_v2_enabled=True,
                bottom_divergence_v2_cluster_pct=0.0,
            ),
        )
        group = self._make_test_df(80).assign(
            data_source="known_source",
            adj_factor=1.0,
            adj_factor_source="known_adjustment",
        )

        result = service._compute_bottom_divergence_v2_factors(group)

        detect_mock.assert_not_called()
        self.assertEqual(set(result), self._v2_keys())
        self.assertFalse(result["bottom_divergence_v2_candidate"])
        self.assertEqual(result["bottom_divergence_v2_stage"], "rejected")
        self.assertEqual(
            result["bottom_divergence_v2_actionability_status"],
            "invalid_config",
        )
        self.assertEqual(
            result["bottom_divergence_v2_degradation_reasons"],
            ["invalid_config"],
        )

    @patch("src.services.factor_service.CausalBottomDivergenceDetector.detect")
    def test_v2_direct_invalid_sync_or_retention_never_calls_detector(
        self,
        detect_mock,
    ):
        invalid_cases = (
            ("bottom_divergence_v2_sync_window", 0),
            ("bottom_divergence_v2_sync_window", -1),
            ("bottom_divergence_v2_sync_window", True),
            ("bottom_divergence_v2_retention_bars", 0),
            ("bottom_divergence_v2_retention_bars", -1),
            ("bottom_divergence_v2_retention_bars", False),
        )
        group = self._make_test_df(80).assign(
            data_source="known_source",
            adj_factor=1.0,
            adj_factor_source="tushare_native",
        )

        for field_name, value in invalid_cases:
            with self.subTest(field_name=field_name, value=value):
                service = FactorService(
                    db_manager=MagicMock(),
                    config=Config(
                        bottom_divergence_v2_enabled=True,
                        **{field_name: value},
                    ),
                )

                result = service._compute_bottom_divergence_v2_factors(group)

                self.assertEqual(
                    result["bottom_divergence_v2_actionability_status"],
                    "invalid_config",
                )
                self.assertEqual(
                    result["bottom_divergence_v2_degradation_reasons"],
                    ["invalid_config"],
                )
        detect_mock.assert_not_called()

    @patch("src.services.factor_service.CausalBottomDivergenceDetector.detect")
    def test_v2_enabled_parse_error_rejects_without_detector(self, detect_mock):
        service = FactorService(
            db_manager=MagicMock(),
            config=Config(
                bottom_divergence_v2_enabled=True,
                _bottom_divergence_v2_parse_errors=(
                    ("BOTTOM_DIVERGENCE_V2_SYNC_WINDOW", "invalid"),
                ),
            ),
        )

        result = service._compute_bottom_divergence_v2_factors(
            self._make_test_df(80)
        )

        detect_mock.assert_not_called()
        self.assertEqual(result["bottom_divergence_v2_stage"], "rejected")
        self.assertEqual(
            result["bottom_divergence_v2_actionability_status"],
            "invalid_config",
        )
        self.assertEqual(
            result["bottom_divergence_v2_degradation_reasons"],
            ["invalid_config"],
        )

    @patch("src.services.factor_service.CausalBottomDivergenceDetector.detect")
    def test_v2_invalid_switch_stays_disabled_without_detector(self, detect_mock):
        service = FactorService(
            db_manager=MagicMock(),
            config=Config(
                bottom_divergence_v2_enabled=False,
                _bottom_divergence_v2_parse_errors=(
                    ("BOTTOM_DIVERGENCE_V2_ENABLED", "invalid"),
                ),
            ),
        )

        result = service._compute_bottom_divergence_v2_factors(
            self._make_test_df(80)
        )

        detect_mock.assert_not_called()
        self.assertEqual(result["bottom_divergence_v2_stage"], "rejected")
        self.assertEqual(
            result["bottom_divergence_v2_actionability_status"],
            "disabled",
        )

    @patch("src.services.factor_service.CausalBottomDivergenceDetector.detect")
    def test_v2_major_breakout_requires_confirmed_event(self, detect_mock):
        group = self._make_test_df(80).assign(
            data_source="known_source",
            adj_factor=1.0,
            adj_factor_source="tushare_native",
        )
        detect_mock.return_value = {
            "found": True,
            "stage": "early",
            "pattern": {"code": "price_down_macd_up", "label": "经典底背离"},
            "early_reversal": {"bar_index": 70, "strength": 0.8},
            "near_zone_events": {},
            "major_zone_breakout": {
                "bar_index": 78,
                "confirmed": False,
            },
            "major_zone_actionable_entry": {"actionable": False},
            "actionability_status": "major_not_confirmed",
            "zone": {
                "zone_version": "zone-1",
                "metadata": {
                    "data_source": "known_source",
                    "adj_factor_source": "tushare_native",
                },
            },
            "candidate_version": "candidate-1",
            "candidate_records": [],
        }
        service = FactorService(
            db_manager=MagicMock(),
            config=Config(bottom_divergence_v2_enabled=True),
        )

        result = service._compute_bottom_divergence_v2_factors(group)

        self.assertFalse(result["bottom_divergence_v2_major_breakout"])
        self.assertTrue(result["bottom_divergence_v2_early_reversal"])

    def test_v2_001337_factor_replay_stages(self):
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "001337_bottom_divergence_20251201_20260805.csv"
        )
        fixture = pd.read_csv(fixture_path, parse_dates=["date"])
        fixture["data_source"] = "fixture_known"
        fixture["adj_factor"] = 1.0
        fixture["adj_factor_source"] = "tushare_native"
        service = FactorService(
            db_manager=MagicMock(),
            config=Config(bottom_divergence_v2_enabled=True),
        )
        expected = {
            "2026-07-22": "early",
            "2026-07-23": "near_cleared",
            "2026-08-05": "major_actionable",
        }

        actual = {}
        for day, expected_stage in expected.items():
            group = fixture.loc[fixture["date"] <= pd.Timestamp(day)].copy()
            factors = service._compute_bottom_divergence_v2_factors(group)
            actual[day] = factors["bottom_divergence_v2_stage"]
            self.assertTrue(factors["bottom_divergence_v2_candidate"])
            if expected_stage == "major_actionable":
                self.assertTrue(
                    factors["bottom_divergence_v2_major_actionable_entry"]
                )

        self.assertEqual(actual, expected)

    def test_factor_service_has_bottom_divergence_factors(self):
        """FactorService 输出包含底背离因子。"""
        df = self._make_test_df()
        service = FactorService()

        # 调用 _compute_extended_factors（内部方法，用于测试）
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])

        # 检查所有底背离因子存在
        expected_keys = [
            "bottom_divergence_double_breakout",
            "bottom_divergence_state",
            "bottom_divergence_pattern_code",
            "bottom_divergence_pattern_label",
            "bottom_divergence_signal_strength",
            "bottom_divergence_entry_price",
            "bottom_divergence_stop_loss",
            "bottom_divergence_horizontal_breakout",
            "bottom_divergence_trendline_breakout",
            "bottom_divergence_sync_breakout",
            "bottom_divergence_actionable_entry",
            "bottom_divergence_confirmation_days",
        ]
        for key in expected_keys:
            self.assertIn(key, result, f"Missing factor: {key}")

    def test_bottom_divergence_factors_types(self):
        """底背离因子类型正确。"""
        df = self._make_test_df()
        service = FactorService()
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])

        # 布尔因子
        self.assertIsInstance(result["bottom_divergence_double_breakout"], bool)
        self.assertIsInstance(result["bottom_divergence_horizontal_breakout"], bool)
        self.assertIsInstance(result["bottom_divergence_trendline_breakout"], bool)
        self.assertIsInstance(result["bottom_divergence_sync_breakout"], bool)

        # 字符串因子
        self.assertIsInstance(result["bottom_divergence_state"], str)
        self.assertIn(
            result["bottom_divergence_state"],
            ("rejected", "divergence_only", "structure_ready", "confirmed", "late_or_weak"),
        )

        # 数值因子
        self.assertIsInstance(result["bottom_divergence_signal_strength"], (int, float))
        self.assertGreaterEqual(result["bottom_divergence_signal_strength"], 0.0)
        self.assertLessEqual(result["bottom_divergence_signal_strength"], 1.0)

    def test_insufficient_data_safe_degradation(self):
        """数据不足时安全降级。"""
        df = self._make_test_df(n=20)  # 太少
        service = FactorService()
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])

        # 所有因子应该有默认值
        self.assertFalse(result["bottom_divergence_double_breakout"])
        self.assertEqual(result["bottom_divergence_state"], "rejected")
        self.assertEqual(result["bottom_divergence_signal_strength"], 0.0)
        self.assertIsNone(result["bottom_divergence_entry_price"])
        self.assertIsNone(result["bottom_divergence_stop_loss"])

    def test_confirmed_state_matches_boolean_factor(self):
        """confirmed 状态与布尔因子一致。"""
        df = self._make_test_df()
        service = FactorService()
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])

        # 如果 state == "confirmed"，则 double_breakout 应为 True
        if result["bottom_divergence_state"] == "confirmed":
            self.assertTrue(result["bottom_divergence_double_breakout"])
        # 反之亦然
        if result["bottom_divergence_double_breakout"]:
            self.assertEqual(result["bottom_divergence_state"], "confirmed")

    def test_pattern_code_transparency(self):
        """pattern_code 正确透传。"""
        df = self._make_test_df()
        service = FactorService()
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])

        # pattern_code 可以是 None 或有效的六种形态之一
        valid_codes = {
            "price_down_macd_up",
            "price_down_macd_flat",
            "price_flat_macd_up",
            "price_flat_macd_down",
            "price_up_macd_down",
            "price_up_macd_flat",
        }
        if result["bottom_divergence_pattern_code"] is not None:
            self.assertIn(result["bottom_divergence_pattern_code"], valid_codes)

    def test_hit_reasons_factor_exists(self):
        """FactorService 输出包含 bottom_divergence_hit_reasons 因子。"""
        df = self._make_test_df()
        service = FactorService()
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])
        self.assertIn("bottom_divergence_hit_reasons", result)
        self.assertIsInstance(result["bottom_divergence_hit_reasons"], list)

    def test_hit_reasons_empty_for_short_data(self):
        """数据不足时 hit_reasons 为空列表。"""
        df = self._make_test_df(n=20)
        service = FactorService()
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])
        self.assertEqual(result["bottom_divergence_hit_reasons"], [])

    @patch("src.services.factor_service.BottomDivergenceBreakoutDetector.detect")
    def test_confirmation_days_tracks_latest_double_breakout_bar(self, detect_mock):
        df = self._make_test_df(n=80)
        detect_mock.return_value = {
            "state": "confirmed",
            "pattern_code": "price_flat_macd_up",
            "pattern_label": "价格持平·MACD抬升",
            "signal_strength": 0.82,
            "entry_price": 12.3,
            "stop_loss_price": 10.8,
            "horizontal_breakout_confirmed": True,
            "trendline_breakout_confirmed": True,
            "double_breakout_sync": True,
            "downtrend_line": {"breakout_bar_index": 76},
            "hit_reasons": ["mocked"],
        }

        service = FactorService()
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])

        self.assertIn("bottom_divergence_confirmation_days", result)
        self.assertEqual(result["bottom_divergence_confirmation_days"], 3)

    @patch("src.services.factor_service.BottomDivergenceBreakoutDetector.detect")
    def test_confirmed_bottom_divergence_fields_are_preserved(self, detect_mock):
        df = self._make_test_df(n=90)
        latest_close = float(df.iloc[-1]["close"])
        detect_mock.return_value = {
            "state": "confirmed",
            "pattern_code": "price_down_macd_up",
            "pattern_label": "经典底背离",
            "signal_strength": 0.91,
            "entry_price": latest_close,
            "stop_loss_price": 10.6,
            "horizontal_breakout_confirmed": True,
            "trendline_breakout_confirmed": True,
            "double_breakout_sync": True,
            "confirmation_bar_index": 89,
            "hit_reasons": ["底背离成立", "双突破同步确认"],
        }

        result = FactorService._compute_bottom_divergence_factors(df, Config())

        self.assertTrue(result["bottom_divergence_double_breakout"])
        self.assertEqual(result["bottom_divergence_state"], "confirmed")
        self.assertEqual(result["bottom_divergence_pattern_code"], "price_down_macd_up")
        self.assertEqual(result["bottom_divergence_pattern_label"], "经典底背离")
        self.assertAlmostEqual(result["bottom_divergence_signal_strength"], 0.91)
        self.assertEqual(result["bottom_divergence_entry_price"], latest_close)
        self.assertEqual(result["bottom_divergence_stop_loss"], 10.6)
        self.assertTrue(result["bottom_divergence_horizontal_breakout"])
        self.assertTrue(result["bottom_divergence_trendline_breakout"])
        self.assertTrue(result["bottom_divergence_sync_breakout"])
        self.assertTrue(result["bottom_divergence_actionable_entry"])
        self.assertEqual(result["bottom_divergence_confirmation_days"], 0)
        self.assertEqual(result["bottom_divergence_hit_reasons"], ["底背离成立", "双突破同步确认"])
        self.assertEqual(result["bottom_divergence_entry_zone"], "just_double_breakout")
        self.assertEqual(result["bottom_divergence_entry_timing_score"], 1.0)

    @patch("src.services.factor_service.BottomDivergenceBreakoutDetector.detect")
    def test_confirmed_bottom_divergence_fields_reach_extended_factors(self, detect_mock):
        df = self._make_test_df(n=90)
        latest_close = float(df.iloc[-1]["close"])
        detect_mock.return_value = {
            "state": "confirmed",
            "pattern_code": "price_down_macd_up",
            "pattern_label": "经典底背离",
            "signal_strength": 0.91,
            "entry_price": latest_close,
            "stop_loss_price": 10.6,
            "horizontal_breakout_confirmed": True,
            "trendline_breakout_confirmed": True,
            "double_breakout_sync": True,
            "confirmation_bar_index": 89,
            "hit_reasons": ["底背离成立", "双突破同步确认"],
        }

        service = FactorService()
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])

        self.assertTrue(result["bottom_divergence_double_breakout"])
        self.assertEqual(result["bottom_divergence_state"], "confirmed")
        self.assertEqual(result["bottom_divergence_pattern_code"], "price_down_macd_up")
        self.assertEqual(result["bottom_divergence_pattern_label"], "经典底背离")
        self.assertAlmostEqual(result["bottom_divergence_signal_strength"], 0.91)
        self.assertEqual(result["bottom_divergence_entry_price"], latest_close)
        self.assertEqual(result["bottom_divergence_stop_loss"], 10.6)
        self.assertTrue(result["bottom_divergence_horizontal_breakout"])
        self.assertTrue(result["bottom_divergence_trendline_breakout"])
        self.assertTrue(result["bottom_divergence_sync_breakout"])
        self.assertTrue(result["bottom_divergence_actionable_entry"])
        self.assertEqual(result["bottom_divergence_confirmation_days"], 0)
        self.assertEqual(result["bottom_divergence_hit_reasons"], ["底背离成立", "双突破同步确认"])
        self.assertEqual(result["bottom_divergence_entry_zone"], "just_double_breakout")

    @patch("src.services.factor_service.BottomDivergenceBreakoutDetector.detect")
    def test_extended_confirmed_bottom_divergence_is_not_main_breakout(self, detect_mock):
        """Confirmed historical divergence should be suppressed once price is extended."""
        df = self._make_test_df(n=90)
        detect_mock.return_value = {
            "state": "confirmed",
            "pattern_code": "price_down_macd_up",
            "pattern_label": "经典底背离",
            "signal_strength": 0.91,
            "entry_price": 10.0,
            "stop_loss_price": 8.8,
            "horizontal_breakout_confirmed": True,
            "trendline_breakout_confirmed": True,
            "double_breakout_sync": True,
            "confirmation_bar_index": 88,
            "hit_reasons": ["底背离成立", "双突破同步确认"],
        }

        result = FactorService._compute_bottom_divergence_factors(df, Config())

        self.assertTrue(result["bottom_divergence_double_breakout"])
        self.assertFalse(result["bottom_divergence_actionable_entry"])
        self.assertEqual(result["bottom_divergence_state"], "confirmed")
        self.assertEqual(result["bottom_divergence_validation_status"], "extended_not_entry")
        self.assertEqual(result["bottom_divergence_entry_zone"], "extended_not_entry")
        self.assertGreater(result["bottom_divergence_extended_pct"], 10.0)
        self.assertEqual(result["bottom_divergence_entry_timing_score"], 0.0)

    @patch("src.services.factor_service.BottomDivergenceBreakoutDetector.detect")
    def test_failed_breakout_below_entry_is_not_actionable(self, detect_mock):
        """A post-confirmation drop below entry price is not a valid near-entry setup."""
        df = self._make_test_df(n=90)
        latest_close = float(df.iloc[-1]["close"])
        detect_mock.return_value = {
            "state": "confirmed",
            "pattern_code": "price_down_macd_up",
            "pattern_label": "经典底背离",
            "signal_strength": 0.91,
            "entry_price": latest_close * 1.2,
            "stop_loss_price": latest_close * 0.9,
            "horizontal_breakout_confirmed": True,
            "trendline_breakout_confirmed": True,
            "double_breakout_sync": True,
            "confirmation_bar_index": 88,
            "hit_reasons": ["底背离成立", "双突破同步确认"],
        }

        result = FactorService._compute_bottom_divergence_factors(df, Config())

        self.assertTrue(result["bottom_divergence_double_breakout"])
        self.assertFalse(result["bottom_divergence_actionable_entry"])
        self.assertEqual(result["bottom_divergence_validation_status"], "extended_not_entry")

    @patch("src.services.factor_service.BottomDivergenceBreakoutDetector.detect")
    def test_weak_bottom_divergence_pattern_is_not_actionable_entry(self, detect_mock):
        """Weak/continuation patterns can be transparent but should not enter main screening."""
        df = self._make_test_df(n=90)
        latest_close = float(df.iloc[-1]["close"])
        detect_mock.return_value = {
            "state": "confirmed",
            "pattern_code": "price_up_macd_flat",
            "pattern_label": "强势回撤·MACD持平",
            "signal_strength": 0.82,
            "entry_price": latest_close,
            "stop_loss_price": latest_close * 0.9,
            "horizontal_breakout_confirmed": True,
            "trendline_breakout_confirmed": True,
            "double_breakout_sync": True,
            "confirmation_bar_index": 89,
            "hit_reasons": ["强势回撤", "双突破同步确认"],
        }

        result = FactorService._compute_bottom_divergence_factors(df, Config())

        self.assertTrue(result["bottom_divergence_double_breakout"])
        self.assertFalse(result["bottom_divergence_actionable_entry"])
        self.assertFalse(result["bottom_divergence_valid_pattern"])


if __name__ == "__main__":
    unittest.main()
