# -*- coding: utf-8 -*-
"""Application orchestration for the validation command."""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Callable, Optional, Sequence

from src.backtest.services.bottom_divergence_v2_dataset import (
    isolated_replay_database,
)
from src.backtest.services.bottom_divergence_v2_performance import (
    CanonicalCheckpointStore,
    ValidationFactorCache,
    replay_batch_from_payload,
    replay_batch_to_payload,
    validation_checkpoint_config_hash,
)
from src.backtest.services.bottom_divergence_v2_replay import (
    HistoricalReplayService,
    _build_default_replay_dependencies,
    _list_future_local_trade_dates,
    _list_local_trade_dates,
    _local_data_version,
    _resolve_local_universe,
    _universe_identity,
    build_isolated_config,
    replay_historical_dates,
    replay_maturation_events,
)
from src.backtest.services.bottom_divergence_v2_report import (
    _enrich_report,
    _read_universe_codes,
    _write_report,
    canonicalize_report,
)
from src.backtest.services.bottom_divergence_v2_validation import (
    BottomDivergenceV2Validator,
    ValidationInputError,
    ValidationSample,
    build_parameter_snapshots,
    chronological_split,
)
from src.config import Config, get_config


def _validated_cost_model(config: Config) -> dict[str, float]:
    costs = {
        "buy_cost_bps": config.backtest_buy_cost_bps,
        "sell_cost_bps": config.backtest_sell_cost_bps,
        "slippage_bps": config.backtest_slippage_bps,
    }
    for name, value in costs.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise ValidationInputError(
                "INVALID_INPUT",
                f"{name} must be a non-negative finite number",
            )
    return {name: float(value) for name, value in costs.items()}


def _zero_cost_result(
    args: argparse.Namespace,
    config: Config,
) -> Optional[tuple[int, dict]]:
    costs = _validated_cost_model(config)
    round_trip = (
        costs["buy_cost_bps"]
        + costs["sell_cost_bps"]
        + 2.0 * costs["slippage_bps"]
    )
    if round_trip != 0.0:
        return None
    snapshots = build_parameter_snapshots()
    report = BottomDivergenceV2Validator.evaluate(
        v1_samples=[],
        v2_samples_by_parameter_hash={},
        parameter_snapshots={},
        **costs,
    )
    report.update({
        "date_range": {
            "from": args.date_from.isoformat(),
            "to": args.date_to.isoformat(),
        },
        "market": args.market,
        "parameter_grid": snapshots,
    })
    report = canonicalize_report(report)
    _write_report(args.output, report)
    return 1, report


