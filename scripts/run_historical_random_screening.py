from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, List, Optional

from sqlalchemy import distinct, select

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.storage import DatabaseManager, ScreeningRun, StockDaily


TRIGGER_TYPE = "historical_random_sample"


@dataclass(frozen=True)
class HistoricalRandomScreeningOptions:
    start_date: date = date(2024, 4, 19)
    end_date: date = date(2026, 5, 11)
    sample_days: int = 100
    candidate_limit: int = 10
    ai_top_k: int = 0
    market: str = "cn"
    seed: Optional[int] = None
    dry_run: bool = False
    force: bool = False
    continue_on_error: bool = True


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"无效日期: {value}，请使用 YYYY-MM-DD") from exc


def list_available_trade_dates(
    db: DatabaseManager,
    *,
    start_date: date,
    end_date: date,
) -> List[date]:
    if start_date > end_date:
        raise ValueError("start_date 不能晚于 end_date")

    with db.get_session() as session:
        rows = session.execute(
            select(distinct(StockDaily.date))
            .where(
                StockDaily.date >= start_date,
                StockDaily.date <= end_date,
            )
            .order_by(StockDaily.date)
        ).all()
    return [row[0] for row in rows]


def sample_trade_dates(
    available_trade_dates: List[date],
    *,
    sample_days: int,
    seed: Optional[int] = None,
) -> List[date]:
    if sample_days <= 0:
        raise ValueError("sample_days 必须大于 0")
    if len(available_trade_dates) < sample_days:
        raise ValueError(
            f"可用交易日不足: 需要 {sample_days} 天，实际只有 {len(available_trade_dates)} 天"
        )

    sampled = random.Random(seed).sample(list(available_trade_dates), sample_days)
    return sorted(sampled)


def run_batch(
    *,
    db: DatabaseManager,
    service: Any,
    options: HistoricalRandomScreeningOptions,
) -> dict[str, Any]:
    available_trade_dates = list_available_trade_dates(
        db,
        start_date=options.start_date,
        end_date=options.end_date,
    )
    selected_trade_dates = sample_trade_dates(
        available_trade_dates,
        sample_days=options.sample_days,
        seed=options.seed,
    )

    print(
        f"Selected {len(selected_trade_dates)} trade dates from "
        f"{options.start_date.isoformat()} to {options.end_date.isoformat()}"
    )
    for trade_date in selected_trade_dates:
        print(f"  - {trade_date.isoformat()}")

    if options.dry_run:
        print("DRY RUN: no screening run will be executed.")
        return {
            "selected_trade_dates": [item.isoformat() for item in selected_trade_dates],
            "success_count": 0,
            "failure_count": 0,
            "results": [],
            "failures": [],
        }

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    total = len(selected_trade_dates)
    for index, trade_date in enumerate(selected_trade_dates, start=1):
        print(f"[{index}/{total}] running screening for {trade_date.isoformat()}...")
        try:
            existing_runs = find_existing_matching_runs(
                db=db,
                service=service,
                trade_date=trade_date,
                options=options,
            )
            non_historical = next(
                (
                    item for item in existing_runs
                    if item.get("trigger_type") != TRIGGER_TYPE
                ),
                None,
            )
            if non_historical:
                raise ValueError(
                    f"发现同配置非历史抽样筛选记录 run_id={non_historical.get('run_id')} "
                    f"trigger_type={non_historical.get('trigger_type')}，为避免影响人工/定时记录已跳过"
                )
            if options.force:
                for existing in existing_runs:
                    run_id = existing.get("run_id")
                    if run_id and service.delete_run(run_id):
                        print(f"[{index}/{total}] force deleted existing run_id={run_id}")
            result = service.execute_run(
                trade_date=trade_date,
                candidate_limit=options.candidate_limit,
                ai_top_k=options.ai_top_k,
                market=options.market,
                trigger_type=TRIGGER_TYPE,
            )
        except Exception as exc:
            failure = {"trade_date": trade_date.isoformat(), "error": str(exc)}
            failures.append(failure)
            print(f"[{index}/{total}] failed {trade_date.isoformat()}: {exc}")
            if not options.continue_on_error:
                raise
            continue

        results.append(
            {
                "trade_date": trade_date.isoformat(),
                "run_id": result.get("run_id"),
                "status": result.get("status"),
                "candidate_count": result.get("candidate_count", 0),
            }
        )
        print(
            f"[{index}/{total}] completed {trade_date.isoformat()} "
            f"run_id={result.get('run_id')} status={result.get('status')} "
            f"candidates={result.get('candidate_count', 0)}"
        )

    summary = {
        "selected_trade_dates": [item.isoformat() for item in selected_trade_dates],
        "success_count": len(results),
        "failure_count": len(failures),
        "results": results,
        "failures": failures,
    }
    print(
        f"Summary: success={summary['success_count']} "
        f"failure={summary['failure_count']} ai_top_k={options.ai_top_k}"
    )
    return summary


