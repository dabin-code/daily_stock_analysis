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


class _FakeBaostockResultSet:
    def __init__(self, fields, rows, error_code="0", error_msg="success"):
        self.fields = fields
        self.error_code = error_code
        self.error_msg = error_msg
        self._rows = list(rows)
        self._cursor = -1

    def next(self) -> bool:
        if self._cursor + 1 >= len(self._rows):
            return False
        self._cursor += 1
        return True

    def get_row_data(self):
        return self._rows[self._cursor]


class _FakeBaostockModule:
    """只实现 sync_from_baostock 用到的四个接口。"""

    def __init__(self, result_set):
        self._result_set = result_set
        self.logged_in = False
        self.logged_out = False
        self.query_calls = []

    def login(self):
        self.logged_in = True
        return _FakeBaostockResultSet([], [])

    def logout(self):
        self.logged_out = True
        return _FakeBaostockResultSet([], [])

    def query_stock_basic(self, *args, **kwargs):
        # 传了 code 就只返回该只证券，已退市的拉不到；这里记录下来供断言。
        self.query_calls.append({"args": args, "kwargs": kwargs})
        return self._result_set


class ListingLifecycleServiceTestCase(_TempDatabaseTestCase):
    def setUp(self) -> None:
        self._db_path = self._prepare_temp_db()
        DatabaseManager.get_instance()

    def _service(self):
        from src.services.listing_lifecycle_service import ListingLifecycleService

        return ListingLifecycleService()

    def test_upsert_writes_list_and_delist_date(self) -> None:
        svc = self._service()
        svc.upsert_lifecycle([
            {"code": "000001", "name": "平安银行", "list_date": date(1991, 4, 3),
             "delist_date": None, "listing_status": "active"},
            {"code": "000033", "name": "新都退", "list_date": date(1994, 1, 3),
             "delist_date": date(2016, 5, 26), "listing_status": "delisted"},
        ])

        rows = svc.get_lifecycle(["000001", "000033"])
        self.assertIsNone(rows["000001"]["delist_date"])
        self.assertEqual(rows["000033"]["delist_date"], date(2016, 5, 26))
        self.assertEqual(rows["000033"]["listing_status"], "delisted")
        self.assertEqual(rows["000033"]["list_date"], date(1994, 1, 3))

    def test_delisted_instruments_are_not_dropped_by_universe_sync(self) -> None:
        """反例测试：幸存者偏差的直接防线。

        已退市股票必须留在 instrument_master，否则历史回测样本里
        永远看不到它们，亏损样本被系统性抹掉。
        """
        svc = self._service()
        svc.upsert_lifecycle([
            {"code": "000033", "name": "新都退", "list_date": date(1994, 1, 3),
             "delist_date": date(2016, 5, 26), "listing_status": "delisted"},
        ])

        codes = svc.list_codes_alive_on(date(2015, 1, 1))
        self.assertIn("000033", codes)
        codes_after = svc.list_codes_alive_on(date(2020, 1, 1))
        self.assertNotIn("000033", codes_after)

    def test_alive_window_is_half_open_on_the_delisting_date(self) -> None:
        """区间口径钉死为 [list_date, delist_date)。

        delist_date 是终止上市生效日，该日证券已不在市、不产生 K 线；把它算作
        在市会让审计期望域每只退市股多出一天「无法归因的缺口」。上市日反过来
        是首个交易日，必须算在内。
        """
        svc = self._service()
        svc.upsert_lifecycle([
            {"code": "000033", "name": "新都退", "list_date": date(1994, 1, 3),
             "delist_date": date(2016, 5, 26), "listing_status": "delisted"},
        ])

        self.assertIn("000033", svc.list_codes_alive_on(date(2016, 5, 25)))
        self.assertNotIn("000033", svc.list_codes_alive_on(date(2016, 5, 26)))
        self.assertIn("000033", svc.list_codes_alive_on(date(1994, 1, 3)))
        self.assertNotIn("000033", svc.list_codes_alive_on(date(1994, 1, 2)))

    def test_unknown_list_date_is_not_reported_as_alive(self) -> None:
        """list_date 未知的行不进在市清单：无法断言的时点一律排除。"""
        svc = self._service()
        svc.upsert_lifecycle([
            {"code": "600000", "name": "浦发银行", "list_date": None,
             "delist_date": None, "listing_status": "active"},
        ])

        self.assertNotIn("600000", svc.list_codes_alive_on(date(2020, 1, 1)))

    def test_upsert_does_not_erase_known_dates_with_missing_values(self) -> None:
        """入参缺日期时不得把已知日期擦成 NULL。

        list_date 可能是别处（scripts/_kline_master_data_treatment.py 的 Phase A）
        用 stock_daily 首个交易日回填出来的；上游某次返回空值就把它清掉，等于
        用一次抓取抖动换掉一份已有证据。
        """
        svc = self._service()
        svc.upsert_lifecycle([
            {"code": "000033", "name": "新都退", "list_date": date(1994, 1, 3),
             "delist_date": date(2016, 5, 26), "listing_status": "delisted"},
        ])

        svc.upsert_lifecycle([
            {"code": "000033", "name": "新都退", "list_date": None,
             "delist_date": None, "listing_status": "delisted"},
        ])

        rows = svc.get_lifecycle(["000033"])
        self.assertEqual(rows["000033"]["list_date"], date(1994, 1, 3))
        self.assertEqual(rows["000033"]["delist_date"], date(2016, 5, 26))

    def test_upsert_clears_delist_date_when_status_is_active(self) -> None:
        """在市即无退市日期：误标必须能被下一次同步纠正。"""
        svc = self._service()
        svc.upsert_lifecycle([
            {"code": "600000", "name": "浦发银行", "list_date": date(1999, 11, 10),
             "delist_date": date(2016, 5, 26), "listing_status": "delisted"},
        ])

        svc.upsert_lifecycle([
            {"code": "600000", "name": "浦发银行", "list_date": date(1999, 11, 10),
             "delist_date": None, "listing_status": "active"},
        ])

        rows = svc.get_lifecycle(["600000"])
        self.assertIsNone(rows["600000"]["delist_date"])
        self.assertIn("600000", svc.list_codes_alive_on(date(2020, 1, 1)))


