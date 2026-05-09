"""
DB migration: Add ``suppression_reason`` column to
``five_layer_backtest_evaluations`` and back-fill legacy rows.

Idempotent — re-running is safe.

Usage:
    python scripts/migrate_evaluation_suppression_reason.py
    python scripts/migrate_evaluation_suppression_reason.py --db data/stocks.db
    python scripts/migrate_evaluation_suppression_reason.py --dry-run

Background:
    Before this migration, evaluation rows without ``forward_return_5d`` or
    ``risk_avoided_pct`` were silently treated as ``loss`` (entries) or
    "missing primary metric" (observations). Aggregators inferred a generic
    suppression reason at read time, which made it impossible to attribute
    a missing metric to its real cause (limit-up block, immature window,
    no forward bars, exception, ...).

    The new ``suppression_reason`` column lets evaluators write the cause at
    the moment the row is suppressed. For pre-existing rows we back-fill the
    same generic codes that the legacy aggregator inferred, so historic runs
    keep their displayed counts.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "stock_analysis.db"

TABLE = "five_layer_backtest_evaluations"
NEW_COLUMN = "suppression_reason"
NEW_COLUMN_TYPE = "VARCHAR(64)"
INDEX_NAME = "ix_flbe_suppression_reason"


def _table_columns(cursor: sqlite3.Cursor, table: str) -> List[str]:
    return [row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()]


def _index_exists(cursor: sqlite3.Cursor, index_name: str) -> bool:
    rows = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchall()
    return bool(rows)


def _backfill_counts(cursor: sqlite3.Cursor) -> Tuple[int, int, int]:
    """Return (entry_backfill, observation_backfill, unknown_backfill)."""
    rows = cursor.execute(
        f"""
        SELECT signal_family, COUNT(*) FROM {TABLE}
        WHERE {NEW_COLUMN} IS NULL
          AND forward_return_5d IS NULL
          AND risk_avoided_pct IS NULL
        GROUP BY signal_family
        """
    ).fetchall()
    entry = obs = other = 0
    for family, count in rows:
        family = (family or "").lower()
        if family == "entry":
            entry = count
        elif family == "observation":
            obs = count
        else:
            other += count
    return entry, obs, other


def migrate(db_path: Path, dry_run: bool = False) -> int:
    if not db_path.exists():
        print(f"[error] Database not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Verify the table exists. If it does not, the new schema will be created
    # by SQLAlchemy on next startup — no migration needed for this DB.
    table_check = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (TABLE,),
    ).fetchone()
    if not table_check:
        print(f"[skip] Table {TABLE} does not exist in {db_path} — no migration needed.")
        conn.close()
        return 0

    columns = _table_columns(cursor, TABLE)
    column_added = False
    if NEW_COLUMN not in columns:
        if dry_run:
            print(f"[dry-run] Would ADD COLUMN {NEW_COLUMN} {NEW_COLUMN_TYPE} on {TABLE}")
        else:
            cursor.execute(
                f"ALTER TABLE {TABLE} ADD COLUMN {NEW_COLUMN} {NEW_COLUMN_TYPE}"
            )
            print(f"[ok] Added column: {NEW_COLUMN} {NEW_COLUMN_TYPE}")
            column_added = True
    else:
        print(f"[skip] Column {NEW_COLUMN} already exists.")

    if not _index_exists(cursor, INDEX_NAME):
        if dry_run:
            print(f"[dry-run] Would CREATE INDEX {INDEX_NAME} ON {TABLE}({NEW_COLUMN})")
        else:
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON {TABLE}({NEW_COLUMN})"
            )
            print(f"[ok] Created index: {INDEX_NAME}")
    else:
        print(f"[skip] Index {INDEX_NAME} already exists.")

    entry_bf, obs_bf, other_bf = _backfill_counts(cursor)
    if dry_run:
        print(
            f"[dry-run] Would back-fill {entry_bf + obs_bf + other_bf} rows: "
            f"entry={entry_bf} observation={obs_bf} other={other_bf}"
        )
        conn.close()
        return 0

    backfill_sql = f"""
        UPDATE {TABLE}
        SET {NEW_COLUMN} = CASE
            WHEN LOWER(COALESCE(signal_family, '')) = 'observation' THEN 'missing_risk_avoided_pct'
            WHEN LOWER(COALESCE(signal_family, '')) = 'entry'       THEN 'missing_forward_return_5d'
            ELSE 'missing_primary_metric'
        END
        WHERE {NEW_COLUMN} IS NULL
          AND forward_return_5d IS NULL
          AND risk_avoided_pct IS NULL
    """
    cursor.execute(backfill_sql)
    backfilled = cursor.rowcount or 0
    print(
        f"[ok] Back-filled {backfilled} row(s): "
        f"entry={entry_bf}, observation={obs_bf}, other={other_bf}"
    )

    conn.commit()
    conn.close()

    if not column_added and backfilled == 0:
        print("[done] No changes applied — migration already complete.")
    else:
        print("[done] Migration complete.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to SQLite database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print intended changes without modifying the database.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    sys.exit(migrate(args.db, dry_run=args.dry_run))
