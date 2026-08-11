"""SQLite 并发保护：确认 DatabaseManager 建立的连接启用 WAL。

阶段 C 的历史回补是数小时的长事务作业，默认 rollback journal 会让写锁阻塞
全部读，与每日任务相撞即 database is locked。
"""

import os
import sqlite3
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


if __name__ == "__main__":
    unittest.main()
