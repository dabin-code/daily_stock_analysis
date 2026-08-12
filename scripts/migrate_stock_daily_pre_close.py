"""
DB migration: Add ``pre_close`` / ``adj_convention`` columns to ``stock_daily``.

``pre_close`` is the previous close price. Tushare's ``daily()`` endpoint
provides it natively and it is the only free basis for reconstructing
adjustment factors (``ratio = pre_close(t) / close(prev_observation)``); all
write paths historically discarded it, which is why the incremental segment
cannot reconstruct its own factors.

``adj_convention`` records the price convention of each individual row
(``raw`` / ``qfq`` / ``unknown``). Sources disagree in practice — Tushare
returns unadjusted prices while Efinance appears to return front-adjusted
ones — so storing both without labelling makes an adjustment rebuild wrong
exactly at the source boundary.

For SQLite the same migration also runs inline at startup
(``DatabaseManager._migrate_sqlite_stock_daily_pre_close_fields``). This
script is the deterministic offline path — useful when the DB lives on a
separate volume / managed host and you want to apply the change before the
service comes up.

Idempotent — re-running is safe.

NO BACKFILL — deliberate, do not add one:
  * ``pre_close`` has no defensible default. Any value written for an
    existing row would be fabricated data, and it feeds factor
    reconstruction directly, so a fabricated value silently corrupts the
    adjustment chain.
  * ``adj_convention`` cannot be determined after the fact for legacy rows
    either; marking them ``raw`` would feed a whole batch of wrong-convention
    prices to the stage that consumes only ``raw``.
  Existing rows stay NULL and are filled later by a re-fetch stage.

Usage:
    python scripts/migrate_stock_daily_pre_close.py
    python scripts/migrate_stock_daily_pre_close.py --db data/stocks.db
    python scripts/migrate_stock_daily_pre_close.py --dry-run

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
    ("pre_close", "FLOAT"),
    ("adj_convention", "VARCHAR(16)"),
]
INDEX_NAME = "ix_stock_daily_adj_convention"
INDEX_COLUMN = "adj_convention"


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

    index_created = False
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
            index_created = True
    else:
        print(f"[skip] Index {INDEX_NAME} already exists.")

    if dry_run:
        conn.close()
        return 0

    conn.commit()
    conn.close()

    if not columns_added and not index_created:
        print("[done] No changes applied — migration already complete.")
    else:
        print("[done] Migration complete. Both columns stay NULL on existing rows.")
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
