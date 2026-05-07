"""一次性批量将 pending_retry gap 转为 approved_skip。

适用场景：bulk 哨兵 + 6 数据源回归测试已经确认这些 gap 对应的 (code, date)
在所有外源都没有数据（停牌/未上市过渡期/退市过渡期），属于"客观无源"。

行为：
  1. 默认仅处理 status='pending_retry' 的 audit_gap
  2. 跳过 market_day_gap（这种是整市场缺失，应单独审视）
  3. 对每条 symbol_range_gap：
     - upsert kline_skip_registry: status=approved_skip
     - upsert kline_audit_gap: status=approved_skip
     - append kline_audit_event: approved_skip_granted

默认 --dry-run 只统计；加 --apply 才真正写库。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.storage import DatabaseManager


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="真正写库；不加则 dry-run")
    parser.add_argument("--reason-type", default="not_in_bulk_universe",
                        help="approved_skip 的 reason_type")
    parser.add_argument("--approved-by", default="bulk_sentinel_auto",
                        help="approved_skip 的 approved_by")
    parser.add_argument("--notes",
                        default="自动批量 approve：bulk 哨兵 + 6 数据源全部确认无源数据",
                        help="approved_skip 的 notes")
    parser.add_argument("--include-market-day-gap", action="store_true",
                        help="也处理 market_day_gap（默认跳过）")
    args = parser.parse_args()

    db = DatabaseManager.get_instance()
    gap_rows = db.list_kline_audit_gaps()

    targets = []
    skipped_market_day = 0
    skipped_other = 0
    for row in gap_rows:
        if row.status != "pending_retry":
            skipped_other += 1
            continue
        if row.gap_scope == "market_day_gap" and not args.include_market_day_gap:
            skipped_market_day += 1
            continue
        targets.append(row)

    print(f"待 approve gap 数        : {len(targets)}")
    print(f"  其中 symbol_range_gap : {sum(1 for r in targets if r.gap_scope == 'symbol_range_gap')}")
    print(f"  其中 market_day_gap   : {sum(1 for r in targets if r.gap_scope == 'market_day_gap')}")
    print(f"跳过 market_day_gap     : {skipped_market_day}")
    print(f"跳过其他 status         : {skipped_other}")

    if not targets:
        print("无目标，退出")
        return 0

    # 例样
    print()
    print("[前 10 条样例]")
    for row in targets[:10]:
        rng = (
            f"{row.missing_date_from.isoformat() if row.missing_date_from else '-'}"
            f"~{row.missing_date_to.isoformat() if row.missing_date_to else '-'}"
        )
        print(f"  {row.gap_scope:20s} code={row.code or '-':8s} {rng}")

    if not args.apply:
        print()
        print("DRY-RUN，未写库。确认后加 --apply 重新执行。")
        return 0

    approved_at = datetime.now()
    written = 0
    for row in targets:
        try:
            db.upsert_kline_skip_registry(
                market=row.market,
                gap_scope=row.gap_scope,
                code=row.code,
                trade_date=row.trade_date,
                missing_date_from=row.missing_date_from,
                missing_date_to=row.missing_date_to,
                status="approved_skip",
                approved_by=args.approved_by,
                approved_at=approved_at,
                reason_type=args.reason_type,
                notes=args.notes,
                success_streak=0,
            )
            db.upsert_kline_audit_gap(
                market=row.market,
                gap_scope=row.gap_scope,
                code=row.code,
                trade_date=row.trade_date,
                missing_date_from=row.missing_date_from,
                missing_date_to=row.missing_date_to,
                source_run_id=row.source_run_id,
                status="approved_skip",
            )
            db.append_kline_audit_event(
                source_run_id=row.source_run_id,
                gap_key=row.gap_key,
                event_type="approved_skip_granted",
                event_status="approved_skip",
                payload={
                    "approved_by": args.approved_by,
                    "approved_at": approved_at.isoformat(),
                    "reason_type": args.reason_type,
                    "notes": args.notes,
                    "auto_grant": True,
                },
            )
            written += 1
        except Exception as exc:
            print(f"  [ERR] gap_id={row.id} code={row.code}: {type(exc).__name__}: {exc}")

    print()
    print("=" * 60)
    print(f"完成：approve {written} / {len(targets)} 条 gap")
    return 0


if __name__ == "__main__":
    sys.exit(main())
