import sqlite3
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from src.services.fast_backfill_service import FastBackfillService


def _init_stock_daily(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE stock_daily (
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                amount REAL,
                pct_chg REAL,
                data_source TEXT,
                created_at TEXT,
                updated_at TEXT,
                PRIMARY KEY (code, date)
            )
            """
        )
        rows = [
            ("000001", "2026-05-08"),
            ("000002", "2026-05-08"),
            ("000001", "2026-05-11"),
        ]
        conn.executemany(
            "INSERT INTO stock_daily (code, date) VALUES (?, ?)",
            rows,
        )


class _FakeTushareApi:
    def __init__(self) -> None:
        self.requested_dates: list[str] = []

    def daily(self, trade_date: str) -> pd.DataFrame:
        self.requested_dates.append(trade_date)
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": trade_date,
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "vol": 1000,
                    "amount": 2000,
                    "pct_chg": 1.5,
                },
                {
                    "ts_code": "000002.SZ",
                    "trade_date": trade_date,
                    "open": 20,
                    "high": 21,
                    "low": 19,
                    "close": 20.5,
                    "vol": 3000,
                    "amount": 4000,
                    "pct_chg": 2.5,
                },
            ]
        )


def test_fast_backfill_to_trade_date_fills_incomplete_dates_until_target(tmp_path):
    db_path = tmp_path / "stock_analysis.db"
    _init_stock_daily(db_path)
    api = _FakeTushareApi()
    governance_service = SimpleNamespace(
        run_daily_governance=lambda **kwargs: {
            "run_result": "succeeded",
            "pass_status": "passed",
            "trade_date": kwargs["trade_date"],
        }
    )

    service = FastBackfillService(
        db_path=str(db_path),
        tushare_api=api,
        governance_service=governance_service,
        min_full_count=2,
        sleep=lambda _seconds: None,
    )

    result = service.backfill_to_trade_date(date(2026, 5, 12))

    assert api.requested_dates == ["20260511", "20260512"]
    assert result["status"] == "completed"
    assert result["target_trade_date"] == "2026-05-12"
    assert result["backfilled_dates"] == ["2026-05-11", "2026-05-12"]
    assert result["saved_rows"] == 4
    assert result["failed_dates"] == []
    assert result["governance_result"]["pass_status"] == "passed"

    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM stock_daily WHERE date='2026-05-12'"
        ).fetchone()[0]
    assert count == 2


def test_fast_backfill_to_trade_date_noops_when_target_already_complete(tmp_path):
    db_path = tmp_path / "stock_analysis.db"
    _init_stock_daily(db_path)
    api = _FakeTushareApi()
    governance_calls = []
    governance_service = SimpleNamespace(
        run_daily_governance=lambda **kwargs: governance_calls.append(kwargs) or {
            "run_result": "succeeded",
            "pass_status": "passed",
        }
    )

    service = FastBackfillService(
        db_path=str(db_path),
        tushare_api=api,
        governance_service=governance_service,
        min_full_count=2,
        sleep=lambda _seconds: None,
    )

    result = service.backfill_to_trade_date(date(2026, 5, 8))

    assert api.requested_dates == []
    assert result["status"] == "already_complete"
    assert result["backfilled_dates"] == []
    assert governance_calls[0]["trade_date"] == date(2026, 5, 8)


def test_fast_backfill_to_trade_date_refills_earlier_incomplete_target(tmp_path):
    db_path = tmp_path / "stock_analysis.db"
    _init_stock_daily(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO stock_daily (code, date) VALUES (?, ?)",
            [
                ("000001", "2026-05-12"),
                ("000002", "2026-05-12"),
            ],
        )
    api = _FakeTushareApi()
    governance_service = SimpleNamespace(
        run_daily_governance=lambda **kwargs: {
            "run_result": "succeeded",
            "pass_status": "passed",
            "trade_date": kwargs["trade_date"],
        }
    )

    service = FastBackfillService(
        db_path=str(db_path),
        tushare_api=api,
        governance_service=governance_service,
        min_full_count=2,
        sleep=lambda _seconds: None,
    )

    result = service.backfill_to_trade_date(date(2026, 5, 11))

    assert api.requested_dates == ["20260511"]
    assert result["status"] == "completed"
    assert result["backfilled_dates"] == ["2026-05-11"]


def test_fast_backfill_to_trade_date_rejects_future_target(tmp_path):
    db_path = tmp_path / "stock_analysis.db"
    _init_stock_daily(db_path)
    service = FastBackfillService(
        db_path=str(db_path),
        tushare_api=_FakeTushareApi(),
        governance_service=SimpleNamespace(run_daily_governance=lambda **_kwargs: {}),
        min_full_count=2,
        sleep=lambda _seconds: None,
    )

    future_date = date.today() + timedelta(days=1)

    try:
        service.backfill_to_trade_date(future_date)
    except ValueError as exc:
        assert "未来日期" in str(exc)
    else:
        raise AssertionError("future target date should be rejected")
