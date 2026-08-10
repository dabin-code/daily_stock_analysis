from __future__ import annotations

import math

from src.schemas.trading_types import SetupType


_V2_STAGE_DEFAULTS = {
    "early": 0.8,
    "near_cleared": 0.9,
    "major_actionable": 1.0,
    "forming": 0.5,
    "stale": 0.3,
    "extended": 0.3,
    "invalidated": 0.3,
    "major_unverified": 0.3,
    "breakout_failed": 0.3,
    "rejected": 0.3,
}


def _non_negative_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


class SetupFreshnessAssessor:
    """统一评估 setup_freshness，输出 0.0~1.0。"""

    _BREAKOUT_DAY_KEYS = (
        "setup_freshness",
        "ma100_60min_freshness_score",
        "breakout_freshness_score",
        "entry_freshness_score",
        # New real-crossing field — interpreted as "bars since breakout",
        # placed before the legacy consecutive-days counter so fresh real
        # crossings take precedence when both fields are present.
        "ma100_bars_since_breakout",
        "ma100_breakout_days",
        "breakout_days",
        "days_since_breakout",
        "gap_breakout_days",
    )

    # Setup types that should consult the new gap/limit-up
    # ``bars_since_event`` signal before falling back to legacy keys.
    _GAP_LIMITUP_TYPES = (SetupType.GAP_BREAKOUT, SetupType.LIMITUP_STRUCTURE)

    def assess(self, setup_type: SetupType, factor_snapshot: dict) -> float:
        if setup_type == SetupType.NONE:
            return 0.0

        if setup_type == SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY:
            stage = str(
                factor_snapshot.get("bottom_divergence_v2_stage") or "rejected"
            )
            if stage not in _V2_STAGE_DEFAULTS:
                return 0.3
            event_days = _non_negative_number(
                factor_snapshot.get("bottom_divergence_v2_event_days")
            )
            if event_days is not None:
                return self._score_from_breakout_days(event_days + 1)
            return _V2_STAGE_DEFAULTS.get(stage, 0.3)

        # ── Priority override for bottom-divergence double-breakout ──
        # BottomDivergenceBreakoutDetector 会写入 ``bottom_divergence_
        # confirmation_days``（0 = 最新一根 K 线刚 confirmed）。使用
        # 该字段作为新鲜度来源，比旧的 ``ma100_breakout_days`` 更贴合
        # 底背离事件本身；当字段缺失但 ``bottom_divergence_double_
        # breakout`` 为 True 时给一个偏高的兜底分数，避免历史上的
        # ``bottom_divergence_signal`` 死键导致永远降到 0.5。
        if setup_type == SetupType.BOTTOM_DIVERGENCE_BREAKOUT:
            days = factor_snapshot.get("bottom_divergence_confirmation_days")
            if isinstance(days, (int, float)) and days >= 0:
                return self._score_from_breakout_days(float(days) + 1)
            if factor_snapshot.get("bottom_divergence_double_breakout"):
                return 0.8
            if factor_snapshot.get("bottom_divergence_state") == "confirmed":
                return 0.8

        # ── Priority override for gap/limit-up setups ────────────────
        # When the new structured detectors emit ``bars_since_event``,
        # prefer that signal over the legacy day-counter fields so the
        # freshness scale reflects the actual breakaway/limit-up event
        # rather than an unrelated MA100 crossing.
        if setup_type in self._GAP_LIMITUP_TYPES:
            # Retest (second-entry) buy points are structurally fresher
            # than the original breakout because the market has already
            # validated the support level.  Score them near-maximum.
            if factor_snapshot.get("retest_hold"):
                return 0.95

            bars_since = factor_snapshot.get("bars_since_event")
            if isinstance(bars_since, (int, float)) and bars_since >= 0:
                # bars_since_event=0 means the event fired on the latest
                # bar (freshest).  Align with ``_score_from_breakout_days``
                # which expects day=1 as the freshest.
                return self._score_from_breakout_days(bars_since + 1)

        for key in self._BREAKOUT_DAY_KEYS:
            raw = factor_snapshot.get(key)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if "freshness" in key:
                return max(0.0, min(value, 1.0))
            # ma100_bars_since_breakout uses -1 sentinel for "no real
            # crossing detected" — skip and fall through to other keys
            # instead of mis-scoring.
            if key == "ma100_bars_since_breakout":
                if value < 0:
                    continue
                # bars_since_breakout=0 means crossed on the latest bar
                # (freshest).  _score_from_breakout_days expects "days"
                # where 1 is the freshest → shift by +1 for alignment.
                return self._score_from_breakout_days(value + 1)
            return self._score_from_breakout_days(value)

        if setup_type == SetupType.GAP_BREAKOUT and factor_snapshot.get("gap_breakaway"):
            return 0.9
        if setup_type == SetupType.LIMITUP_STRUCTURE and factor_snapshot.get("is_limit_up"):
            return 0.95
        if factor_snapshot.get("pattern_123_signal"):
            return 0.8
        # Back-compat：历史上用过 ``bottom_divergence_signal`` 这个从未被
        # 真正写入的键，这里保留同值兜底，同时识别当前真实使用的
        # ``bottom_divergence_double_breakout`` 布尔。主流路径已由顶部
        # 的 BOTTOM_DIVERGENCE_BREAKOUT 分支优先处理。
        if (
            factor_snapshot.get("bottom_divergence_signal")
            or factor_snapshot.get("bottom_divergence_double_breakout")
        ):
            return 0.75
        return 0.5

    @staticmethod
    def _score_from_breakout_days(days: float) -> float:
        if days <= 0:
            return 0.0
        if days <= 1:
            return 1.0
        if days <= 2:
            return 0.9
        if days <= 3:
            return 0.8
        if days <= 5:
            return 0.7
        if days <= 8:
            return 0.5
        return 0.3
