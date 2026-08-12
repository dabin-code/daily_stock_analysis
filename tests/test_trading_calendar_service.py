# -*- coding: utf-8 -*-
"""交易日历表与 fail-closed 查询测试。"""
import os
import sqlite3
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
