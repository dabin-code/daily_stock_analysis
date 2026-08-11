"""SQLite 并发保护：确认 DatabaseManager 建立的连接启用 WAL。

阶段 C 的历史回补是数小时的长事务作业，默认 rollback journal 会让写锁阻塞
全部读，与每日任务相撞即 database is locked。
"""

import os
import sqlite3
import stat
import tempfile
import unittest

from sqlalchemy import text

from src.config import Config
from src.storage import DatabaseManager


class SqliteWalModeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_storage_wal.db")
        os.environ["DATABASE_PATH"] = self._db_path

        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()

    def tearDown(self) -> None:
        # Windows 下必须先 dispose 引擎，否则临时目录清理会抛 PermissionError
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def test_database_manager_connection_uses_wal(self) -> None:
        """通过 DatabaseManager 拿到的连接处于 WAL 模式。"""
        with self.db.session_scope() as session:
            mode = session.execute(text("PRAGMA journal_mode")).scalar()

        self.assertEqual(str(mode).lower(), "wal")

    def test_wal_mode_is_persisted_on_the_database_file(self) -> None:
        """WAL 是持久化设置：外部进程重新打开同一文件仍是 WAL。"""
        DatabaseManager.reset_instance()

        conn = sqlite3.connect(self._db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(str(mode).lower(), "wal")

    def test_connection_effective_busy_timeout_is_30_seconds(self) -> None:
        """钉住 DatabaseManager 交出的连接上「生效的」忙等超时是 30 秒。

        WAL 不能替代忙等超时，回补期间该值退化会直接表现为 database is locked。
        但这个断言只覆盖最终生效值：当前它由 create_engine 的
        connect_args={"timeout": 30} 建立，因此**不能**证明连接事件里那条显式的
        `PRAGMA busy_timeout=30000` 仍然存在——删掉那条 pragma 本测试依然通过。
        """
        with self.db.session_scope() as session:
            timeout = session.execute(text("PRAGMA busy_timeout")).scalar()

        self.assertEqual(int(timeout), 30000)


class SqliteWalPragmaFailureTestCase(unittest.TestCase):
    """切换 WAL 失败时必须降级，不能让整个 DatabaseManager 交不出连接。

    真实触发场景就是升级那一刻：新代码首次运行时 API 服务、调度器或运维的 sqlite3
    shell 正持有读事务，切 WAL 会在等满 busy_timeout 后抛 database is locked；库文件
    只读或放在网络盘同样会失败。DELETE 模式下应用只是慢，功能是正确的，所以这里必须
    是告警降级而不是崩溃。

    这里用「库文件只读」复现（reviewer 确认的失败模式之一），它是确定性的、秒级的，
    而 database is locked 需要等满 30 秒忙等超时。
    """

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_storage_wal_failure.db")
        # addCleanup 是 LIFO：先恢复可写权限，再 dispose 引擎，最后删临时目录。
        # 用 addCleanup 而不是 tearDown，避免 setUp 中途失败时 DATABASE_PATH 与两个
        # 单例泄漏到后续测试。
        self.addCleanup(self._temp_dir.cleanup)
        self.addCleanup(os.environ.pop, "DATABASE_PATH", None)
        self.addCleanup(Config.reset_instance)
        self.addCleanup(DatabaseManager.reset_instance)
        self.addCleanup(self._restore_writable)

        os.environ["DATABASE_PATH"] = self._db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()

    def _restore_writable(self) -> None:
        if os.path.exists(self._db_path):
            os.chmod(self._db_path, stat.S_IWRITE | stat.S_IREAD)

    def _make_wal_switch_fail(self) -> None:
        """把库退回 DELETE 模式并置为只读，使下一次连接上的 WAL 切换必然失败。"""
        # 先释放连接池，否则改完权限后仍可能复用旧连接，绕过连接事件。
        self.db._engine.dispose()

        raw = sqlite3.connect(self._db_path)
        try:
            raw.execute("PRAGMA journal_mode=DELETE")
        finally:
            raw.close()

        os.chmod(self._db_path, stat.S_IREAD)

        # 确认这个环境真的能拒绝写入（例如以 root 运行时只读属性会被忽略），
        # 否则这个测试什么都没验证，宁可跳过也不要给出假绿灯。
        probe = sqlite3.connect(self._db_path)
        try:
            probe.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            return
        else:
            self.skipTest("当前环境不强制只读文件权限，无法复现 WAL 切换失败")
        finally:
            probe.close()

    def test_connection_still_works_when_wal_switch_fails(self) -> None:
        """WAL 切换失败只降级为告警，连接照常交付且仍可读写数据。"""
        self._make_wal_switch_fail()

        with self.assertLogs("src.storage", level="WARNING") as captured:
            with self.db.session_scope() as session:
                mode = session.execute(text("PRAGMA journal_mode")).scalar()
                row_count = session.execute(text("SELECT COUNT(*) FROM stock_daily")).scalar()

        self.assertEqual(str(mode).lower(), "delete")
        self.assertEqual(int(row_count), 0)
        self.assertTrue(
            any("WAL" in message for message in captured.output),
            f"expected a WAL degradation warning, got: {captured.output}",
        )

    def test_wal_switch_failure_keeps_foreign_keys_and_busy_timeout(self) -> None:
        """WAL 失败不能连带吃掉同一个连接事件里前面已经生效的 pragma。"""
        self._make_wal_switch_fail()

        with self.assertLogs("src.storage", level="WARNING"):
            with self.db.session_scope() as session:
                foreign_keys = session.execute(text("PRAGMA foreign_keys")).scalar()
                busy_timeout = session.execute(text("PRAGMA busy_timeout")).scalar()

        self.assertEqual(int(foreign_keys), 1)
        self.assertEqual(int(busy_timeout), 30000)


if __name__ == "__main__":
    unittest.main()
