import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd

from src.config import Config
from src.services.factor_service import FactorService
from src.services.kline_audit_service import KlineAuditService
from src.services.theme_context_ingest_service import ExternalTheme, OpenClawThemeContext
from src.storage import DatabaseManager


class FactorServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_factor_service.db")
        os.environ["DATABASE_PATH"] = self._db_path

        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self.service = FactorService(self.db, lookback_days=40, breakout_lookback_days=20)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def test_build_factor_snapshot_uses_prior_window_for_breakout_and_volume_ratio(self) -> None:
        start_date = date(2026, 2, 20)
        rows = []
        for idx in range(21):
            trade_date = start_date + timedelta(days=idx)
            close = 10.0 + idx * 0.1
            high = close + 0.2
            volume = 1000 + idx * 10
            amount = volume * close
            rows.append(
                {
                    "date": trade_date,
                    "open": close - 0.1,
                    "high": high,
                    "low": close - 0.2,
                    "close": close,
                    "volume": volume,
                    "amount": amount,
                    "pct_chg": 1.0,
                }
            )

        rows[-1]["close"] = 15.0
        rows[-1]["high"] = 15.3
        rows[-1]["volume"] = 5000
        rows[-1]["amount"] = 75_000

        df = pd.DataFrame(rows)
        self.db.save_daily_data(df, "600519", data_source="test")

        universe_df = pd.DataFrame(
            [{"code": "600519", "name": "Kweichow Moutai", "is_st": False, "list_date": date(2020, 1, 1)}]
        )
        snapshot_df = self.service.build_factor_snapshot(universe_df=universe_df, trade_date=rows[-1]["date"])

        self.assertEqual(len(snapshot_df), 1)
        row = snapshot_df.iloc[0]
        self.assertGreater(row["breakout_ratio"], 1.0)
        self.assertGreater(row["volume_ratio"], 3.0)
        self.assertEqual(row["days_since_listed"], (rows[-1]["date"] - date(2020, 1, 1)).days)

    def test_get_latest_trade_date_returns_latest_available_date(self) -> None:
        universe_df = pd.DataFrame([{"code": "000001", "name": "Ping An Bank", "is_st": False, "list_date": None}])
        self.db.create_kline_audit_run(
            run_id="audit-run-passed-20260311",
            market="cn",
            trade_date=date(2026, 3, 11),
            run_type="daily",
            trigger_type="manual",
            run_result="succeeded",
            pass_status="passed",
            rule_version="test-v1",
            window_start=date(2025, 12, 20),
            window_end=date(2026, 3, 11),
        )
        self.db.upsert_kline_audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 11),
            pass_status="passed",
            window_start=date(2025, 12, 20),
            window_end=date(2026, 3, 11),
            rule_version="test-v1",
            source_run_id="audit-run-passed-20260311",
            passed_at=None,
        )
        self.db.create_kline_audit_run(
            run_id="audit-run-not-passed-20260312",
            market="cn",
            trade_date=date(2026, 3, 12),
            run_type="daily",
            trigger_type="manual",
            run_result="degraded",
            pass_status="not_passed",
            rule_version="test-v1",
            window_start=date(2026, 1, 2),
            window_end=date(2026, 3, 12),
        )
        self.db.upsert_kline_audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 12),
            pass_status="not_passed",
            window_start=date(2026, 1, 2),
            window_end=date(2026, 3, 12),
            rule_version="test-v1",
            source_run_id="audit-run-not-passed-20260312",
            passed_at=None,
        )

        latest_date = self.service.get_latest_trade_date(universe_df)

        self.assertEqual(latest_date, date(2026, 3, 11))

    def test_get_latest_trade_date_returns_none_when_passed_window_does_not_cover_lookback(self) -> None:
        universe_df = pd.DataFrame([{"code": "000001", "name": "Ping An Bank", "is_st": False, "list_date": None}])
        self.db.create_kline_audit_run(
            run_id="audit-run-short-window-20260311",
            market="cn",
            trade_date=date(2026, 3, 11),
            run_type="daily",
            trigger_type="manual",
            run_result="succeeded",
            pass_status="passed",
            rule_version="test-v1",
            window_start=date(2026, 1, 30),
            window_end=date(2026, 3, 11),
        )
        self.db.upsert_kline_audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 11),
            pass_status="passed",
            window_start=date(2026, 1, 30),
            window_end=date(2026, 3, 11),
            rule_version="test-v1",
            source_run_id="audit-run-short-window-20260311",
            passed_at=None,
        )

        with self.assertLogs("src.services.factor_service", level="WARNING") as captured:
            latest_date = self.service.get_latest_trade_date(universe_df)

        self.assertIsNone(latest_date)
        self.assertTrue(any("factor window" in message for message in captured.output))

    def test_get_latest_trade_date_uses_market_from_universe(self) -> None:
        universe_df = pd.DataFrame(
            [{"code": "00700", "name": "Tencent", "is_st": False, "list_date": None}]
        )
        self.db.create_kline_audit_run(
            run_id="audit-run-passed-hk-20260311",
            market="hk",
            trade_date=date(2026, 3, 11),
            run_type="daily",
            trigger_type="manual",
            run_result="succeeded",
            pass_status="passed",
            rule_version="test-v1",
            window_start=date(2025, 12, 20),
            window_end=date(2026, 3, 11),
        )
        self.db.upsert_kline_audit_trade_date(
            market="hk",
            trade_date=date(2026, 3, 11),
            pass_status="passed",
            window_start=date(2025, 12, 20),
            window_end=date(2026, 3, 11),
            rule_version="test-v1",
            source_run_id="audit-run-passed-hk-20260311",
            passed_at=None,
        )

        latest_date = self.service.get_latest_trade_date(universe_df)

        self.assertEqual(latest_date, date(2026, 3, 11))

    def test_get_latest_trade_date_ignores_fail_closed_empty_universe_audit(self) -> None:
        audit_service = KlineAuditService(self.db)
        audit_service.audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 11),
            window_start=date(2025, 12, 20),
            window_end=date(2026, 3, 11),
        )
        universe_df = pd.DataFrame(
            [{"code": "000001", "name": "Ping An Bank", "is_st": False, "list_date": None}]
        )

        with self.assertLogs("src.services.factor_service", level="WARNING") as captured:
            latest_date = self.service.get_latest_trade_date(universe_df)

        self.assertIsNone(latest_date)
        self.assertTrue(any("No passed kline audit trade date found" in message for message in captured.output))

    def test_build_factor_snapshot_can_persist_snapshots(self) -> None:
        start_date = date(2026, 3, 1)
        rows = []
        for idx in range(21):
            trade_date = start_date + timedelta(days=idx)
            close = 20.0 + idx * 0.2
            rows.append(
                {
                    "date": trade_date,
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "volume": 1000 + idx * 50,
                    "amount": (1000 + idx * 50) * close,
                    "pct_chg": 1.0,
                }
            )

        df = pd.DataFrame(rows)
        self.db.save_daily_data(df, "300750", data_source="test")
        universe_df = pd.DataFrame(
            [{"code": "300750", "name": "CATL", "is_st": False, "list_date": date(2018, 6, 11)}]
        )

        snapshot_df = self.service.build_factor_snapshot(
            universe_df=universe_df,
            trade_date=rows[-1]["date"],
            persist=True,
        )

        self.assertEqual(len(snapshot_df), 1)
        stored = self.db.list_factor_snapshots(trade_date=rows[-1]["date"])
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["code"], "300750")
        self.assertIn("trend_score", stored[0])

    @patch("src.services.factor_service.get_config")
    def test_factor_service_uses_config_lookbacks_by_default(self, get_config_mock) -> None:
        get_config_mock.return_value.screening_factor_lookback_days = 120
        get_config_mock.return_value.screening_breakout_lookback_days = 30
        get_config_mock.return_value.screening_min_list_days = 180

        service = FactorService(self.db)

        self.assertEqual(service.lookback_days, 120)
        self.assertEqual(service.breakout_lookback_days, 30)
        self.assertEqual(service.min_list_days, 180)

    def test_build_risk_flags_uses_configurable_min_list_days(self) -> None:
        service = FactorService(self.db, min_list_days=180)

        risk_flags = service._build_risk_flags(
            is_st=False,
            days_since_listed=150,
            volume_ratio=1.5,
            breakout_ratio=1.0,
        )

        self.assertIn("new_listing", risk_flags)

    @patch("src.services.hot_theme_factor_enricher.HotThemeFactorEnricher.enrich_snapshot")
    def test_build_factor_snapshot_ignores_theme_context_for_local_pipeline(self, enrich_snapshot_mock) -> None:
        start_date = date(2026, 3, 1)
        rows = []
        for idx in range(21):
            trade_date = start_date + timedelta(days=idx)
            close = 20.0 + idx * 0.2
            rows.append(
                {
                    "date": trade_date,
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "volume": 1000 + idx * 50,
                    "amount": (1000 + idx * 50) * close,
                    "pct_chg": 1.0,
                }
            )

        df = pd.DataFrame(rows)
        self.db.save_daily_data(df, "300750", data_source="test")
        universe_df = pd.DataFrame(
            [{"code": "300750", "name": "Test Stock", "is_st": False, "list_date": date(2018, 6, 11)}]
        )
        theme_context = OpenClawThemeContext(
            source="openclaw",
            trade_date="2026-03-21",
            market="cn",
            themes=[
                ExternalTheme(
                    name="AI Chip",
                    heat_score=90.0,
                    confidence=0.9,
                    catalyst_summary="policy catalyst",
                    keywords=["AI", "chip", "compute"],
                    evidence=[],
                )
            ],
            accepted_at="2026-03-21T15:00:00",
        )

        class _FakeFetcherManager:
            def __init__(self) -> None:
                self.calls = []

            def get_belong_boards(self, stock_code: str):
                self.calls.append(stock_code)
                return [{"name": "AI Chip"}, {"name": "Compute"}]

        manager = _FakeFetcherManager()
        service = FactorService(
            self.db,
            lookback_days=40,
            breakout_lookback_days=20,
            theme_context=theme_context,
            fetcher_manager=manager,
        )
        enrich_snapshot_mock.side_effect = lambda snapshot, theme_context, boards, normalized_themes=None: {
            **snapshot
        }

        snapshot_df = service.build_factor_snapshot(
            universe_df=universe_df,
            trade_date=rows[-1]["date"],
        )

        self.assertEqual(len(snapshot_df), 1)
        enrich_snapshot_mock.assert_not_called()
        self.assertEqual(manager.calls, [])
        self.assertNotIn("is_hot_theme_stock", snapshot_df.columns)
        self.assertNotIn("theme_boards", snapshot_df.columns)

    def test_compute_extended_factors_exposes_shrink_pullback_fields(self) -> None:
        """新 ShrinkPullbackDetector 的输出被展平到 snapshot，且兼容字段
        ``pullback_touched_ma`` 仍然存在。

        注意：detector 前置要求多头排列 + 阶段缩量 + 收盘站回等完整结构，
        单元测试用简化 fixture 不一定能走到 ``confirmed``；这里只断言字段
        存在与类型正确，真正的状态机行为由 ``test_shrink_pullback_detector``
        覆盖。
        """
        rows = []
        start_date = date(2026, 3, 1)
        # 上涨 25 天 + 回踩 3 天 + 企稳 1 天
        uptrend_closes = [9.0 + i * 0.06 for i in range(25)]      # 9.0 → 10.44
        pullback_closes = [10.35, 10.25, 10.42]
        closes = uptrend_closes + pullback_closes
        uptrend_vol = 2000
        pullback_vol = [900, 700, 1100]

        for idx, close in enumerate(closes):
            trade_date = start_date + timedelta(days=idx)
            if idx < 25:
                vol = uptrend_vol
                low = close - 0.05
                high = close + 0.05
                open_ = close - 0.02
                pct = 0.5
            else:
                vol = pullback_vol[idx - 25]
                low = close - 0.10
                high = close + 0.08
                open_ = close + (0.1 if idx < 27 else -0.14)
                pct = -1.0 if idx < 27 else 1.6
            rows.append(
                {
                    "date": trade_date,
                    "code": "600519",
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": vol,
                    "amount": vol * close,
                    "pct_chg": pct,
                }
            )

        group = pd.DataFrame(rows)
        result = self.service._compute_extended_factors(group, group.iloc[-1], group["close"])

        self.assertIn("pullback_touched_ma", result)
        self.assertIn("shrink_pullback_state", result)
        self.assertIn("shrink_pullback_support_ma", result)
        self.assertIn("shrink_pullback_volume_shrink", result)
        self.assertIn("shrink_pullback_rebound_confirmed", result)
        self.assertIn("shrink_pullback_stop_loss_price", result)
        self.assertIsInstance(result["shrink_pullback_state"], str)
        self.assertIsInstance(result["pullback_touched_ma"], bool)


if __name__ == "__main__":
    unittest.main()
