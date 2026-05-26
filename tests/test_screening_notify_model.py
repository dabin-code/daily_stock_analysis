"""RED phase: Tests for screening run notification model fields.

These tests verify that the ScreeningRun ORM model has the new
notification-related columns (trigger_type, notification_status, etc.)
and that default values are set correctly for each trigger type.
"""
import os
import tempfile
import unittest
from datetime import date

from src.config import Config
from src.storage import DatabaseManager


class ScreeningRunNotificationFieldsTestCase(unittest.TestCase):
    """Verify notification fields exist on ScreeningRun and defaults are correct."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_notify_model.db")
        os.environ["DATABASE_PATH"] = self._db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def _create_run(self, trigger_type: str = "manual") -> str:
        return self.db.create_screening_run(
            trade_date=date(2026, 3, 15),
            market="cn",
            config_snapshot={"mode": "balanced", "candidate_limit": 30, "ai_top_k": 5},
            trigger_type=trigger_type,
        )

    # -- trigger_type field --

    def test_create_run_default_trigger_type_is_manual(self) -> None:
        run_id = self.db.create_screening_run(
            trade_date=date(2026, 3, 15),
            market="cn",
            config_snapshot={},
        )
        run = self.db.get_screening_run(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run["trigger_type"], "manual")

    def test_create_run_with_scheduled_trigger_type(self) -> None:
        run_id = self._create_run(trigger_type="scheduled")
        run = self.db.get_screening_run(run_id)
        self.assertEqual(run["trigger_type"], "scheduled")

    def test_create_run_with_rerun_trigger_type(self) -> None:
        run_id = self._create_run(trigger_type="rerun")
        run = self.db.get_screening_run(run_id)
        self.assertEqual(run["trigger_type"], "rerun")

    # -- notification_status defaults per trigger_type --

    def test_scheduled_run_notification_status_defaults_to_pending(self) -> None:
        run_id = self._create_run(trigger_type="scheduled")
        run = self.db.get_screening_run(run_id)
        self.assertEqual(run["notification_status"], "pending")

    def test_manual_run_notification_status_defaults_to_pending(self) -> None:
        run_id = self._create_run(trigger_type="manual")
        run = self.db.get_screening_run(run_id)
        self.assertEqual(run["notification_status"], "pending")

    def test_rerun_notification_status_defaults_to_pending(self) -> None:
        run_id = self._create_run(trigger_type="rerun")
        run = self.db.get_screening_run(run_id)
        self.assertEqual(run["notification_status"], "pending")

    # -- notification_attempts default --

    def test_notification_attempts_defaults_to_zero(self) -> None:
        run_id = self._create_run(trigger_type="scheduled")
        run = self.db.get_screening_run(run_id)
        self.assertEqual(run["notification_attempts"], 0)

    # -- notification_sent_at and notification_error default --

    def test_notification_sent_at_defaults_to_none(self) -> None:
        run_id = self._create_run(trigger_type="scheduled")
        run = self.db.get_screening_run(run_id)
        self.assertIsNone(run["notification_sent_at"])

    def test_notification_error_defaults_to_none(self) -> None:
        run_id = self._create_run(trigger_type="scheduled")
        run = self.db.get_screening_run(run_id)
        self.assertIsNone(run["notification_error"])

    # -- update notification status --

    def test_update_notification_status_to_sent(self) -> None:
        run_id = self._create_run(trigger_type="scheduled")
        updated = self.db.update_notification_status(
            run_id=run_id,
            notification_status="sent",
        )
        self.assertTrue(updated)
        run = self.db.get_screening_run(run_id)
        self.assertEqual(run["notification_status"], "sent")
        self.assertIsNotNone(run["notification_sent_at"])
        self.assertEqual(run["notification_attempts"], 1)

    def test_update_notification_status_to_failed_records_error(self) -> None:
        run_id = self._create_run(trigger_type="scheduled")
        updated = self.db.update_notification_status(
            run_id=run_id,
            notification_status="failed",
            notification_error="connection timeout",
        )
        self.assertTrue(updated)
        run = self.db.get_screening_run(run_id)
        self.assertEqual(run["notification_status"], "failed")
        self.assertEqual(run["notification_error"], "connection timeout")
        self.assertEqual(run["notification_attempts"], 1)

    def test_update_notification_status_increments_attempts(self) -> None:
        run_id = self._create_run(trigger_type="scheduled")
        self.db.update_notification_status(run_id=run_id, notification_status="failed", notification_error="err1")
        self.db.update_notification_status(run_id=run_id, notification_status="failed", notification_error="err2")
        run = self.db.get_screening_run(run_id)
        self.assertEqual(run["notification_attempts"], 2)

    def test_to_dict_includes_notification_fields(self) -> None:
        run_id = self._create_run(trigger_type="scheduled")
        run = self.db.get_screening_run(run_id)
        for field in ("trigger_type", "notification_status", "notification_attempts",
                       "notification_sent_at", "notification_error"):
            self.assertIn(field, run, f"Missing field: {field}")


if __name__ == "__main__":
    unittest.main()