def _run_validation_cli_core(
    args: argparse.Namespace,
    *,
    replay_service: Optional[HistoricalReplayService] = None,
    base_config: Optional[Config] = None,
) -> tuple[int, dict]:
    """Execute tuning replay, one locked test replay, and canonical output."""
    config = base_config or get_config()
    zero_cost = _zero_cost_result(args, config)
    if zero_cost is not None:
        return zero_cost
    costs = _validated_cost_model(config)
    default_dependencies = (
        _build_default_replay_dependencies(config)
        if replay_service is None
        else None
    )
    snapshots = build_parameter_snapshots()
    codes = _read_universe_codes(args.universe_codes)
    if replay_service is None:
        assert default_dependencies is not None
        universe = _resolve_local_universe(
            default_dependencies.db_manager,
            args.market,
            codes,
        )
        metadata_service = SimpleNamespace(
            data_version=lambda: _local_data_version(
                default_dependencies.db_manager,
                universe,
                args.date_from,
                args.date_to,
            ),
            universe_identity=lambda selected: _universe_identity(selected),
        )
    else:
        universe = replay_service.resolve_universe(args.market, codes)
        metadata_service = replay_service

    if replay_service is None:
        trade_dates = _list_local_trade_dates(
            default_dependencies.db_manager,
            date_from=args.date_from,
            date_to=args.date_to,
            universe=universe,
        )
    else:
        trade_dates = replay_service.list_trade_dates(
            date_from=args.date_from,
            date_to=args.date_to,
            market=args.market,
            universe=universe,
        )
    if not trade_dates:
        raise ValidationInputError(
            "NO_TRADING_DATES",
            "source stock_daily contains no trading dates in requested range",
        )
    try:
        split = chronological_split([], date_universe=trade_dates)
    except ValueError as exc:
        raise ValidationInputError(
            "NO_TRADING_DATES",
            str(exc),
        ) from exc
    v1_config = replace(config, bottom_divergence_v2_enabled=False)
    replay = (
        replay_service.replay
        if replay_service is not None
        else lambda **kwargs: replay_historical_dates(
            **kwargs,
            dependencies=default_dependencies,
        )
    )
    v1_batch = replay(
        strategy_version="v1",
        config=v1_config,
        trade_dates=trade_dates,
        universe=universe,
    )
    opportunity_counts = dict(v1_batch.opportunity_counts)
    checkpoint_store = None
    checkpoint_path = getattr(args, "checkpoint", None)
    if checkpoint_path is not None:
        data_version = metadata_service.data_version()
        checkpoint_store = CanonicalCheckpointStore(
            checkpoint_path,
            data_version=data_version,
            config_hash=validation_checkpoint_config_hash(
                config=config,
                date_from=args.date_from,
                date_to=args.date_to,
                market=args.market,
                trading_dates=trade_dates,
                universe_identity=metadata_service.universe_identity(universe),
                data_version=data_version,
                costs=costs,
                parameter_snapshots=snapshots,
            ),
        )

    tuning_dates = list(split.train_dates + split.validation_dates)
    v2_samples_by_hash: dict[str, Sequence[ValidationSample]] = {}
    selection_evidence_by_hash = {}
    for parameter_hash, snapshot in snapshots.items():
        saved = (
            checkpoint_store.load_partition(
                parameter_hash,
                "selection",
            )
            if checkpoint_store is not None
            and getattr(args, "resume", False)
            else None
        )
        if saved is not None:
            batch = replay_batch_from_payload(saved)
        else:
            isolated = build_isolated_config(config, snapshot)
            batch = replay(
                strategy_version="v2",
                config=isolated,
                trade_dates=tuning_dates,
                universe=universe,
            )
            if checkpoint_store is not None:
                checkpoint_store.save_partition(
                    parameter_hash=parameter_hash,
                    partition="selection",
                    payload=replay_batch_to_payload(batch),
                )
        v2_samples_by_hash[parameter_hash] = batch.samples
        selection_evidence_by_hash[parameter_hash] = getattr(
            batch,
            "event_evidence",
            (),
        )
    all_selection_samples = [
        *v1_batch.samples,
        *(
            sample
            for samples in v2_samples_by_hash.values()
            for sample in samples
        ),
    ]
    if not any(sample.is_executable for sample in all_selection_samples):
        raise ValidationInputError(
            "NO_ELIGIBLE_SAMPLES",
            "replay produced no executable validation samples",
        )

    tuning_report = BottomDivergenceV2Validator.evaluate(
        v1_samples=v1_batch.samples,
        v2_samples_by_parameter_hash=v2_samples_by_hash,
        parameter_snapshots=snapshots,
        trading_dates=trade_dates,
        opportunity_counts=opportunity_counts,
        selection_event_evidence_by_parameter_hash=(
            selection_evidence_by_hash
        ),
        **costs,
    )
    selected_hash = tuning_report.get("selected_parameter_hash")
    if selected_hash is None:
        report = _enrich_report(
            tuning_report,
            args=args,
            replay_service=metadata_service,
            universe=universe,
            parameter_snapshots=snapshots,
        )
        _write_report(args.output, report)
        return 1, report

    selected_test_batch = replay(
        strategy_version="v2",
        config=build_isolated_config(config, snapshots[selected_hash]),
        trade_dates=list(split.test_dates),
        universe=universe,
    )
    test_date_set = set(split.test_dates)
    target_candidates = {
        (sample.code, sample.candidate_version)
        for sample in selected_test_batch.samples
        if sample.early_event_date in test_date_set
    }
    test_event_evidence = tuple(
        evidence
        for evidence in getattr(
            selected_test_batch,
            "event_evidence",
            (),
        )
        if (evidence.code, evidence.candidate_version)
        in target_candidates
    )
    future_dates = (
        replay_service.list_future_trade_dates(
            after_date=split.test_dates[-1],
            count=20,
            universe=universe,
        )
        if replay_service is not None
        and hasattr(replay_service, "list_future_trade_dates")
        else []
    )
    if len(future_dates) < 20:
        raise ValidationInputError(
            "FUTURE_HISTORY_INSUFFICIENT",
            "test conversion requires 20 future trading dates",
        )
    future_maturation_evidence = (
        replay_service.mature_events(
            config=build_isolated_config(
                config,
                snapshots[selected_hash],
            ),
            maturation_dates=list(future_dates),
            universe=universe,
            target_candidates=target_candidates,
        )
        if replay_service is not None
        and hasattr(replay_service, "mature_events")
        else ()
    )
    maturation_evidence = (
        test_event_evidence + tuple(future_maturation_evidence)
    )
    v2_samples_by_hash[selected_hash] = (
        tuple(v2_samples_by_hash[selected_hash])
        + tuple(selected_test_batch.samples)
    )
    report = BottomDivergenceV2Validator.evaluate(
        v1_samples=v1_batch.samples,
        v2_samples_by_parameter_hash=v2_samples_by_hash,
        parameter_snapshots=snapshots,
        trading_dates=trade_dates,
        opportunity_counts=opportunity_counts,
        selection_contract=tuning_report["selection_contract"],
        test_maturation_evidence=maturation_evidence,
        test_observation_dates=tuple(
            [*split.test_dates, *future_dates]
        ),
        **costs,
    )
    report = _enrich_report(
        report,
        args=args,
        replay_service=metadata_service,
        universe=universe,
        parameter_snapshots=snapshots,
    )
    _write_report(args.output, report)
    exit_code = 0 if report["eligible"] and report["passed"] else 1
    return exit_code, report


