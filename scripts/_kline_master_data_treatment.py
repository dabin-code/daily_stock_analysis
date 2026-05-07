"""一次性主数据治理：
  Phase A. 回填 instrument_master.list_date（NULL 的用 stock_daily.MIN(date) 补齐）
  Phase B. 把长期无新数据的 active 股票标记为 delisted
  Phase C. sweep kline_audit_gaps：把已经实际入库的 pending_retry gap 标 healthy

默认 --dry-run 只统计不写库；加 --apply 才真正写库。

退市阈值（B）：在 stock_daily 上的最近 100 个交易日里，
取第 N 个最远日期（N = --delist-threshold-trading-days，默认 30）作为分界，
last_seen 早于该日期的 cn-active 股票视为已退市/长期停牌。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import List, Tuple

DB_PATH = os.environ.get("DATABASE_PATH", "./data/stock_analysis.db")


def open_conn(path: str, readonly: bool) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{os.path.abspath(path)}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=10)
    return sqlite3.connect(path, timeout=30)


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def phase_a_backfill_list_date(conn: sqlite3.Connection, apply: bool) -> int:
    """回填 list_date：用 stock_daily.MIN(date) 补齐 NULL 的 cn-active 记录。"""
    section("[Phase A] 回填 instrument_master.list_date（NULL → stock_daily.MIN(date)）")

    rows = conn.execute(
        """
        SELECT im.code, MIN(sd.date) AS first_date
        FROM instrument_master im
        JOIN stock_daily sd ON sd.code = im.code
        WHERE im.market='cn'
          AND (im.list_date IS NULL OR im.list_date='')
        GROUP BY im.code
        HAVING first_date IS NOT NULL
        """
    ).fetchall()

    print(f"  待回填记录: {len(rows)} 条")
    if rows:
        # 按 first_date 分桶展示
        from collections import Counter
        years = Counter()
        for _, d in rows:
            years[d[:4] if d else "?"] += 1
        print("  首入库年份分布:")
        for year, cnt in sorted(years.items()):
            print(f"    {year}: {cnt}")
        print("  样例（按 first_date 倒序前 5）:")
        for code, d in sorted(rows, key=lambda x: x[1], reverse=True)[:5]:
            print(f"    {code}  first_date={d}")

    if apply and rows:
        cur = conn.cursor()
        cur.executemany(
            "UPDATE instrument_master SET list_date=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE code=? AND market='cn' AND (list_date IS NULL OR list_date='')",
            [(d, c) for c, d in rows],
        )
        conn.commit()
        print(f"  ✓ 已写入 {cur.rowcount} 条 UPDATE")
    elif rows:
        print("  (dry-run，未写库)")
    return len(rows)


def phase_b_mark_delisted(
    conn: sqlite3.Connection, apply: bool, threshold_trading_days: int
) -> int:
    """长期无新数据的 cn-active 股票标 delisted。"""
    section(f"[Phase B] 标记 delisted（阈值={threshold_trading_days} 个交易日无新数据）")

    # 在 stock_daily 上取最近的 100 个交易日，再取第 N 个最远日期作为分界
    recent_dates = [
        r[0] for r in conn.execute(
            "SELECT date FROM stock_daily GROUP BY date ORDER BY date DESC LIMIT 100"
        ).fetchall()
    ]
    if len(recent_dates) <= threshold_trading_days:
        print(f"  [WARN] stock_daily 仅有 {len(recent_dates)} 个交易日，无法应用阈值")
        return 0
    cutoff = recent_dates[threshold_trading_days]
    latest = recent_dates[0]
    print(f"  最近交易日: {latest}")
    print(f"  分界日（含此日及之后视为活跃）: {cutoff}")

    rows = conn.execute(
        """
        SELECT im.code, im.name,
               COALESCE((SELECT MAX(date) FROM stock_daily WHERE code=im.code),
                        '<no-data>') AS last_seen
        FROM instrument_master im
        WHERE im.market='cn'
          AND im.listing_status='active'
        """
    ).fetchall()

    candidates: List[Tuple[str, str, str]] = []
    for code, name, last_seen in rows:
        if last_seen == "<no-data>" or last_seen < cutoff:
            candidates.append((code, name or "", last_seen))

    print(f"  待剥离 active → delisted 候选: {len(candidates)} 只")
    if candidates:
        # 按 last_seen 升序展示，前 30 个
        ordered = sorted(candidates, key=lambda x: (x[2], x[0]))
        print(f"  {'code':10s} {'name':24s} {'last_seen':14s}")
        for code, name, last_seen in ordered[:30]:
            print(f"  {code:10s} {name[:24]:24s} {last_seen:14s}")
        if len(ordered) > 30:
            print(f"  ... 另有 {len(ordered) - 30} 只未列出")

    if apply and candidates:
        cur = conn.cursor()
        cur.executemany(
            "UPDATE instrument_master SET listing_status='delisted', "
            "updated_at=CURRENT_TIMESTAMP WHERE code=? AND market='cn' AND listing_status='active'",
            [(c,) for c, _, _ in candidates],
        )
        conn.commit()
        print(f"  ✓ 已写入 {cur.rowcount} 条 UPDATE（active → delisted）")
    elif candidates:
        print("  (dry-run，未写库)")
    return len(candidates)


def phase_c_sweep_gaps(conn: sqlite3.Connection, apply: bool) -> int:
    """sweep 已经实际入库的 pending_retry gap，标 healthy。"""
    section("[Phase C] sweep kline_audit_gaps：已入库的 pending_retry → healthy")

    # 取所有 pending_retry gap
    rows = conn.execute(
        """
        SELECT id, gap_scope, code, market, trade_date, missing_date_from, missing_date_to
        FROM kline_audit_gaps
        WHERE status='pending_retry'
        """
    ).fetchall()
    print(f"  pending_retry 总数: {len(rows)}")

    healthy_ids: List[int] = []
    still_missing_ids: List[int] = []

    # 预取已 delisted 的 code，对应 gap 视为已剥离（不再期望）
    delisted_codes = {
        r[0] for r in conn.execute(
            "SELECT code FROM instrument_master WHERE listing_status='delisted'"
        ).fetchall()
    }

    for gap_id, scope, code, market, trade_date, dfrom, dto in rows:
        is_healthy = False
        if code and code in delisted_codes:
            healthy_ids.append(gap_id)
            continue
        if scope == "market_day_gap":
            d = trade_date or dfrom
            if d:
                # 当日是否有 active 非ST 股票入库（覆盖率 > 95% 视为 healthy，跟 audit 容差一致）
                cnt = conn.execute(
                    "SELECT COUNT(DISTINCT sd.code) FROM stock_daily sd "
                    "JOIN instrument_master im ON im.code=sd.code "
                    "WHERE sd.date=? AND im.market=? AND im.listing_status='active' "
                    "AND COALESCE(im.is_st,0)=0",
                    (d, market or 'cn'),
                ).fetchone()[0]
                expected = conn.execute(
                    "SELECT COUNT(*) FROM instrument_master "
                    "WHERE market=? AND listing_status='active' AND COALESCE(is_st,0)=0",
                    (market or 'cn',),
                ).fetchone()[0]
                ratio = cnt / expected if expected else 0
                if ratio >= 0.95:
                    is_healthy = True
        elif scope == "symbol_range_gap" and code:
            start = dfrom or trade_date
            end = dto or trade_date
            if start and end:
                # 整段 [start, end] 内每个交易日（用 stock_daily 上的日期作为代理）都有该 code 入库
                expected_dates = [
                    r[0] for r in conn.execute(
                        "SELECT DISTINCT date FROM stock_daily "
                        "WHERE date BETWEEN ? AND ? ORDER BY date",
                        (start, end),
                    ).fetchall()
                ]
                actual = {
                    r[0] for r in conn.execute(
                        "SELECT date FROM stock_daily WHERE code=? AND date BETWEEN ? AND ?",
                        (code, start, end),
                    ).fetchall()
                }
                if expected_dates and all(d in actual for d in expected_dates):
                    is_healthy = True
        if is_healthy:
            healthy_ids.append(gap_id)
        else:
            still_missing_ids.append(gap_id)

    print(f"  实际已修复（待标 healthy）: {len(healthy_ids)}")
    print(f"  仍真实缺失（保持 pending_retry）: {len(still_missing_ids)}")

    if apply and healthy_ids:
        cur = conn.cursor()
        cur.executemany(
            "UPDATE kline_audit_gaps SET status='healthy', "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [(i,) for i in healthy_ids],
        )
        conn.commit()
        print(f"  ✓ 已写入 {cur.rowcount} 条 UPDATE")
    elif healthy_ids:
        print("  (dry-run，未写库)")
    return len(healthy_ids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="真正写库（不加则 dry-run）")
    parser.add_argument("--delist-threshold-trading-days", type=int, default=30,
                        help="多少个交易日没新数据视为退市（默认 30）")
    parser.add_argument("--phases", default="abc",
                        help="执行的阶段子集，比如 'a' / 'ab' / 'abc'（默认 abc）")
    args = parser.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"[ERROR] DB not found: {DB_PATH}")
        return 2

    conn = open_conn(DB_PATH, readonly=not args.apply)
    print(f"DB: {os.path.abspath(DB_PATH)}  mode={'WRITE' if args.apply else 'DRY-RUN'}")

    a = b = c = 0
    if "a" in args.phases:
        a = phase_a_backfill_list_date(conn, args.apply)
    if "b" in args.phases:
        b = phase_b_mark_delisted(conn, args.apply, args.delist_threshold_trading_days)
    if "c" in args.phases:
        c = phase_c_sweep_gaps(conn, args.apply)

    section("总览")
    print(f"  Phase A 回填 list_date         : {a}")
    print(f"  Phase B 标记 delisted          : {b}")
    print(f"  Phase C sweep healthy 的 gap   : {c}")
    if not args.apply:
        print()
        print("  ↑↑↑ DRY-RUN，未写库。确认后加 --apply 重新执行。")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
