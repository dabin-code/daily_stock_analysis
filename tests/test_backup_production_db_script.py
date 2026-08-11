# -*- coding: utf-8 -*-
"""备份脚本的往返测试。

这个脚本是长时数据作业唯一的兜底手段，整个灾难恢复故事只押在一个性质上：
备份文件里必须有源库的全部数据，失败时必须不留下一个看起来正常的文件。
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import backup_production_db

_FROZEN_NOW = datetime(2026, 8, 11, 19, 0, 0)
_FROZEN_STAMP = "20260811_190000"


class _FrozenDatetime:
    """冻结时间戳，让目标文件名可预测（否则无法构造“目标已存在”）。"""

    @staticmethod
    def now() -> datetime:
        return _FROZEN_NOW


class BackupProductionDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.root = Path(self._temp_dir.name)
        self.db_path = self.root / "source.db"
        self.out_dir = self.root / "backups"

    def _frozen_target(self) -> Path:
        return self.out_dir / f"{self.db_path.stem}_{_FROZEN_STAMP}.db"

    def _seed_source_with_uncheckpointed_wal(self) -> int:
        """建库并让一部分已提交的行只存在于 -wal 边车文件里。

        持有第二个读连接可以阻止 WAL 被 checkpoint，从而复现生产库的真实形态：
        单纯拷贝主库文件会丢掉这部分行。
        """
        writer = sqlite3.connect(str(self.db_path))
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE stock_daily (code TEXT, trade_date TEXT, close REAL)")
            writer.executemany(
                "INSERT INTO stock_daily (code, trade_date, close) VALUES (?, ?, ?)",
                [("600519", f"2026-01-{day:02d}", 10.0 + day) for day in range(1, 11)],
            )
            writer.commit()

            reader = sqlite3.connect(str(self.db_path))
            self.addCleanup(reader.close)
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM stock_daily").fetchone()

            writer.executemany(
                "INSERT INTO stock_daily (code, trade_date, close) VALUES (?, ?, ?)",
                [("000001", f"2026-02-{day:02d}", 20.0 + day) for day in range(1, 6)],
            )
            writer.commit()
        finally:
            writer.close()

        wal_path = self.db_path.with_name(self.db_path.name + "-wal")
        self.assertTrue(wal_path.exists(), "前置条件不成立：未生成 -wal 边车文件")
        self.assertGreater(
            wal_path.stat().st_size,
            0,
            "前置条件不成立：-wal 已被 checkpoint，本测试无法证明备份读到了未落盘的行",
        )
        return 15

    def test_backup_round_trip_includes_rows_still_in_the_wal(self) -> None:
        """备份必须包含源库全部行，含仅存在于 -wal 中的部分。"""
        expected_rows = self._seed_source_with_uncheckpointed_wal()

        target = backup_production_db.backup(self.db_path, self.out_dir)

        self.assertTrue(target.exists())
        copy = sqlite3.connect(str(target))
        try:
            actual_rows = copy.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
            distinct_codes = copy.execute(
                "SELECT COUNT(DISTINCT code) FROM stock_daily"
            ).fetchone()[0]
        finally:
            copy.close()

        self.assertEqual(actual_rows, expected_rows)
        self.assertEqual(distinct_codes, 2)

    def test_backup_leaves_no_temporary_artifacts_behind(self) -> None:
        """成功路径不残留 .tmp 中间文件。"""
        self._seed_source_with_uncheckpointed_wal()

        target = backup_production_db.backup(self.db_path, self.out_dir)

        leftovers = [path.name for path in self.out_dir.iterdir() if path.name != target.name]
        self.assertEqual(leftovers, [])

    def test_backup_refuses_to_overwrite_an_existing_target(self) -> None:
        """目标名已存在时报错，而不是把已有库的表清空后写进去。"""
        self._seed_source_with_uncheckpointed_wal()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        existing = self._frozen_target()
        existing.write_bytes(b"previous backup")

        with patch.object(backup_production_db, "datetime", _FrozenDatetime):
            with self.assertRaises(FileExistsError):
                backup_production_db.backup(self.db_path, self.out_dir)

        self.assertEqual(existing.read_bytes(), b"previous backup")

    def test_failed_copy_leaves_no_file_behind(self) -> None:
        """拷贝中途失败不能留下名字合规、时间戳正确但读不出表的“备份”。"""
        # 源文件不是合法的 sqlite 库：连接是惰性的，直到 backup() 真正读页才失败，
        # 也就是目标文件已经建好之后。
        self.db_path.write_bytes(b"this is not a sqlite database" * 100)

        with patch.object(backup_production_db, "datetime", _FrozenDatetime):
            with self.assertRaises(sqlite3.DatabaseError):
                backup_production_db.backup(self.db_path, self.out_dir)

        self.assertFalse(self._frozen_target().exists())
        self.assertEqual(sorted(path.name for path in self.out_dir.iterdir()), [])

    def test_backup_fails_before_writing_when_free_space_is_insufficient(self) -> None:
        """空间不足时在动手写之前失败：默认输出目录与源库同卷，写满是最先遇到的失败。"""
        self._seed_source_with_uncheckpointed_wal()
        required = self.db_path.stat().st_size

        def _fake_disk_usage(_path):
            return SimpleNamespace(total=required, used=required, free=required - 1)

        with patch.object(backup_production_db.shutil, "disk_usage", _fake_disk_usage):
            with self.assertRaises(OSError):
                backup_production_db.backup(self.db_path, self.out_dir)

        self.assertEqual(sorted(path.name for path in self.out_dir.iterdir()), [])


class BackupProductionDbCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.root = Path(self._temp_dir.name)

    def test_missing_database_is_reported_before_touching_the_output_dir(self) -> None:
        out_dir = self.root / "backups"

        with self.assertRaises(FileNotFoundError):
            backup_production_db.backup(self.root / "absent.db", out_dir)

        self.assertFalse(out_dir.exists())

    def test_cli_writes_backup_for_explicit_db_argument(self) -> None:
        db_path = self.root / "cli_source.db"
        out_dir = self.root / "cli_backups"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("CREATE TABLE t (v INTEGER)")
            conn.execute("INSERT INTO t (v) VALUES (7)")
            conn.commit()
        finally:
            conn.close()

        with patch.object(
            backup_production_db.sys,
            "argv",
            ["backup_production_db.py", "--db", str(db_path), "--out", str(out_dir)],
        ):
            exit_code = backup_production_db.main()

        self.assertEqual(exit_code, 0)
        backups = sorted(out_dir.iterdir())
        self.assertEqual(len(backups), 1)
        copy = sqlite3.connect(str(backups[0]))
        try:
            self.assertEqual(copy.execute("SELECT v FROM t").fetchone()[0], 7)
        finally:
            copy.close()


if __name__ == "__main__":
    unittest.main()
