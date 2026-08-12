"""
DB migration: Add ``raw_rule_score`` column to ``screening_candidates``.

For SQLite the same migration also runs inline at startup
(``DatabaseManager._migrate_sqlite_screening_candidate_raw_rule_score``).
This script is a deterministic offline path you can run before bringing the
service up — useful when the DB lives on a separate volume / managed host.

Idempotent — re-running is safe.

Background:
    The five-layer priority sort overwrites ``rule_score`` in place with
    ``stage + pool + theme + rule_score * 0.01``, so every persisted row holds
    the composite priority score and the screener's original quality score was
    lost. ``raw_rule_score`` keeps that original score, which is what
    continuous-strength analysis (Rank IC) needs — the composite score is
    dominated by three discrete priorities and is far too coarse for rank
    correlation.

NO BACKFILL — deliberate, do not add one:
    Existing rows only ever stored the composite score. The pre-overwrite
    quality score is not recoverable from it (the priority terms are not
    separable), so any value written would be fabricated. Legacy rows stay
    NULL and consumers must treat NULL as "unknown".

Usage:
    python scripts/migrate_screening_candidate_raw_rule_score.py
    python scripts/migrate_screening_candidate_raw_rule_score.py --db data/stocks.db
    python scripts/migrate_screening_candidate_raw_rule_score.py --dry-run

The default database path follows the main program: ``DATABASE_PATH`` if set,
otherwise ``<repo>/data/stock_analysis.db``. That matters exactly in the
separate-volume / managed-host case above, where migrating a same-named file
inside the repo would silently leave the real database untouched. The resolved
path is echoed on every run.

NOTE: SQLite ALTER TABLE ADD COLUMN auto-commits, so a partial run that errors
mid-way may leave the column added. The migration is idempotent and re-running
will skip a column already present, so the safest recovery path is to re-run
without --dry-run.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FALLBACK_DB_PATH = REPO_ROOT / "data" / "stock_analysis.db"

TABLE = "screening_candidates"
NEW_COLUMN = "raw_rule_score"
NEW_COLUMN_TYPE = "FLOAT"


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

    if NEW_COLUMN in _columns(cursor, TABLE):
        print(f"[skip] Column {NEW_COLUMN} already exists.")
        print("[done] No changes applied — migration already complete.")
        return 0

    if dry_run:
        print(f"[dry-run] Would ADD COLUMN {NEW_COLUMN} {NEW_COLUMN_TYPE} on {TABLE}")
        print("[dry-run] No back-fill — existing rows stay NULL.")
        return 0

    cursor.execute(f"ALTER TABLE {TABLE} ADD COLUMN {NEW_COLUMN} {NEW_COLUMN_TYPE}")
    print(f"[ok] Added column: {NEW_COLUMN} {NEW_COLUMN_TYPE}")
    conn.commit()
    print("[done] Migration complete. Existing rows stay NULL.")
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
