from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.notification import NotificationService
from src.services.screening_task_service import ScreeningTaskService
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

_COMPLETED_STATUSES = {"completed", "completed_with_ai_degraded"}

# ---------------------------------------------------------------------------
# Audit-content constants
# ---------------------------------------------------------------------------

# AI operation advice → score bonus mapping (mirrors DatabaseManager._screening_ai_bonus)
_AI_BONUS_MAP: Dict[str, float] = {
    "买入": 8.0,
    "加仓": 6.0,
    "关注": 4.0,
    "持有": 2.0,
    "观望": 0.0,
    "减仓": -4.0,
    "卖出": -8.0,
}

# Rule hit key → Chinese display name
_RULE_HIT_ZH: Dict[str, str] = {
    "trend_aligned": "趋势对齐",
    "volume_expanding": "放量",
    "near_breakout": "临近突破",
    "liquidity_ok": "流动性合格",
}

# Number of top candidates that receive the full audit block; the rest get a summary line.
_AUDIT_TOP_N_DEFAULT = 5

# 自适应推送内容的目标字节上限（UTF-8）。飞书交互卡片单条消息硬上限约 30KB，预留 2KB
# 给卡片包装（header / collapsible_panel / lark_md 元素的 JSON 结构），目标内容控制在 28KB
# 以内即可保证单条消息装下，不再触发 _send_feishu_chunked 分页。
# 注意：实际飞书 sender 还会再按 FEISHU_MAX_BYTES 做一次大小校验，那里的默认值已同步放宽。
_PUSH_MAX_CONTENT_BYTES = 28000

# 自适应降级时尝试的 audit_top_n 序列：优先保留所有候选的完整审计，超限再逐档削减完整块数。
# 0 表示所有候选都用一行紧凑摘要（最后兜底，仍能展示在飞书折叠面板里）。
_PUSH_AUDIT_TOP_N_LADDER: tuple[Optional[int], ...] = (None, 15, 10, 7, 5, 3, 1, 0)

# ---------------------------------------------------------------------------
# Five-layer decision label mappings (Phase 3B-2)
# ---------------------------------------------------------------------------

_REGIME_LABELS: Dict[str, str] = {
    "aggressive": "进攻",
    "balanced": "均衡",
    "defensive": "防守",
    "stand_aside": "观望",
}

_STAGE_LABELS: Dict[str, str] = {
    "probe_entry": "试探进场",
    "add_on_strength": "强势加仓",
    "focus": "重点关注",
    "watch": "观察",
    "stand_aside": "观望",
    "reject": "拒绝",
}

_SETUP_LABELS: Dict[str, str] = {
    "bottom_divergence_breakout": "底背离突破",
    "low123_breakout": "123结构突破",
    "trend_breakout": "趋势突破",
    "trend_pullback": "趋势回踩",
    "gap_breakout": "缺口突破",
    "limitup_structure": "涨停结构突破",
    "none": "无",
}

_POOL_LABELS: Dict[str, str] = {
    "leader_pool": "龙头池",
    "focus_list": "关注池",
    "watchlist": "观察池",
}

_THEME_POSITION_LABELS: Dict[str, str] = {
    "main_theme": "主线题材",
    "secondary_theme": "次主线题材",
    "follower_theme": "跟随题材",
    "fading_theme": "退潮题材",
    "non_theme": "非题材",
}

_SIGNAL_LABELS: Dict[str, str] = {
    "bottom_divergence_breakout": "底背离双突破",
    "low123_breakout": "低位 123 结构",
    "breakaway_gap_ma100": "缺口突破 MA100",
    "limitup_structure": "涨停结构",
    "structure_low_entry": "结构低吸",
    "momentum_breakout": "动量突破",
    "momentum_chase": "动量追涨",
    "none": "无",
}


# ---------------------------------------------------------------------------
# Module-level private helpers
# ---------------------------------------------------------------------------


def _fmt_amount(val: Any) -> str:
    """Format a raw avg_amount value into a human-readable string (亿 / 万 / raw)."""
    if val is None:
        return "N/A"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "N/A"
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.1f}万"
    return f"{v:.0f}"


def _fmt_percent(val: Any) -> str:
    if val is None:
        return "N/A"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "N/A"
    return f"{v * 100:.0f}%"


def _get_rule_hits(item: Dict[str, Any]) -> List[str]:
    """Extract rule_hits list from a candidate dict, handling JSON string or list."""
    rule_hits = item.get("rule_hits")
    if rule_hits is None:
        raw = item.get("rule_hits_json") or "[]"
        rule_hits = _safe_json(raw, []) if isinstance(raw, str) else raw
    elif isinstance(rule_hits, str):
        parsed = _safe_json(rule_hits, None)
        rule_hits = parsed if isinstance(parsed, list) else [rule_hits]
    return list(rule_hits) if isinstance(rule_hits, (list, tuple, set)) else []


