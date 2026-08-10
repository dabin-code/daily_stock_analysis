# -*- coding: utf-8 -*-
"""Streaming isolated-dataset copy for deterministic validation."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import math
from typing import Any, Callable, Iterator, Mapping, Optional

from .bottom_divergence_v2_report import canonical_json_dumps
from .bottom_divergence_v2_validation import ValidationInputError


COPY_BATCH_SIZE = 2000
_HASH_BAR_FIELDS = (
    "code",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "pct_chg",
    "data_source",
    "adj_factor",
    "adj_factor_source",
)
_FINITE_BAR_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "pct_chg",
    "adj_factor",
)
_HASH_BOARD_FIELDS = (
    "id",
    "board_code",
    "board_name",
    "board_type",
    "market",
    "source",
    "is_active",
)
_HASH_MEMBERSHIP_FIELDS = (
    "instrument_code",
    "board_id",
    "market",
    "source",
    "is_primary",
)
_HASH_SECTOR_HEAT_FIELDS = (
    "trade_date",
    "board_name",
    "board_type",
    "breadth_score",
    "strength_score",
    "persistence_score",
    "leadership_score",
    "sector_hot_score",
    "sector_status",
    "sector_stage",
    "stock_count",
    "up_count",
    "limit_up_count",
    "avg_pct_chg",
    "leader_codes_json",
    "front_codes_json",
    "board_strength_score",
    "board_strength_rank",
    "board_strength_percentile",
    "leader_candidate_count",
    "quality_flags_json",
    "reason",
)


def iter_query_batches(
    session: Any,
    statement: Any,
    *,
    batch_size: int = COPY_BATCH_SIZE,
) -> Iterator[list[dict[str, Any]]]:
    """Yield detached mappings without constructing an ORM-sized result list."""
    if not 1 <= batch_size <= COPY_BATCH_SIZE:
        raise ValueError(f"batch_size must be within 1..{COPY_BATCH_SIZE}")
    result = session.execute(
        statement.execution_options(
            stream_results=True,
            yield_per=batch_size,
        )
    ).mappings()
    for partition in result.partitions(batch_size):
        batch = [dict(row) for row in partition]
        if batch:
            yield batch


def _statement_for(model: Any, *criteria: Any, order_by: Any = None) -> Any:
    from sqlalchemy import select

    statement = select(*model.__table__.columns)
    if criteria:
        statement = statement.where(*criteria)
    if order_by is not None:
        statement = statement.order_by(*order_by)
    return statement


def _copy_batches(
    *,
    source_session: Any,
    target_session: Any,
    model: Any,
    statement: Any,
    keep_id: bool = False,
    observe: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> int:
    copied = 0
    for batch in iter_query_batches(source_session, statement):
        mappings = []
        for raw in batch:
            row = dict(raw)
            if not keep_id:
                row.pop("id", None)
            if observe is not None:
                observe(row)
            mappings.append(row)
        target_session.bulk_insert_mappings(model, mappings)
        target_session.flush()
        target_session.expunge_all()
        copied += len(mappings)
    return copied


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _hash_record(
    hasher: Any,
    record_type: str,
    row: Mapping[str, Any],
    fields: tuple[str, ...],
) -> None:
    payload = {
        "record_type": record_type,
        **{field_name: _iso(row.get(field_name)) for field_name in fields},
    }
    hasher.update(canonical_json_dumps(payload).encode("utf-8"))
    hasher.update(b"\n")


def _validate_and_hash_bar(hasher: Any, row: Mapping[str, Any]) -> None:
    for field_name in _FINITE_BAR_FIELDS:
        value = row.get(field_name)
        if value is None:
            continue
        try:
            finite = math.isfinite(float(value))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationInputError(
                "INVALID_INPUT",
                f"stock_daily {field_name} must be finite",
            ) from exc
        if not finite:
            raise ValidationInputError(
                "INVALID_INPUT",
                f"stock_daily {field_name} must be finite",
            )
    for field_name in ("open", "high", "low", "close"):
        value = row.get(field_name)
        if value is None or float(value) <= 0:
            raise ValidationInputError(
                "INVALID_INPUT",
                f"stock_daily {field_name} must be positive",
            )
    _hash_record(hasher, "stock_daily", row, _HASH_BAR_FIELDS)


def _config_identity(config: Any) -> dict[str, Any]:
    if config is None:
        return {}
    return {
        "backtest_engine_version": getattr(
            config,
            "backtest_engine_version",
            None,
        ),
        "buy_cost_bps": getattr(config, "backtest_buy_cost_bps", None),
        "sell_cost_bps": getattr(config, "backtest_sell_cost_bps", None),
        "slippage_bps": getattr(config, "backtest_slippage_bps", None),
        "factor_lookback_days": getattr(
            config,
            "screening_factor_lookback_days",
            None,
        ),
        "market_guard_index": getattr(
            config,
            "screening_market_guard_index",
            None,
        ),
    }


@contextmanager
def isolated_replay_database(
    *,
    source_db: Any,
    universe: Any,
    date_from: date,
    date_to: date,
    market: str,
    market_guard_index: str,
    config: Any = None,
    market_environment_version: str = "market_environment_engine:v1",
) -> Iterator[Any]:
    """Stream a warmup+evaluation+future slice into an isolated SQLite DB."""
    from sqlalchemy import select

    from src.storage import (
        BoardMaster,
        DailySectorHeat,
        DatabaseManager,
        InstrumentBoardMembership,
        InstrumentMaster,
        ScreeningRun,
        StockDaily,
    )

    codes = sorted(str(code) for code in universe["code"].tolist())
    if not codes:
        raise ValidationInputError("EMPTY_UNIVERSE", "universe is empty")
    stock_codes = list(dict.fromkeys([*codes, market_guard_index]))
    with TemporaryDirectory(prefix="bottom-divergence-v2-validation-") as root:
        database_path = Path(root) / "validation.db"
        temporary = object.__new__(DatabaseManager)
        DatabaseManager.__init__(
            temporary,
            f"sqlite:///{database_path.as_posix()}",
        )
        try:
            with (
                source_db.get_session() as source_session,
                temporary.get_session() as target_session,
            ):
                future_dates = list(source_session.execute(
                    select(StockDaily.date)
                    .where(
                        StockDaily.code.in_(codes),
                        StockDaily.date > date_to,
                    )
                    .distinct()
                    .order_by(StockDaily.date)
                    .limit(20)
                ).scalars())
                if len(future_dates) < 20:
                    raise ValidationInputError(
                        "FUTURE_HISTORY_INSUFFICIENT",
                        "stock_daily requires 20 future trading dates",
                    )
                copy_end = future_dates[-1]
                warmup_dates = list(source_session.execute(
                    select(StockDaily.date)
                    .where(
                        StockDaily.code.in_(codes),
                        StockDaily.date <= date_from,
                    )
                    .distinct()
                    .order_by(StockDaily.date.desc())
                    .limit(400)
                ).scalars())
                if not warmup_dates:
                    raise ValidationInputError(
                        "NO_TRADING_DATES",
                        "stock_daily has no warmup history",
                    )
                copy_start = min(warmup_dates)
                identity = {
                    "universe_codes": codes,
                    "copy_range": {
                        "from": copy_start.isoformat(),
                        "to": copy_end.isoformat(),
                    },
                    "requested_range": {
                        "from": date_from.isoformat(),
                        "to": date_to.isoformat(),
                    },
                    "market": market,
                    "market_environment_version": market_environment_version,
                    "config": _config_identity(config),
                }
                hasher = sha256()
                hasher.update(canonical_json_dumps(identity).encode("utf-8"))
                hasher.update(b"\n")

                _copy_batches(
                    source_session=source_session,
                    target_session=target_session,
                    model=InstrumentMaster,
                    statement=_statement_for(
                        InstrumentMaster,
                        InstrumentMaster.code.in_(codes),
                        order_by=(InstrumentMaster.code,),
                    ),
                )
                board_ids: set[Any] = set()
                membership_statement = _statement_for(
                    InstrumentBoardMembership,
                    InstrumentBoardMembership.instrument_code.in_(codes),
                    order_by=(
                        InstrumentBoardMembership.instrument_code,
                        InstrumentBoardMembership.board_id,
                        InstrumentBoardMembership.source,
                    ),
                )
                for batch in iter_query_batches(
                    source_session,
                    membership_statement,
                ):
                    board_ids.update(row["board_id"] for row in batch)
                board_names: set[str] = set()

                def observe_board(row: Mapping[str, Any]) -> None:
                    board_names.add(str(row["board_name"]))
                    _hash_record(
                        hasher,
                        "board_master",
                        row,
                        _HASH_BOARD_FIELDS,
                    )

                if board_ids:
                    _copy_batches(
                        source_session=source_session,
                        target_session=target_session,
                        model=BoardMaster,
                        statement=_statement_for(
                            BoardMaster,
                            BoardMaster.id.in_(sorted(board_ids)),
                            order_by=(BoardMaster.id,),
                        ),
                        keep_id=True,
                        observe=observe_board,
                    )
                _copy_batches(
                    source_session=source_session,
                    target_session=target_session,
                    model=InstrumentBoardMembership,
                    statement=membership_statement,
                    observe=lambda row: _hash_record(
                        hasher,
                        "instrument_board_membership",
                        row,
                        _HASH_MEMBERSHIP_FIELDS,
                    ),
                )
                sector_heat_count = 0
                if board_names:
                    sector_heat_count = _copy_batches(
                        source_session=source_session,
                        target_session=target_session,
                        model=DailySectorHeat,
                        statement=_statement_for(
                            DailySectorHeat,
                            DailySectorHeat.board_name.in_(
                                sorted(board_names)
                            ),
                            DailySectorHeat.trade_date >= copy_start,
                            DailySectorHeat.trade_date <= copy_end,
                            order_by=(
                                DailySectorHeat.trade_date,
                                DailySectorHeat.board_name,
                            ),
                        ),
                        observe=lambda row: _hash_record(
                            hasher,
                            "daily_sector_heat",
                            row,
                            _HASH_SECTOR_HEAT_FIELDS,
                        ),
                    )
                stock_count = _copy_batches(
                    source_session=source_session,
                    target_session=target_session,
                    model=StockDaily,
                    statement=_statement_for(
                        StockDaily,
                        StockDaily.code.in_(stock_codes),
                        StockDaily.date >= copy_start,
                        StockDaily.date <= copy_end,
                        order_by=(StockDaily.code, StockDaily.date),
                    ),
                    observe=lambda row: _validate_and_hash_bar(hasher, row),
                )
                data_version = hasher.hexdigest()
                run_identity = data_version[:20]
                target_session.add(ScreeningRun(
                    run_id=f"validation-{run_identity}",
                    trade_date=date_from,
                    market=market,
                    status="factorizing",
                    universe_size=len(codes),
                ))
                target_session.commit()
                temporary.validation_data_version = data_version
                temporary.validation_copy_manifest = {
                    **identity,
                    "stock_daily_count": stock_count,
                    "daily_sector_heat_count": sector_heat_count,
                }
            yield temporary
        finally:
            temporary._engine.dispose()
