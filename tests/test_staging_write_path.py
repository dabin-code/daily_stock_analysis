# -*- coding: utf-8 -*-
"""stock_daily 新增列的结构与内联迁移测试。"""
import os
import sqlite3
import tempfile
import unittest

from src.config import Config
from src.storage import DatabaseManager, StockDaily


def _columns(db_path: str, table: str) -> set:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _indexes(db_path: str, table: str) -> set:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}
    finally:
        conn.close()


class SchemaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "schema.db")
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

    def test_stock_daily_has_pre_close_and_adj_convention(self) -> None:
        cols = _columns(self._db_path, "stock_daily")
        self.assertIn("pre_close", cols)
        self.assertIn("adj_convention", cols)

    def test_orm_model_maps_both_new_columns(self) -> None:
        """两列必须挂在 ORM 模型上，光有物理列不够。

        建库时内联迁移也会跑，所以上一个用例在 ORM 少了列时依然通过；一旦 ORM
        没映射，走 ORM 的写入路径会静默丢掉这两个字段。
        """
        self.assertIn("pre_close", StockDaily.__table__.columns)
        self.assertIn("adj_convention", StockDaily.__table__.columns)

    def test_staging_table_exists_and_mirrors_stock_daily(self) -> None:
        staging = _columns(self._db_path, "stock_daily_staging")
        production = _columns(self._db_path, "stock_daily")

        self.assertTrue(staging, "stock_daily_staging table missing")
        # staging 必须是生产表的超集：提升时逐列对应，缺列会让提升语句写不出来
        missing = production - staging
        self.assertEqual(missing, set(), f"staging missing production columns: {missing}")
        # staging 独有的批次追踪列
        self.assertIn("batch_id", staging)
        self.assertIn("convention_version", staging)


class LegacyStockDailyMigrationTestCase(unittest.TestCase):
    """存量库走的是迁移路径，不是 create_all，必须单独覆盖。

    生产库已有 stock_daily，create_all 会整表跳过，唯一会补列的是内联迁移。
    """

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "legacy_stock_daily.db")
        self._create_legacy_db()

        # 与 SchemaTestCase 同样的 LIFO 清理顺序，见那里的说明。
        self.addCleanup(self._temp_dir.cleanup)
        self.addCleanup(os.environ.pop, "DATABASE_PATH", None)
        self.addCleanup(Config.reset_instance)
        self.addCleanup(DatabaseManager.reset_instance)

        os.environ["DATABASE_PATH"] = self._db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()

    def _create_legacy_db(self) -> None:
        """建一个缺两列的旧版 stock_daily，模拟存量生产库。"""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """
                CREATE TABLE stock_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code VARCHAR(10) NOT NULL,
                    date DATE NOT NULL,
                    open FLOAT,
                    high FLOAT,
                    low FLOAT,
                    close FLOAT,
                    volume FLOAT,
                    amount FLOAT,
                    pct_chg FLOAT,
                    data_source VARCHAR(50),
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
            conn.execute(
                "INSERT INTO stock_daily (code, date, close) VALUES ('600519', '2026-03-16', 1688.0)"
            )
            conn.commit()
        finally:
            conn.close()

    def test_inline_migration_adds_columns_to_legacy_schema(self) -> None:
        """存量库走的是迁移路径，不是 create_all，必须单独覆盖。"""
        DatabaseManager.get_instance()

        cols = _columns(self._db_path, "stock_daily")
        self.assertIn("pre_close", cols)
        self.assertIn("adj_convention", cols)

    def test_inline_migration_creates_adj_convention_index(self) -> None:
        """索引只由迁移创建，存量库上没有别的路径会补它。"""
        DatabaseManager.get_instance()

        self.assertIn("ix_stock_daily_adj_convention", _indexes(self._db_path, "stock_daily"))

    def test_inline_migration_leaves_legacy_rows_null(self) -> None:
        """存量行不回填：pre_close 没有可辩护的默认值，写任何值都是编造数据。"""
        DatabaseManager.get_instance()

        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT pre_close, adj_convention FROM stock_daily WHERE code='600519'"
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row, (None, None))


class MissingIndexOnlyMigrationTestCase(unittest.TestCase):
    """列已存在但索引缺失：这是可达状态，且只有内联迁移能补。

    SQLite 的 ALTER TABLE ADD COLUMN 即使没有显式 commit 也会落盘，所以离线
    脚本在几条 ALTER 之后、CREATE INDEX 之前被打断（Ctrl-C / 磁盘满 / 进程被
    杀）就会留下「列全在、索引没建」的库。此时 create_all 对已存在的表整表
    跳过，索引一起跳过，缺的索引没有任何别的路径会补。
    """

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "missing_index.db")
        self._create_db_with_columns_but_no_index()

        # 与 SchemaTestCase 同样的 LIFO 清理顺序，见那里的说明。
        self.addCleanup(self._temp_dir.cleanup)
        self.addCleanup(os.environ.pop, "DATABASE_PATH", None)
        self.addCleanup(Config.reset_instance)
        self.addCleanup(DatabaseManager.reset_instance)

        os.environ["DATABASE_PATH"] = self._db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()

    def _create_db_with_columns_but_no_index(self) -> None:
        """建一个两组新列都在、但两个索引都没建的 stock_daily。"""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """
                CREATE TABLE stock_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code VARCHAR(10) NOT NULL,
                    date DATE NOT NULL,
                    open FLOAT,
                    high FLOAT,
                    low FLOAT,
                    close FLOAT,
                    volume FLOAT,
                    amount FLOAT,
                    pct_chg FLOAT,
                    data_source VARCHAR(50),
                    adj_factor FLOAT,
                    adj_anchor_date DATE,
                    adj_factor_source VARCHAR(32),
                    pre_close FLOAT,
                    adj_convention VARCHAR(16),
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

        # 前置条件：两个索引确实都不存在，否则用例证明不了任何事。
        indexes = _indexes(self._db_path, "stock_daily")
        assert "ix_stock_daily_adj_convention" not in indexes
        assert "ix_stock_daily_adj_factor_source" not in indexes

    def test_inline_migration_creates_adj_convention_index_when_column_exists(self) -> None:
        DatabaseManager.get_instance()

        self.assertIn(
            "ix_stock_daily_adj_convention", _indexes(self._db_path, "stock_daily")
        )

    def test_inline_migration_creates_adj_factor_source_index_when_column_exists(self) -> None:
        DatabaseManager.get_instance()

        self.assertIn(
            "ix_stock_daily_adj_factor_source", _indexes(self._db_path, "stock_daily")
        )


if __name__ == "__main__":
    unittest.main()
