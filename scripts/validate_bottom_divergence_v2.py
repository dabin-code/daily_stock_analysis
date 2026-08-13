# -*- coding: utf-8 -*-
"""Run the bottom-divergence v2 sample-out release gate."""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.backtest.evaluators.entry_evaluator import EntrySignalEvaluator
from src.backtest.services.bottom_divergence_v2_cli_service import (
    _run_validation_cli_core,
    run_validation_cli,
)
from src.backtest.services.bottom_divergence_v2_dataset import (
    isolated_replay_database,
)
from src.backtest.services.bottom_divergence_v2_replay import (
    ReplayBatch,
    ReplayDependencies,
    _build_validation_sample,
    _parse_position_weight,
    _resolve_local_universe,
    _v1_breakout_floor,
    build_isolated_config,
    compute_pre_signal_features,
    replay_historical_dates,
    replay_maturation_events,
)
from src.backtest.services.bottom_divergence_v2_report import canonical_json_dumps
from src.backtest.services.bottom_divergence_v2_validation import (
    CandidateEventEvidence,
    ValidationInputError,
)
from src.backtest.services.bottom_divergence_v2_report import (
    _write_report,
    build_failure_report,
)

__all__ = [
    "CandidateEventEvidence",
    "EntrySignalEvaluator",
    "ReplayBatch",
    "ReplayDependencies",
    "_build_validation_sample",
    "_parse_position_weight",
    "_resolve_local_universe",
    "_run_validation_cli_core",
    "_v1_breakout_floor",
    "build_isolated_config",
    "canonical_json_dumps",
    "compute_pre_signal_features",
    "isolated_replay_database",
    "replay_historical_dates",
    "replay_maturation_events",
    "run_validation_cli",
]


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date: {value}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate bottom-divergence v2 on local stock_daily data",
    )
    parser.add_argument("--date-from", required=True, type=_parse_date)
    parser.add_argument("--date-to", required=True, type=_parse_date)
    parser.add_argument("--market", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--universe-codes", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help=(
            "persist the factor cache here so a later run can reuse the base "
            "snapshots and frozen evidence instead of recomputing them; "
            "omit it to keep the previous behaviour of a per-process "
            "temporary directory. Reuse only happens when the data version, "
            "universe, base config and algorithm versions all match, so a "
            "stale directory costs a recompute, never a wrong answer"
        ),
    )
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--workers", type=int, default=4)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        exit_code, _ = run_validation_cli(args)
        return exit_code
    except ValidationInputError as exc:
        report = build_failure_report(
            status="ineligible",
            error_code=exc.error_code,
            message=exc.message,
            data_version=exc.data_version,
        )
        try:
            _write_report(args.output, report)
        except Exception as write_error:
            print(
                f"failed to write validation report: {write_error}",
                file=sys.stderr,
            )
        print(f"{exc.error_code}: {exc.message}", file=sys.stderr)
        return 1
    except Exception as exc:
        report = build_failure_report(
            status="error",
            error_code="UNEXPECTED_ERROR",
            message="unexpected validation failure",
        )
        try:
            _write_report(args.output, report)
        except Exception as write_error:
            print(
                f"failed to write validation report: {write_error}",
                file=sys.stderr,
            )
        print(
            f"unexpected validation failure: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
