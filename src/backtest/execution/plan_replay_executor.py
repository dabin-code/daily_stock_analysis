# -*- coding: utf-8 -*-
"""Replay frozen structured trade plans against forward daily bars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class PlanReplayResult:
    status: str
    entry_price: Optional[float] = None
    entry_date: Optional[date] = None
    entry_index: Optional[int] = None
    exit_price: Optional[float] = None
    exit_date: Optional[date] = None
    exit_index: Optional[int] = None
    exit_reason: Optional[str] = None
    holding_days: Optional[int] = None
    trade_return_pct: Optional[float] = None


class PlanReplayExecutor:
    """Executes a frozen structured trade plan with conservative daily-bar rules."""

    @staticmethod
    def replay(
        trade_plan: Dict[str, Any],
        forward_bars: List[Any],
    ) -> PlanReplayResult:
        entry_price = _positive_float(trade_plan.get("entry_price"))
        if entry_price is None:
            return PlanReplayResult(status="missing_structured_trade_plan")
        if not forward_bars:
            return PlanReplayResult(status="no_forward_bars")

        entry_valid_days = _positive_int(trade_plan.get("entry_valid_days")) or 1
        entry_index = _find_entry_index(forward_bars, entry_price, entry_valid_days)
        if entry_index is None:
            return PlanReplayResult(status="entry_not_filled")

        entry_bar = forward_bars[entry_index]
        stop_loss_price = _positive_float(trade_plan.get("stop_loss_price"))
        take_profit_price = _positive_float(trade_plan.get("take_profit_price"))
        time_stop_days = _positive_int(trade_plan.get("time_stop_days")) or len(forward_bars)
        exit_index, exit_price, exit_reason = _resolve_exit(
            forward_bars=forward_bars,
            start_index=entry_index,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            time_stop_days=time_stop_days,
        )

        if exit_price is None or exit_index is None:
            return PlanReplayResult(
                status="completed",
                entry_price=entry_price,
                entry_date=getattr(entry_bar, "date", None),
                holding_days=0,
            )

        trade_return_pct = (exit_price - entry_price) / entry_price * 100
        return PlanReplayResult(
            status="completed",
            entry_price=round(entry_price, 4),
            entry_date=getattr(entry_bar, "date", None),
            entry_index=entry_index,
            exit_price=round(exit_price, 4),
            exit_date=getattr(forward_bars[exit_index], "date", None),
            exit_index=exit_index,
            exit_reason=exit_reason,
            holding_days=exit_index - entry_index + 1,
            trade_return_pct=round(trade_return_pct, 4),
        )


def _positive_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _find_entry_index(
    forward_bars: List[Any],
    entry_price: float,
    entry_valid_days: int,
) -> Optional[int]:
    for idx, bar in enumerate(forward_bars[:entry_valid_days]):
        low, high = _bar_low_high(bar)
        if low is not None and high is not None and low <= entry_price <= high:
            return idx
    return None


def _resolve_exit(
    *,
    forward_bars: List[Any],
    start_index: int,
    entry_price: float,
    stop_loss_price: Optional[float],
    take_profit_price: Optional[float],
    time_stop_days: int,
) -> tuple[Optional[int], Optional[float], Optional[str]]:
    last_index = min(len(forward_bars) - 1, start_index + max(time_stop_days - 1, 0))

    for idx in range(start_index, last_index + 1):
        bar = forward_bars[idx]
        low, high = _bar_low_high(bar)
        if low is None or high is None:
            continue
        stop_hit = stop_loss_price is not None and low <= stop_loss_price
        take_profit_hit = take_profit_price is not None and high >= take_profit_price

        # On the entry bar, do not allow a stop below the entry price to be
        # considered before the entry itself. Daily bars cannot prove the
        # intraday order, so the first post-entry bar is the earliest exit.
        if idx == start_index:
            continue

        if stop_hit and take_profit_hit:
            return idx, stop_loss_price, "ambiguous_stop_loss"
        if stop_hit:
            return idx, stop_loss_price, "stop_loss"
        if take_profit_hit:
            return idx, take_profit_price, "take_profit"

    time_bar = forward_bars[last_index]
    return last_index, _positive_float(getattr(time_bar, "close", None)) or entry_price, "time_stop"


def _bar_low_high(bar: Any) -> tuple[Optional[float], Optional[float]]:
    low = _positive_float(getattr(bar, "low", None))
    high = _positive_float(getattr(bar, "high", None))
    if low is None or high is None or low > high:
        return None, None
    return low, high
