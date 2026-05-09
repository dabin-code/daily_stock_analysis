# -*- coding: utf-8 -*-
"""ExecutionModelResolver: three-tier fill logic with limit/gap/dual-trigger handling.

Models:
  - conservative: T+1 open entry. Strictest limit-up/down blocks. Stop-loss first on dual-trigger.
  - baseline: T+1 VWAP proxy ((H+L+C)/3). Only 一字涨停 blocks.
  - optimistic: T+1 open with favorable assumptions. No limit-up blocks. Research only.

Limit-up / limit-down judgment delegates to :mod:`limit_pct_resolver` so the
threshold matches the actual board (±10 % 主板, ±20 % 科创/创业, ±30 % 北交所,
±5 % ST). Pass ``code`` / ``is_st`` / ``market`` / ``exchange`` to
:meth:`ExecutionModelResolver.resolve` to enable per-board limits; omit them
to fall back to the legacy ±10 % default (preserves existing behaviour for
callers that haven't been updated yet).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, List, Optional, Protocol, Tuple

from src.backtest.execution.limit_pct_resolver import (
    DEFAULT_A_SHARE_LIMIT_DOWN_PCT,
    DEFAULT_A_SHARE_LIMIT_UP_PCT,
    is_at_or_near_limit_down,
    is_at_or_near_limit_up,
    resolve_limit_pct,
)


class DailyBarLike(Protocol):
    """Protocol for daily bar objects."""
    date: date
    open: float
    high: float
    low: float
    close: float
    pct_chg: float


@dataclass
class ExecutionResult:
    """Result of execution model resolution."""
    fill_status: str = "pending"       # filled / limit_blocked / pending
    fill_price: Optional[float] = None
    fill_date: Optional[date] = None
    limit_blocked: bool = False
    gap_adjusted: bool = False
    ambiguous_intraday_order: bool = False
    exit_fill_status: Optional[str] = None
    exit_fill_price: Optional[float] = None
    exit_fill_date: Optional[date] = None
    exit_limit_blocked: bool = False
    holding_days: int = 0
    # Recorded so consumers (and tests) can verify the per-board limit
    # threshold actually used for this evaluation. None when caller did
    # not provide instrument metadata (legacy default ±10 % was used).
    limit_up_pct: Optional[float] = None
    limit_down_pct: Optional[float] = None


class ExecutionModelResolver:
    """Resolves entry/exit fills based on execution model tier."""

    @staticmethod
    def resolve(
        execution_model: str,
        forward_bars: List[Any],
        signal_price: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        stop_loss_pct: Optional[float] = None,
        *,
        code: Optional[str] = None,
        is_st: bool = False,
        market: str = "cn",
        exchange: Optional[str] = None,
    ) -> ExecutionResult:
        if not forward_bars:
            return ExecutionResult(fill_status="pending")

        entry_bar = forward_bars[0]

        # Resolve per-board limits once for the whole window. Pass code +
        # is_st + market + exchange to get the board-specific threshold;
        # otherwise fall back to ±10 % so legacy callers (and unit tests
        # that don't set the metadata) keep their previous behaviour.
        if code is not None:
            limit_up_pct, limit_down_pct = resolve_limit_pct(
                code=code, is_st=is_st, market=market, exchange=exchange,
            )
        else:
            limit_up_pct, limit_down_pct = (
                DEFAULT_A_SHARE_LIMIT_UP_PCT,
                DEFAULT_A_SHARE_LIMIT_DOWN_PCT,
            )

        # ── Entry fill ────────────────────────────────────────────────────
        if execution_model == "conservative":
            if _is_limit_up_conservative(entry_bar, limit_up_pct):
                return ExecutionResult(
                    fill_status="limit_blocked",
                    limit_blocked=True,
                    fill_date=entry_bar.date,
                    limit_up_pct=limit_up_pct,
                    limit_down_pct=limit_down_pct,
                )
            entry_price = entry_bar.open
        elif execution_model == "baseline":
            if _is_limit_up_yizi(entry_bar, limit_up_pct):
                return ExecutionResult(
                    fill_status="limit_blocked",
                    limit_blocked=True,
                    fill_date=entry_bar.date,
                    limit_up_pct=limit_up_pct,
                    limit_down_pct=limit_down_pct,
                )
            entry_price = (entry_bar.high + entry_bar.low + entry_bar.close) / 3
        else:  # optimistic
            entry_price = entry_bar.open

        result = ExecutionResult(
            fill_status="filled",
            fill_price=entry_price,
            fill_date=entry_bar.date,
            limit_up_pct=limit_up_pct,
            limit_down_pct=limit_down_pct,
        )

        # ── Exit resolution (if stop/take-profit specified) ───────────────
        if take_profit_pct is None and stop_loss_pct is None:
            return result

        if len(forward_bars) < 2:
            return result

        tp_price = entry_price * (1 + take_profit_pct / 100) if take_profit_pct else None
        sl_price = entry_price * (1 + stop_loss_pct / 100) if stop_loss_pct else None

        for i, bar in enumerate(forward_bars[1:], start=1):
            # Check limit-down exit block
            if execution_model == "conservative" and _is_limit_down(bar, limit_down_pct):
                result.exit_limit_blocked = True
                continue

            # Check gap scenarios
            if sl_price and bar.open < sl_price:
                # Gap down through stop-loss
                result.exit_fill_price = bar.open
                result.exit_fill_date = bar.date
                result.exit_fill_status = "filled"
                result.gap_adjusted = True
                result.holding_days = i
                return result

            if tp_price and bar.open > tp_price:
                # Gap up through take-profit
                result.exit_fill_price = bar.open
                result.exit_fill_date = bar.date
                result.exit_fill_status = "filled"
                result.gap_adjusted = True
                result.holding_days = i
                return result

            # Check dual-trigger (both stop and take-profit hit same day)
            sl_hit = sl_price and bar.low <= sl_price
            tp_hit = tp_price and bar.high >= tp_price

            if sl_hit and tp_hit:
                result.ambiguous_intraday_order = True
                if execution_model == "conservative":
                    # Worst case: stop-loss first
                    result.exit_fill_price = sl_price
                else:
                    # Baseline/optimistic: take-profit first
                    result.exit_fill_price = tp_price
                result.exit_fill_date = bar.date
                result.exit_fill_status = "filled"
                result.holding_days = i
                return result

            if sl_hit:
                result.exit_fill_price = sl_price
                result.exit_fill_date = bar.date
                result.exit_fill_status = "filled"
                result.holding_days = i
                return result

            if tp_hit:
                result.exit_fill_price = tp_price
                result.exit_fill_date = bar.date
                result.exit_fill_status = "filled"
                result.holding_days = i
                return result

        # No exit triggered within window
        result.holding_days = len(forward_bars) - 1
        return result


def _is_limit_up_conservative(bar: Any, limit_up_pct: Optional[float]) -> bool:
    """Conservative: open==high and pct_chg within tolerance of limit_up_pct.

    Returns False when ``limit_up_pct`` is None (e.g. non-cn market) so
    HK / US bars are never flagged as limit-blocked here.
    """
    if limit_up_pct is None:
        return False
    return bar.open == bar.high and is_at_or_near_limit_up(
        getattr(bar, "pct_chg", None), limit_up_pct,
    )


def _is_limit_up_yizi(bar: Any, limit_up_pct: Optional[float]) -> bool:
    """一字涨停: open==high==low==close and pct_chg within tolerance of limit_up_pct."""
    if limit_up_pct is None:
        return False
    return (
        bar.open == bar.high == bar.low == bar.close
        and is_at_or_near_limit_up(getattr(bar, "pct_chg", None), limit_up_pct)
    )


def _is_limit_down(bar: Any, limit_down_pct: Optional[float]) -> bool:
    """一字跌停: open==low and pct_chg within tolerance of limit_down_pct."""
    if limit_down_pct is None:
        return False
    return bar.open == bar.low and is_at_or_near_limit_down(
        getattr(bar, "pct_chg", None), limit_down_pct,
    )
