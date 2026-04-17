from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.storage import DatabaseManager


def _parse_date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Approve candidate K-line skips.")
    parser.add_argument("--market", default="cn")
    parser.add_argument("--list", action="store_true", help="List current candidate skips.")
    parser.add_argument("--code", help="Approve candidate symbol-range skips for the given code.")
    parser.add_argument("--trade-date", dest="trade_date", type=_parse_date)
    parser.add_argument("--from-date", dest="from_date", type=_parse_date)
    parser.add_argument("--to-date", dest="to_date", type=_parse_date)
    parser.add_argument("--approved-by")
    parser.add_argument("--reason-type")
    parser.add_argument("--notes")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _select_candidates(args, db: DatabaseManager):
    candidates = db.list_kline_skip_registry(market=args.market, status="candidate_skip")
    if not args.code and args.trade_date is None:
        return candidates

    filtered = candidates
    if args.code:
        filtered = [row for row in filtered if row.code == args.code]
    if args.trade_date is not None:
        filtered = [row for row in filtered if row.trade_date == args.trade_date]
    if args.from_date is not None:
        filtered = [row for row in filtered if row.missing_date_from == args.from_date]
    if args.to_date is not None:
        filtered = [row for row in filtered if row.missing_date_to == args.to_date]
    return filtered


def _select_matching_gaps(db: DatabaseManager, candidate_rows):
    gap_rows = db.list_kline_audit_gaps()
    by_key = {row.gap_key: row for row in gap_rows}
    return {row.skip_key: by_key.get(row.skip_key) for row in candidate_rows}


def _has_approval_target(args) -> bool:
    return bool(args.code) or args.trade_date is not None


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    db = DatabaseManager.get_instance()
    candidates = _select_candidates(args, db)
    matching_gaps = _select_matching_gaps(db, candidates)

    if args.list or not _has_approval_target(args):
        for row in candidates:
            range_text = (
                f"{row.missing_date_from.isoformat()}..{row.missing_date_to.isoformat()}"
                if row.missing_date_from and row.missing_date_to
                else (row.trade_date.isoformat() if row.trade_date else "-")
            )
            print(f"{row.market} {row.gap_scope} {row.code or '-'} {range_text} {row.status}")
        return 0

    if not args.approved_by or not args.reason_type:
        parser.error("--approved-by and --reason-type are required when approving")

    if not candidates:
        print("No matching candidate skips found.", file=sys.stderr)
        return 1

    if args.dry_run:
        for row in candidates:
            print(f"DRY-RUN approve {row.skip_key}")
        return 0

    approved_at = datetime.now()
    for row in candidates:
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
        gap_row = matching_gaps.get(row.skip_key)
        if gap_row is not None:
            db.upsert_kline_audit_gap(
                market=gap_row.market,
                gap_scope=gap_row.gap_scope,
                code=gap_row.code,
                trade_date=gap_row.trade_date,
                missing_date_from=gap_row.missing_date_from,
                missing_date_to=gap_row.missing_date_to,
                source_run_id=gap_row.source_run_id,
                status="approved_skip",
            )
            db.append_kline_audit_event(
                source_run_id=gap_row.source_run_id,
                gap_key=gap_row.gap_key,
                event_type="approved_skip_granted",
                event_status="approved_skip",
                payload={
                    "approved_by": args.approved_by,
                    "approved_at": approved_at.isoformat(),
                    "reason_type": args.reason_type,
                    "notes": args.notes,
                },
            )
        print(f"APPROVED {row.skip_key}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
