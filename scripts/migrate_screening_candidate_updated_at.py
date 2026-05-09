"""
DB migration: Add ``updated_at`` column to ``screening_candidates`` and
back-fill from ``created_at``. (A4 lite)

For SQLite the same migration also runs inline at startup
(``DatabaseManager._migrate_sqlite_screening_candidates_updated_at_field``).
This script is a deterministic offline path you can run before bringing the
service up — useful when the DB lives on a separate volume / managed host.

Idempotent — re-running is safe.

Usage:
    python scripts/migrate_screening_candidate_updated_at.py
    python scripts/migrate_screening_candidate_updated_at.py --db data/stocks.db
    python scripts/migrate_screening_candidate_updated_at.py --dry-run
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "stock_analysis.db"

TABLE = "screening_candidates"
NEW_COLUMN = "updated_at"
NEW_COLUMN_TYPE = "DATETIME"
INDEX_NAME = "ix_screening_candidates_updated_at"


def _columns(cursor: sqlite3.Cursor, table: str) -> list[str]:
    return [row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()]


def _index_exists(cursor: sqlite3.Cursor, index_name: str) -> bool:
    rows = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchall()
    return bool(rows)


def migrate(db_path: Path, dry_run: bool = False) -> int:
    if not db_path.exists():
        print(f"[error] Database not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    table_check = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (TABLE,),
    ).fetchone()
    if not table_check:
        print(f"[skip] Table {TABLE} does not exist in {db_path} — no migration needed.")
        conn.close()
        return 0

    columns = _columns(cursor, TABLE)
    column_added = False
    if NEW_COLUMN not in columns:
        if dry_run:
            print(f"[dry-run] Would ADD COLUMN {NEW_COLUMN} {NEW_COLUMN_TYPE} on {TABLE}")
            print(
                "[dry-run] NOTE: SQLite DDL auto-commits; if you previously hit an "
                "error mid-run the column may already exist. The migration is idempotent."
            )
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

    if dry_run:
        # In dry-run we may not have actually added the column yet; estimate
        # from rows whose created_at is set (worst-case backfill candidates).
        if NEW_COLUMN not in _columns(cursor, TABLE):
            pending = cursor.execute(
                f"SELECT COUNT(*) FROM {TABLE} WHERE created_at IS NOT NULL"
            ).fetchone()[0]
        else:
            pending = cursor.execute(
                f"SELECT COUNT(*) FROM {TABLE} "
                f"WHERE {NEW_COLUMN} IS NULL AND created_at IS NOT NULL"
            ).fetchone()[0]
        print(f"[dry-run] Would back-fill up to {pending} row(s) from created_at")
        conn.close()
        return 0

    cursor.execute(
        f"UPDATE {TABLE} SET {NEW_COLUMN} = created_at "
        f"WHERE {NEW_COLUMN} IS NULL AND created_at IS NOT NULL"
    )
    backfilled = cursor.rowcount or 0
    print(f"[ok] Back-filled {backfilled} row(s) (updated_at <- created_at).")

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
