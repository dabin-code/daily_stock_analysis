# -*- coding: utf-8 -*-
"""Streaming-copy and content-version tests for validation replay."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import func, select

from src.backtest.services.bottom_divergence_v2_dataset import (
    COPY_BATCH_SIZE,
    isolated_replay_database,
    iter_query_batches,
)
from src.config import Config
from src.storage import (
    BoardMaster,
    DailySectorHeat,
    DatabaseManager,
    InstrumentBoardMembership,
    InstrumentMaster,
    StockDaily,
)


class _StatementSpy:
    def __init__(self) -> None:
        self.options = {}

    def execution_options(self, **options):
        self.options = options
        return self


class _ResultSpy:
    def __init__(self, row_count: int) -> None:
        self.rows = [{"value": index} for index in range(row_count)]
        self.partition_sizes: list[int] = []

    def mappings(self):
        return self

    def partitions(self, size: int):
        self.partition_sizes.append(size)
        for start in range(0, len(self.rows), size):
            yield self.rows[start:start + size]

    def all(self):
        raise AssertionError("streaming copy must not call all()")


def test_stream_iterator_never_buffers_more_than_2000_or_calls_all() -> None:
    statement = _StatementSpy()
    result = _ResultSpy(4501)
    session = type("SessionSpy", (), {"execute": lambda self, stmt: result})()

    batches = list(iter_query_batches(session, statement))

    assert [len(batch) for batch in batches] == [2000, 2000, 501]
    assert max(map(len, batches)) <= COPY_BATCH_SIZE
    assert result.partition_sizes == [COPY_BATCH_SIZE]
    assert statement.options == {
        "stream_results": True,
        "yield_per": COPY_BATCH_SIZE,
    }


def _database(path) -> DatabaseManager:
    manager = object.__new__(DatabaseManager)
    DatabaseManager.__init__(manager, f"sqlite:///{path.as_posix()}")
    return manager


def _seed_source(manager: DatabaseManager) -> date:
    start = date(2024, 1, 1)
    with manager.get_session() as session:
        session.add(InstrumentMaster(code="000001", name="示例", market="cn"))
        session.add_all([
            StockDaily(
                code="000001",
                date=start + timedelta(days=index),
                open=100.0 + index,
                high=102.0 + index,
                low=99.0 + index,
                close=101.0 + index,
                volume=1_000.0 + index,
                amount=1_000_000.0 + index,
                pct_chg=1.0,
                data_source="fixture",
                adj_factor=1.0,
                adj_factor_source="fixture",
            )
            for index in range(35)
        ])
        session.commit()
    return start


def _copy_version(
    source: DatabaseManager,
    start: date,
    config: Config,
) -> tuple[str, int, float]:
    universe = pd.DataFrame([{"code": "000001"}])
    with isolated_replay_database(
        source_db=source,
        universe=universe,
        date_from=start + timedelta(days=5),
        date_to=start + timedelta(days=10),
        market="cn",
        market_guard_index="000300",
        config=config,
    ) as copied:
        with copied.get_session() as session:
            count = session.execute(
                select(func.count(StockDaily.id))
            ).scalar_one()
            close = session.execute(
                select(StockDaily.close)
                .where(StockDaily.code == "000001")
                .order_by(StockDaily.date)
            ).scalars().first()
        return copied.validation_data_version, count, float(close)


def test_real_sqlite_copy_preserves_rows_and_hashes_warmup_future_content(
    tmp_path,
) -> None:
    source = _database(tmp_path / "source.db")
    start = _seed_source(source)
    config = Config(
        backtest_buy_cost_bps=1.0,
        backtest_sell_cost_bps=2.0,
        backtest_slippage_bps=3.0,
    )
    first_hash, count, first_close = _copy_version(source, start, config)
    assert count == 31
    assert first_close == 101.0

    with source.get_session() as session:
        warmup = session.execute(
            select(StockDaily).where(StockDaily.date == start)
        ).scalar_one()
        warmup.close = 100.5
        session.commit()
    warmup_hash, _, _ = _copy_version(source, start, config)
    assert warmup_hash != first_hash

    with source.get_session() as session:
        future = session.execute(
            select(StockDaily).where(
                StockDaily.date == start + timedelta(days=30)
            )
        ).scalar_one()
        future.close = 999.0
        session.commit()
    future_hash, _, _ = _copy_version(source, start, config)
    assert future_hash != warmup_hash
    source._engine.dispose()


def test_content_hash_changes_when_cost_config_changes(tmp_path) -> None:
    source = _database(tmp_path / "source.db")
    start = _seed_source(source)
    first, _, _ = _copy_version(
        source,
        start,
        Config(backtest_buy_cost_bps=1.0),
    )
    second, _, _ = _copy_version(
        source,
        start,
        Config(backtest_buy_cost_bps=2.0),
    )
    assert first != second
    source._engine.dispose()


def test_content_hash_changes_when_only_pre_close_changes(tmp_path) -> None:
    """`pre_close` 变了就是另一份数据集，哪怕 OHLCV 一模一样。

    自 gate-3 起 `pre_close` 决定复权链，进而决定回测看到的每一个价格
    （`src/services/adjustment_chain.py`）。它不进内容哈希的话，两份复权
    结果完全不同的数据集会共用同一个 data_version，冻结证据与 base 快照缓存
    会跨数据集复用——是算错，不是算慢。
    """
    source = _database(tmp_path / "source.db")
    start = _seed_source(source)
    config = Config(backtest_buy_cost_bps=1.0)
    first, _, _ = _copy_version(source, start, config)

    with source.get_session() as session:
        bar = session.execute(
            select(StockDaily).where(
                StockDaily.date == start + timedelta(days=7)
            )
        ).scalar_one()
        bar.pre_close = float(bar.close) / 2.0
        session.commit()
    mutated, _, _ = _copy_version(source, start, config)

    assert mutated != first
    source._engine.dispose()


def _add_board_membership(
    manager: DatabaseManager,
    *,
    board_id: int,
    board_name: str,
    source: str = "fixture",
) -> None:
    with manager.get_session() as session:
        session.add(BoardMaster(
            id=board_id,
            board_code=f"B{board_id}",
            board_name=board_name,
            board_type="concept",
            market="cn",
            source=source,
            is_active=True,
        ))
        session.add(InstrumentBoardMembership(
            instrument_code="000001",
            board_id=board_id,
            market="cn",
            source=source,
            is_primary=False,
        ))
        session.commit()


def test_content_hash_includes_board_and_membership_content(tmp_path) -> None:
    source = _database(tmp_path / "source.db")
    start = _seed_source(source)
    config = Config(backtest_buy_cost_bps=1.0)
    _add_board_membership(source, board_id=11, board_name="白酒")
    first, _, _ = _copy_version(source, start, config)

    with source.get_session() as session:
        board = session.get(BoardMaster, 11)
        board.board_name = "消费"
        session.commit()
    renamed, _, _ = _copy_version(source, start, config)
    assert renamed != first

    _add_board_membership(source, board_id=22, board_name="沪深300")
    membership_changed, _, _ = _copy_version(source, start, config)
    assert membership_changed != renamed
    source._engine.dispose()


def test_board_hash_is_independent_of_insertion_order(tmp_path) -> None:
    first_source = _database(tmp_path / "first.db")
    second_source = _database(tmp_path / "second.db")
    first_start = _seed_source(first_source)
    second_start = _seed_source(second_source)
    for board_id, name in ((11, "白酒"), (22, "沪深300")):
        _add_board_membership(
            first_source,
            board_id=board_id,
            board_name=name,
        )
    for board_id, name in ((22, "沪深300"), (11, "白酒")):
        _add_board_membership(
            second_source,
            board_id=board_id,
            board_name=name,
        )

    config = Config(backtest_buy_cost_bps=1.0)
    first_hash, _, _ = _copy_version(first_source, first_start, config)
    second_hash, _, _ = _copy_version(second_source, second_start, config)
    assert first_hash == second_hash
    first_source._engine.dispose()
    second_source._engine.dispose()


def test_content_hash_includes_sector_heat_used_by_five_layer(tmp_path) -> None:
    source = _database(tmp_path / "source.db")
    start = _seed_source(source)
    _add_board_membership(source, board_id=11, board_name="白酒")
    with source.get_session() as session:
        session.add(DailySectorHeat(
            trade_date=start + timedelta(days=4),
            board_name="白酒",
            board_type="concept",
            sector_hot_score=60.0,
            board_strength_score=70.0,
            sector_status="warm",
            sector_stage="ferment",
        ))
        session.commit()
    config = Config(backtest_buy_cost_bps=1.0)
    first, _, _ = _copy_version(source, start, config)

    with source.get_session() as session:
        heat = session.execute(select(DailySectorHeat)).scalar_one()
        heat.board_strength_score = 80.0
        session.commit()
    changed, _, _ = _copy_version(source, start, config)

    assert changed != first
    source._engine.dispose()
