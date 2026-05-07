"""扫描最近 N 个交易日，对每个覆盖率 < 100% 的日期跑一次 sync_trade_date。

策略：
  1. 从 stock_daily 上取最近 N 个交易日；
  2. 用 instrument_master 当前 cn-active 非ST 集合（Phase A/B 治理后）作为 expected universe；
  3. 计算每日实际入库数 vs expected，跳过已 100% 完整的日期；
  4. 对剩余日期调 MarketDataSyncService.sync_trade_date(force=False)
     —— bulk 一次拉全市场，bulk 哨兵自动跳过 universe 之外的代码；
  5. 打印每日处理结果。

不走 KlineRepairService，是因为 repair 对 symbol_range_gap 逐 (code, date) 调 sync，
对当前规模（5298 单股缺口集中在 2026-03-23）效率太低。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.market_data_sync_service import MarketDataSyncService
from src.storage import DatabaseManager


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=100, help="回看交易日数（默认 100）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不发外网请求")
    parser.add_argument("--force", action="store_true", help="对每个目标日跑 force=True")
    args = parser.parse_args()

    db = DatabaseManager.get_instance()
    sync = MarketDataSyncService(db_manager=db)

    # 取 expected active universe 数量
    active_count = db.list_instruments(market="cn", listing_status="active", exclude_st=True)
    expected_total = len(active_count)
    print(f"expected universe (cn-active 非ST): {expected_total}")

    # 取最近 N 个交易日
    from sqlalchemy import text
    with db._engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT date FROM stock_daily GROUP BY date ORDER BY date DESC LIMIT :n"
        ), {"n": args.days}).fetchall()
    recent_dates = [r[0] for r in rows]
    print(f"window: {recent_dates[-1]} → {recent_dates[0]} ({len(recent_dates)} 个交易日)")

    # 计算每日覆盖率
    targets: list[tuple[str, int, int]] = []
    with db._engine.connect() as conn:
        for d in recent_dates:
            cnt = conn.execute(text(
                "SELECT COUNT(DISTINCT sd.code) FROM stock_daily sd "
                "JOIN instrument_master im ON im.code=sd.code "
                "WHERE sd.date=:d AND im.market='cn' "
                "AND im.listing_status='active' AND COALESCE(im.is_st,0)=0"
            ), {"d": d}).fetchone()[0]
            missing = expected_total - cnt
            if missing > 0:
                targets.append((d, cnt, missing))

    print(f"待处理日期数: {len(targets)} / {len(recent_dates)}")
    print(f"累计需补 stock-day: {sum(m for _, _, m in targets)}")

    if args.dry_run:
        for d, cnt, missing in targets:
            print(f"  {d}  {cnt}/{expected_total}  缺 {missing}")
        return 0

    # 转换日期字符串为 date 对象
    started = time.time()
    succeeded = 0
    failed_dates: list[tuple[str, str]] = []

    for idx, (d_str, cnt, missing) in enumerate(targets, 1):
        try:
            target_date = datetime.strptime(d_str, "%Y-%m-%d").date()
        except Exception as exc:
            print(f"  [{idx}/{len(targets)}] {d_str} 跳过：日期解析失败 {exc}")
            failed_dates.append((d_str, f"date parse failed: {exc}"))
            continue

        elapsed_total = time.time() - started
        print(f"\n[{idx}/{len(targets)}] {d_str} 缺 {missing}  累计耗时 {elapsed_total:.1f}s")
        try:
            result = sync.sync_trade_date(trade_date=target_date, force=args.force)
        except KeyboardInterrupt:
            print("用户中断")
            break
        except Exception as exc:
            print(f"  {type(exc).__name__}: {exc}")
            failed_dates.append((d_str, f"{type(exc).__name__}: {exc}"))
            continue

        synced = result.get("synced", 0)
        errors = result.get("errors", [])
        hr = result.get("health_report", {})
        avail = hr.get("available_count", 0)
        miss_after = hr.get("missing_count", 0)
        print(f"  → synced={synced} 现入库={avail}/{expected_total} 仍缺={miss_after} errors={len(errors)}")
        if miss_after == 0:
            succeeded += 1

    elapsed = time.time() - started
    print()
    print("=" * 70)
    print(f"完成 {len(targets)} 个目标日处理，成功覆满: {succeeded}")
    print(f"总耗时: {elapsed:.1f}s")
    if failed_dates:
        print(f"失败日期: {len(failed_dates)}")
        for d, reason in failed_dates[:20]:
            print(f"  {d}  {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
