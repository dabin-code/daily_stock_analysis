# -*- coding: utf-8 -*-
"""交易日历表与 fail-closed 查询测试。"""
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from src.config import Config
from src.storage import DatabaseManager


def _index_specs(db_path: str, table: str) -> dict:
    """索引名 -> 是否唯一。只取名字看不出唯一性有没有真的建上。"""
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[1]: bool(row[2])
            for row in conn.execute(f"PRAGMA index_list({table})")
        }
    finally:
        conn.close()


class TradingCalendarSchemaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "cal.db")
        # addCleanup 是 LIFO：先 dispose 引擎（WAL 下还有 -wal / -shm 句柄），
        # 再清 DATABASE_PATH 与两个单例，最后删临时目录。用 addCleanup 而不是
        # tearDown，避免 setUp 中途失败时环境变量和单例泄漏到后续测试。
        self.addCleanup(self._temp_dir.cleanup)
        self.addCleanup(os.environ.pop, "DATABASE_PATH", None)
        self.addCleanup(Config.reset_instance)
        self.addCleanup(DatabaseManager.reset_instance)

        os.environ["DATABASE_PATH"] = self._db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        DatabaseManager.get_instance()

    def test_trading_calendar_table_exists(self) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(trading_calendar)")}
        finally:
            conn.close()
        self.assertEqual(
            cols & {"market", "trade_date", "is_open", "source", "cross_check"},
            {"market", "trade_date", "is_open", "source", "cross_check"},
        )

    def test_calendar_unique_index_is_discoverable_and_unique(self) -> None:
        """(market, trade_date) 的唯一性必须以命名唯一索引存在。

        换成 UniqueConstraint 时它会落成 sqlite_autoindex_trading_calendar_1，
        给定的名字只留在建表 DDL 里，PRAGMA index_list 查不到，排查的人会
        误判成「约束根本没建」。
        """
        specs = _index_specs(self._db_path, "trading_calendar")
        self.assertIn("uix_calendar_market_date", specs)
        self.assertTrue(
            specs["uix_calendar_market_date"],
            "uix_calendar_market_date exists but is not a unique index",
        )

    def test_calendar_rejects_duplicate_market_date(self) -> None:
        """命名唯一索引仍要真的挡住重复 (market, trade_date)，不只是名字好看。"""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT INTO trading_calendar (market, trade_date, is_open, source)"
                " VALUES ('cn', '2026-08-10', 1, 'akshare')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO trading_calendar (market, trade_date, is_open, source)"
                    " VALUES ('cn', '2026-08-10', 0, 'akshare')"
                )
            conn.commit()
        finally:
            conn.close()


class TradingCalendarServiceTestCase(unittest.TestCase):
    """fail-closed 语义测试。

    这三条用例定义了本服务与既有 src/core/trading_calendar.py 的根本差异：
    未覆盖的日期必须报错，而不是猜一个答案。
    """

    def setUp(self) -> None:
        """建临时库并建表。

        **必须把 db_path 显式传给服务**：服务缺省会从 config 取
        database_path，那是生产库；无参构造会让这些用例写脏生产数据。
        """
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self._db_path = os.path.join(self._tmp, "cal.db")
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "CREATE TABLE trading_calendar ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT NOT NULL, "
                "trade_date TEXT NOT NULL, is_open INTEGER NOT NULL, "
                "source TEXT, cross_check TEXT, "
                "created_at TIMESTAMP, updated_at TIMESTAMP, "
                "UNIQUE(market, trade_date))"
            )
            conn.commit()
        finally:
            conn.close()

    def test_is_trading_day_reads_from_table(self) -> None:
        from src.services.trading_calendar_service import TradingCalendarService

        svc = TradingCalendarService(db_path=self._db_path)
        svc.upsert_days("cn", [(date(2026, 8, 10), True), (date(2026, 8, 9), False)])

        self.assertTrue(svc.is_trading_day(date(2026, 8, 10)))
        self.assertFalse(svc.is_trading_day(date(2026, 8, 9)))

    def test_uncovered_date_raises_instead_of_guessing(self) -> None:
        from src.services.trading_calendar_service import (
            CalendarNotCoveredError,
            TradingCalendarService,
        )

        svc = TradingCalendarService(db_path=self._db_path)
        svc.upsert_days("cn", [(date(2026, 8, 10), True)])

        with self.assertRaises(CalendarNotCoveredError):
            svc.is_trading_day(date(2019, 3, 1))

    def test_connections_use_long_busy_timeout(self) -> None:
        """忙等超时必须对齐 DatabaseManager 的 30 秒。

        本服务与 FastBackfillService 共用同一个库文件，回补期间还有别的写入方
        在活动。WAL 是文件级持久设置能自动继承，busy_timeout 是连接级的，
        不显式传就只有 sqlite3 默认的 5 秒。
        """
        from src.services.trading_calendar_service import TradingCalendarService

        svc = TradingCalendarService(db_path=self._db_path)

        with svc._connect() as conn:
            busy_timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]

        self.assertGreaterEqual(busy_timeout_ms, 30000)

    def test_get_trading_days_range_is_sorted_and_open_only(self) -> None:
        from src.services.trading_calendar_service import TradingCalendarService

        svc = TradingCalendarService(db_path=self._db_path)
        svc.upsert_days("cn", [
            (date(2026, 8, 10), True),
            (date(2026, 8, 8), False),
            (date(2026, 8, 7), True),
        ])

        days = svc.get_trading_days(date(2026, 8, 7), date(2026, 8, 10))

        self.assertEqual(days, [date(2026, 8, 7), date(2026, 8, 10)])

    def test_sync_does_not_fabricate_days_beyond_source_coverage(self) -> None:
        """信源只到 2026 年底时，2027 年不能被写成「休市」。

        源里「不在开市日集合中」有两种含义：覆盖范围内是休市，范围外是未知。
        混为一谈会让 fail-closed 查询对未知日期给出确定答案——首次同步
        2018–2027 时实测凭空写出 365 天假休市，正是这条路径。
        """
        from src.services.trading_calendar_service import TradingCalendarService

        svc = TradingCalendarService(db_path=self._db_path)
        # 源覆盖 12-28 至 12-31，其中 12-29、12-30 是区间内的休市日。
        source_days = [date(2026, 12, 28), date(2026, 12, 31)]

        with patch.object(svc, "_fetch_source_open_days", return_value=source_days):
            svc.sync(date(2026, 12, 28), date(2027, 12, 31), market="cn")

        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT trade_date, is_open FROM trading_calendar ORDER BY trade_date"
            ).fetchall()

        self.assertEqual(
            [r[0] for r in rows],
            ["2026-12-28", "2026-12-29", "2026-12-30", "2026-12-31"],
            "超出信源覆盖范围的日期不应落库",
        )
        self.assertEqual(
            [r[1] for r in rows],
            [1, 0, 0, 1],
            "覆盖范围内的休市日仍要如实写成 0",
        )

    def test_sync_reports_the_range_it_actually_covered(self) -> None:
        """调用方要能看出请求区间被裁剪了，否则缺口会被当成休市。"""
        from src.services.trading_calendar_service import TradingCalendarService

        svc = TradingCalendarService(db_path=self._db_path)

        with patch.object(
            svc, "_fetch_source_open_days", return_value=[date(2026, 12, 31)]
        ):
            result = svc.sync(date(2026, 12, 30), date(2027, 6, 30), market="cn")

        self.assertEqual(result["covered_to"], "2026-12-31")
        self.assertEqual(result["requested_to"], "2027-06-30")


if __name__ == "__main__":
    unittest.main()
