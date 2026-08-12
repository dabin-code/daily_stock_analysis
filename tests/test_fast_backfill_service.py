import hashlib
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.config import Config
from src.services.fast_backfill_service import FastBackfillService


@pytest.fixture
def reset_config():
    """改环境变量前后都要清单例，否则配置会串到别的用例。"""
    Config.reset_instance()
    yield
    Config.reset_instance()


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


def _init_staging(db_path: Path) -> None:
    """建 stock_daily_staging，DDL 与 storage.py 的 StockDailyStaging 保持一致。"""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE stock_daily_staging (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                date DATE NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                amount REAL,
                pct_chg REAL,
                ma5 REAL,
                ma10 REAL,
                ma20 REAL,
                volume_ratio REAL,
                data_source TEXT,
                adj_factor REAL,
                adj_anchor_date DATE,
                adj_factor_source TEXT,
                pre_close REAL,
                adj_convention TEXT,
                batch_id TEXT,
                convention_version TEXT,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX uix_staging_code_date ON stock_daily_staging (code, date)"
        )
        conn.execute(
            "CREATE INDEX ix_staging_date_version ON stock_daily_staging (date, convention_version)"
        )


def _init_calendar(db_path: Path, days: dict) -> None:
    """建 trading_calendar 并灌入指定的开/休市日。

    刻意只灌测试要的日子：若服务读的是生产库日历（已覆盖 2018-2026），
    取到的交易日会与这里不同，用例就会因为「读错了库」而失败。
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE trading_calendar ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT NOT NULL, "
            "trade_date TEXT NOT NULL, is_open INTEGER NOT NULL, "
            "source TEXT, cross_check TEXT, "
            "created_at TIMESTAMP, updated_at TIMESTAMP, "
            "UNIQUE(market, trade_date))"
        )
        conn.executemany(
            "INSERT INTO trading_calendar (market, trade_date, is_open, source) "
            "VALUES ('cn', ?, ?, 'test')",
            [(day.isoformat(), 1 if is_open else 0) for day, is_open in days.items()],
        )


def _seed_staging(db_path: Path, trade_date: date, codes: int, convention_version: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO stock_daily_staging (code, date, convention_version) VALUES (?, ?, ?)",
            [
                (f"{index:06d}", trade_date.isoformat(), convention_version)
                for index in range(codes)
            ],
        )


def _seed_production(db_path: Path, trade_date: date, codes: int) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO stock_daily (code, date) VALUES (?, ?)",
            [(f"{index:06d}", trade_date.isoformat()) for index in range(codes)],
        )


def _make_service(db_path: Path, **overrides) -> FastBackfillService:
    """集中既有用例里重复的构造方式，避免每个测试各写一份。"""
    kwargs = {
        "db_path": str(db_path),
        "tushare_api": _FakeTushareApi(),
        "governance_service": SimpleNamespace(
            run_daily_governance=lambda **kw: {
                "run_result": "succeeded",
                "pass_status": "passed",
                "trade_date": kw["trade_date"],
            }
        ),
        "min_full_count": 2,
        "sleep": lambda _seconds: None,
    }
    kwargs.update(overrides)
    return FastBackfillService(**kwargs)


def _query(db_path: Path, sql: str) -> list:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _row_hash(db_path: Path, table: str) -> str:
    """整表内容指纹，用于证明生产表一个字节都没被动过。"""
    rows = _query(db_path, f"SELECT * FROM {table} ORDER BY rowid")
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def _tushare_fixture_df(trade_date: str = "20260511") -> pd.DataFrame:
    """一天的 Tushare daily() 返回，含 pre_close 列。"""
    return _FakeTushareApi().daily(trade_date)


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


def test_save_day_data_defaults_to_production_table(tmp_path):
    """既有生产入口的行为必须原样保留。

    backfill_to_trade_date 服务于 Web 的「回填至该日」与数据健康治理，
    改到 staging 会让它们静默失效。
    """
    db_path = tmp_path / "backfill.db"
    _init_stock_daily(db_path)
    _init_staging(db_path)

    svc = _make_service(db_path)
    saved = svc._save_day_data(_tushare_fixture_df())

    assert saved > 0
    assert _query(db_path, "SELECT COUNT(*) FROM stock_daily")[0][0] > 0
    assert _query(db_path, "SELECT COUNT(*) FROM stock_daily_staging")[0][0] == 0


def test_save_day_data_can_target_staging_without_touching_production(tmp_path):
    """反例测试：阶段 C 绝不能碰生产表。

    生产表 2024-2026 的存量正是判断数据源口径差异的证据本身，
    被 INSERT OR REPLACE 覆盖后证据就没了。
    """
    db_path = tmp_path / "backfill.db"
    _init_stock_daily(db_path)
    _init_staging(db_path)
    before = _row_hash(db_path, "stock_daily")

    svc = _make_service(db_path)
    svc._save_day_data(
        _tushare_fixture_df(),
        target_table="stock_daily_staging",
        batch_id="test-batch",
    )

    assert _row_hash(db_path, "stock_daily") == before, "production table was modified"
    rows = _query(
        db_path,
        "SELECT pre_close, adj_convention, batch_id, convention_version "
        "FROM stock_daily_staging",
    )
    assert rows, "staging table is empty"
    assert rows[0][0] == pytest.approx(10.2)
    assert rows[0][1] == "raw"
    assert rows[0][2] == "test-batch"
    assert rows[0][3] == svc._convention_version


def test_save_day_data_rejects_unknown_target_table(tmp_path):
    """表名要拼进 SQL，只放行两个字面量。"""
    db_path = tmp_path / "backfill.db"
    _init_stock_daily(db_path)

    svc = _make_service(db_path)

    with pytest.raises(ValueError):
        svc._save_day_data(_tushare_fixture_df(), target_table="stock_daily; DROP TABLE stock_daily")


def test_staging_date_with_old_convention_is_not_complete(tmp_path):
    """已有数据但口径版本不匹配时，必须重取而不是跳过。

    这是阶段 C 能否真正拿到 pre_close 的关键：561 天存量的股票数都超过
    阈值，纯计数判定会把它们全部跳过。
    """
    db_path = tmp_path / "backfill.db"
    _init_staging(db_path)
    _seed_staging(db_path, date(2026, 5, 8), codes=4000, convention_version="v0_legacy")

    svc = _make_service(db_path)

    assert svc._is_date_complete(date(2026, 5, 8), target_table="stock_daily_staging") is False


def test_staging_date_with_current_convention_is_complete(tmp_path):
    db_path = tmp_path / "backfill.db"
    _init_staging(db_path)
    svc = _make_service(db_path)
    _seed_staging(db_path, date(2026, 5, 8), codes=4000,
                  convention_version=svc._convention_version)

    assert svc._is_date_complete(date(2026, 5, 8), target_table="stock_daily_staging") is True


def test_production_completeness_check_is_unchanged(tmp_path):
    """既有生产入口的完成判定行为必须原样保留。"""
    db_path = tmp_path / "backfill.db"
    _init_stock_daily(db_path)
    _seed_production(db_path, date(2026, 5, 8), codes=4000)

    svc = _make_service(db_path)

    assert svc._is_date_complete(date(2026, 5, 8)) is True


def test_backfill_range_writes_staging_using_trading_calendar(tmp_path):
    """区间回补按落库日历逐日写 staging，且不碰生产表。

    日历必须来自 trading_calendar，用 SELECT DISTINCT date FROM stock_daily
    反推的话，2018-2023 这段生产表没有数据，会得到空集。
    """
    db_path = tmp_path / "backfill.db"
    _init_stock_daily(db_path)
    _init_staging(db_path)
    # 2019-01-04 真实是交易日，这里刻意标成休市：读到生产日历就会多请求一天
    _init_calendar(db_path, {
        date(2019, 1, 2): True,
        date(2019, 1, 3): True,
        date(2019, 1, 4): False,
        date(2019, 1, 5): False,
    })
    before = _row_hash(db_path, "stock_daily")

    api = _FakeTushareApi()
    svc = _make_service(db_path, tushare_api=api)
    result = svc.backfill_range(date(2019, 1, 1), date(2019, 1, 6), batch_id="batch-2019")

    assert api.requested_dates == ["20190102", "20190103"]
    assert result["status"] == "completed"
    assert result["saved_rows"] == 4
    assert result["backfilled_dates"] == ["2019-01-02", "2019-01-03"]
    assert _row_hash(db_path, "stock_daily") == before, "production table was modified"
    assert _query(
        db_path,
        "SELECT DISTINCT batch_id, convention_version FROM stock_daily_staging",
    ) == [("batch-2019", svc._convention_version)]


def test_backfill_range_skips_dates_already_written_with_current_convention(tmp_path):
    """断点续跑：当前口径已写过的日子不重复消耗额度。"""
    db_path = tmp_path / "backfill.db"
    _init_staging(db_path)
    _init_calendar(db_path, {date(2019, 1, 2): True, date(2019, 1, 3): True})
    api = _FakeTushareApi()
    svc = _make_service(db_path, tushare_api=api)
    _seed_staging(db_path, date(2019, 1, 2), codes=3,
                  convention_version=svc._convention_version)

    result = svc.backfill_range(date(2019, 1, 1), date(2019, 1, 6), batch_id="batch-2019")

    assert api.requested_dates == ["20190103"]
    assert result["skipped_dates"] == ["2019-01-02"]


def test_backfill_range_rejects_unknown_target_table(tmp_path):
    db_path = tmp_path / "backfill.db"
    _init_staging(db_path)
    svc = _make_service(db_path)

    with pytest.raises(ValueError):
        svc.backfill_range(
            date(2019, 1, 1),
            date(2019, 1, 6),
            batch_id="batch-2019",
            target_table="stock_daily_tmp",
        )


def test_backfill_sleep_interval_follows_configured_rate_limit(tmp_path, monkeypatch, reset_config):
    """限速必须来自配置：免费档 45 次/分，高积分账号可以提到 500。"""
    monkeypatch.setenv("BACKFILL_RATE_LIMIT_PER_MIN", "120")
    Config.reset_instance()

    db_path = tmp_path / "backfill.db"
    _init_staging(db_path)
    _init_calendar(db_path, {date(2019, 1, 2): True})
    slept: list[float] = []
    svc = _make_service(db_path, sleep=slept.append)

    svc.backfill_range(date(2019, 1, 1), date(2019, 1, 6), batch_id="batch-2019")

    assert svc._rate_limit_per_min == 120
    assert slept == [pytest.approx(0.5)]


def test_backfill_connections_use_long_busy_timeout(tmp_path):
    """回补是最可能撞锁的一条路径，忙等超时必须对齐 DatabaseManager 的 30 秒。

    sqlite3 默认只等 5 秒，而 busy_timeout 是连接级设置，
    不像 WAL 那样能从 DatabaseManager 的连接继承过来。
    """
    db_path = tmp_path / "backfill.db"
    _init_stock_daily(db_path)
    svc = _make_service(db_path)

    with svc._connect() as conn:
        busy_timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert busy_timeout_ms >= 30000
