# -*- coding: utf-8 -*-
"""instrument_master 上市/退市日期字段与上市生命周期服务测试。"""
import importlib.util
import io
import contextlib
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.config import Config
from src.storage import DatabaseManager

# 建库语句刻意写成「加 delist_date 之前」的形状，用来验证内联迁移这条路径。
# 不复用 ORM metadata：ORM 已经带上了新列，用它建表等于把待验证的迁移绕过去。
LEGACY_INSTRUMENT_MASTER_DDL = (
    "CREATE TABLE instrument_master ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "code VARCHAR(16) NOT NULL UNIQUE, "
    "name VARCHAR(64) NOT NULL, "
    "market VARCHAR(16) NOT NULL, "
    "exchange VARCHAR(16), "
    "listing_status VARCHAR(16) NOT NULL, "
    "is_st BOOLEAN NOT NULL, "
    "industry VARCHAR(64), "
    "list_date DATE, "
    "updated_at DATETIME)"
)


def _columns(db_path: str, table: str) -> set:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _index_names(db_path: str, table: str) -> set:
    """索引名集合。只看列在不在不够——索引缺失同样是可达状态。"""
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}
    finally:
        conn.close()


def _load_migration_module():
    script_path = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "migrate_instrument_delist_date.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migrate_instrument_delist_date", script_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _TempDatabaseTestCase(unittest.TestCase):
    """临时库脚手架，与 tests/test_trading_calendar_service.py 保持一致。

    addCleanup 是 LIFO：先 dispose 引擎（WAL 下还有 -wal / -shm 句柄），再清
    DATABASE_PATH 与两个单例，最后删临时目录。顺序反了 Windows 会抛
    PermissionError: [WinError 32]。用 addCleanup 而不是 tearDown，避免 setUp
    中途失败时环境变量和单例泄漏到后续测试。
    """

    def _prepare_temp_db(self, filename: str = "instruments.db") -> str:
        temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(temp_dir.name, filename)
        self.addCleanup(temp_dir.cleanup)
        self.addCleanup(os.environ.pop, "DATABASE_PATH", None)
        self.addCleanup(Config.reset_instance)
        self.addCleanup(DatabaseManager.reset_instance)

        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        return db_path


class InstrumentDelistDateSchemaTestCase(_TempDatabaseTestCase):
    def test_instrument_master_has_delist_date(self) -> None:
        db_path = self._prepare_temp_db()
        DatabaseManager.get_instance()

        self.assertIn("delist_date", _columns(db_path, "instrument_master"))

    def test_delist_date_index_is_discoverable_by_name(self) -> None:
        """索引必须以给定的名字存在。

        PRAGMA index_list 查不到名字时，排查的人会误判成「索引根本没建」，
        而 point-in-time 在市清单查询正是按 delist_date 过滤的。
        """
        db_path = self._prepare_temp_db()
        DatabaseManager.get_instance()

        self.assertIn(
            "ix_instrument_master_delist_date",
            _index_names(db_path, "instrument_master"),
        )

    def test_inline_migration_adds_delist_date_to_legacy_table(self) -> None:
        """存量库（表已存在、列缺失）必须被内联迁移补齐列与索引。

        create_all 对已存在的表整表跳过，索引一起跳过，只有内联迁移能补。
        """
        db_path = self._prepare_temp_db("legacy.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(LEGACY_INSTRUMENT_MASTER_DDL)
            conn.commit()
        finally:
            conn.close()
        self.assertNotIn("delist_date", _columns(db_path, "instrument_master"))

        DatabaseManager.get_instance()

        self.assertIn("delist_date", _columns(db_path, "instrument_master"))
        self.assertIn(
            "ix_instrument_master_delist_date",
            _index_names(db_path, "instrument_master"),
        )

    def test_inline_migration_creates_index_when_only_index_is_missing(self) -> None:
        """列已存在而索引缺失同样要被补上。

        SQLite 的 ALTER TABLE ADD COLUMN 会立即落盘，离线脚本在 ALTER 之后、
        CREATE INDEX 之前被打断就会留下这种库。索引创建若嵌在「列缺失」分支里，
        这种库永远等不到索引。
        """
        db_path = self._prepare_temp_db("half_migrated.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(LEGACY_INSTRUMENT_MASTER_DDL)
            conn.execute("ALTER TABLE instrument_master ADD COLUMN delist_date DATE")
            conn.commit()
        finally:
            conn.close()
        self.assertNotIn(
            "ix_instrument_master_delist_date",
            _index_names(db_path, "instrument_master"),
        )

        DatabaseManager.get_instance()

        self.assertIn(
            "ix_instrument_master_delist_date",
            _index_names(db_path, "instrument_master"),
        )


class InstrumentDelistDateMigrationScriptTestCase(unittest.TestCase):
    """离线迁移脚本测试。

    只在临时库副本上跑，绝不碰生产库。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self._db_path = Path(self._tmp) / "copy.db"
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute(LEGACY_INSTRUMENT_MASTER_DDL)
            conn.commit()
        finally:
            conn.close()

    def _run(self, module) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = module.migrate(self._db_path)
        self.assertEqual(exit_code, 0)
        return buffer.getvalue()

    def test_migration_script_is_idempotent(self) -> None:
        module = _load_migration_module()

        first = self._run(module)
        self.assertIn("delist_date", _columns(str(self._db_path), "instrument_master"))
        self.assertIn(
            "ix_instrument_master_delist_date",
            _index_names(str(self._db_path), "instrument_master"),
        )
        self.assertIn("[ok] Added column: delist_date", first)

        second = self._run(module)
        self.assertIn("[skip] Column delist_date already exists.", second)
        self.assertIn("[skip] Index ix_instrument_master_delist_date already exists.", second)

    def test_migration_script_echoes_resolved_db_path(self) -> None:
        """成功路径也要回显真正动过的库文件，否则操作员无法确认目标。"""
        module = _load_migration_module()

        output = self._run(module)

        self.assertIn(f"[info] Target database: {self._db_path}", output)

    def test_migration_script_default_db_path_honours_env(self) -> None:
        """默认库路径必须取 DATABASE_PATH。

        写死仓库内路径时，库在独立卷上的部署会去迁移一个同名空壳库，
        满屏 [ok] 而真正的库一个字节没动。
        """
        module = _load_migration_module()
        self.addCleanup(os.environ.pop, "DATABASE_PATH", None)
        os.environ["DATABASE_PATH"] = str(self._db_path)

        self.assertEqual(module.default_db_path(), self._db_path)


if __name__ == "__main__":
    unittest.main()
