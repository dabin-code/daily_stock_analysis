# -*- coding: utf-8 -*-
"""scripts/fast_backfill.py 的落库列清单守卫。

该脚本用 ``INSERT OR REPLACE`` + 显式列清单写 ``stock_daily``，语义是「删行重插」
而非字段级更新，清单外的列会在重写同一 ``(code, date)`` 时被清空。因此
``pre_close`` / ``adj_convention`` 一旦掉出清单，回填结果就会静默丢掉复权重建
唯一的免费依据。
"""

import os
import sqlite3
import tempfile
import unittest

import pandas as pd
import pytest

from scripts import fast_backfill
from src.config import Config
from src.storage import DatabaseManager


@pytest.mark.unit
class SaveDayDataAdjustmentMetadataTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self._db_path = os.path.join(self._temp_dir.name, "test_fast_backfill.db")

        self._previous_database_path = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = self._db_path
        self.addCleanup(self._restore_database_path)

        Config.reset_instance()
        DatabaseManager.reset_instance()
        # 借 DatabaseManager 建表，避免在测试里手写一份 schema 副本
        DatabaseManager.get_instance()
        DatabaseManager.reset_instance()
        Config.reset_instance()

    def _restore_database_path(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        if self._previous_database_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self._previous_database_path

    @staticmethod
    def _day_frame() -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260508",
                    "open": 12.1,
                    "high": 12.4,
                    "low": 12.0,
                    "close": 12.3,
                    "pre_close": 12.0,
                    "vol": 2000.0,
                    "amount": 2_460.0,
                    "pct_chg": 2.5,
                }
            ]
        )

    def _read_row(self):
        conn = sqlite3.connect(self._db_path)
        try:
            return conn.execute(
                "SELECT pre_close, adj_convention FROM stock_daily "
                "WHERE code = '000001' AND date = '2026-05-08'"
            ).fetchone()
        finally:
            conn.close()

    def test_save_day_data_persists_pre_close_and_raw_convention(self) -> None:
        saved = fast_backfill.save_day_data(
            self._db_path, self._day_frame(), pd.DataFrame()
        )
        self.assertEqual(saved, 1)

        row = self._read_row()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row[0], 12.0, places=4)
        # 数据来自 api.daily()，不经过 TushareFetcher，永远是不复权价
        self.assertEqual(row[1], "raw")

    def test_rewriting_same_day_does_not_wipe_pre_close(self) -> None:
        fast_backfill.save_day_data(self._db_path, self._day_frame(), pd.DataFrame())
        fast_backfill.save_day_data(self._db_path, self._day_frame(), pd.DataFrame())

        row = self._read_row()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row[0], 12.0, places=4)
        self.assertEqual(row[1], "raw")


if __name__ == "__main__":
    unittest.main()
