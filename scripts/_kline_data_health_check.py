"""一次性只读体检脚本：检查 stock_daily 覆盖度 + 缺口治理状态。

只读连接（mode=ro），不会和正在跑的 repair 进程冲突。
不入仓库流程，跑完即丢。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections import Counter
from datetime import date, timedelta

DB_PATH = os.environ.get("DATABASE_PATH", "./data/stock_analysis.db")


def open_ro(path: str) -> sqlite3.Connection:
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def fetch_one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> tuple:
    return conn.execute(sql, params).fetchone()


def fetch_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
    return conn.execute(sql, params).fetchall()


def main() -> int:
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] DB not found: {DB_PATH}")
        return 2

    conn = open_ro(DB_PATH)

    # 列出所有表
    tables = {row[0] for row in fetch_all(
        conn, "SELECT name FROM sqlite_master WHERE type='table'"
    )}

    section("1. 数据库基本信息")
    print(f"DB 路径    : {os.path.abspath(DB_PATH)}")
    print(f"DB 大小    : {os.path.getsize(DB_PATH) / 1024 / 1024:.2f} MB")
    print(f"WAL 文件   : "
          f"{'存在' if os.path.exists(DB_PATH + '-wal') else '无'}"
          f" / SHM "
          f"{'存在' if os.path.exists(DB_PATH + '-shm') else '无'}")
    print(f"表数       : {len(tables)}")
    print(f"关键表     : "
          f"stock_daily={'OK' if 'stock_daily' in tables else 'MISSING'}, "
          f"instrument_master={'OK' if 'instrument_master' in tables else 'MISSING'}, "
          f"kline_audit_runs={'OK' if 'kline_audit_runs' in tables else 'MISSING'}, "
          f"kline_audit_gaps={'OK' if 'kline_audit_gaps' in tables else 'MISSING'}")

    if "instrument_master" in tables:
        section("2. instrument_master 主数据画像 (cn 市场)")
        rows = fetch_all(
            conn,
            "SELECT listing_status, COUNT(*) FROM instrument_master "
            "WHERE market='cn' GROUP BY listing_status ORDER BY 2 DESC",
        )
        for status, count in rows:
            print(f"  {status or '<null>':12s} {count:>6d}")
        is_st_rows = fetch_all(
            conn,
            "SELECT is_st, COUNT(*) FROM instrument_master "
            "WHERE market='cn' AND listing_status='active' GROUP BY is_st",
        )
        for flag, count in is_st_rows:
            print(f"  active.is_st={str(flag):5s}  {count:>6d}")

    if "stock_daily" in tables:
        section("3. stock_daily 总览")
        total_rows, codes_total = fetch_one(
            conn,
            "SELECT COUNT(*), COUNT(DISTINCT code) FROM stock_daily",
        )
        date_min, date_max = fetch_one(
            conn,
            "SELECT MIN(date), MAX(date) FROM stock_daily",
        )
        print(f"  总行数      : {total_rows:,}")
        print(f"  覆盖股票数  : {codes_total:,}")
        print(f"  日期范围    : {date_min} → {date_max}")

        section("4. 每个 cn-active 股票最新数据日期分布")
        rows = fetch_all(
            conn,
            """
            SELECT latest_date, COUNT(*) AS code_cnt
            FROM (
                SELECT im.code, MAX(sd.date) AS latest_date
                FROM instrument_master im
                LEFT JOIN stock_daily sd ON sd.code = im.code
                WHERE im.market='cn' AND im.listing_status='active' AND COALESCE(im.is_st, 0)=0
                GROUP BY im.code
            )
            GROUP BY latest_date
            ORDER BY latest_date DESC
            LIMIT 15
            """,
        )
        active_total = fetch_one(
            conn,
            "SELECT COUNT(*) FROM instrument_master "
            "WHERE market='cn' AND listing_status='active' AND COALESCE(is_st,0)=0",
        )[0]
        print(f"  cn-active(非ST) 总数: {active_total}")
        print(f"  {'最新数据日期':16s} {'股票数':>10s}  {'占比':>8s}")
        for d, cnt in rows:
            label = d if d else "<无任何数据>"
            ratio = cnt / active_total * 100 if active_total else 0
            print(f"  {label:16s} {cnt:>10d}  {ratio:>7.2f}%")

        section("5. 最近 100 个交易日（按日聚合）的覆盖度")
        recent_dates = [
            r[0] for r in fetch_all(
                conn,
                "SELECT date FROM stock_daily GROUP BY date ORDER BY date DESC LIMIT 100",
            )
        ]
        per_day_stats = []
        for d in recent_dates:
            cnt = fetch_one(
                conn,
                "SELECT COUNT(DISTINCT sd.code) "
                "FROM stock_daily sd JOIN instrument_master im ON im.code=sd.code "
                "WHERE sd.date=? AND im.market='cn' AND im.listing_status='active' "
                "AND COALESCE(im.is_st,0)=0",
                (d,),
            )[0]
            missing = active_total - cnt
            ratio = cnt / active_total * 100 if active_total else 0
            per_day_stats.append((d, cnt, missing, ratio))

        # 5a 概览
        days_full = sum(1 for _, _, m, _ in per_day_stats if m == 0)
        days_lt_99 = sum(1 for _, _, _, r in per_day_stats if r < 99.0)
        days_lt_100 = sum(1 for _, _, m, _ in per_day_stats if m > 0)
        total_missing_stock_days = sum(m for _, _, m, _ in per_day_stats)
        worst_day = min(per_day_stats, key=lambda x: x[3]) if per_day_stats else None
        print(f"  窗口大小                   : {len(per_day_stats)} 个交易日")
        print(f"  100% 完整的天数            : {days_full}")
        print(f"  覆盖率 < 100% 的天数       : {days_lt_100}")
        print(f"  覆盖率 < 99%  的天数       : {days_lt_99}")
        print(f"  累计缺失 stock-day         : {total_missing_stock_days:,}")
        if worst_day:
            print(f"  最差一天                   : "
                  f"{worst_day[0]}  {worst_day[1]}/{active_total}  ({worst_day[3]:.2f}%)")

        # 5b 列出所有 < 100% 的日期
        print()
        print(f"  [覆盖率 < 100% 的日期，按时间倒序]")
        print(f"  {'日期':12s} {'入库':>8s} {'缺失':>8s} {'覆盖率':>10s}")
        any_lt_100 = False
        for d, cnt, missing, ratio in per_day_stats:
            if missing > 0:
                any_lt_100 = True
                print(f"  {d:12s} {cnt:>8d} {missing:>8d} {ratio:>9.2f}%")
        if not any_lt_100:
            print("  （全部 100% 完美，无需操作）")

        # 5c 在 100 个交易日窗口内，至少有 1 天缺数据的代码
        if recent_dates:
            window_start = recent_dates[-1]
            window_end = recent_dates[0]
            section("6. 100 个交易日窗口内：每只 cn-active 股票的缺失天数 Top 30")
            print(f"  窗口: {window_start} → {window_end}（{len(recent_dates)} 个交易日）")
            top_missing = fetch_all(
                conn,
                """
                WITH active_codes AS (
                    SELECT code FROM instrument_master
                    WHERE market='cn' AND listing_status='active' AND COALESCE(is_st,0)=0
                ),
                expected AS (
                    SELECT a.code, d.date FROM active_codes a
                    CROSS JOIN (
                        SELECT DISTINCT date FROM stock_daily
                        WHERE date BETWEEN ? AND ?
                    ) d
                ),
                actual AS (
                    SELECT code, date FROM stock_daily
                    WHERE date BETWEEN ? AND ?
                )
                SELECT e.code, COUNT(*) AS missing_days
                FROM expected e
                LEFT JOIN actual a ON a.code = e.code AND a.date = e.date
                WHERE a.date IS NULL
                GROUP BY e.code
                ORDER BY missing_days DESC, e.code
                LIMIT 30
                """,
                (window_start, window_end, window_start, window_end),
            )
            total_codes_with_gap = fetch_one(
                conn,
                """
                WITH active_codes AS (
                    SELECT code FROM instrument_master
                    WHERE market='cn' AND listing_status='active' AND COALESCE(is_st,0)=0
                ),
                expected AS (
                    SELECT a.code, d.date FROM active_codes a
                    CROSS JOIN (
                        SELECT DISTINCT date FROM stock_daily
                        WHERE date BETWEEN ? AND ?
                    ) d
                ),
                actual AS (
                    SELECT code, date FROM stock_daily
                    WHERE date BETWEEN ? AND ?
                )
                SELECT COUNT(DISTINCT e.code)
                FROM expected e
                LEFT JOIN actual a ON a.code = e.code AND a.date = e.date
                WHERE a.date IS NULL
                """,
                (window_start, window_end, window_start, window_end),
            )[0]
            print(f"  窗口内至少缺 1 天的 active 股票数: {total_codes_with_gap}")
            print()
            print(f"  {'code':10s} {'缺失天数':>10s}  说明")
            for code, missing_days in top_missing:
                # 取该 code 最新有数据的日期
                last_date = fetch_one(
                    conn,
                    "SELECT MAX(date) FROM stock_daily WHERE code=?",
                    (code,),
                )[0]
                # 取 listing_status / list_date
                meta = fetch_one(
                    conn,
                    "SELECT listing_status, list_date FROM instrument_master WHERE code=?",
                    (code,),
                )
                meta_str = f"status={meta[0]}, list_date={meta[1]}" if meta else "(no meta)"
                print(f"  {code:10s} {missing_days:>10d}  last_date={last_date or '-'}  {meta_str}")

    if "kline_audit_runs" in tables:
        section("7. 最近 5 次 audit run")
        rows = fetch_all(
            conn,
            """
            SELECT id, run_type, trigger_type, market, trade_date,
                   pass_status, run_result,
                   COALESCE(completed_at, created_at) AS ts
            FROM kline_audit_runs
            ORDER BY id DESC LIMIT 5
            """,
        )
        print(f"  {'id':>5s} {'type':10s} {'trig':10s} {'mkt':4s} {'date':12s} "
              f"{'pass':10s} {'result':10s} {'when':20s}")
        for r in rows:
            print(f"  {r[0]:>5d} {str(r[1]):10s} {str(r[2]):10s} {str(r[3]):4s} "
                  f"{str(r[4]):12s} {str(r[5]):10s} {str(r[6]):10s} {str(r[7])[:19]:20s}")

    if "kline_audit_gaps" in tables:
        section("8. kline_audit_gaps 状态分布")
        rows = fetch_all(
            conn,
            "SELECT status, gap_scope, COUNT(*) FROM kline_audit_gaps "
            "GROUP BY status, gap_scope ORDER BY status, gap_scope",
        )
        agg_status: Counter = Counter()
        for status, gscope, cnt in rows:
            print(f"  {str(status):16s} {str(gscope):20s} {cnt:>8d}")
            agg_status[status] += cnt
        print()
        print(f"  {'按 status 汇总':16s}")
        for status, cnt in agg_status.most_common():
            print(f"    {str(status):16s} {cnt:>8d}")

        section("9. pending_retry / candidate_skip 缺口的 trade_date 最近分布")
        for status in ("pending_retry", "candidate_skip"):
            rows = fetch_all(
                conn,
                """
                SELECT
                    COALESCE(trade_date, missing_date_from, '<none>') AS d,
                    COUNT(*)
                FROM kline_audit_gaps
                WHERE status=?
                GROUP BY d
                ORDER BY d DESC
                LIMIT 10
                """,
                (status,),
            )
            if rows:
                print(f"\n  [{status}] 按 trade_date 倒序前 10:")
                for d, cnt in rows:
                    print(f"    {str(d):14s} {cnt:>6d}")
            else:
                print(f"\n  [{status}] 无记录")

        section("10. 当前 pending_retry 缺口示例（最旧 10 条）")
        rows = fetch_all(
            conn,
            """
            SELECT id, COALESCE(code,'*') AS code,
                   COALESCE(trade_date, missing_date_from) AS dfrom,
                   missing_date_to, gap_scope, updated_at
            FROM kline_audit_gaps
            WHERE status='pending_retry'
            ORDER BY dfrom ASC, id ASC
            LIMIT 10
            """,
        )
        for r in rows:
            print(f"  id={r[0]:>6d} code={r[1]:8s} {r[2]} → {r[3]} scope={r[4]} updated={r[5]}")

    section("11. 体检结论建议")
    print("  · 看 第 4 节 '最新数据日期分布' 判断主数据是否同步到最近交易日")
    print("  · 看 第 5 节 '最近 100 个交易日覆盖度' 判断每日 sync 是否健康")
    print("  · 看 第 6 节 '窗口内单股缺失天数 Top 30' 找出真正影响完整性的代码")
    print("  · 看 第 8 节 'kline_audit_gaps 状态分布' 看治理欠账规模")
    print("  · 持续未恢复的 pending_retry 通常是真无源（已退市/停牌）→ 刷新 instrument_master 或 approve_skip")

    return 0


if __name__ == "__main__":
    sys.exit(main())