class ListingLifecycleSyncTestCase(_TempDatabaseTestCase):
    """sync_from_baostock 的字段映射与过滤，全部用假 baostock 模块离线跑。"""

    FIELDS = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
    ROWS = [
        ["sh.600000", "浦发银行", "1999-11-10", "", "1", "1"],
        ["sz.000033", "新都退", "1994-01-03", "2016-05-26", "1", "0"],
        ["sh.000001", "上证综合指数", "1991-07-15", "", "2", "1"],
        ["sz.159901", "深100ETF", "2006-04-24", "", "5", "1"],
    ]

    def setUp(self) -> None:
        self._db_path = self._prepare_temp_db()
        DatabaseManager.get_instance()

        import sys

        self._fake_bs = _FakeBaostockModule(
            _FakeBaostockResultSet(self.FIELDS, self.ROWS)
        )
        self._previous = sys.modules.get("baostock")
        sys.modules["baostock"] = self._fake_bs
        self.addCleanup(self._restore_baostock)

    def _restore_baostock(self) -> None:
        import sys

        if self._previous is None:
            sys.modules.pop("baostock", None)
        else:
            sys.modules["baostock"] = self._previous

    def test_sync_maps_fields_strips_prefix_and_keeps_delisted(self) -> None:
        from src.services.listing_lifecycle_service import ListingLifecycleService

        svc = ListingLifecycleService()
        stats = svc.sync_from_baostock()

        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["written"], 2)
        self.assertEqual(stats["delisted"], 1)

        rows = svc.get_lifecycle(["600000", "000033"])
        self.assertEqual(
            set(rows),
            {"600000", "000033"},
            "baostock 的 sh./sz. 前缀必须剥离，否则回填出一批匹配不到任何行情的孤儿代码",
        )
        self.assertEqual(rows["600000"]["list_date"], date(1999, 11, 10))
        self.assertIsNone(rows["600000"]["delist_date"])
        self.assertEqual(rows["600000"]["listing_status"], "active")
        self.assertEqual(rows["000033"]["delist_date"], date(2016, 5, 26))
        self.assertEqual(rows["000033"]["listing_status"], "delisted")
        self.assertTrue(self._fake_bs.logged_out, "登录后必须登出，避免会话泄漏")

    def test_sync_pulls_whole_market_without_code_filter(self) -> None:
        """query_stock_basic 必须不带 code 调用。

        传 code 就只返回那一只，已退市证券整批拉不到，幸存者偏差原地复发。
        """
        from src.services.listing_lifecycle_service import ListingLifecycleService

        ListingLifecycleService().sync_from_baostock()

        self.assertEqual(len(self._fake_bs.query_calls), 1)
        call = self._fake_bs.query_calls[0]
        self.assertEqual(call["args"], ())
        self.assertNotIn("code", call["kwargs"])

    def test_sync_excludes_non_stock_securities(self) -> None:
        """type != '1' 的指数与基金不能进 instrument_master。"""
        from src.services.listing_lifecycle_service import ListingLifecycleService

        svc = ListingLifecycleService()
        svc.sync_from_baostock()

        self.assertEqual(svc.get_lifecycle(["000001", "159901"]), {})

    def test_sync_raises_when_query_fails(self) -> None:
        """抓取失败必须报错，不能返回 0 行让调用方误判成「全市场为空」。"""
        from src.services.listing_lifecycle_service import ListingLifecycleService

        import sys

        sys.modules["baostock"] = _FakeBaostockModule(
            _FakeBaostockResultSet([], [], error_code="10001", error_msg="network down")
        )

        with self.assertRaises(RuntimeError):
            ListingLifecycleService().sync_from_baostock()


if __name__ == "__main__":
    unittest.main()