def _get_factor_snapshot(item: Dict[str, Any]) -> Dict[str, Any]:
    """Extract factor_snapshot dict from a candidate dict, handling JSON string or dict."""
    factor = item.get("factor_snapshot")
    if factor is None:
        raw = item.get("factor_snapshot_json") or "{}"
        factor = _safe_json(raw, {}) if isinstance(raw, str) else raw
    elif isinstance(factor, str):
        factor = _safe_json(factor, {})
    return dict(factor) if isinstance(factor, dict) else {}


def _safe_json(raw: Any, default: Any) -> Any:
    if not isinstance(raw, str):
        return default
    try:
        return json.loads(raw or "")
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("筛选通知字段 JSON 解析失败，已降级为空值")
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_optional_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ensure_list(value: Any) -> List[Any]:
    """Normalize a JSON string / tuple / scalar into a list for display."""
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                return list(parsed) if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                return [value]
        return [value]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _format_value(value: Any) -> str:
    """Format a scalar/list/dict value for compact Markdown display."""
    if value is None or value == "":
        return "N/A"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, (list, tuple, set)):
        return "、".join(_format_value(v) for v in value if v not in (None, ""))
    if isinstance(value, dict):
        return "；".join(
            f"{k}: {_format_value(v)}"
            for k, v in value.items()
            if v not in (None, "", [], {})
        ) or "N/A"
    return str(value)


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ScreeningNotificationError(Exception):
    """Base exception for screening notification failures."""


class ScreeningRunNotFoundError(ScreeningNotificationError):
    """Raised when the target screening run does not exist."""


class ScreeningRunNotReadyError(ScreeningNotificationError):
    """Raised when the target screening run is not ready for notification."""


