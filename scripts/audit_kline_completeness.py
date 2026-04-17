from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_config
from src.services.kline_audit_service import KlineAuditService
from src.services.kline_governance_schedule_service import KlineGovernanceScheduleService
from src.storage import DatabaseManager


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit or repair K-line completeness.")
    parser.add_argument("--market", default="cn")
    parser.add_argument("--trade-date", dest="trade_date", type=_parse_date)
    parser.add_argument("--repair", action="store_true", help="Run sync + audit + repair workflow.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned action without executing it.")
    return parser


def _compute_window_start(target_trade_date: date, lookback_days: int) -> date:
    safe_lookback_days = max(1, int(lookback_days))
    return target_trade_date - timedelta(days=safe_lookback_days - 1)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    config = get_config()
    db = DatabaseManager.get_instance()
    governance_service = KlineGovernanceScheduleService(config=config, db_manager=db)
    target_trade_date = args.trade_date or governance_service.resolve_target_trade_date(
        trade_date=None,
        market=args.market,
    )

    if args.dry_run:
        mode = "repair" if args.repair else "audit"
        print(f"DRY-RUN mode={mode} market={args.market} trade_date={target_trade_date.isoformat()}")
        return 0

    try:
        if args.repair:
            result = governance_service.run_daily_governance(
                trade_date=target_trade_date,
                market=args.market,
            )
            print(
                "DONE "
                f"mode=repair market={args.market} trade_date={target_trade_date.isoformat()} "
                f"run_result={result.get('run_result')} pass_status={result.get('pass_status')}"
            )
            return 0 if result.get("run_result") == "succeeded" and result.get("pass_status") == "passed" else 1

        audit_service = KlineAuditService(db_manager=db)
        window_start = _compute_window_start(
            target_trade_date=target_trade_date,
            lookback_days=getattr(config, "kline_audit_lookback_days", 30),
        )
        result = audit_service.audit_trade_date(
            market=args.market,
            trade_date=target_trade_date,
            window_start=window_start,
            window_end=target_trade_date,
            run_type="manual_audit",
            trigger_type="manual",
        )
        governance_service._promote_current_run_gaps_for_repair(
            market=args.market,
            source_run_id=result.get("run_id"),
        )
        print(
            "DONE "
            f"mode=audit market={args.market} trade_date={target_trade_date.isoformat()} "
            f"run_result={result.get('run_result')} pass_status={result.get('pass_status')}"
        )
        return 0 if result.get("run_result") == "succeeded" and result.get("pass_status") == "passed" else 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
