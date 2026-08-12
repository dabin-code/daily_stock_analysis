# -*- coding: utf-8 -*-
"""setup 级止损解析的单一真源。

L5 交易阶段裁决与交易计划构建都需要「这个 setup 有没有止损位」。
二者在管线中一前一后（`stage_judge.judge` 的结果是 `plan_builder.build`
的入参），直接互调会成环，因此把解析逻辑提到这里，两边都依赖本模块。
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional

from src.schemas.trading_types import SetupType
from src.services.bottom_divergence_v2_trade_support import (
    resolve_current_stage_stop_loss,
)


def safe_positive_finite_price(value: Any) -> Optional[float]:
    """只接受正的有限价格，其余一律 None。"""
    # bool 是 int 的子类，float(True) == 1.0 会被当成 1 元的止损价。
    if isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return price


def resolve_setup_stop_loss(
    setup_type: SetupType,
    factor_snapshot: Mapping[str, Any],
) -> Optional[float]:
    """按 setup 类型解析止损位，解析不出返回 None。

    返回 None 是有意义的信号：L5 据此把候选压在 focus，
    不得用兜底值填充，否则这道闸门形同废除。
    """
    if setup_type == SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY:
        # v2 分层买点只认当前阶段解析出的止损，不吃 risk_params 兜底。
        return safe_positive_finite_price(
            resolve_current_stage_stop_loss(factor_snapshot)
        )

    if setup_type == SetupType.LOW123_BREAKOUT:
        stop = safe_positive_finite_price(factor_snapshot.get("pattern_123_stop_loss"))
    elif setup_type == SetupType.BOTTOM_DIVERGENCE_BREAKOUT:
        exit_plan = factor_snapshot.get("bottom_divergence_exit_plan")
        if not isinstance(exit_plan, dict):
            exit_plan = {}
        stop = safe_positive_finite_price(
            factor_snapshot.get("bottom_divergence_stop_loss")
        ) or safe_positive_finite_price(exit_plan.get("initial_stop_loss"))
    elif setup_type == SetupType.TREND_PULLBACK:
        stop = safe_positive_finite_price(
            factor_snapshot.get("shrink_pullback_stop_loss_price")
        )
    else:
        stop = None

    if stop is None:
        risk_params = factor_snapshot.get("risk_params")
        if isinstance(risk_params, dict):
            stop = safe_positive_finite_price(risk_params.get("stop_loss"))
    return stop
