"""分类排查 100 个交易日窗口的缺口根因。

输出：
  A. instrument_master.list_date 填充质量
  B. 2026-03-23 灾难日的实际入库情况（是否真的全市场失败）
  C. Top 30 缺口股的"自身首入库日 vs list_date"对比
     -- 用 stock_daily 上该 code 的最早出现日期作为它实际能拉到数据的起点
     -- 如果首入库日 > 窗口起点，说明这只股票根本不应该期望窗口起点的数据
  D. 100 天窗口扣除"上市前缺口"后的真实欠账规模
"""

from __future__ import annotations

import os
import sqlite3

DB_PATH = os.environ.get("DATABASE_PATH", "./data/stock_analysis.db")


def main() -> None:
    conn = sqlite3.connect(f"file:{os.path.abspath(DB_PATH)}?mode=ro", uri=True)
    cur = conn.cursor()

    # 100 天窗口
    recent = [r[0] for r in cur.execute(
        "SELECT date FROM stock_daily GROUP BY date ORDER BY date DESC LIMIT 100"
    ).fetchall()]
    window_start, window_end = recent[-1], recent[0]
    print(f"窗口: {window_start} → {window_end}（{len(recent)} 个交易日）")

    print()
    print("=" * 70)
    print("[A] instrument_master.list_date 填充质量（cn-active 非ST）")
    print("=" * 70)
    rows = cur.execute(
        """
        SELECT
            CASE WHEN list_date IS NULL OR list_date='' THEN 'NULL'
                 WHEN list_date < '2025-01-01' THEN '<2025-01-01'
                 WHEN list_date < '2026-01-01' THEN '2025-xx'
                 ELSE '>=2026-01-01' END AS bucket,
            COUNT(*)
        FROM instrument_master
        WHERE market='cn' AND listing_status='active' AND COALESCE(is_st,0)=0
        GROUP BY bucket
        ORDER BY bucket
        """
    ).fetchall()
    for bucket, cnt in rows:
        print(f"  {bucket:18s} {cnt:>6d}")

    print()
    print("=" * 70)
    print("[B] 2026-03-23 灾难日：实际入库哪 11 只？")
    print("=" * 70)
    rows = cur.execute(
        "SELECT code, data_source FROM stock_daily WHERE date='2026-03-23' "
        "ORDER BY code"
    ).fetchall()
    for code, src in rows:
        print(f"  {code}  source={src}")
    print(f"  → 共 {len(rows)} 只入库，结合上下文判断：当日 sync 任务大概率根本没跑")

    print()
    print("=" * 70)
    print("[C] Top 30 高缺口股票：'首入库日' vs 窗口起点")
    print("=" * 70)
    top_codes = [
        '603056', '920036', '301680', '920187', '920183', '920168',
        '920166', '688816', '603284', '688818', '920180', '688712',
        '001220', '920119', '601112', '688785', '920159', '920076',
        '920050', '920086', '603352', '603402', '301687', '920045',
        '001369', '001396', '688809', '002931', '920121', '688805',
    ]
    print(f"  {'code':10s} {'首入库日':12s} {'最末入库日':12s} {'窗口内入库':>10s} "
          f"{'list_date':12s} {'listing':10s}")
    for code in top_codes:
        r = cur.execute(
            "SELECT MIN(date), MAX(date) FROM stock_daily WHERE code=?",
            (code,),
        ).fetchone()
        first_date, last_date = r if r else (None, None)
        in_win = cur.execute(
            "SELECT COUNT(*) FROM stock_daily WHERE code=? AND date BETWEEN ? AND ?",
            (code, window_start, window_end),
        ).fetchone()[0]
        meta = cur.execute(
            "SELECT list_date, listing_status FROM instrument_master WHERE code=?",
            (code,),
        ).fetchone()
        list_date = (meta[0] if meta else None) or '<null>'
        status = (meta[1] if meta else None) or '-'
        print(f"  {code:10s} {(first_date or '-'):12s} {(last_date or '-'):12s} "
              f"{in_win:>10d} {list_date:12s} {status:10s}")

    print()
    print("=" * 70)
    print("[D] 真实欠账：扣除上市前 / 退市后空缺，每只 code 在 [list_proxy, last_proxy] 内的缺失")
    print("=" * 70)
    # 用 stock_daily 自身的 (MIN(date), MAX(date)) 作为该 code 真实可拉数据的窗口
    # 真实缺口 = expected_dates ∩ [first_seen, last_seen] - actual_dates
    real_gap = cur.execute(
        """
        WITH active_codes AS (
            SELECT code FROM instrument_master
            WHERE market='cn' AND listing_status='active' AND COALESCE(is_st,0)=0
        ),
        code_window AS (
            SELECT a.code, MIN(sd.date) AS first_seen, MAX(sd.date) AS last_seen
            FROM active_codes a
            LEFT JOIN stock_daily sd ON sd.code=a.code
            GROUP BY a.code
        ),
        win_dates AS (
            SELECT DISTINCT date FROM stock_daily WHERE date BETWEEN ? AND ?
        ),
        expected AS (
            SELECT cw.code, wd.date
            FROM code_window cw
            CROSS JOIN win_dates wd
            WHERE cw.first_seen IS NOT NULL
              AND wd.date >= cw.first_seen
              AND wd.date <= cw.last_seen
        ),
        actual AS (
            SELECT code, date FROM stock_daily WHERE date BETWEEN ? AND ?
        )
        SELECT COUNT(*) FROM expected e
        LEFT JOIN actual a ON a.code=e.code AND a.date=e.date
        WHERE a.date IS NULL
        """,
        (window_start, window_end, window_start, window_end),
    ).fetchone()[0]
    print(f"  扣除上市前/退市后后，100 天窗口内真实缺失的 stock-day 数：{real_gap:,}")
    print(f"  （对照：原始口径 7,612；差距即来自 list_date 不准 / 已退市的虚假期望）")

    # Top 真实缺口股
    print()
    print(f"  按真实缺失天数排序的 Top 20:")
    rows = cur.execute(
        """
        WITH active_codes AS (
            SELECT code FROM instrument_master
            WHERE market='cn' AND listing_status='active' AND COALESCE(is_st,0)=0
        ),
        code_window AS (
            SELECT a.code, MIN(sd.date) AS first_seen, MAX(sd.date) AS last_seen
            FROM active_codes a
            LEFT JOIN stock_daily sd ON sd.code=a.code
            GROUP BY a.code
        ),
        win_dates AS (
            SELECT DISTINCT date FROM stock_daily WHERE date BETWEEN ? AND ?
        ),
        expected AS (
            SELECT cw.code, wd.date, cw.first_seen, cw.last_seen
            FROM code_window cw
            CROSS JOIN win_dates wd
            WHERE cw.first_seen IS NOT NULL
              AND wd.date >= cw.first_seen
              AND wd.date <= cw.last_seen
        ),
        actual AS (
            SELECT code, date FROM stock_daily WHERE date BETWEEN ? AND ?
        )
        SELECT e.code, COUNT(*) AS missing, MIN(e.first_seen), MAX(e.last_seen)
        FROM expected e
        LEFT JOIN actual a ON a.code=e.code AND a.date=e.date
        WHERE a.date IS NULL
        GROUP BY e.code
        ORDER BY missing DESC, e.code
        LIMIT 20
        """,
        (window_start, window_end, window_start, window_end),
    ).fetchall()
    print(f"  {'code':10s} {'真实缺失':>8s}  {'首入库':12s} {'末入库':12s}")
    for code, missing, first_seen, last_seen in rows:
        print(f"  {code:10s} {missing:>8d}  {first_seen:12s} {last_seen:12s}")


if __name__ == "__main__":
    main()
