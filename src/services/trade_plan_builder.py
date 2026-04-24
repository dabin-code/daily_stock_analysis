# -*- coding: utf-8 -*-
"""
Phase 3A — TradePlanBuilder: 根据 trade_stage + setup_type 生成可执行交易计划。

仅 probe_entry / add_on_strength 生成 TradePlan，其余阶段返回 None。
"""

from __future__ import annotations

from typing import Dict, Optional

from src.schemas.trading_types import (
    CandidatePoolLevel,
    EntryMaturity,
    RiskLevel,
    SetupType,
    TradePlan,
    TradeStage,
)

# ── 止损模板 ─────────────────────────────────────────────────────────────────

_STOP_LOSS_TEMPLATES: Dict[SetupType, str] = {
    SetupType.BOTTOM_DIVERGENCE_BREAKOUT: "跌破底背离确认K线低点止损",
    SetupType.LOW123_BREAKOUT: "初始跌破P3止损;突破后形成新低点→止损上移至最新低点（自然止损法）",
    SetupType.TREND_BREAKOUT: "跌破突破K线实体下沿或MA20止损",
    SetupType.TREND_PULLBACK: "跌破回踩MA20低点止损",
    SetupType.GAP_BREAKOUT: "回补缺口止损",
    SetupType.LIMITUP_STRUCTURE: "跌破涨停板开板价止损",
}

_DEFAULT_STOP_LOSS = "跌破近期支撑位止损"

# ── 止盈模板 ─────────────────────────────────────────────────────────────────

_TAKE_PROFIT_TEMPLATES: Dict[SetupType, str] = {
    SetupType.BOTTOM_DIVERGENCE_BREAKOUT: "目标前高压力位;分批止盈,首目标+10%减半",
    SetupType.LOW123_BREAKOUT: "目标前高或MA100;突破后随新低点推升止损;冲高分批减仓",
    SetupType.TREND_BREAKOUT: "沿MA10移动止盈;跌破MA10减仓",
    SetupType.TREND_PULLBACK: "反弹至前高区域止盈;跌破MA20离场",
    SetupType.GAP_BREAKOUT: "持仓3日内冲高减仓;缩量回落离场",
    SetupType.LIMITUP_STRUCTURE: "次日高开冲高减半;3日内未续涨则离场",
}

_DEFAULT_TAKE_PROFIT = "分批止盈;跌破关键均线离场"

# ── 加仓模板 ─────────────────────────────────────────────────────────────────

_ADD_RULE_TEMPLATES: Dict[SetupType, str] = {
    SetupType.BOTTOM_DIVERGENCE_BREAKOUT: "突破前高+放量确认后加仓;最多加仓1次;跌破加仓K低点取消",
    SetupType.LOW123_BREAKOUT: "回踩P2支撑位+缩量企稳二次加仓;最多加仓1次;跌破P2支撑取消",
    SetupType.TREND_BREAKOUT: "回踩MA20不破+放量反弹加仓;最多加仓1次;跌破MA20取消",
    SetupType.TREND_PULLBACK: "二次回踩MA20+缩量企稳加仓;最多加仓1次;跌破前低取消",
    SetupType.GAP_BREAKOUT: "缺口上方放量突破前高加仓;最多加仓1次;回补缺口取消",
    SetupType.LIMITUP_STRUCTURE: "连板次日竞价强势加仓;最多加仓1次;开板即取消",
}

_DEFAULT_ADD_RULE = "确认突破+放量后加仓;最多加仓1次;跌破关键位取消"

# ── 持仓期望 ─────────────────────────────────────────────────────────────────

_SWING_SETUPS = frozenset({
    SetupType.BOTTOM_DIVERGENCE_BREAKOUT,
    SetupType.LOW123_BREAKOUT,
    SetupType.TREND_BREAKOUT,
})

# ── probe_entry 仓位映射 ────────────────────────────────────────────────────

_PROBE_POSITION: Dict[RiskLevel, str] = {
    RiskLevel.HIGH: "1/10仓",
    RiskLevel.MEDIUM: "1/5仓",
    RiskLevel.LOW: "1/3仓",
}

# ── add_on_strength 仓位映射 ─────────────────────────────────────────────────

_ADD_ON_POSITION: Dict[RiskLevel, str] = {
    RiskLevel.HIGH: "1/5仓",
    RiskLevel.MEDIUM: "1/3仓",
    RiskLevel.LOW: "1/2仓",
}

_INVALIDATION_RULE = "买入后3个交易日未启动则离场"


def _build_divergence_add_rule(buy_points: list) -> Optional[str]:
    """根据底背离检测器的三级 buy_points 构造动态 add_rule。

    规则：
    - 先找第一个未触发（triggered=False）且 level>=2 的买点，描述"如何触发→加仓"
    - 若三级都已触发，则描述最高级别已完成 + 跟踪止损
    - 若 level2/3 都触发但 level3 信息不完整（没有价格），回退到 None
    """
    tiered = [bp for bp in buy_points if isinstance(bp, dict) and bp.get("level", 0) >= 2]
    if not tiered:
        return None

    tiered_sorted = sorted(tiered, key=lambda bp: bp.get("level", 0))
    next_bp = next((bp for bp in tiered_sorted if not bp.get("triggered")), None)

    def _fmt(bp: dict) -> str:
        label = str(bp.get("label") or f"Level{bp.get('level')}")
        price = bp.get("trigger_price")
        ratio = bp.get("position_ratio") or "1/3仓"
        stop = bp.get("stop_loss_price")
        parts = [f"{label}"]
        if isinstance(price, (int, float)) and price > 0:
            parts.append(f"触发价{float(price):.2f}")
        parts.append(f"加仓{ratio}")
        if isinstance(stop, (int, float)) and stop > 0:
            parts.append(f"止损{float(stop):.2f}")
        return "·".join(parts)

    if next_bp is not None:
        prefix = "下一级买点："
        return f"{prefix}{_fmt(next_bp)}；跌破止损价即取消加仓"

    highest = tiered_sorted[-1]
    return f"三级买点已触发；维持{_fmt(highest)}，改用自然止损法跟踪"