class ScreeningNotificationDeliveryError(ScreeningNotificationError):
    """Raised when notification delivery fails for all channels."""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ScreeningNotificationService:
    """全市场筛选推荐名单通知服务。"""

    def __init__(
        self,
        screening_task_service: Optional[ScreeningTaskService] = None,
        notifier: Optional[NotificationService] = None,
        db_manager: Optional[DatabaseManager] = None,
    ) -> None:
        self.screening_task_service = screening_task_service or ScreeningTaskService()
        self.notifier = notifier or NotificationService()
        self.db = db_manager or DatabaseManager.get_instance()

    # ------------------------------------------------------------------
    # Idempotent notification entry point
    # ------------------------------------------------------------------

    @staticmethod
    def can_notify(run: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
        """Check whether a run is eligible for notification.

        Returns dict with ``allowed`` (bool) and optional ``reason``.
        """
        status = run.get("status", "")
        if status not in _COMPLETED_STATUSES:
            return {"allowed": False, "reason": "run_not_completed"}

        ns = run.get("notification_status") or "pending"
        if ns == "sent":
            # v1: never allow re-send of already-sent
            return {"allowed": False, "reason": "already_sent"}
        if ns == "pending" or ns == "failed":
            return {"allowed": True}
        if ns == "skipped":
            if force:
                return {"allowed": True}
            return {"allowed": False, "reason": "skipped_need_force"}
        return {"allowed": False, "reason": f"unknown_status_{ns}"}

    def notify_run(self, run_id: str, force: bool = False) -> Dict[str, Any]:
        """Idempotent notification entry point for a screening run.

        1. Fetch run and validate
        2. Gate via ``can_notify``
        3. Build message, send, update notification status
        """
        run = self.screening_task_service.get_run(run_id)
        if run is None:
            raise ScreeningRunNotFoundError("筛选任务不存在")
        if run.get("status") not in _COMPLETED_STATUSES:
            raise ScreeningRunNotReadyError("筛选任务尚未完成，暂不可推送")

        gate = self.can_notify(run, force=force)
        if not gate["allowed"]:
            reason = gate.get("reason", "unknown")
            return {"skipped": True, "reason": reason, "run_id": run_id}

        # Build notification content
        candidates = self.screening_task_service.list_candidates(run_id=run_id, limit=100)
        # 文件存档保留完整审计块，便于追溯每只候选的规则命中、五层决策、AI 复核等信息。
        archive_content = self.build_run_notification(run=run, candidates=candidates)
        # 推送给飞书 / 邮件 / 企业微信等渠道的内容：优先保留所有候选的完整审计块，让用户点开
        # 飞书折叠面板就能看到每只候选的[评分汇总]/[规则分拆解]/[原始指标]/[审计证据] 等详情。
        # 当完整审计体积超过单条飞书消息上限（~28KB）时，按 _PUSH_AUDIT_TOP_N_LADDER 自适应
        # 降级——保留 Top N 完整审计、其余降为单行摘要——直到内容能塞进一条消息为止。
        push_content = self._build_adaptive_push_content(run=run, candidates=candidates)
        stock_codes = [str(item.get("code")) for item in candidates if item.get("code")]

        # Attempt delivery
        try:
            self.notifier.save_report_to_file(archive_content, filename=f"screening_{run_id}.md")
            success = self.notifier.send(push_content, email_stock_codes=stock_codes or None)
        except Exception as exc:
            self._mark_notification_failed(run_id, str(exc))
            return {
                "success": False,
                "notification_status": "failed",
                "run_id": run_id,
                "error": str(exc),
            }

        if success:
            self._mark_notification_sent(run_id)
            return {
                "success": True,
                "notification_status": "sent",
                "run_id": run_id,
                "candidate_count": len(candidates),
            }
        else:
            self._mark_notification_failed(run_id, "delivery returned false")
            return {
                "success": False,
                "notification_status": "failed",
                "run_id": run_id,
                "error": "delivery returned false",
            }

    # ------------------------------------------------------------------
    # Adaptive push content helpers
    # ------------------------------------------------------------------

    def _build_adaptive_push_content(
        self,
        run: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        max_bytes: int = _PUSH_MAX_CONTENT_BYTES,
    ) -> str:
        """构建能塞进单条飞书消息的推送内容，尽可能保留完整审计详情。

        遍历 ``_PUSH_AUDIT_TOP_N_LADDER`` 中的候选值：
        - 先尝试 ``None``（所有候选都是完整审计块），让用户在飞书折叠面板里看到全部详情。
        - 当字节数超过 ``max_bytes`` 时，依次降级为 Top 15 / 10 / 7 / 5 / 3 / 1 完整审计，
          其余候选退化为单行摘要。
        - 最坏情况降到 0（全部紧凑摘要）也仍然返回一个能装下的内容。

        本方法只控制 Markdown 原文体积；最终飞书卡片 payload 还会被 ``FeishuSender``
        按 ``FEISHU_MAX_BYTES`` 再校验一次（已同步放宽默认值），双层兜底避免分页。
        """
        last_content = ""
        for top_n in _PUSH_AUDIT_TOP_N_LADDER:
            content = self.build_run_notification(
                run=run,
                candidates=candidates,
                audit_top_n=top_n,
            )
            last_content = content
            if len(content.encode("utf-8")) <= max_bytes:
                if top_n is not None:
                    logger.info(
                        "screening notification push content降级 audit_top_n=%s "
                        "以适配单条飞书消息上限 (%d bytes)",
                        top_n,
                        max_bytes,
                    )
                return content
        # 全部级别都超限（极端情况：候选数巨多且摘要也很长）；返回最后一次（最紧凑）的结果。
        logger.warning(
            "screening notification push content even at audit_top_n=0 still exceeds %d bytes; "
            "Feishu sender may fall back to chunked send",
            max_bytes,
        )
        return last_content

    # ------------------------------------------------------------------
    # Status persistence helpers
    # ------------------------------------------------------------------

    def _mark_notification_sent(self, run_id: str) -> None:
        self.db.update_notification_status(run_id=run_id, notification_status="sent")

    def _mark_notification_failed(self, run_id: str, error: str) -> None:
        self.db.update_notification_status(
            run_id=run_id,
            notification_status="failed",
            notification_error=error,
        )

    # ------------------------------------------------------------------
    # Audit-content helpers (new)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_score_breakdown(item: Dict[str, Any]) -> Dict[str, Any]:
        """Build a structured score breakdown for a single candidate.

        Returns:
            {
                "rule_score": float,
                "ai_bonus": float,
                "news_bonus": int,
                "final_score": float,
                "rule_breakdown": [{"name": str, "score": float, "reason": str}, ...]
            }
        The breakdown faithfully mirrors the formulas in ScreenerService._score()
        and DatabaseManager._enrich_screening_candidates(). No scoring logic is changed.
        """
        rule_hits = _get_rule_hits(item)
        factor = _get_factor_snapshot(item)

        rule_score = _safe_float(item.get("rule_score"), 0.0)
        advice = item.get("ai_operation_advice") or ""
        ai_bonus = _AI_BONUS_MAP.get(advice, 0.0)
        news_count = _safe_int(item.get("news_count"), 0)
        news_bonus = min(news_count, 3)
        final_score = round(rule_score + ai_bonus + news_bonus, 2)

        # Extract raw factor values used in score formula
        close = _safe_float(factor.get("close"), 0.0)
        ma5 = _safe_float(factor.get("ma5"), 0.0)
        ma10 = _safe_float(factor.get("ma10"), 0.0)
        ma20 = _safe_float(factor.get("ma20"), 0.0)
        volume_ratio = _safe_float(factor.get("volume_ratio"), 0.0)
        breakout_ratio = _safe_float(factor.get("breakout_ratio"), 0.0)

        rule_breakdown: List[Dict[str, Any]] = []

        # --- discrete rule hits ---
        if "trend_aligned" in rule_hits:
            rule_breakdown.append({
                "name": "趋势对齐",
                "score": 40.0,
                "reason": (
                    f"close({close:.2f}) >= MA20({ma20:.2f}) "
                    f"且 MA5({ma5:.2f}) >= MA10({ma10:.2f}) >= MA20({ma20:.2f})"
                ),
            })
        if "volume_expanding" in rule_hits:
            rule_breakdown.append({
                "name": "放量条件",
                "score": 30.0,
                "reason": f"volume_ratio={volume_ratio:.2f}",
            })
        if "near_breakout" in rule_hits:
            rule_breakdown.append({
                "name": "临近突破",
                "score": 20.0,
                "reason": f"breakout_ratio={breakout_ratio:.4f} >= 0.995",
            })
        if "liquidity_ok" in rule_hits:
            rule_breakdown.append({
                "name": "流动性合格",
                "score": 10.0,
                "reason": "avg_amount 达到阈值",
            })

        # --- continuous weighted components (mirrors ScreenerService._score) ---
        breakout_premium = max(breakout_ratio - 1.0, 0.0) * 1000
        if breakout_premium > 0:
            rule_breakdown.append({
                "name": "突破溢价加分",
                "score": round(breakout_premium, 1),
                "reason": f"max({breakout_ratio:.4f} - 1, 0) × 1000",
            })

        volume_supplement = min(volume_ratio, 3.0)
        if volume_supplement > 0:
            rule_breakdown.append({
                "name": "量比补充分",
                "score": round(volume_supplement, 2),
                "reason": f"min({volume_ratio:.2f}, 3.0)",
            })

        return {
            "rule_score": rule_score,
            "ai_bonus": ai_bonus,
            "news_bonus": news_bonus,
            "final_score": final_score,
            "rule_breakdown": rule_breakdown,
        }

    @staticmethod
    def _build_factor_snapshot_summary(item: Dict[str, Any]) -> Dict[str, Any]:
        """Extract raw factor values from a candidate's factor_snapshot.

        Returns a dict with close, ma5/ma10/ma20, volume_ratio, breakout_ratio,
        avg_amount (raw + human-readable), days_since_listed, is_st.
        """
        factor = _get_factor_snapshot(item)

        avg_amount_raw = factor.get("avg_amount")
        return {
            "close": factor.get("close"),
            "ma5": factor.get("ma5"),
            "ma10": factor.get("ma10"),
            "ma20": factor.get("ma20"),
            "volume_ratio": factor.get("volume_ratio"),
            "breakout_ratio": factor.get("breakout_ratio"),
            "avg_amount": avg_amount_raw,
            "avg_amount_readable": _fmt_amount(avg_amount_raw),
            "days_since_listed": factor.get("days_since_listed"),
            "is_st": factor.get("is_st"),
        }

    @staticmethod
    def _format_bullet_list(title: str, values: List[Any]) -> List[str]:
        if not values:
            return []
        return [f"- {title}: " + "；".join(_format_value(v) for v in values)]

    def _format_frontend_like_detail_sections(
        self,
        item: Dict[str, Any],
        factor: Dict[str, Any],
    ) -> List[str]:
        """Render candidate details using the same field groups as the Web drawer."""
        lines: List[str] = []

        market_regime = item.get("market_regime")
        risk_level = item.get("risk_level")
        market_message = _first_present(
            item.get("market_message"),
            factor.get("market_message"),
        )
        if market_regime or risk_level or market_message:
            lines.append("**[L1 大盘环境]**")
            lines.append(
                "- 市场状态: "
                f"{_REGIME_LABELS.get(market_regime or '', market_regime or 'N/A')} | "
                f"风险: {_format_value(risk_level)}"
            )
            if market_message:
                lines.append(f"- 环境说明: {_format_value(market_message)}")
            lines.append("")

        primary_theme = _first_present(
            item.get("theme_tag"),
            factor.get("primary_theme"),
            factor.get("theme"),
        )
        sector = _first_present(factor.get("sector"), factor.get("board_name"))
        industry = _first_present(factor.get("industry"), factor.get("sw_industry"))
        theme_position = item.get("theme_position")
        l2_fields_present = any(
            value not in (None, "", [], {})
            for value in (
                primary_theme,
                sector,
                industry,
                item.get("theme_score"),
                item.get("theme_duration"),
                factor.get("theme_heat_score"),
                factor.get("leader_score"),
                factor.get("extreme_strength_score"),
                factor.get("theme_pool_score"),
                factor.get("leadership_score"),
                factor.get("entry_signal_score"),
                factor.get("timing_penalty"),
                theme_position,
            )
        )
        if l2_fields_present:
            lines.append("**[L2 题材/板块]**")
            lines.append(
                "- 题材/板块: "
                f"{_format_value(primary_theme)} | "
                f"板块: {_format_value(sector)} | "
                f"行业: {_format_value(industry)}"
            )
            lines.append(
                "- 题材地位: "
                f"{_THEME_POSITION_LABELS.get(theme_position or '', theme_position or 'N/A')}"
                f"{f' ({theme_position})' if theme_position else ''} | "
                f"热度: {_format_value(_first_present(item.get('theme_score'), factor.get('theme_heat_score')))} | "
                f"持续: {_format_value(item.get('theme_duration'))}"
            )
            leader_stocks = _ensure_list(item.get("leader_stocks") or factor.get("leader_stocks"))
            if leader_stocks:
                lines.append(f"- 龙头/前排: {_format_value(leader_stocks)}")
            layered = {
                "主题池": factor.get("theme_pool_score"),
                "龙头": factor.get("leadership_score"),
                "入场信号": factor.get("entry_signal_score"),
                "时机惩罚": factor.get("timing_penalty"),
                "总分": factor.get("extreme_strength_score"),
                "去重净分": factor.get("extreme_strength_score_deduplicated"),
                "重复加权": factor.get("leader_double_count"),
            }
            layered_text = " | ".join(
                f"{label} {_format_value(value)}"
                for label, value in layered.items()
                if value not in (None, "")
            )
            if layered_text:
                lines.append(f"- 分层评分: {layered_text}")
            catalyst = factor.get("theme_catalyst_summary")
            if catalyst:
                lines.append(f"- 催化摘要: {_format_value(catalyst)}")
            lines.append("")

        candidate_pool = item.get("candidate_pool_level")
        if candidate_pool:
            lines.append("**[L3 候选池]**")
            lines.append(
                f"- 候选池级别: {_POOL_LABELS.get(candidate_pool, candidate_pool)}"
            )
            lines.append("")

        setup = item.get("setup_type")
        setup_reasons = _ensure_list(item.get("setup_hit_reasons"))
        timing_reasons = _ensure_list(factor.get("timing_reasons"))
        phase_explanations = _ensure_list(factor.get("phase_explanations"))
        stage_label = factor.get("stage_label")
        primary_signal = factor.get("primary_signal")
        signal_kind = factor.get("signal_kind")
        if setup or setup_reasons or timing_reasons or phase_explanations or primary_signal or stage_label:
            lines.append("**[L4 入场信号]**")
            lines.append(
                "- 买点: "
                f"{_SETUP_LABELS.get(setup or '', setup or 'N/A')} | "
                f"阶段: {_format_value(stage_label)} | "
                f"主信号: {_SIGNAL_LABELS.get(primary_signal or '', primary_signal or 'N/A')} | "
                f"信号类型: {_SIGNAL_LABELS.get(signal_kind or '', signal_kind or 'N/A')}"
            )
            lines.extend(self._format_bullet_list("命中原因", setup_reasons))
            lines.extend(self._format_bullet_list("时机说明", timing_reasons))
            if phase_explanations:
                lines.append("- 阶段命中明细:")
                for phase in phase_explanations:
                    if isinstance(phase, dict):
                        marker = "命中" if phase.get("hit") else "未命中"
                        label = phase.get("label") or phase.get("phase_key") or "阶段"
                        summary = phase.get("summary") or ""
                        lines.append(f"  - {label}: {marker}，{summary}")
                    else:
                        lines.append(f"  - {_format_value(phase)}")
            lines.append("")

        trade_stage = item.get("trade_stage")
        trade_plan = item.get("trade_plan") if isinstance(item.get("trade_plan"), dict) else {}
        ai_review = item.get("ai_review") if isinstance(item.get("ai_review"), dict) else {}
        execution_plan = {
            "initial_position": _first_present(
                trade_plan.get("initial_position"),
                item.get("initial_position"),
                ai_review.get("initial_position"),
            ),
            "stop_loss_rule": _first_present(
                trade_plan.get("stop_loss_rule"),
                item.get("stop_loss_rule"),
                ai_review.get("stop_loss_rule"),
            ),
            "take_profit_plan": _first_present(
                trade_plan.get("take_profit_plan"),
                item.get("take_profit_plan"),
                ai_review.get("take_profit_plan"),
            ),
            "invalidation_rule": _first_present(
                trade_plan.get("invalidation_rule"),
                item.get("invalidation_rule"),
                ai_review.get("invalidation_rule"),
            ),
            "holding_expectation": trade_plan.get("holding_expectation"),
            "execution_note": trade_plan.get("execution_note"),
            "add_rule": trade_plan.get("add_rule"),
        }
        risk_params = factor.get("risk_params") if isinstance(factor.get("risk_params"), dict) else {}
        if trade_stage or any(execution_plan.values()) or risk_params:
            lines.append("**[L5 交易计划]**")
            lines.append(
                f"- 交易阶段: {_STAGE_LABELS.get(trade_stage or '', trade_stage or 'N/A')}"
            )
            if any(execution_plan.get(k) for k in ("initial_position", "stop_loss_rule", "take_profit_plan")):
                lines.append(
                    "- 买卖点: "
                    f"仓位 {_format_value(execution_plan.get('initial_position'))} | "
                    f"止损 {_format_value(execution_plan.get('stop_loss_rule'))} | "
                    f"止盈 {_format_value(execution_plan.get('take_profit_plan'))}"
                )
                if execution_plan.get("add_rule"):
                    lines.append(f"- 加仓规则: {_format_value(execution_plan.get('add_rule'))}")
                if execution_plan.get("invalidation_rule"):
                    lines.append(f"- 失效规则: {_format_value(execution_plan.get('invalidation_rule'))}")
                if execution_plan.get("holding_expectation") or execution_plan.get("execution_note"):
                    lines.append(
                        "- 执行备注: "
                        f"持仓 {_format_value(execution_plan.get('holding_expectation'))} | "
                        f"{_format_value(execution_plan.get('execution_note'))}"
                    )
            if risk_params:
                lines.append(
                    "- 风险参数: "
                    f"止损价 {_format_value(risk_params.get('stop_loss'))} | "
                    f"依据 {_format_value(risk_params.get('stop_loss_basis'))} | "
                    f"仓位 {_format_value(risk_params.get('position_size'))} | "
                    f"止盈比例 {_format_value(risk_params.get('take_profit_ratio'))}"
                )
            lines.append("")

        ai_reasoning = _first_present(item.get("ai_reasoning"), ai_review.get("ai_reasoning"))
        ai_confidence = _first_present(item.get("ai_confidence"), ai_review.get("ai_confidence"))
        ai_trade_stage = _first_present(item.get("ai_trade_stage"), ai_review.get("ai_trade_stage"))
        if ai_reasoning or ai_confidence is not None or ai_trade_stage:
            lines.append("**[AI 复核]**")
            lines.append(
                "- AI阶段/信心: "
                f"{_STAGE_LABELS.get(ai_trade_stage or '', ai_trade_stage or 'N/A')} | "
                f"{_format_value(ai_confidence)}"
            )
            if ai_reasoning:
                lines.append(f"- AI理由: {_format_value(ai_reasoning)}")
            if item.get("ai_operation_advice"):
                lines.append(f"- 操作建议: {_format_value(item.get('ai_operation_advice'))}")
            lines.append("")

        matched = _ensure_list(item.get("matched_strategies"))
        if matched:
            lines.append("**[匹配策略]**")
            lines.append(f"- {_format_value(matched)}")
            lines.append("")

        return lines

    def _format_candidate_audit_block(self, item: Dict[str, Any]) -> List[str]:
        """Format a single candidate as a full audit block (used for Top N).

        Sections: [总览] [五层决策] [评分汇总] [审计证据] [原始指标]
                  [AI增强] (if available)  [新闻增强] (if available)
        """
        code = item.get("code", "-")
        name = item.get("name") or code
        final_rank = item.get("final_rank", "-")
        source = item.get("recommendation_source") or "rules_only"
        source_text = "AI 增强" if source == "rules_plus_ai" else "规则输出"

        breakdown = self._build_score_breakdown(item)
        snapshot = self._build_factor_snapshot_summary(item)
        rule_hits = _get_rule_hits(item)

        # Use item's authoritative final_score when available; fall back to computed value.
        final_score_display = (
            _safe_optional_float(item.get("final_score"))
            if item.get("final_score") is not None
            else breakdown["final_score"]
        )
        if final_score_display is None:
            final_score_display = breakdown["final_score"]
        rule_score_display = _safe_float(item.get("rule_score"), breakdown["rule_score"])

        lines: List[str] = [f"### {final_rank}. {name} ({code})", ""]

        # [总览]
        lines.append("**[总览]**")
        lines.append(f"- 来源: `{source_text}`")
        lines.append(
            f"- 最终评分: **{final_score_display:.1f}** | 规则分: **{rule_score_display:.1f}**"
        )
        lines.append("")

        has_five_layer = item.get("trade_stage") is not None or item.get("setup_type") is not None
        if has_five_layer:
            lines.append("**[五层决策]**")
            lines.append("")
        lines.extend(self._format_frontend_like_detail_sections(item, _get_factor_snapshot(item)))

        # [评分汇总]
        lines.append("**[评分汇总]**")
        lines.append(f"- 规则分：{rule_score_display:.1f}")
        ai_sign = "+" if breakdown["ai_bonus"] >= 0 else ""
        lines.append(f"- AI加分：{ai_sign}{breakdown['ai_bonus']:.1f}")
        lines.append(f"- 新闻加分：+{breakdown['news_bonus']}")
        lines.append(f"- 最终总分：{final_score_display:.1f}")
        lines.append("")

        # [规则分拆解]
        lines.append("**[规则分拆解]**")
        for rb in breakdown["rule_breakdown"]:
            lines.append(f"- {rb['name']}：+{rb['score']} → {rb['reason']}")
        if not breakdown["rule_breakdown"]:
            lines.append("- （无规则命中）")
        lines.append("")

        # [审计证据]
        hit_texts = [_RULE_HIT_ZH.get(h, h) for h in rule_hits]
        lines.append(f"**[审计证据]** {' | '.join(hit_texts) if hit_texts else '无'}")
        lines.append("")

        # [原始指标]
        lines.append("**[原始指标]**")
        close = snapshot.get("close")
        ma5 = snapshot.get("ma5")
        ma10 = snapshot.get("ma10")
        ma20 = snapshot.get("ma20")
        close_val = _safe_optional_float(close)
        ma5_val = _safe_optional_float(ma5)
        ma10_val = _safe_optional_float(ma10)
        ma20_val = _safe_optional_float(ma20)
        close_str = f"{close_val:.2f}" if close_val is not None else "N/A"
        ma5_str = f"{ma5_val:.2f}" if ma5_val is not None else "N/A"
        ma10_str = f"{ma10_val:.2f}" if ma10_val is not None else "N/A"
        ma20_str = f"{ma20_val:.2f}" if ma20_val is not None else "N/A"
        lines.append(
            f"- close: {close_str} | ma5/ma10/ma20: {ma5_str} / {ma10_str} / {ma20_str}"
        )
        vr = snapshot.get("volume_ratio")
        br = snapshot.get("breakout_ratio")
        vr_val = _safe_optional_float(vr)
        br_val = _safe_optional_float(br)
        vr_str = f"{vr_val:.2f}" if vr_val is not None else "N/A"
        br_str = f"{br_val:.4f}" if br_val is not None else "N/A"
        lines.append(f"- volume_ratio: {vr_str} | breakout_ratio: {br_str}")
        lines.append(
            f"- avg_amount: {snapshot['avg_amount_readable']} | "
            f"days_since_listed: {snapshot.get('days_since_listed', 'N/A')} | "
            f"is_st: {snapshot.get('is_st', 'N/A')}"
        )
        lines.append("")

        # [AI增强] — shown when any AI-related field is present
        has_ai = bool(
            item.get("has_ai_analysis")
            or item.get("ai_operation_advice")
            or item.get("ai_summary")
        )
        if has_ai:
            lines.append("**[AI增强]**")
            advice = item.get("ai_operation_advice") or ""
            ai_sign = "+" if breakdown["ai_bonus"] >= 0 else ""
            lines.append(
                f"- 操作建议: {advice} | AI加分: {ai_sign}{breakdown['ai_bonus']:.1f}"
            )
            if item.get("ai_summary"):
                lines.append(f"- AI摘要: {item['ai_summary']}")
            lines.append("")

        # [新闻增强] — shown when news_count > 0 or news_summary is present
        news_count = _safe_int(item.get("news_count"), 0)
        has_news = news_count > 0 or bool(item.get("news_summary"))
        if has_news:
            lines.append("**[新闻增强]**")
            lines.append(f"- 新闻条数: {news_count} | 新闻加分: +{breakdown['news_bonus']}")
            if item.get("news_summary"):
                lines.append(f"- 新闻摘要: {item['news_summary']}")
            lines.append("")

        return lines

    def _format_candidate_summary_block(self, item: Dict[str, Any]) -> List[str]:
        """Format a single candidate as a compact summary line (used for rank > audit_top_n)."""
        code = item.get("code", "-")
        name = item.get("name") or code
        final_rank = item.get("final_rank", "-")
        final_score = item.get("final_score")
        score_text = f"{float(final_score):.1f}" if final_score is not None else "N/A"
        source = item.get("recommendation_source") or "rules_only"
        source_text = "AI 增强" if source == "rules_plus_ai" else "规则输出"
        stage = item.get("trade_stage")
        stage_text = f" | {_STAGE_LABELS.get(stage, stage)}" if stage else ""
        return [
            f"### {final_rank}. {name} ({code}) — {score_text}分{stage_text} | 来源: {source_text}",
            "",
        ]

    # ------------------------------------------------------------------
    # Main notification builder (extended)
    # ------------------------------------------------------------------

    def build_run_notification(
        self,
        run: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        audit_top_n: Optional[int] = None,
    ) -> str:
        """Build the full Markdown notification content for a screening run.

        By default every candidate passed in is rendered as a full detail block so
        notifications stay close to the Web candidate drawer. Set ``audit_top_n``
        to keep the older compact-summary behavior for candidates after N.
        """
        candidates = list(candidates or [])
        trade_date = run.get("trade_date") or datetime.now().strftime("%Y-%m-%d")
        mode = run.get("mode") or "balanced"
        status = run.get("status") or "completed"
        universe_size = int(run.get("universe_size") or 0)
        candidate_count = int(run.get("candidate_count") or len(candidates))

        # Extract market_regime from candidates (all share the same regime)
        regime = self._extract_regime(candidates)
        regime_label = _REGIME_LABELS.get(regime, "") if regime else ""
        title_suffix = f" | {regime_label}" if regime_label else ""

        lines = [
            f"# 📣 {trade_date} 全市场筛选推荐名单{title_suffix}",
            "",
            (
                f"> run_id: `{run.get('run_id', '-')}` | "
                f"模式: `{mode}` | "
                f"候选数: **{candidate_count}** | "
                f"股票池规模: **{universe_size}**"
            ),
            "",
        ]

        if status == "completed_with_ai_degraded":
            lines.extend(
                [
                    "> ⚠️ AI 二筛已降级，本次结果以规则输出为主，候选中仅保留已成功回链的 AI/新闻增强信息。",
                    "",
                ]
            )

        if regime == "stand_aside":
            lines.extend(
                [
                    "> 当前市场处于观望期，以下为观察列表，不含交易计划。",
                    "",
                ]
            )

        if not candidates:
            lines.extend(
                [
                    "## 今日结果",
                    "",
                    "本次筛选未产生可推送候选。",
                    "",
                ]
            )
            return "\n".join(lines)

        lines.extend(["## Top 推荐", ""])

        for idx, item in enumerate(candidates, 1):
            if audit_top_n is None or idx <= audit_top_n:
                lines.extend(self._format_candidate_audit_block(item))
            else:
                lines.extend(self._format_candidate_summary_block(item))

        lines.extend(
            [
                "---",
                "",
                f"*通知生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _extract_regime(candidates: List[Dict[str, Any]]) -> Optional[str]:
        """Extract market_regime from candidates (all share the same regime)."""
        for c in candidates:
            regime = c.get("market_regime")
            if regime:
                return regime
        return None

    # ------------------------------------------------------------------
    # Legacy entry point (preserved for backward compatibility)
    # ------------------------------------------------------------------

    def send_run_notification(
        self, run_id: str, limit: int = 10, with_ai_only: bool = False
    ) -> Dict[str, Any]:
        run = self.screening_task_service.get_run(run_id)
        if run is None:
            raise ScreeningRunNotFoundError("筛选任务不存在")
        if run.get("status") not in _COMPLETED_STATUSES:
            raise ScreeningRunNotReadyError("筛选任务尚未完成，暂不可推送")

        candidates = self.screening_task_service.list_candidates(
            run_id=run_id,
            limit=limit,
            with_ai_only=with_ai_only,
        )
        content = self.build_run_notification(run=run, candidates=candidates)
        stock_codes = [str(item.get("code")) for item in candidates if item.get("code")]
        report_path = self.notifier.save_report_to_file(content, filename=f"screening_{run_id}.md")
        success = self.notifier.send(content, email_stock_codes=stock_codes or None)
        if not success:
            raise ScreeningNotificationDeliveryError("筛选推荐通知发送失败")
        return {
            "success": True,
            "message": "筛选推荐通知发送成功",
            "run_id": run_id,
            "candidate_count": len(candidates),
            "report_path": report_path,
        }
