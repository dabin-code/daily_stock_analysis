import os
import sqlite3
import tempfile
import unittest
from datetime import date

from sqlalchemy import create_engine

from src.config import Config
from src.storage import Base, DatabaseManager


class InlineBacktestEvaluationMigrationTestCase(unittest.TestCase):
    """Verify inline SQLite migration upgrades legacy backtest evaluation schema."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "legacy_backtest.db")
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
        from src.backtest.models.backtest_models import FiveLayerBacktestEvaluation

        engine = create_engine(f"sqlite:///{self._db_path}")
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "DROP INDEX IF EXISTS ix_five_layer_backtest_evaluations_suppression_reason"
            )
            conn.exec_driver_sql(
                "DROP INDEX IF EXISTS ix_flbe_suppression_reason"
            )
            conn.exec_driver_sql(
                "DROP INDEX IF EXISTS ix_five_layer_backtest_evaluations_trade_replay_status"
            )
            conn.exec_driver_sql(
                f"ALTER TABLE {FiveLayerBacktestEvaluation.__tablename__} "
                "DROP COLUMN optimal_entry_deviation"
            )
            conn.exec_driver_sql(
                f"ALTER TABLE {FiveLayerBacktestEvaluation.__tablename__} "
                "DROP COLUMN optimal_entry_timing"
            )
            conn.exec_driver_sql(
                f"ALTER TABLE {FiveLayerBacktestEvaluation.__tablename__} "
                "DROP COLUMN eval_status"
            )
            conn.exec_driver_sql(
                f"ALTER TABLE {FiveLayerBacktestEvaluation.__tablename__} "
                "DROP COLUMN suppression_reason"
            )
            for column_name in (
                "planned_entry_price",
                "planned_stop_loss_price",
                "planned_take_profit_price",
                "actual_entry_price",
                "actual_entry_date",
                "actual_exit_price",
                "actual_exit_date",
                "exit_reason",
                "trade_return_pct",
                "trade_replay_status",
            ):
                conn.exec_driver_sql(
                    f"ALTER TABLE {FiveLayerBacktestEvaluation.__tablename__} "
                    f"DROP COLUMN {column_name}"
                )
            conn.exec_driver_sql(
                """
                INSERT INTO five_layer_backtest_evaluations (
                    backtest_run_id, trade_date, code, signal_family, evaluator_type
                ) VALUES (
                    'legacy-row-001', '2026-05-13', '600519', 'entry', 'entry'
                )
                """
            )
        engine.dispose()

    def test_inline_migration_adds_entry_timing_columns_to_legacy_evaluations(self) -> None:
        db = DatabaseManager.get_instance()

        conn = sqlite3.connect(self._db_path)
        try:
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(five_layer_backtest_evaluations)"
                ).fetchall()
            }
        finally:
            conn.close()

        self.assertIn("optimal_entry_deviation", columns)
        self.assertIn("optimal_entry_timing", columns)
        self.assertIn("eval_status", columns)
        self.assertIn("suppression_reason", columns)
        self.assertIn("planned_entry_price", columns)
        self.assertIn("planned_stop_loss_price", columns)
        self.assertIn("planned_take_profit_price", columns)
        self.assertIn("actual_entry_price", columns)
        self.assertIn("actual_entry_date", columns)
        self.assertIn("actual_exit_price", columns)
        self.assertIn("actual_exit_date", columns)
        self.assertIn("exit_reason", columns)
        self.assertIn("trade_return_pct", columns)
        self.assertIn("trade_replay_status", columns)

        conn = sqlite3.connect(self._db_path)
        try:
            status = conn.execute(
                "SELECT eval_status FROM five_layer_backtest_evaluations "
                "WHERE backtest_run_id = ?",
                ("legacy-row-001",),
            ).fetchone()[0]
            indexes = {
                row[1]
                for row in conn.execute(
                    "PRAGMA index_list(five_layer_backtest_evaluations)"
                ).fetchall()
            }
        finally:
            conn.close()

        self.assertEqual(status, "pending")
        self.assertIn(
            "ix_five_layer_backtest_evaluations_suppression_reason",
            indexes,
        )

        from src.backtest.models.backtest_models import FiveLayerBacktestEvaluation

        with db.get_session() as session:
            evaluation = FiveLayerBacktestEvaluation(
                backtest_run_id="run-legacy-001",
                trade_date=date(2026, 5, 13),
                code="600519",
                signal_family="entry",
                evaluator_type="entry",
                optimal_entry_deviation=1.25,
                optimal_entry_timing=2,
                eval_status="evaluated",
                suppression_reason=None,
                planned_entry_price=10.0,
                planned_stop_loss_price=9.2,
                planned_take_profit_price=11.0,
                actual_entry_price=10.0,
                actual_entry_date=date(2026, 5, 14),
                actual_exit_price=11.0,
                actual_exit_date=date(2026, 5, 15),
                exit_reason="take_profit",
                trade_return_pct=10.0,
                trade_replay_status="completed",
            )
            session.add(evaluation)
            session.commit()

            self.assertIsNotNone(evaluation.id)


if __name__ == "__main__":
    unittest.main()
