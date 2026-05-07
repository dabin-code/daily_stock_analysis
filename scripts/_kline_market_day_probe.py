"""验证 pending_retry 集中的 2026-03-30 起这段时间是不是真的交易日，
以及 stock_daily 里这段窗口的实际入库情况。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta

DB_PATH = os.environ.get("DATABASE_PATH", "./data/stock_analysis.db")


def main() -> None:
    conn = sqlite3.connect(f"file:{os.path.abspath(DB_PATH)}?mode=ro", uri=True)
    cur = conn.cursor()

    print("=" * 70)
    print("[A] stock_daily 在 2026-03-25 ~ 2026-04-15 的每日入库行数")
    print("=" * 70)
    rows = cur.execute(
        "SELECT date, COUNT(*) FROM stock_daily "
        "WHERE date BETWEEN '2026-03-25' AND '2026-04-15' "
        "GROUP BY date ORDER BY date"
    ).fetchall()
    print(f"  {'date':12s} {'rows':>8s}")
    for d, c in rows:
        print(f"  {d:12s} {c:>8d}")

    print()
    print("=" * 70)
    print("[B] exchange_calendars 判定上海交易所 2026-03-25 ~ 2026-04-15 哪些是交易日")
    print("=" * 70)
    try:
        import exchange_calendars as ecals
        cal = ecals.get_calendar("XSHG")
        d = date(2026, 3, 25)
        end = date(2026, 4, 15)
        while d <= end:
            is_open = cal.is_session(d.isoformat())
            print(f"  {d.isoformat()}  weekday={d.strftime('%a')}  trading={is_open}")
            d += timedelta(days=1)
    except Exception as exc:
        print(f"  [无法加载 exchange_calendars] {exc}")

    print()
    print("=" * 70)
    print("[C] 看一只 pending_retry 例样代码 000959 在 stock_daily 实际入库情况")
    print("=" * 70)
    rows = cur.execute(
        "SELECT date, data_source FROM stock_daily WHERE code='000959' "
        "AND date BETWEEN '2026-03-25' AND '2026-04-15' ORDER BY date"
    ).fetchall()
    if not rows:
        print("  000959 在窗口内完全无数据（→ pending_retry 真实存在）")
    else:
        for d, src in rows:
            print(f"  {d}  source={src}")


if __name__ == "__main__":
    main()
