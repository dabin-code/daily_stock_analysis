# -*- coding: utf-8 -*-
"""计划交易回放器测试：按选股时冻结的买卖点做真实交易闭环。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from src.backtest.execution.plan_replay_executor import PlanReplayExecutor


@dataclass
class Bar:
    date: date
    open: float
    high: float
    low: float
    close: float
    pct_chg: float = 0.0


def test_replay_marks_missing_structured_plan_as_not_evaluable() -> None:
    result = PlanReplayExecutor.replay(
        trade_plan={},
        forward_bars=[Bar(date(2026, 1, 2), 10.0, 10.5, 9.8, 10.2)],
    )

    assert result.status == "missing_structured_trade_plan"
    assert result.trade_return_pct is None


def test_replay_enters_when_planned_price_is_touched_and_exits_on_take_profit() -> None:
    result = PlanReplayExecutor.replay(
        trade_plan={
            "entry_price": 10.0,
            "entry_valid_days": 2,
            "stop_loss_price": 9.2,
            "take_profit_price": 11.0,
            "time_stop_days": 5,
        },
        forward_bars=[
            Bar(date(2026, 1, 2), 10.2, 10.4, 9.9, 10.1),
            Bar(date(2026, 1, 3), 10.2, 11.2, 10.1, 11.0),
        ],
    )

    assert result.status == "completed"
    assert result.entry_price == 10.0
    assert result.entry_date == date(2026, 1, 2)
    assert result.exit_price == 11.0
    assert result.exit_reason == "take_profit"
    assert result.trade_return_pct == 10.0


def test_replay_marks_entry_not_filled_when_price_is_not_touched() -> None:
    result = PlanReplayExecutor.replay(
        trade_plan={
            "entry_price": 10.0,
            "entry_valid_days": 1,
            "stop_loss_price": 9.2,
            "time_stop_days": 3,
        },
        forward_bars=[
            Bar(date(2026, 1, 2), 10.5, 11.0, 10.4, 10.8),
            Bar(date(2026, 1, 3), 10.3, 10.7, 10.2, 10.4),
        ],
    )

    assert result.status == "entry_not_filled"
    assert result.entry_price is None
    assert result.trade_return_pct is None


def test_replay_records_entry_index_for_delayed_fill() -> None:
    result = PlanReplayExecutor.replay(
        trade_plan={
            "entry_price": 10.0,
            "entry_valid_days": 2,
            "stop_loss_price": 9.2,
            "time_stop_days": 3,
        },
        forward_bars=[
            Bar(date(2026, 1, 2), 10.5, 10.8, 10.2, 10.6),
            Bar(date(2026, 1, 3), 10.2, 10.4, 9.8, 10.1),
            Bar(date(2026, 1, 4), 10.1, 10.6, 10.0, 10.5),
        ],
    )

    assert result.status == "completed"
    assert result.entry_index == 1
    assert result.entry_date == date(2026, 1, 3)


def test_replay_exits_on_stop_loss_before_window_end() -> None:
    result = PlanReplayExecutor.replay(
        trade_plan={
            "entry_price": 10.0,
            "entry_valid_days": 1,
            "stop_loss_price": 9.5,
            "take_profit_price": 11.5,
            "time_stop_days": 5,
        },
        forward_bars=[
            Bar(date(2026, 1, 2), 10.1, 10.4, 9.9, 10.2),
            Bar(date(2026, 1, 3), 10.0, 10.1, 9.4, 9.6),
        ],
    )

    assert result.status == "completed"
    assert result.exit_price == 9.5
    assert result.exit_reason == "stop_loss"
    assert result.trade_return_pct == -5.0


def test_replay_uses_stop_loss_first_for_same_day_dual_trigger() -> None:
    result = PlanReplayExecutor.replay(
        trade_plan={
            "entry_price": 10.0,
            "entry_valid_days": 1,
            "stop_loss_price": 9.5,
            "take_profit_price": 11.0,
            "time_stop_days": 5,
        },
        forward_bars=[
            Bar(date(2026, 1, 2), 10.0, 10.2, 9.9, 10.1),
            Bar(date(2026, 1, 3), 10.2, 11.2, 9.4, 10.6),
        ],
    )

    assert result.status == "completed"
    assert result.exit_reason == "ambiguous_stop_loss"
    assert result.exit_price == 9.5
    assert result.trade_return_pct == -5.0


def test_replay_time_exits_when_no_stop_or_take_profit_hit() -> None:
    result = PlanReplayExecutor.replay(
        trade_plan={
            "entry_price": 10.0,
            "entry_valid_days": 1,
            "stop_loss_price": 9.0,
            "take_profit_price": 12.0,
            "time_stop_days": 2,
        },
        forward_bars=[
            Bar(date(2026, 1, 2), 10.0, 10.2, 9.9, 10.1),
            Bar(date(2026, 1, 3), 10.1, 10.4, 9.8, 10.3),
            Bar(date(2026, 1, 4), 10.3, 10.6, 10.0, 10.5),
        ],
    )

    assert result.status == "completed"
    assert result.exit_date == date(2026, 1, 3)
    assert result.exit_reason == "time_stop"
    assert result.exit_price == 10.3
    assert result.trade_return_pct == 3.0
