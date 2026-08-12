"""
DB migration: Add ``code_revision`` / ``config_hash`` columns to
``five_layer_backtest_runs``.

The run table already declared five version columns, but only ``data_version``
had a write path, so a run could claim reproducibility without carrying any
evidence of the code and configuration it ran under. ``code_revision`` records
the git HEAD for post-mortem lookup (it deliberately does NOT participate in
comparability judgement — an unrelated commit would otherwise mark two
comparable runs as incomparable); ``config_hash`` fingerprints the
backtest-relevant configuration, which is what actually decides whether two
runs may be compared.

For SQLite the same migration also runs inline at startup
(``DatabaseManager._migrate_sqlite_five_layer_backtest_run_version_fields``).
This script is the deterministic offline path — useful when the DB lives on a
separate volume / managed host and you want to apply the change before the
service comes up.

Idempotent — re-running is safe.

NO BACKFILL — deliberate, do not add one: runs recorded before these columns
existed ran under an unknown commit and an unknown configuration. Writing the
current values onto them would fabricate provenance for exactly the rows whose
provenance is unknown. Legacy rows stay NULL, which stays distinguishable from
the explicit ``n/a`` that new runs write for a genuinely inapplicable field.

Usage:
    python scripts/migrate_backtest_run_versions.py
    python scripts/migrate_backtest_run_versions.py --db data/stocks.db
    python scripts/migrate_backtest_run_versions.py --dry-run

The default database path follows the main program: ``DATABASE_PATH`` if set,
otherwise ``<repo>/data/stock_analysis.db``. The resolved path is echoed on
every run.

NOTE: SQLite ALTER TABLE ADD COLUMN auto-commits, so a partial run that errors
mid-way may leave some columns added. The migration is idempotent and
re-running will skip the columns already present, so the safest recovery path
is to re-run without --dry-run.
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

TABLE = "five_layer_backtest_runs"
NEW_COLUMNS: List[Tuple[str, str]] = [
    ("code_revision", "VARCHAR(64)"),
    ("config_hash", "VARCHAR(64)"),
]


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

    if dry_run:
        return 0

    conn.commit()

    if not columns_added:
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
