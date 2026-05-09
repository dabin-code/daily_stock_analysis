# -*- coding: utf-8 -*-
"""EntrySignalEvaluator: forward returns, MAE/MFE, drawdown, signal quality."""
from __future__ import annotations

from typing import Any, List, Optional

from src.backtest.evaluators.base_evaluator import BaseEvaluator, EvaluationResult

_DEFAULT_STOP_PCT = -3.0
_DEFAULT_TP_PCT = 5.0


class EntrySignalEvaluator(BaseEvaluator):
    """Evaluates entry signals using forward price bars."""

    @staticmethod
    def evaluate(
        entry_price: float,
        forward_bars: List[Any],
        take_profit_pct: Optional[float] = None,
        stop_loss_pct: Optional[float] = None,
        **kwargs,
    ) -> EvaluationResult:
        if not forward_bars or entry_price <= 0:
            return EvaluationResult()

        closes = [bar.close for bar in forward_bars]
        lows = [bar.low for bar in forward_bars]
        highs = [bar.high for bar in forward_bars]

        # Forward returns
        def _ret(idx):
            if idx < len(closes):
                return (closes[idx] - entry_price) / entry_price * 100
            return None

        forward_return_1d = _ret(0)
        forward_return_3d = _ret(2)
        forward_return_5d = _ret(4)
        forward_return_10d = _ret(9)

        # MAE / MFE
        min_low = min(lows)
        max_high = max(highs)
        mae = (min_low - entry_price) / entry_price * 100
        mfe = (max_high - entry_price) / entry_price * 100

        # Optimal entry deviation: how far actual entry was from best possible entry
        optimal_entry_deviation = (entry_price - min_low) / entry_price * 100
        optimal_entry_timing = lows.index(min_low) + 1  # 1-based day of lowest price

        # Max drawdown from peak
        peak = entry_price
        max_dd = 0.0
        for bar in forward_bars:
            if bar.high > peak:
                peak = bar.high
            dd = (bar.low - peak) / peak * 100
            if dd < max_dd:
                max_dd = dd

        # Signal quality score
        abs_mae = abs(mae)
        if mfe + abs_mae > 0:
            signal_quality_score = mfe / (mfe + abs_mae)
        else:
            signal_quality_score = 0.0

        # Plan success (take-profit vs stop-loss)
        plan_success = None
        exit_bar_index = None
        if take_profit_pct is not None and stop_loss_pct is not None:
            tp_price = entry_price * (1 + take_profit_pct / 100)
            sl_price = entry_price * (1 + stop_loss_pct / 100)
            plan_success = False
            for idx, bar in enumerate(forward_bars):
                sl_hit = bar.low <= sl_price
                tp_hit = bar.high >= tp_price
                if tp_hit and not sl_hit:
                    plan_success = True
                    exit_bar_index = idx
                    break
                if sl_hit:
                    plan_success = False
                    exit_bar_index = idx
                    break
                if tp_hit and sl_hit:
                    # Ambiguous — conservative: stop first
                    plan_success = False
                    exit_bar_index = idx
                    break

        # holding_days: actual exit day (1-based) if TP/SL triggered, else full window
        actual_holding_days = (exit_bar_index + 1) if exit_bar_index is not None else len(forward_bars)

        # outcome: align with plan_success when available, fallback to 5d return.
        # IMPORTANT: when forward_return_5d is None (window not mature, or returns
        # un-computable), keep outcome=None instead of forcing "loss". The previous
        # behaviour `(forward_return_5d or 0) > 0` silently labelled all immature
        # samples as losses and systematically under-stated entry win rates.
        if plan_success is True:
            outcome = "win"
        elif plan_success is False:
            outcome = "loss"
        elif forward_return_5d is not None:
            outcome = "win" if forward_return_5d > 0 else "loss"
        else:
            outcome = None

        return EvaluationResult(
            forward_return_1d=forward_return_1d,
            forward_return_3d=forward_return_3d,
            forward_return_5d=forward_return_5d,
            forward_return_10d=forward_return_10d,
            mae=mae,
            mfe=mfe,
            max_drawdown_from_peak=round(max_dd, 2),
            optimal_entry_deviation=round(optimal_entry_deviation, 4),
            optimal_entry_timing=optimal_entry_timing,
            holding_days=actual_holding_days,
            plan_success=plan_success,
            signal_quality_score=round(signal_quality_score, 4),
            outcome=outcome,
        )
