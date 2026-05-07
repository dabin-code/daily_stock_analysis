"""精准 repair：只针对当前 pending_retry gap 中的真实欠账做外网拉取。

策略：
  1. 扫描 kline_audit_gaps where status='pending_retry' AND scope='symbol_range_gap'
  2. 按 trade_date 聚合：每个日期需要同步的 code 列表
  3. 对每个日期调 MarketDataSyncService.sync_trade_date(stock_codes=[...], force=True)
     - bulk 一次拉全市场，bulk 哨兵自动过滤 universe 外的代码
  4. 每日处理后 sleep --rate-sleep 秒，规避 Tushare 50 次/分钟限流
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.market_data_sync_service import MarketDataSyncService
from src.storage import DatabaseManager


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rate-sleep", type=float, default=1.5,
                        help="每个 trade_date 处理后 sleep 秒数（默认 1.5，对应 Tushare ~40/min 限速）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划")
    args = parser.parse_args()

    db = DatabaseManager.get_instance()
    sync = MarketDataSyncService(db_manager=db)

    from sqlalchemy import text

    # 1. 收集所有 (code, missing_date) 对
    plan: dict[str, set[str]] = defaultdict(set)
    with db._engine.connect() as conn:
        rows = conn.execute(text(
            """
            SELECT code, COALESCE(missing_date_from, trade_date) AS d_from,
                   COALESCE(missing_date_to, trade_date) AS d_to
            FROM kline_audit_gaps
            WHERE status='pending_retry' AND gap_scope='symbol_range_gap' AND code IS NOT NULL
            """
        )).fetchall()
        # 把 [d_from, d_to] 展开成 stock_daily 上的实际交易日
        for code, d_from, d_to in rows:
            if not (code and d_from and d_to):
                continue
            actual_in_range = {
                r[0] for r in conn.execute(text(
                    "SELECT date FROM stock_daily WHERE code=:c AND date BETWEEN :a AND :b"
                ), {"c": code, "a": d_from, "b": d_to}).fetchall()
            }
            expected_in_range = [
                r[0] for r in conn.execute(text(
                    "SELECT DISTINCT date FROM stock_daily WHERE date BETWEEN :a AND :b"
                ), {"a": d_from, "b": d_to}).fetchall()
            ]
            for d in expected_in_range:
                if d not in actual_in_range:
                    plan[d].add(code)

    print(f"按 trade_date 聚合后：{len(plan)} 个目标日，累计需补 {sum(len(v) for v in plan.values())} stock-day")
    if not plan:
        print("无 pending repair 任务")
        return 0

    sorted_dates = sorted(plan.keys())
    if args.dry_run:
        for d in sorted_dates:
            codes = sorted(plan[d])
            print(f"  {d}  待补 {len(codes)} 只: {','.join(codes[:10])}{'...' if len(codes) > 10 else ''}")
        return 0

    started = time.time()
    fixed = 0
    failed = 0
    for idx, d in enumerate(sorted_dates, 1):
        codes = sorted(plan[d])
        try:
            target_date = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception as exc:
            print(f"[{idx}/{len(sorted_dates)}] {d} 跳过：{exc}")
            failed += 1
            continue

        elapsed = time.time() - started
        print(f"\n[{idx}/{len(sorted_dates)}] {d} 待补 {len(codes)} 只  累计耗时 {elapsed:.1f}s")
        try:
            result = sync.sync_trade_date(
                trade_date=target_date,
                stock_codes=codes,
                force=True,
            )
        except KeyboardInterrupt:
            print("用户中断")
            break
        except Exception as exc:
            print(f"  {type(exc).__name__}: {exc}")
            failed += 1
            continue

        synced = result.get("synced", 0)
        errors = result.get("errors", [])
        from collections import Counter
        reason_dist = Counter(str(e.get("reason", "?")) for e in errors)
        reason_str = ", ".join(f"{k}={v}" for k, v in reason_dist.most_common())
        print(f"  → synced={synced}/{len(codes)}  errors={len(errors)}  [{reason_str}]")
        fixed += synced

        if args.rate_sleep > 0 and idx < len(sorted_dates):
            time.sleep(args.rate_sleep)

    elapsed = time.time() - started
    print()
    print("=" * 70)
    print(f"完成：处理 {len(sorted_dates)} 个日期，新增入库 {fixed} 行，失败/异常 {failed}")
    print(f"总耗时: {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
