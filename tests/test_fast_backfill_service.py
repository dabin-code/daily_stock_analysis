import sqlite3
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

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
                pre_close REAL,
                adj_convention TEXT,
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
                    "pre_close": 10.2,
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
                    "pre_close": 20.1,
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


def test_fast_backfill_converts_tushare_volume_and_amount_units(tmp_path):
    """Tushare 的 vol 为手、amount 为千元，落库必须换算成股与元。

    漏掉换算会让量比等量能因子在数据源边界上出现 100 倍级断层。
    """
    db_path = tmp_path / "stock_analysis.db"
    _init_stock_daily(db_path)
    service = FastBackfillService(
        db_path=str(db_path),
        tushare_api=_FakeTushareApi(),
        governance_service=SimpleNamespace(
            run_daily_governance=lambda **kwargs: {
                "run_result": "succeeded",
                "pass_status": "passed",
            }
        ),
        min_full_count=2,
        sleep=lambda _seconds: None,
    )

    service.backfill_to_trade_date(date(2026, 5, 11))

    with sqlite3.connect(db_path) as conn:
        volume, amount = conn.execute(
            "SELECT volume, amount FROM stock_daily WHERE code='000001' AND date='2026-05-11'"
        ).fetchone()

    assert volume == 1000 * 100
    assert amount == 2000 * 1000


def test_fast_backfill_rewrite_does_not_wipe_pre_close_and_convention(tmp_path):
    """重写同一 (code, date) 不能把 pre_close / adj_convention 清成 NULL。

    本服务用 ``INSERT OR REPLACE`` + 显式列清单落库，语义是「删行重插」，清单外的
    列会被清空。该路径由 Web 数据健康页与 /api/v1/screening/backfill-to-date 触发，
    一旦两列掉出清单，一次回填就会抹掉整段区间复权重建唯一的免费依据。
    """
    db_path = tmp_path / "stock_analysis.db"
    _init_stock_daily(db_path)
    api = _FakeTushareApi()
    service = FastBackfillService(
        db_path=str(db_path),
        tushare_api=api,
        governance_service=SimpleNamespace(
            run_daily_governance=lambda **kwargs: {
                "run_result": "succeeded",
                "pass_status": "passed",
            }
        ),
        min_full_count=2,
        sleep=lambda _seconds: None,
    )

    service.backfill_to_trade_date(date(2026, 5, 11))
    # 第二次回填走同一条写入语句重写同一 (code, date)
    service._save_day_data(api.daily("20260511"))

    with sqlite3.connect(db_path) as conn:
        pre_close, adj_convention = conn.execute(
            "SELECT pre_close, adj_convention FROM stock_daily "
            "WHERE code='000001' AND date='2026-05-11'"
        ).fetchone()

    assert pre_close == pytest.approx(10.2)
    # 数据来自 api.daily()，不经过 TushareFetcher，永远是不复权价
    assert adj_convention == "raw"


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