def delete_existing_matching_run(
    *,
    db: DatabaseManager,
    service: Any,
    trade_date: date,
    options: HistoricalRandomScreeningOptions,
) -> Optional[str]:
    matches = find_existing_matching_runs(
        db=db,
        service=service,
        trade_date=trade_date,
        options=options,
    )
    if not matches:
        return None
    if any(item.get("trigger_type") != TRIGGER_TYPE for item in matches):
        return None

    run_id = matches[0].get("run_id")
    if run_id and service.delete_run(run_id):
        return run_id
    return None


def find_existing_matching_run(
    *,
    db: DatabaseManager,
    service: Any,
    trade_date: date,
    options: HistoricalRandomScreeningOptions,
) -> Optional[dict[str, Any]]:
    matches = find_existing_matching_runs(
        db=db,
        service=service,
        trade_date=trade_date,
        options=options,
    )
    return matches[0] if matches else None


def find_existing_matching_runs(
    *,
    db: DatabaseManager,
    service: Any,
    trade_date: date,
    options: HistoricalRandomScreeningOptions,
) -> list[dict[str, Any]]:
    runtime_config = service.resolve_run_config(
        mode=None,
        candidate_limit=options.candidate_limit,
        ai_top_k=options.ai_top_k,
    )
    snapshot = service._build_run_config_snapshot(
        requested_trade_date=trade_date,
        normalized_stock_codes=[],
        runtime_config=runtime_config,
        ingest_failure_threshold=float(
            getattr(service.config, "screening_ingest_failure_threshold", 0.20)
        ),
        strategy_names=None,
        theme_context=None,
    )
    expected_identity = DatabaseManager._screening_run_identity(snapshot)
    with db.get_session() as session:
        rows = session.execute(
            select(ScreeningRun)
            .where(ScreeningRun.market == options.market)
            .order_by(ScreeningRun.started_at.desc())
        ).scalars().all()

    matches: list[dict[str, Any]] = []
    for row in rows:
        payload = row.to_dict()
        if DatabaseManager._screening_run_identity(payload.get("config_snapshot") or {}) == expected_identity:
            matches.append(payload)
    return matches


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="随机抽取历史交易日并批量执行选股任务")
    parser.add_argument("--start-date", type=parse_date, default=date(2024, 4, 19))
    parser.add_argument("--end-date", type=parse_date, default=date(2026, 5, 11))
    parser.add_argument("--sample-days", type=int, default=100)
    parser.add_argument("--candidate-limit", type=int, default=10)
    parser.add_argument("--ai-top-k", type=int, default=0)
    parser.add_argument("--market", default="cn")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="删除同日期同配置的既有筛选 run 后重新执行",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="任一交易日执行失败后立即退出，默认继续处理后续日期",
    )
    return parser


def options_from_args(args: argparse.Namespace) -> HistoricalRandomScreeningOptions:
    return HistoricalRandomScreeningOptions(
        start_date=args.start_date,
        end_date=args.end_date,
        sample_days=args.sample_days,
        candidate_limit=args.candidate_limit,
        ai_top_k=args.ai_top_k,
        market=args.market,
        seed=args.seed,
        dry_run=args.dry_run,
        force=args.force,
        continue_on_error=not args.fail_fast,
    )


def exit_code_from_summary(summary: dict[str, Any]) -> int:
    return 1 if int(summary.get("failure_count", 0) or 0) > 0 else 0


def main() -> int:
    args = build_arg_parser().parse_args()
    options = options_from_args(args)

    from src.agent.skills.base import SkillManager
    from src.services.screening_task_service import ScreeningTaskService

    skill_manager = SkillManager()
    skill_manager.load_builtin_strategies()
    db = DatabaseManager.get_instance()
    service = ScreeningTaskService(skill_manager=skill_manager)

    summary = run_batch(db=db, service=service, options=options)
    return exit_code_from_summary(summary)


if __name__ == "__main__":
    raise SystemExit(main())
