# -*- coding: utf-8 -*-
"""把 stock_daily_staging 晋升为生产表 stock_daily。

前置：必须先跑 scripts/validate_staging_before_promotion.py 且全部通过，
并且已用 scripts/backup_production_db.py 做过备份。

为什么不是「清空 stock_daily 再灌 staging」：
    生产表里除了 A 股个股，还有指数（sh000001 上证指数、sh000905 中证 500）和
    个别港股，这些 staging 的回补范围并不覆盖。sh000001 正是配置项
    screening_market_guard_index 的默认值，清表会直接打掉大盘风控的数据源。
    因此这里按「staging 覆盖到的 code 集合」删除，其余行原样保留。

关于会被清空的列：
    ma5 / ma10 / ma20 / volume_ratio / adj_factor / adj_factor_source 在 staging
    里是空的，晋升后这些列变 NULL。这是有意为之——生产表原有的均线是用前复权价
    算的，与即将写入的原始价不配套，保留会得到「价格是原始价、均线是前复权均线」
    的错配。实际消费方 src/services/factor_service.py 是从收盘价序列现算这些
    指标的，不读存储列，因此清空无影响。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime

PROD = "stock_daily"
STAGING = "stock_daily_staging"

# staging 独有、不进生产表的列
STAGING_ONLY = {"batch_id", "convention_version"}


def _default_db_path() -> str:
    return os.getenv("DATABASE_PATH") or os.path.join("data", "stock_analysis.db")


def _columns(cur: sqlite3.Cursor, table: str) -> list[str]:
    return [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Promote stock_daily_staging into production stock_daily"
    )
    parser.add_argument("--db", default=None, help="database path")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="真正执行；缺省只打印计划（dry-run）",
    )
    args = parser.parse_args()

    db_path = args.db or _default_db_path()
    print(f"[info] database: {os.path.abspath(db_path)}")
    if not os.path.exists(db_path):
        print(f"[error] database not found: {db_path}")
        return 1

    con = sqlite3.connect(db_path, timeout=30)
    cur = con.cursor()

    prod_cols = _columns(cur, PROD)
    stag_cols = _columns(cur, STAGING)

    shared = [
        c for c in prod_cols
        if c != "id" and c in stag_cols and c not in STAGING_ONLY
    ]
    missing = [c for c in prod_cols if c != "id" and c not in stag_cols]
    if missing:
        print(f"[error] staging 缺少生产表的列，晋升会丢数据: {missing}")
        con.close()
        return 1

    cur.execute(f"SELECT COUNT(*) FROM {PROD}")
    prod_before = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM {STAGING}")
    stag_total = cur.fetchone()[0]

    if not stag_total:
        print(f"[error] {STAGING} 为空，拒绝晋升")
        con.close()
        return 1

    cur.execute(
        f"SELECT COUNT(*) FROM {PROD} WHERE code IN"
        f" (SELECT DISTINCT code FROM {STAGING})"
    )
    to_delete = cur.fetchone()[0]
    preserved = prod_before - to_delete

    cur.execute(
        f"""
        SELECT COUNT(DISTINCT code) FROM {PROD} WHERE code NOT IN
            (SELECT DISTINCT code FROM {STAGING})
        """
    )
    preserved_codes = cur.fetchone()[0]

    print(f"\n[plan] 生产表现有 {prod_before} 行")
    print(f"[plan] 删除 staging 覆盖到的 code: {to_delete} 行")
    print(f"[plan] 保留非 staging 代码: {preserved} 行 / {preserved_codes} 个代码")
    print(f"[plan] 写入 staging: {stag_total} 行")
    print(f"[plan] 晋升后预计: {preserved + stag_total} 行")
    print(f"[plan] 复制列 ({len(shared)}): {', '.join(shared)}")

    if not args.execute:
        print("\n[dry-run] 未做任何改动。确认无误后加 --execute 执行。")
        con.close()
        return 0

    print(f"\n[info] 开始晋升 {datetime.now():%H:%M:%S}")
    col_list = ", ".join(f'"{c}"' for c in shared)
    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            f"DELETE FROM {PROD} WHERE code IN"
            f" (SELECT DISTINCT code FROM {STAGING})"
        )
        deleted = cur.rowcount
        print(f"[info] 已删除 {deleted} 行 {datetime.now():%H:%M:%S}")
        cur.execute(
            f"INSERT INTO {PROD} ({col_list})"
            f" SELECT {col_list} FROM {STAGING}"
        )
        inserted = cur.rowcount
        print(f"[info] 已写入 {inserted} 行 {datetime.now():%H:%M:%S}")
        con.commit()
    except sqlite3.Error as exc:
        con.rollback()
        print(f"[error] 晋升失败并已回滚: {exc}")
        con.close()
        return 1

    cur.execute(f"SELECT COUNT(*), MIN(date), MAX(date) FROM {PROD}")
    total, dmin, dmax = cur.fetchone()
    print(f"\n[ok] 晋升完成：{total} 行 / {dmin} ~ {dmax}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
