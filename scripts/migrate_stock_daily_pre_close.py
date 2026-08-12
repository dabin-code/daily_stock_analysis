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

The default database path follows the main program: ``DATABASE_PATH`` if set,
otherwise ``<repo>/data/stock_analysis.db``. That matters exactly in the
separate-volume / managed-host case above, where migrating a same-named file
inside the repo would silently leave the real database untouched. The resolved
path is echoed on every run.

NOTE: SQLite ALTER TABLE ADD COLUMN auto-commits, so a partial run that
errors mid-way may leave some columns added. The migration is idempotent
and re-running will skip the columns already present, so the safest
recovery path is to re-run without --dry-run.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
FALLBACK_DB_PATH = REPO_ROOT / "data" / "stock_analysis.db"

TABLE = "stock_daily"
NEW_COLUMNS: List[Tuple[str, str]] = [
    ("pre_close", "FLOAT"),
    ("adj_convention", "VARCHAR(16)"),
]
INDEX_NAME = "ix_stock_daily_adj_convention"
INDEX_COLUMN = "adj_convention"


def default_db_path() -> Path:
    """默认库路径复用主程序约定：优先 DATABASE_PATH，否则仓库内 data/。

    读环境变量而不是固定仓库内路径：本脚本的使用场景正是「库不在仓库里」
    （独立卷 / 托管实例），此时写死仓库内路径会去迁移一个同名的空壳库，
    满屏 [ok] + 退出码 0，真正的库一个字节没动。
    """
    env_path = os.environ.get("DATABASE_PATH")
    if env_path:
        return Path(env_path)
    return FALLBACK_DB_PATH


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

    # 成功路径也要回显最终解析出的库路径，否则操作员看到一屏 [ok] 却无法
    # 确认动的是哪个文件。
    print(f"[info] Target database: {db_path}")

    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        print(f"[error] Cannot open database {db_path}: {exc}", file=sys.stderr)
        return 1

    # sqlite 的报错（文件不是数据库、只读库、库被锁）转成脚本自己的 [error]
    # 约定 + 非零退出码，不要甩一坨 traceback；但绝不吞掉错误码。
    try:
        return _migrate_open_db(conn, db_path, dry_run=dry_run)
    except sqlite3.Error as exc:
        print(f"[error] Migration failed on {db_path}: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def _migrate_open_db(conn: sqlite3.Connection, db_path: Path, dry_run: bool) -> int:
    cursor = conn.cursor()

    table_check = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (TABLE,),
    ).fetchone()
    if not table_check:
        print(f"[skip] Table {TABLE} does not exist in {db_path} — no migration needed.")
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
        return 0

    conn.commit()

    if not columns_added and not index_created:
        print("[done] No changes applied — migration already complete.")
    else:
        print("[done] Migration complete. Both columns stay NULL on existing rows.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    default_db = default_db_path()
    parser.add_argument(
        "--db",
        type=Path,
        default=default_db,
        help=(
            f"Path to SQLite database (default: {default_db}; "
            "taken from $DATABASE_PATH when set)"
        ),
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