def _format_anchor_value(label: str, value: object) -> Optional[str]:
    try:
        if label == "现价":
            return f"{label}{float(value):.2f}"
        return f"{label}={float(value):.2f}"
    except (TypeError, ValueError):
        return None


def _build_execution_note(setup_type: SetupType, factor_snapshot: dict) -> str:
    ma20 = factor_snapshot.get("ma20")
    ma100 = factor_snapshot.get("ma100")
    close = factor_snapshot.get("close")
    anchors = []
    for anchor in (
        _format_anchor_value("现价", close),
        _format_anchor_value("MA20", ma20),
        _format_anchor_value("MA100", ma100),
    ):
        if anchor:
            anchors.append(anchor)

    anchor_note = "，".join(anchors) if anchors else "以盘中结构低点与均线支撑作为执行锚点"
    if setup_type == SetupType.LIMITUP_STRUCTURE:
        return f"优先观察涨停结构是否继续封板或缩量承接，{anchor_note}"
    if setup_type == SetupType.GAP_BREAKOUT:
        return f"重点盯缺口不回补与前高突破，{anchor_note}"
    if setup_type in _SWING_SETUPS:
        return f"围绕趋势延续与关键均线支撑执行，{anchor_note}"
    return f"按结构确认和止损锚点执行，{anchor_note}"


class TradePlanBuilder:
    """根据 L5 trade_stage 和 L4 setup_type 生成可执行交易计划。"""

    def build(
        self,
        trade_stage: TradeStage,
        setup_type: SetupType,
        entry_maturity: EntryMaturity,
        risk_level: RiskLevel,
        pool_level: CandidatePoolLevel,
        factor_snapshot: dict,
    ) -> Optional[TradePlan]:
        if trade_stage not in (TradeStage.PROBE_ENTRY, TradeStage.ADD_ON_STRENGTH):
            return None

        is_add_on = trade_stage == TradeStage.ADD_ON_STRENGTH

        # 底背离双突破：用检测器输出的量化数据替代文本模板
        stop_loss_rule = _STOP_LOSS_TEMPLATES.get(setup_type, _DEFAULT_STOP_LOSS)
        take_profit_plan = _TAKE_PROFIT_TEMPLATES.get(setup_type, _DEFAULT_TAKE_PROFIT)
        initial_position = (
            _ADD_ON_POSITION.get(risk_level, "1/3仓")
            if is_add_on
            else _PROBE_POSITION.get(risk_level, "1/5仓")
        )

        if setup_type == SetupType.TREND_PULLBACK:
            support_ma = factor_snapshot.get("shrink_pullback_support_ma")
            sl_price = factor_snapshot.get("shrink_pullback_stop_loss_price")
            sl_basis = factor_snapshot.get("shrink_pullback_stop_loss_basis")
            if sl_price and support_ma and support_ma != "none":
                basis_label = "MA20" if sl_basis == "MA20" else "回踩低点×0.98"
                stop_loss_rule = (
                    f"入场锚点={support_ma}；止损价{float(sl_price):.2f}"
                    f"（{basis_label}）；跌破即离场"
                )

        dynamic_add_rule: Optional[str] = None

        if setup_type == SetupType.BOTTOM_DIVERGENCE_BREAKOUT:
            exit_plan = factor_snapshot.get("bottom_divergence_exit_plan") or {}
            buy_points = factor_snapshot.get("bottom_divergence_buy_points") or []

            sl_price = exit_plan.get("initial_stop_loss")
            if sl_price:
                stop_loss_rule = (
                    f"止损价{sl_price:.2f}（底背离新低点下方3%）；"
                    f"方法：自然止损法，突破后每形成新低点→止损上移至最新低点"
                )

            tp_targets = exit_plan.get("take_profit_targets", [])
            if tp_targets:
                take_profit_plan = "；".join(
                    f"{t['label']}→{t['action']}" for t in tp_targets
                )

            # 从买点结构中提取仓位建议
            triggered_bps = [bp for bp in buy_points if bp.get("triggered")]
            if triggered_bps:
                latest_bp = max(triggered_bps, key=lambda x: x.get("level", 0))
                initial_position = latest_bp.get("position_ratio", initial_position)

            # ADD_ON 阶段：按"趋势线 → 阻力线 → 回踩支撑"递进阶梯，
            # 从 detector 的 buy_points 里挑下一个未触发的层级生成
            # add_rule，携带真实价格/仓位，替代静态模板。
            if is_add_on and buy_points:
                dynamic_add_rule = _build_divergence_add_rule(buy_points)

        return TradePlan(
            initial_position=initial_position,
            add_rule=(
                (dynamic_add_rule or _ADD_RULE_TEMPLATES.get(setup_type, _DEFAULT_ADD_RULE))
                if is_add_on
                else None
            ),
            stop_loss_rule=stop_loss_rule,
            take_profit_plan=take_profit_plan,
            invalidation_rule=_INVALIDATION_RULE,
            risk_level=risk_level,
            holding_expectation=(
                "1~2周波段" if setup_type in _SWING_SETUPS else "3~5日短线"
            ),
            execution_note=_build_execution_note(setup_type, factor_snapshot),
        )
