import os
import tempfile
import unittest
from datetime import date

from src.config import Config
from src.storage import DatabaseManager


class InlineScreeningRunMigrationTestCase(unittest.TestCase):
    """Verify inline SQLite migration upgrades legacy screening_runs schema."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "legacy_screening.db")
        self._create_legacy_db()
        os.environ["DATABASE_PATH"] = self._db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def _create_legacy_db(self) -> None:
        import sqlite3

        conn = sqlite3.connect(self._db_path)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE screening_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id VARCHAR(64) NOT NULL UNIQUE,
                trade_date DATE NOT NULL,
                market VARCHAR(16) NOT NULL,
                status VARCHAR(32) NOT NULL,
                universe_size INTEGER NOT NULL DEFAULT 0,
                candidate_count INTEGER NOT NULL DEFAULT 0,
                ai_top_k INTEGER NOT NULL DEFAULT 0,
                config_snapshot TEXT,
                error_summary TEXT,
                started_at DATETIME,
                completed_at DATETIME
            )
            """
        )
        cur.execute(
            """
            INSERT INTO screening_runs (
                run_id, trade_date, market, status, universe_size, candidate_count,
                ai_top_k, config_snapshot, error_summary, started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (
                "legacy-run-001",
                date(2026, 3, 15).isoformat(),
                "cn",
                "completed",
                5307,
                8,
                5,
                '{"mode":"aggressive"}',
                None,
            ),
        )
        conn.commit()
        conn.close()

    def test_inline_migration_adds_notification_columns_to_legacy_db(self) -> None:
        db = DatabaseManager.get_instance()
        run = db.get_screening_run("legacy-run-001")

        self.assertIsNotNone(run)
        self.assertEqual(run["trigger_type"], "manual")
        self.assertEqual(run["notification_status"], "pending")
        self.assertEqual(run["notification_attempts"], 0)
        self.assertIsNone(run["notification_sent_at"])
        self.assertIsNone(run["notification_error"])


if __name__ == "__main__":
    unittest.main()
