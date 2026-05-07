"""检查 274 条 pending_retry 的真实情况：
  - gap 内每天的实际入库情况
  - listing_status
  - 与 stock_daily 真正缺的天数
"""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict

DB_PATH = os.environ.get("DATABASE_PATH", "./data/stock_analysis.db")


def main() -> None:
    conn = sqlite3.connect(f"file:{os.path.abspath(DB_PATH)}?mode=ro", uri=True)

    rows = conn.execute(
        """
        SELECT id, gap_scope, code, market, trade_date, missing_date_from, missing_date_to
        FROM kline_audit_gaps
        WHERE status='pending_retry'
        """
    ).fetchall()
    print(f"总 pending_retry: {len(rows)}")

    bucket = {
        "delisted": [],
        "fully_missing": [],   # 区间内整段无数据
        "partially_missing": [],  # 区间内部分日期缺
        "fully_filled": [],    # 区间内每天都有数据（理论应 sweep）
        "no_window": [],       # 没有可计算窗口
    }

    for gid, scope, code, market, td, dfrom, dto in rows:
        if scope == "market_day_gap":
            d = td or dfrom
            if not d:
                bucket["no_window"].append((gid, scope, code, td, dfrom, dto))
                continue
            cnt = conn.execute(
                "SELECT COUNT(*) FROM stock_daily WHERE date=?", (d,)
            ).fetchone()[0]
            if cnt == 0:
                bucket["fully_missing"].append((gid, scope, code, d, d, 0, 0))
            else:
                bucket["fully_filled"].append((gid, scope, code, d, d, cnt, 1))
            continue

        # symbol_range_gap
        start = dfrom or td
        end = dto or td
        if not (code and start and end):
            bucket["no_window"].append((gid, scope, code, td, dfrom, dto))
            continue

        listing = conn.execute(
            "SELECT listing_status FROM instrument_master WHERE code=?",
            (code,),
        ).fetchone()
        if listing and listing[0] == "delisted":
            bucket["delisted"].append((gid, scope, code, start, end))
            continue

        expected_dates = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT date FROM stock_daily "
                "WHERE date BETWEEN ? AND ? ORDER BY date",
                (start, end),
            ).fetchall()
        ]
        actual_dates = {
            r[0] for r in conn.execute(
                "SELECT date FROM stock_daily WHERE code=? AND date BETWEEN ? AND ?",
                (code, start, end),
            ).fetchall()
        }
        n_exp = len(expected_dates)
        n_act = len(actual_dates)
        n_miss = n_exp - n_act

        if n_exp == 0:
            bucket["no_window"].append((gid, scope, code, td, dfrom, dto))
        elif n_miss == 0:
            bucket["fully_filled"].append((gid, scope, code, start, end, n_act, n_exp))
        elif n_act == 0:
            bucket["fully_missing"].append((gid, scope, code, start, end, n_act, n_exp))
        else:
            bucket["partially_missing"].append((gid, scope, code, start, end, n_act, n_exp, n_miss))

    print()
    print(f"分类汇总：")
    print(f"  已 delisted（应 reclassify）       : {len(bucket['delisted'])}")
    print(f"  区间整段无数据 (fully_missing)     : {len(bucket['fully_missing'])}")
    print(f"  区间部分缺 (partially_missing)     : {len(bucket['partially_missing'])}")
    print(f"  区间全部已入库 (fully_filled)      : {len(bucket['fully_filled'])}")
    print(f"  无可计算窗口 (no_window)           : {len(bucket['no_window'])}")

    if bucket["delisted"]:
        print()
        print(f"--- delisted gap 例样（前 10）---")
        for r in bucket["delisted"][:10]:
            print(f"  {r}")

    if bucket["fully_missing"]:
        print()
        print(f"--- fully_missing gap 例样（前 10）---")
        for r in bucket["fully_missing"][:10]:
            print(f"  {r}")

    if bucket["partially_missing"]:
        print()
        print(f"--- partially_missing gap 例样（前 10）---")
        for r in bucket["partially_missing"][:10]:
            print(f"  id={r[0]} {r[1]} code={r[2]} {r[3]}~{r[4]} 入库 {r[5]}/{r[6]} 缺 {r[7]}")

    if bucket["fully_filled"]:
        print()
        print(f"--- fully_filled gap 例样（前 10，应该 sweep）---")
        for r in bucket["fully_filled"][:10]:
            print(f"  {r}")


if __name__ == "__main__":
    main()