def run_validation_cli(
    args: argparse.Namespace,
    *,
    replay_service: Optional[HistoricalReplayService] = None,
    base_config: Optional[Config] = None,
    isolation_observer: Optional[Callable[[Any, Any], None]] = None,
) -> tuple[int, dict]:
    """Run against an injected replay boundary or an isolated local copy."""
    config = base_config or get_config()
    zero_cost = _zero_cost_result(args, config)
    if zero_cost is not None:
        return zero_cost
    if replay_service is not None:
        try:
            return _run_validation_cli_core(
                args,
                replay_service=replay_service,
                base_config=config,
            )
        except ValidationInputError as exc:
            if exc.data_version is None:
                try:
                    exc.data_version = replay_service.data_version()
                except Exception:
                    pass
            raise

    from src.storage import DatabaseManager

    source_db = DatabaseManager.get_instance()
    codes = _read_universe_codes(args.universe_codes)
    universe = _resolve_local_universe(source_db, args.market, codes)
    trade_dates = _list_local_trade_dates(
        source_db,
        date_from=args.date_from,
        date_to=args.date_to,
        universe=universe,
    )
    with isolated_replay_database(
        source_db=source_db,
        universe=universe,
        date_from=args.date_from,
        date_to=args.date_to,
        market=args.market,
        market_guard_index=config.screening_market_guard_index,
        config=config,
    ) as temporary_db:
        future_cache_dates = _list_future_local_trade_dates(
            temporary_db,
            after_date=trade_dates[-1],
            count=20,
            universe=universe,
        )

        def report_progress(event: dict[str, Any]) -> None:
            print(
                "validation-progress "
                f"completed={event['completed']}/{event['total']} "
                f"elapsed={event['elapsed_seconds']}s "
                f"eta={event['eta_seconds']}s",
                file=sys.stderr,
                flush=True,
            )

        factor_cache = ValidationFactorCache.from_database(
            db_manager=temporary_db,
            data_version=temporary_db.validation_data_version,
            trade_dates=tuple([*trade_dates, *future_cache_dates]),
            codes=sorted(str(code) for code in universe["code"].tolist()),
            lookback_days=config.screening_factor_lookback_days,
            progress_every=getattr(args, "progress_every", 100),
            progress_callback=report_progress,
            # 不给 `--cache-dir` 时保持既有行为：进程内临时目录，
            # 用完即删，不跨进程复用任何因子。
            cache_directory=getattr(args, "cache_dir", None),
            workers=getattr(args, "workers", 4),
        )
        dependencies = _build_default_replay_dependencies(
            config,
            db_manager=temporary_db,
            factor_cache=factor_cache,
        )
        boundary = SimpleNamespace(
            resolve_universe=lambda market, selected_codes: universe,
            list_trade_dates=lambda **kwargs: list(trade_dates),
            replay=lambda **kwargs: replay_historical_dates(
                **kwargs,
                dependencies=dependencies,
            ),
            list_future_trade_dates=lambda **kwargs: (
                _list_future_local_trade_dates(
                    temporary_db,
                    **kwargs,
                )
            ),
            mature_events=lambda **kwargs: replay_maturation_events(
                **kwargs,
                dependencies=dependencies,
            ),
            data_version=lambda: temporary_db.validation_data_version,
            universe_identity=lambda selected: _universe_identity(selected),
        )
        try:
            try:
                result = _run_validation_cli_core(
                    args,
                    replay_service=boundary,
                    base_config=config,
                )
            except ValidationInputError as exc:
                if exc.data_version is None:
                    exc.data_version = temporary_db.validation_data_version
                raise
            if isolation_observer is not None:
                isolation_observer(source_db, temporary_db)
            return result
        finally:
            # 三层计数是判断因子复用是否真的生效的唯一可观测量。不打出来，
            # 操作者给了 `--cache-dir` 也无从知道命中与否，只能靠总耗时猜。
            stats = factor_cache.stats
            print(
                "validation-factor-cache "
                f"base_snapshot_builds={stats['base_snapshot_builds']} "
                f"frozen_evidence_builds={stats['frozen_evidence_builds']} "
                f"parameter_evaluations={stats['parameter_evaluations']} "
                f"sql_bar_queries={stats['sql_bar_queries']}",
                file=sys.stderr,
                flush=True,
            )
            factor_cache.close()
