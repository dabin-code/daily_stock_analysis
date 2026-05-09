"""
DB migration: Add ``adj_factor`` / ``adj_anchor_date`` / ``adj_factor_source``
columns to ``stock_daily``, then back-fill legacy rows so the backtest layer
can rely on a non-NULL ``adj_factor`` (B6).

For SQLite the same migration also runs inline at startup
(``DatabaseManager._migrate_sqlite_stock_daily_adj_factor_fields``). This
script is the deterministic offline path — useful when the DB lives on a
separate volume / managed host and you want to apply the change before the
service comes up.

Idempotent — re-running is safe.

Backfill semantics:
  * adj_factor=1.0 — assume no adjustment; lossy for rows that pre-date
    B6 because the original raw price is unknown, but stable: subsequent
    backtests against these rows will not drift.
  * adj_factor_source='legacy_assume_one' — distinct marker so analysts
    can audit how much of the corpus pre-dates B6.

Usage:
    python scripts/migrate_stock_daily_adj_factor.py
    python scripts/migrate_stock_daily_adj_factor.py --db data/stocks.db
    python scripts/migrate_stock_daily_adj_factor.py --dry-run

NOTE: SQLite ALTER TABLE ADD COLUMN auto-commits, so a partial run that
errors mid-way may leave some columns added. The migration is idempotent
and re-running will skip the columns already present, so the safest
recovery path is to re-run without --dry-run.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "stock_analysis.db"

TABLE = "stock_daily"
NEW_COLUMNS: List[Tuple[str, str]] = [
    ("adj_factor", "FLOAT"),
    ("adj_anchor_date", "DATE"),
    ("adj_factor_source", "VARCHAR(32)"),
]
INDEX_NAME = "ix_stock_daily_adj_factor_source"
INDEX_COLUMN = "adj_factor_source"

LEGACY_SOURCE_MARKER = "legacy_assume_one"


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

    existing = set(_columns(cursor, TABLE))
    columns_added: List[str] = []

    for col_name, col_type in NEW_COLUMNS:
        if col_name in existing:
            print(f"[skip] Column {col_name} already exists.")
            continue
        if dry_run:
            print(f"[dry-run] Would ADD COLUMN {col_name} {col_type} on {TABLE}")
        else:
            cursor.execute(f"ALTER TABLE {TABLE} ADD COLUMN {col_name} {col_type}")
            print(f"[ok] Added column: {col_name} {col_type}")
            columns_added.append(col_name)
            existing.add(col_name)

    if not _index_exists(cursor, INDEX_NAME):
        if dry_run:
            print(
                f"[dry-run] Would CREATE INDEX {INDEX_NAME} ON {TABLE}({INDEX_COLUMN})"
            )
        else:
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON {TABLE}({INDEX_COLUMN})"
            )
            print(f"[ok] Created index: {INDEX_NAME}")
    else:
        print(f"[skip] Index {INDEX_NAME} already exists.")

    if dry_run:
        # Estimate rows to backfill — handles the case where columns aren't
        # present yet by counting the whole table as the upper bound.
        if "adj_factor" in _columns(cursor, TABLE):
            pending = cursor.execute(
                f"SELECT COUNT(*) FROM {TABLE} WHERE adj_factor IS NULL"
            ).fetchone()[0]
        else:
            pending = cursor.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        print(
            f"[dry-run] Would back-fill up to {pending} row(s) "
            f"(adj_factor=1.0, adj_factor_source='{LEGACY_SOURCE_MARKER}')"
        )
        conn.close()
        return 0

    # Backfill — idempotent thanks to IS NULL guards.
    cursor.execute(
        f"UPDATE {TABLE} SET adj_factor = 1.0 WHERE adj_factor IS NULL"
    )
    factor_filled = cursor.rowcount or 0
    cursor.execute(
        f"UPDATE {TABLE} SET adj_factor_source = ? WHERE adj_factor_source IS NULL",
        (LEGACY_SOURCE_MARKER,),
    )
    source_filled = cursor.rowcount or 0
    print(
        f"[ok] Back-filled adj_factor on {factor_filled} row(s); "
        f"adj_factor_source on {source_filled} row(s)."
    )

    conn.commit()
    conn.close()

    if not columns_added and factor_filled == 0 and source_filled == 0:
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
