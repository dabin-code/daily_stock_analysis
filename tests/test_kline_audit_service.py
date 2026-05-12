import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from src.config import Config
from src.services.kline_audit_service import KlineAuditService
from src.storage import DatabaseManager


def _build_daily_rows(trade_dates: list[date], close: float = 10.0) -> pd.DataFrame:
    rows = []
    for idx, trade_date in enumerate(trade_dates):
        price = close + idx * 0.1
        rows.append(
            {
                "date": trade_date,
                "open": price - 0.1,
                "high": price + 0.1,
                "low": price - 0.2,
                "close": price,
                "volume": 1000 + idx * 10,
                "amount": (1000 + idx * 10) * price,
                "pct_chg": 1.0,
            }
        )
    return pd.DataFrame(rows)


class KlineAuditServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_kline_audit_service.db")
        os.environ["DATABASE_PATH"] = self._db_path

        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self.service = KlineAuditService(self.db)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def _seed_active_instruments(self) -> None:
        self.db.upsert_instruments(
            [
                {
                    "code": "000001",
                    "name": "Ping An Bank",
                    "market": "cn",
                    "exchange": "SZSE",
                    "listing_status": "active",
                    "is_st": False,
                    "industry": "Bank",
                    "list_date": date(2000, 1, 1),
                },
                {
                    "code": "000002",
                    "name": "Vanke",
                    "market": "cn",
                    "exchange": "SZSE",
                    "listing_status": "active",
                    "is_st": False,
                    "industry": "Property",
                    "list_date": date(2000, 1, 1),
                },
            ]
        )

    def test_audit_service_builds_market_day_gap_for_sparse_trade_date(self) -> None:
        self._seed_active_instruments()
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10), date(2026, 3, 11)]),
            "000001",
            data_source="test",
        )
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10)]),
            "000002",
            data_source="test",
        )

        result = self.service.audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 11),
            window_start=date(2026, 3, 10),
            window_end=date(2026, 3, 11),
        )

        self.assertEqual(result["run_result"], "degraded")
        market_day_gaps = [
            gap for gap in self.db.list_kline_audit_gaps(market="cn", gap_scope="market_day_gap")
            if gap.trade_date == date(2026, 3, 11)
        ]
        self.assertEqual(len(market_day_gaps), 1)
        self.assertEqual(market_day_gaps[0].status, "open")

    def test_audit_service_builds_market_day_gap_when_target_trade_date_has_no_rows(self) -> None:
        self._seed_active_instruments()
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10)]),
            "000001",
            data_source="test",
        )
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10)]),
            "000002",
            data_source="test",
        )

        result = self.service.audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 11),
            window_start=date(2026, 3, 10),
            window_end=date(2026, 3, 11),
        )

        self.assertEqual(result["run_result"], "degraded")
        market_day_gaps = [
            gap for gap in self.db.list_kline_audit_gaps(market="cn", gap_scope="market_day_gap")
            if gap.trade_date == date(2026, 3, 11)
        ]
        self.assertEqual(len(market_day_gaps), 1)
        self.assertEqual(market_day_gaps[0].status, "open")

    def test_audit_service_builds_market_day_gap_for_historical_full_missing_day(self) -> None:
        self._seed_active_instruments()
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10), date(2026, 3, 12)]),
            "000001",
            data_source="test",
        )
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10), date(2026, 3, 12)]),
            "000002",
            data_source="test",
        )

        result = self.service.audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 12),
            window_start=date(2026, 3, 10),
            window_end=date(2026, 3, 12),
        )

        self.assertEqual(result["run_result"], "degraded")
        market_day_gaps = [
            gap for gap in self.db.list_kline_audit_gaps(market="cn", gap_scope="market_day_gap")
            if gap.trade_date == date(2026, 3, 11)
        ]
        self.assertEqual(len(market_day_gaps), 1)

    def test_audit_service_builds_market_day_gap_for_leading_missing_window_days(self) -> None:
        self._seed_active_instruments()
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 12), date(2026, 3, 13)]),
            "000001",
            data_source="test",
        )
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 12), date(2026, 3, 13)]),
            "000002",
            data_source="test",
        )

        result = self.service.audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 13),
            window_start=date(2026, 3, 10),
            window_end=date(2026, 3, 13),
        )

        self.assertEqual(result["run_result"], "degraded")
        market_day_gaps = self.db.list_kline_audit_gaps(market="cn", gap_scope="market_day_gap")
        gap_dates = [gap.trade_date for gap in market_day_gaps]
        self.assertEqual(gap_dates, [date(2026, 3, 10), date(2026, 3, 11)])

    def test_audit_service_splits_non_contiguous_symbol_gaps_into_ranges(self) -> None:
        self._seed_active_instruments()
        window_dates = [
            date(2026, 3, 10),
            date(2026, 3, 11),
            date(2026, 3, 12),
            date(2026, 3, 13),
            date(2026, 3, 14),
        ]
        self.db.save_daily_data(_build_daily_rows(window_dates), "000001", data_source="test")
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10), date(2026, 3, 12), date(2026, 3, 14)]),
            "000002",
            data_source="test",
        )

        self.service.audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 14),
            window_start=date(2026, 3, 10),
            window_end=date(2026, 3, 14),
        )

        symbol_range_gaps = [
            gap for gap in self.db.list_kline_audit_gaps(market="cn", gap_scope="symbol_range_gap")
            if gap.code == "000002"
        ]
        ranges = [(gap.missing_date_from, gap.missing_date_to) for gap in symbol_range_gaps]
        self.assertEqual(
            ranges,
            [
                (date(2026, 3, 11), date(2026, 3, 11)),
                (date(2026, 3, 13), date(2026, 3, 13)),
            ],
        )

    def test_audit_service_keeps_weekend_spanning_symbol_gap_as_single_range(self) -> None:
        self._seed_active_instruments()
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 13), date(2026, 3, 16)]),
            "000001",
            data_source="test",
        )

        self.service.audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 16),
            window_start=date(2026, 3, 13),
            window_end=date(2026, 3, 16),
        )

        symbol_range_gaps = [
            gap for gap in self.db.list_kline_audit_gaps(market="cn", gap_scope="symbol_range_gap")
            if gap.code == "000002"
        ]
        self.assertEqual(len(symbol_range_gaps), 1)
        self.assertEqual(symbol_range_gaps[0].missing_date_from, date(2026, 3, 13))
        self.assertEqual(symbol_range_gaps[0].missing_date_to, date(2026, 3, 16))

    def test_audit_service_ignores_non_trading_target_date_for_gap_detection(self) -> None:
        self._seed_active_instruments()
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 13)]),
            "000001",
            data_source="test",
        )
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 13)]),
            "000002",
            data_source="test",
        )

        with patch.object(
            KlineAuditService,
            "_is_market_session",
            side_effect=lambda *, market, trade_date: trade_date.weekday() < 5,
        ):
            result = self.service.audit_trade_date(
                market="cn",
                trade_date=date(2026, 3, 14),
                window_start=date(2026, 3, 13),
                window_end=date(2026, 3, 16),
            )

        self.assertEqual(result["run_result"], "degraded")
        gap_dates = [
            gap.trade_date
            for gap in self.db.list_kline_audit_gaps(market="cn", gap_scope="market_day_gap")
        ]
        self.assertNotIn(date(2026, 3, 14), gap_dates)
        self.assertNotIn(date(2026, 3, 15), gap_dates)
        self.assertEqual(gap_dates, [date(2026, 3, 16)])

    def test_audit_service_ignores_observed_weekend_rows_when_building_symbol_gaps(self) -> None:
        self._seed_active_instruments()
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 13), date(2026, 3, 14), date(2026, 3, 16)]),
            "000001",
            data_source="test",
        )
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 14)]),
            "000002",
            data_source="test",
        )

        with patch.object(
            KlineAuditService,
            "_is_market_session",
            side_effect=lambda *, market, trade_date: trade_date.weekday() < 5,
        ):
            self.service.audit_trade_date(
                market="cn",
                trade_date=date(2026, 3, 16),
                window_start=date(2026, 3, 13),
                window_end=date(2026, 3, 16),
            )

        symbol_range_gaps = [
            gap
            for gap in self.db.list_kline_audit_gaps(market="cn", gap_scope="symbol_range_gap")
            if gap.code == "000002"
        ]
        ranges = [(gap.missing_date_from, gap.missing_date_to) for gap in symbol_range_gaps]
        self.assertEqual(ranges, [(date(2026, 3, 13), date(2026, 3, 16))])

    def test_audit_service_ignores_weekday_market_holiday_when_building_gaps(self) -> None:
        self._seed_active_instruments()
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10), date(2026, 3, 12)]),
            "000001",
            data_source="test",
        )
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10), date(2026, 3, 12)]),
            "000002",
            data_source="test",
        )

        with patch.object(
            KlineAuditService,
            "_is_market_session",
            side_effect=lambda *, market, trade_date: trade_date.weekday() < 5
            and trade_date != date(2026, 3, 11),
        ):
            result = self.service.audit_trade_date(
                market="cn",
                trade_date=date(2026, 3, 12),
                window_start=date(2026, 3, 10),
                window_end=date(2026, 3, 12),
            )

        self.assertEqual(result["run_result"], "succeeded")
        self.assertEqual(result["pass_status"], "passed")
        self.assertEqual(self.db.list_kline_audit_gaps(market="cn"), [])

    def test_audit_service_does_not_pass_non_trading_target_date_with_complete_window(self) -> None:
        self._seed_active_instruments()
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 13)]),
            "000001",
            data_source="test",
        )
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 13)]),
            "000002",
            data_source="test",
        )

        with patch.object(
            KlineAuditService,
            "_is_market_session",
            side_effect=lambda *, market, trade_date: trade_date.weekday() < 5,
        ):
            result = self.service.audit_trade_date(
                market="cn",
                trade_date=date(2026, 3, 14),
                window_start=date(2026, 3, 13),
                window_end=date(2026, 3, 14),
            )

        self.assertEqual(result["run_result"], "degraded")
        self.assertEqual(result["pass_status"], "not_passed")
        self.assertEqual(self.db.list_kline_audit_gaps(market="cn"), [])

        trade_date_row = self.db.get_kline_audit_trade_date(market="cn", trade_date=date(2026, 3, 14))
        self.assertIsNotNone(trade_date_row)
        self.assertEqual(trade_date_row.pass_status, "not_passed")
        self.assertIsNone(trade_date_row.passed_at)

    def test_audit_service_marks_trade_date_not_passed_when_run_degraded(self) -> None:
        self._seed_active_instruments()
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10), date(2026, 3, 11)]),
            "000001",
            data_source="test",
        )

        result = self.service.audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 11),
            window_start=date(2026, 3, 10),
            window_end=date(2026, 3, 11),
        )

        self.assertEqual(result["run_result"], "degraded")
        self.assertEqual(result["pass_status"], "not_passed")

        trade_date_row = self.db.get_kline_audit_trade_date(market="cn", trade_date=date(2026, 3, 11))
        self.assertIsNotNone(trade_date_row)
        self.assertEqual(trade_date_row.pass_status, "not_passed")
        self.assertEqual(trade_date_row.window_start, date(2026, 3, 10))
        self.assertEqual(trade_date_row.window_end, date(2026, 3, 11))
        self.assertEqual(trade_date_row.source_run_id, result["run_id"])
        self.assertIsNone(trade_date_row.passed_at)

    def test_audit_service_fail_closes_when_expected_universe_is_empty(self) -> None:
        result = self.service.audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 11),
            window_start=date(2025, 12, 20),
            window_end=date(2026, 3, 11),
        )

        self.assertNotEqual(result["pass_status"], "passed")
        trade_date_row = self.db.get_kline_audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 11),
        )
        self.assertIsNotNone(trade_date_row)
        self.assertEqual(trade_date_row.pass_status, "not_passed")
        self.assertIsNone(trade_date_row.passed_at)

    def test_audit_service_excludes_pre_listing_missing_dates_from_gaps(self) -> None:
        self.db.upsert_instruments(
            [
                {
                    "code": "000001",
                    "name": "Ping An Bank",
                    "market": "cn",
                    "exchange": "SZSE",
                    "listing_status": "active",
                    "is_st": False,
                    "industry": "Bank",
                    "list_date": date(2000, 1, 1),
                },
                {
                    "code": "000002",
                    "name": "New Listing",
                    "market": "cn",
                    "exchange": "SZSE",
                    "listing_status": "active",
                    "is_st": False,
                    "industry": "Tech",
                    "list_date": date(2026, 3, 11),
                },
            ]
        )
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10), date(2026, 3, 11)]),
            "000001",
            data_source="test",
        )
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 11)]),
            "000002",
            data_source="test",
        )

        result = self.service.audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 11),
            window_start=date(2026, 3, 10),
            window_end=date(2026, 3, 11),
        )

        self.assertEqual(result["run_result"], "succeeded")
        self.assertEqual(result["pass_status"], "passed")
        self.assertEqual(self.db.list_kline_audit_gaps(market="cn"), [])

    def test_audit_service_excludes_non_active_symbols_from_gaps(self) -> None:
        self.db.upsert_instruments(
            [
                {
                    "code": "000001",
                    "name": "Ping An Bank",
                    "market": "cn",
                    "exchange": "SZSE",
                    "listing_status": "active",
                    "is_st": False,
                    "industry": "Bank",
                    "list_date": date(2000, 1, 1),
                },
                {
                    "code": "000003",
                    "name": "Suspended Symbol",
                    "market": "cn",
                    "exchange": "SZSE",
                    "listing_status": "delisted",
                    "is_st": False,
                    "industry": "Legacy",
                    "list_date": date(2000, 1, 1),
                },
            ]
        )
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10), date(2026, 3, 11)]),
            "000001",
            data_source="test",
        )

        result = self.service.audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 11),
            window_start=date(2026, 3, 10),
            window_end=date(2026, 3, 11),
        )

        self.assertEqual(result["run_result"], "succeeded")
        self.assertEqual(result["pass_status"], "passed")
        self.assertEqual(self.db.list_kline_audit_gaps(market="cn"), [])

    def test_audit_service_excludes_st_symbols_from_cn_universe(self) -> None:
        self._seed_active_instruments()
        self.db.upsert_instruments(
            [
                {
                    "code": "000004",
                    "name": "*ST Test",
                    "market": "cn",
                    "exchange": "SZSE",
                    "listing_status": "active",
                    "is_st": True,
                    "industry": "Test",
                    "list_date": date(2000, 1, 1),
                }
            ]
        )
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10), date(2026, 3, 11)]),
            "000001",
            data_source="test",
        )
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10), date(2026, 3, 11)]),
            "000002",
            data_source="test",
        )

        result = self.service.audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 11),
            window_start=date(2026, 3, 10),
            window_end=date(2026, 3, 11),
        )

        self.assertEqual(result["run_result"], "succeeded")
        self.assertEqual(result["pass_status"], "passed")
        self.assertEqual(self.db.list_kline_audit_gaps(market="cn"), [])

    def test_audit_service_applies_approved_market_day_skip(self) -> None:
        self._seed_active_instruments()
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10), date(2026, 3, 11)]),
            "000001",
            data_source="test",
        )
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10)]),
            "000002",
            data_source="test",
        )
        self.db.upsert_kline_skip_registry(
            market="cn",
            gap_scope="market_day_gap",
            trade_date=date(2026, 3, 11),
            status="approved_skip",
            reason_type="manual_approval",
            notes="approved missing market day",
        )

        result = self.service.audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 11),
            window_start=date(2026, 3, 10),
            window_end=date(2026, 3, 11),
        )

        self.assertEqual(result["run_result"], "succeeded")
        self.assertEqual(result["pass_status"], "passed")
        self.assertEqual(self.db.list_kline_audit_gaps(market="cn"), [])

    def test_audit_service_applies_approved_symbol_range_skip(self) -> None:
        self._seed_active_instruments()
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10), date(2026, 3, 11)]),
            "000001",
            data_source="test",
        )
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10)]),
            "000002",
            data_source="test",
        )
        self.db.upsert_kline_skip_registry(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000002",
            missing_date_from=date(2026, 3, 11),
            missing_date_to=date(2026, 3, 11),
            status="approved_skip",
            reason_type="manual_approval",
            notes="approved symbol range gap",
        )

        result = self.service.audit_trade_date(
            market="cn",
            trade_date=date(2026, 3, 11),
            window_start=date(2026, 3, 10),
            window_end=date(2026, 3, 11),
        )

        self.assertEqual(result["run_result"], "succeeded")
        self.assertEqual(result["pass_status"], "passed")
        self.assertEqual(self.db.list_kline_audit_gaps(market="cn"), [])

    def test_audit_service_rolls_back_all_persistence_when_event_append_fails(self) -> None:
        self._seed_active_instruments()
        self.db.save_daily_data(
            _build_daily_rows([date(2026, 3, 10), date(2026, 3, 11)]),
            "000001",
            data_source="test",
        )

        with patch.object(self.db, "_append_kline_audit_event_in_session", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.service.audit_trade_date(
                    market="cn",
                    trade_date=date(2026, 3, 11),
                    window_start=date(2026, 3, 10),
                    window_end=date(2026, 3, 11),
                )

        self.assertEqual(self.db.list_kline_audit_runs(market="cn"), [])
        self.assertIsNone(
            self.db.get_kline_audit_trade_date(
                market="cn",
                trade_date=date(2026, 3, 11),
            )
        )
        self.assertEqual(self.db.list_kline_audit_gaps(market="cn"), [])


if __name__ == "__main__":
    unittest.main()
