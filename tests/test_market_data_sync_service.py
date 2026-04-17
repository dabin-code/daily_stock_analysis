import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from data_provider.base import DataFetchError
from src.config import Config
from src.services.market_data_sync_service import MarketDataSyncService
from src.storage import DatabaseManager


class _StubDataFetcherManager:
    def __init__(self, frames):
        self.frames = frames
        self.calls = []

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30):
        self.calls.append((stock_code, start_date, end_date, days))
        return self.frames[stock_code].copy(), "StubFetcher"


class _ExceptionDataFetcherManager:
    def __init__(self, error_by_code):
        self.error_by_code = error_by_code
        self.calls = []

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30):
        self.calls.append((stock_code, start_date, end_date, days))
        raise self.error_by_code[stock_code]


class MarketDataSyncServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_market_data_sync.db")
        os.environ["DATABASE_PATH"] = self._db_path

        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()

        # 隔离 Tushare 批量同步，避免真实 API 调用干扰单元测试
        self._bulk_sync_patcher = patch.object(
            MarketDataSyncService, "_try_bulk_sync", return_value=None
        )
        self._bulk_sync_patcher.start()

        self.db.upsert_instruments(
            [
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "market": "cn",
                    "exchange": "SSE",
                    "listing_status": "active",
                    "is_st": False,
                    "industry": "白酒",
                    "list_date": date(2001, 8, 27),
                },
                {
                    "code": "000001",
                    "name": "平安银行",
                    "market": "cn",
                    "exchange": "SZSE",
                    "listing_status": "active",
                    "is_st": False,
                    "industry": "银行",
                    "list_date": date(1991, 4, 3),
                },
            ]
        )

    def tearDown(self) -> None:
        self._bulk_sync_patcher.stop()
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def test_sync_trade_date_persists_daily_data_for_active_universe(self) -> None:
        target_date = date(2026, 3, 13)
        frames = {
            "600519": pd.DataFrame(
                [
                    {
                        "date": target_date,
                        "open": 1500.0,
                        "high": 1520.0,
                        "low": 1490.0,
                        "close": 1510.0,
                        "volume": 1000,
                        "amount": 1_510_000,
                        "pct_chg": 2.2,
                    }
                ]
            ),
            "000001": pd.DataFrame(
                [
                    {
                        "date": target_date,
                        "open": 12.0,
                        "high": 12.3,
                        "low": 11.9,
                        "close": 12.1,
                        "volume": 2000,
                        "amount": 24_200,
                        "pct_chg": 1.1,
                    }
                ]
            ),
        }
        fetcher_manager = _StubDataFetcherManager(frames)
        service = MarketDataSyncService(db_manager=self.db, fetcher_manager=fetcher_manager)

        result = service.sync_trade_date(trade_date=target_date)

        self.assertEqual(result["synced"], 2)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(fetcher_manager.calls), 2)
        latest = self.db.get_latest_data("600519", days=1)
        self.assertEqual(latest[0].date, target_date)
        self.assertEqual(latest[0].data_source, "StubFetcher")

    def test_sync_trade_date_skips_codes_with_existing_target_date_when_not_force(self) -> None:
        target_date = date(2026, 3, 13)
        existing = pd.DataFrame(
            [
                {
                    "date": target_date,
                    "open": 1500.0,
                    "high": 1520.0,
                    "low": 1490.0,
                    "close": 1510.0,
                    "volume": 1000,
                    "amount": 1_510_000,
                    "pct_chg": 2.2,
                }
            ]
        )
        self.db.save_daily_data(existing, "600519", data_source="Existing")

        frames = {
            "600519": existing,
            "000001": pd.DataFrame(
                [
                    {
                        "date": target_date,
                        "open": 12.0,
                        "high": 12.3,
                        "low": 11.9,
                        "close": 12.1,
                        "volume": 2000,
                        "amount": 24_200,
                        "pct_chg": 1.1,
                    }
                ]
            ),
        }
        fetcher_manager = _StubDataFetcherManager(frames)
        service = MarketDataSyncService(db_manager=self.db, fetcher_manager=fetcher_manager)

        result = service.sync_trade_date(trade_date=target_date, force=False)

        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(fetcher_manager.calls[0][0], "000001")

    def test_sync_trade_date_returns_health_report_for_missing_codes(self) -> None:
        target_date = date(2026, 3, 13)
        frames = {
            "600519": pd.DataFrame(
                [
                    {
                        "date": target_date,
                        "open": 1500.0,
                        "high": 1520.0,
                        "low": 1490.0,
                        "close": 1510.0,
                        "volume": 1000,
                        "amount": 1_510_000,
                        "pct_chg": 2.2,
                    }
                ]
            ),
            "000001": pd.DataFrame(),
        }
        fetcher_manager = _StubDataFetcherManager(frames)
        service = MarketDataSyncService(db_manager=self.db, fetcher_manager=fetcher_manager)

        result = service.sync_trade_date(trade_date=target_date)

        self.assertEqual(result["synced"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("health_report", result)
        self.assertEqual(result["health_report"]["expected_count"], 2)
        self.assertEqual(result["health_report"]["available_count"], 1)
        self.assertEqual(result["health_report"]["missing_count"], 1)
        self.assertEqual(result["health_report"]["error_count"], 1)
        self.assertEqual(result["health_report"]["missing_codes"], ["000001"])
        self.assertEqual(result["health_report"]["success_rate"], 0.5)

    def test_sync_trade_date_force_update_counts_existing_row_as_success(self) -> None:
        target_date = date(2026, 3, 13)
        existing = pd.DataFrame(
            [
                {
                    "date": target_date,
                    "open": 1500.0,
                    "high": 1520.0,
                    "low": 1490.0,
                    "close": 1510.0,
                    "volume": 1000,
                    "amount": 1_510_000,
                    "pct_chg": 2.2,
                }
            ]
        )
        self.db.save_daily_data(existing, "600519", data_source="Existing")

        frames = {"600519": existing}
        fetcher_manager = _StubDataFetcherManager(frames)
        service = MarketDataSyncService(db_manager=self.db, fetcher_manager=fetcher_manager)

        result = service.sync_trade_date(trade_date=target_date, stock_codes=["600519"], force=True)

        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["health_report"]["available_count"], 1)

    def test_sync_trade_date_force_failure_reports_zero_refresh_success_rate(self) -> None:
        target_date = date(2026, 3, 13)
        existing = pd.DataFrame(
            [
                {
                    "date": target_date,
                    "open": 1500.0,
                    "high": 1520.0,
                    "low": 1490.0,
                    "close": 1510.0,
                    "volume": 1000,
                    "amount": 1_510_000,
                    "pct_chg": 2.2,
                }
            ]
        )
        self.db.save_daily_data(existing, "600519", data_source="Existing")

        frames = {"600519": pd.DataFrame()}
        fetcher_manager = _StubDataFetcherManager(frames)
        service = MarketDataSyncService(db_manager=self.db, fetcher_manager=fetcher_manager)

        result = service.sync_trade_date(trade_date=target_date, stock_codes=["600519"], force=True)

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["health_report"]["available_count"], 1)
        self.assertEqual(result["health_report"]["refresh_success_count"], 0)
        self.assertEqual(result["health_report"]["refresh_success_rate"], 0.0)

    def test_sync_trade_date_normalizes_fetcher_exception_to_empty_data_reason(self) -> None:
        target_date = date(2026, 3, 13)
        fetcher_manager = _ExceptionDataFetcherManager(
            {
                "600519": DataFetchError(
                    "所有数据源获取 600519 失败:\n"
                    "[AkShareFetcher] (DataFetchError) 未找到数据源\n"
                    "[TushareFetcher] (DataFetchError) 未获取到数据"
                )
            }
        )
        service = MarketDataSyncService(db_manager=self.db, fetcher_manager=fetcher_manager)

        result = service.sync_trade_date(trade_date=target_date, stock_codes=["600519"])

        self.assertEqual(result["synced"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["code"], "600519")
        self.assertEqual(result["errors"][0]["reason"], "empty_data")
        self.assertIn("所有数据源获取 600519 失败", result["errors"][0]["detail"])

    def test_sync_trade_date_keeps_timeout_like_fetch_errors_as_blocking_reason(self) -> None:
        target_date = date(2026, 3, 13)
        fetcher_manager = _ExceptionDataFetcherManager(
            {
                "600519": DataFetchError(
                    "所有数据源获取 600519 失败:\n"
                    "[AkShareFetcher] (TimeoutError) provider timeout\n"
                    "[TushareFetcher] (ConnectionError) connect failed"
                )
            }
        )
        service = MarketDataSyncService(db_manager=self.db, fetcher_manager=fetcher_manager)

        result = service.sync_trade_date(trade_date=target_date, stock_codes=["600519"])

        self.assertEqual(result["synced"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["code"], "600519")
        self.assertEqual(result["errors"][0]["reason"], "fetch_failed")
        self.assertIn("provider timeout", result["errors"][0]["detail"])

    def test_sync_trade_date_treats_mixed_no_data_and_timeout_chain_as_blocking(self) -> None:
        target_date = date(2026, 3, 13)
        fetcher_manager = _ExceptionDataFetcherManager(
            {
                "600519": DataFetchError(
                    "所有数据源获取 600519 失败:\n"
                    "[AkShareFetcher] (DataFetchError) 未找到数据源\n"
                    "[TushareFetcher] (TimeoutError) provider timeout"
                )
            }
        )
        service = MarketDataSyncService(db_manager=self.db, fetcher_manager=fetcher_manager)

        result = service.sync_trade_date(trade_date=target_date, stock_codes=["600519"])

        self.assertEqual(result["errors"][0]["reason"], "fetch_failed")
        self.assertIn("未找到数据源", result["errors"][0]["detail"])
        self.assertIn("provider timeout", result["errors"][0]["detail"])

    def test_sync_trade_date_returns_retryable_vs_blocking_vs_skip_eligible_metadata(self) -> None:
        target_date = date(2026, 3, 13)
        fetcher_manager = _ExceptionDataFetcherManager(
            {
                "600519": DataFetchError(
                    "所有数据源获取 600519 失败:\n"
                    "[AkShareFetcher] (DataFetchError) 未获取到数据\n"
                    "[TushareFetcher] (DataFetchError) no data"
                ),
                "000001": DataFetchError(
                    "所有数据源获取 000001 失败:\n"
                    "[AkShareFetcher] (TimeoutError) provider timeout\n"
                    "[TushareFetcher] (ConnectionError) connect failed"
                ),
                "000002": DataFetchError(
                    "所有数据源获取 000002 失败:\n"
                    "[AkShareFetcher] (DataFetchError) no data\n"
                    "[TushareFetcher] (TimeoutError) provider timeout"
                ),
                "000003": RuntimeError("unexpected parser failure"),
            }
        )
        service = MarketDataSyncService(db_manager=self.db, fetcher_manager=fetcher_manager)

        result = service.sync_trade_date(
            trade_date=target_date,
            stock_codes=["600519", "000001", "000002", "000003"],
        )

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["target_trade_date"], target_date.isoformat())
        self.assertEqual(result["health_report"]["target_trade_date"], target_date.isoformat())
        self.assertEqual(len(result["errors"]), 4)

        errors_by_code = {item["code"]: item for item in result["errors"]}

        skip_eligible_error = errors_by_code["600519"]
        self.assertEqual(skip_eligible_error["target_trade_date"], target_date.isoformat())
        self.assertEqual(skip_eligible_error["reason_class"], "skip_eligible")
        self.assertTrue(skip_eligible_error["candidate_skip"]["eligible"])
        self.assertGreaterEqual(len(skip_eligible_error["source_attempts"]), 2)
        self.assertEqual(skip_eligible_error["source_attempts"][0]["source"], "AkShareFetcher")
        self.assertEqual(skip_eligible_error["source_attempts"][0]["reason_class"], "skip_eligible")

        retryable_error = errors_by_code["000001"]
        self.assertEqual(retryable_error["reason_class"], "retryable")
        self.assertFalse(retryable_error["candidate_skip"]["eligible"])
        self.assertGreaterEqual(len(retryable_error["source_attempts"]), 2)
        self.assertEqual(retryable_error["source_attempts"][0]["source"], "AkShareFetcher")
        self.assertEqual(retryable_error["source_attempts"][0]["reason_class"], "retryable")
        self.assertIn("provider timeout", retryable_error["detail"])

        mixed_error = errors_by_code["000002"]
        self.assertEqual(mixed_error["reason_class"], "blocking")
        self.assertFalse(mixed_error["candidate_skip"]["eligible"])
        self.assertEqual(mixed_error["candidate_skip"]["signal"], "needs_manual_review")
        self.assertGreaterEqual(len(mixed_error["source_attempts"]), 2)
        self.assertEqual(
            {item["reason_class"] for item in mixed_error["source_attempts"]},
            {"skip_eligible", "retryable"},
        )

        blocking_error = errors_by_code["000003"]
        self.assertEqual(blocking_error["reason_class"], "blocking")
        self.assertFalse(blocking_error["candidate_skip"]["eligible"])
        self.assertEqual(blocking_error["target_trade_date"], target_date.isoformat())
        self.assertEqual(blocking_error["detail"], "unexpected parser failure")
        self.assertEqual(blocking_error["source_attempts"][0]["reason_class"], "blocking")
        self.assertEqual(result["health_report"]["reason_class_counts"]["skip_eligible"], 1)
        self.assertEqual(result["health_report"]["reason_class_counts"]["retryable"], 1)
        self.assertEqual(result["health_report"]["reason_class_counts"]["blocking"], 2)

    def test_sync_trade_date_does_not_treat_80_percent_coverage_as_healthy(self) -> None:
        target_date = date(2026, 3, 13)
        existing = pd.DataFrame(
            [
                {
                    "date": target_date,
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "volume": 1000,
                    "amount": 10_200,
                    "pct_chg": 1.0,
                }
            ]
        )
        existing_codes = ["600519", "000001", "000002", "000003"]
        for code in existing_codes:
            self.db.save_daily_data(existing, code, data_source="Existing")

        fetcher_manager = _ExceptionDataFetcherManager(
            {
                "000004": DataFetchError(
                    "所有数据源获取 000004 失败:\n"
                    "[AkShareFetcher] (TimeoutError) provider timeout\n"
                    "[TushareFetcher] (ConnectionError) connect failed"
                )
            }
        )
        service = MarketDataSyncService(db_manager=self.db, fetcher_manager=fetcher_manager)

        result = service.sync_trade_date(
            trade_date=target_date,
            stock_codes=["600519", "000001", "000002", "000003", "000004"],
            min_available_ratio=0.8,
        )

        self.assertEqual(fetcher_manager.calls, [("000004", target_date.isoformat(), target_date.isoformat(), 1)])
        self.assertEqual(result["target_trade_date"], target_date.isoformat())
        self.assertEqual(result["health_report"]["available_count"], 4)
        self.assertEqual(result["health_report"]["missing_count"], 1)
        self.assertEqual(result["health_report"]["success_rate"], 0.8)
        self.assertFalse(result["health_report"]["is_healthy"])
        self.assertEqual(result["health_report"]["target_trade_date"], target_date.isoformat())
        self.assertEqual(result["health_report"]["status"], "degraded")
        self.assertEqual(result["health_report"]["reason_class_counts"]["retryable"], 1)
        self.assertEqual(result["errors"][0]["code"], "000004")
        self.assertEqual(result["errors"][0]["reason_class"], "retryable")

    def test_sync_trade_date_treats_unexpected_timeout_wording_as_blocking(self) -> None:
        target_date = date(2026, 3, 13)
        fetcher_manager = _ExceptionDataFetcherManager(
            {
                "600519": RuntimeError("parser timeout sentinel leaked from sync service"),
            }
        )
        service = MarketDataSyncService(db_manager=self.db, fetcher_manager=fetcher_manager)

        result = service.sync_trade_date(trade_date=target_date, stock_codes=["600519"])

        self.assertEqual(result["synced"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["code"], "600519")
        self.assertEqual(result["errors"][0]["reason"], "unexpected_error")
        self.assertEqual(result["errors"][0]["reason_class"], "blocking")
        self.assertFalse(result["errors"][0]["candidate_skip"]["eligible"])

    def test_sync_trade_date_treats_internal_chinese_timeout_error_as_retryable(self) -> None:
        target_date = date(2026, 3, 13)
        service = MarketDataSyncService(db_manager=self.db, fetcher_manager=_StubDataFetcherManager({}))

        with patch.object(
            MarketDataSyncService,
            "_fetch_single_with_timeout",
            side_effect=DataFetchError("获取 600519 超时（60s）"),
        ):
            result = service.sync_trade_date(trade_date=target_date, stock_codes=["600519"])

        self.assertEqual(result["synced"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["reason"], "fetch_failed")
        self.assertEqual(result["errors"][0]["reason_class"], "retryable")
        self.assertEqual(result["health_report"]["reason_class_counts"]["retryable"], 1)


if __name__ == "__main__":
    unittest.main()
