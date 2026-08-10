"""五层交易系统的核心枚举与数据结构。

Layer architecture:
  L1 Market Environment → L2 Theme/Sector → L3 Strong Stock Pool
  → L4 Setup Identification → L5 Trade Management

Each layer's output constrains downstream layers via hard rules
(e.g., stand_aside caps trade_stage at watch).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
from typing import Any, ClassVar, Dict, List, Optional


# ── Enums ─────────────────────────────────────────────────────────────────────

class MarketRegime(str, Enum):
    """L1 市场环境状态"""
    AGGRESSIVE = "aggressive"
    BALANCED = "balanced"
    DEFENSIVE = "defensive"
    STAND_ASIDE = "stand_aside"


class ThemePosition(str, Enum):
    """L2 题材地位"""
    MAIN_THEME = "main_theme"
    SECONDARY_THEME = "secondary_theme"
    FOLLOWER_THEME = "follower_theme"
    FADING_THEME = "fading_theme"
    NON_THEME = "non_theme"


class CandidatePoolLevel(str, Enum):
    """L3 候选池分级"""
    LEADER_POOL = "leader_pool"
    FOCUS_LIST = "focus_list"
    WATCHLIST = "watchlist"


class TradeStage(str, Enum):
    """L5 交易阶段——贯穿全系统的核心输出"""
    STAND_ASIDE = "stand_aside"
    WATCH = "watch"
    FOCUS = "focus"
    PROBE_ENTRY = "probe_entry"
    ADD_ON_STRENGTH = "add_on_strength"
    REJECT = "reject"


class SetupType(str, Enum):
    """L4 买点类型"""
    BOTTOM_DIVERGENCE_BREAKOUT = "bottom_divergence_breakout"
    BOTTOM_DIVERGENCE_LAYERED_ENTRY = "bottom_divergence_layered_entry"
    LOW123_BREAKOUT = "low123_breakout"
    TREND_BREAKOUT = "trend_breakout"
    TREND_PULLBACK = "trend_pullback"
    GAP_BREAKOUT = "gap_breakout"
    LIMITUP_STRUCTURE = "limitup_structure"
    NONE = "none"


class EntryMaturity(str, Enum):
    """买点成熟度"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class StrategyFamily(str, Enum):
    """三大买点模板家族"""
    REVERSAL = "reversal"
    TREND = "trend"
    MOMENTUM = "momentum"
    AUXILIARY = "auxiliary"


class StrategyRole(str, Enum):
    """策略在系统中的角色"""
    ENTRY_CORE = "entry_core"
    STOCK_POOL = "stock_pool"
    THEME_SCORE = "theme_score"
    CONFIRM = "confirm"
    BONUS_SIGNAL = "bonus_signal"
    OBSERVATION = "observation"


_ENUM_ALIASES: Dict[type[Enum], Dict[str, str]] = {
    SetupType: {
        "limitup_structure_breakout": SetupType.LIMITUP_STRUCTURE.value,
    },
}


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class MarketEnvironment:
    """L1 市场环境层输出"""
    regime: MarketRegime
    risk_level: RiskLevel
    index_price: float = 0.0
    index_ma100: float = 0.0
    is_safe: bool = True
    message: str = ""


@dataclass
class ThemeDecision:
    """L2 题材层输出（单个题材）"""
    theme_tag: str
    theme_score: float = 0.0
    theme_position: ThemePosition = ThemePosition.NON_THEME
    leader_score: float = 0.0
    sector_strength: float = 0.0
    theme_duration: str = "unknown"
    trade_theme_stage: str = "unknown"
    leader_stocks: List[str] = field(default_factory=list)
    front_stocks: List[str] = field(default_factory=list)


@dataclass
class SetupDecision:
    """L4 买点识别层输出"""
    setup_type: SetupType = SetupType.NONE
    entry_maturity: EntryMaturity = EntryMaturity.LOW
    setup_freshness: float = 0.0
    hit_reasons: List[str] = field(default_factory=list)


@dataclass
class TradePlan:
    """L5 交易管理层输出"""
    initial_position: Optional[str] = None
    add_rule: Optional[str] = None
    stop_loss_rule: Optional[str] = None
    take_profit_plan: Optional[str] = None
    invalidation_rule: Optional[str] = None
    risk_level: RiskLevel = RiskLevel.MEDIUM
    holding_expectation: Optional[str] = None
    execution_note: Optional[str] = None
    # 结构化交易回放字段：选股时刻冻结的可执行买卖点。
    entry_price: Optional[float] = None
    entry_rule: Optional[str] = None
    entry_valid_days: Optional[int] = None
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    time_stop_days: Optional[int] = None
    exit_rules: Optional[List[str]] = None

    def to_payload(self) -> Dict[str, Any]:
        """Serialize every plan field, including explicit null values."""
        return _serialize_value(asdict(self))


@dataclass
class AiReviewDecision:
    """AI 二筛统一输出。"""
    ai_query_id: Optional[str] = None
    ai_summary: Optional[str] = None
    ai_operation_advice: Optional[str] = None
    ai_trade_stage: Optional[TradeStage] = None
    ai_reasoning: str = ""
    ai_confidence: float = 0.0
    ai_environment_ok: Optional[bool] = None
    ai_theme_alignment: Optional[bool] = None
    ai_entry_quality: Optional[str] = None
    stage_conflict: bool = False
    result_source: Optional[str] = None
    is_fallback: bool = False
    fallback_reason: Optional[str] = None
    downgrade_reasons: List[str] = field(default_factory=list)
    initial_position: Optional[str] = None
    stop_loss_rule: Optional[str] = None
    take_profit_plan: Optional[str] = None
    invalidation_rule: Optional[str] = None
    prompt_version: Optional[str] = None
    model_name: Optional[str] = None
    parse_status: Optional[str] = None
    retry_count: int = 0


@dataclass
class CandidateDecision:
    """统一候选股决策结果——贯穿 5 层的最终输出"""
    code: str
    name: str = ""
    rank: int = 0
    selected_for_ai: bool = False
    rule_hits: List[str] = field(default_factory=list)
    factor_snapshot: Dict[str, Any] = field(default_factory=dict)
    strategy_scores: Dict[str, float] = field(default_factory=dict)
    # L1
    market_regime: MarketRegime = MarketRegime.BALANCED
    risk_level: RiskLevel = RiskLevel.MEDIUM
    environment_ok: bool = True
    index_price: float = 0.0
    index_ma100: float = 0.0
    market_message: str = ""
    # L2
    theme_tag: str = ""
    theme_score: float = 0.0
    theme_position: ThemePosition = ThemePosition.NON_THEME
    leader_score: float = 0.0
    sector_strength: float = 0.0
    theme_duration: str = "unknown"
    trade_theme_stage: str = "unknown"
    leader_stocks: List[str] = field(default_factory=list)
    front_stocks: List[str] = field(default_factory=list)
    # L3
    candidate_pool_level: CandidatePoolLevel = CandidatePoolLevel.WATCHLIST
    relative_strength_market: float = 0.0
    relative_strength_sector: float = 0.0
    # L4
    setup_type: SetupType = SetupType.NONE
    entry_maturity: EntryMaturity = EntryMaturity.LOW
    setup_freshness: float = 0.0
    strategy_family: Optional[StrategyFamily] = None
    primary_strategy: Optional[str] = None
    contributing_strategies: List[str] = field(default_factory=list)
    setup_hit_reasons: List[str] = field(default_factory=list)
    matched_strategies: List[str] = field(default_factory=list)
    # L5
    trade_stage: TradeStage = TradeStage.WATCH
    trade_plan: Optional[TradePlan] = None
    # AI 二筛
    ai_review: Optional[AiReviewDecision] = None
    # 展示/推荐
    has_ai_analysis: bool = False
    news_count: int = 0
    news_summary: Optional[str] = None
    recommendation_source: Optional[str] = None
    recommendation_reason: Optional[str] = None
    # 评分
    rule_score: float = 0.0
    final_score: float = 0.0
    final_rank: int = 0

    @classmethod
    def from_record(cls, record: Any) -> "CandidateDecision":
        """从初筛候选记录构造统一决策对象。"""
        return cls(
            code=getattr(record, "code"),
            name=getattr(record, "name", ""),
            rank=int(getattr(record, "rank", 0) or 0),
            rule_score=float(getattr(record, "rule_score", 0.0) or 0.0),
            rule_hits=list(getattr(record, "rule_hits", []) or []),
            factor_snapshot=dict(getattr(record, "factor_snapshot", {}) or {}),
            matched_strategies=list(getattr(record, "matched_strategies", []) or []),
            strategy_scores=dict(getattr(record, "strategy_scores", {}) or {}),
            primary_strategy=getattr(record, "primary_strategy", None),
            contributing_strategies=list(getattr(record, "contributing_strategies", []) or []),
            setup_type=cls._coerce_enum(
                SetupType,
                getattr(record, "setup_type", None),
                SetupType.NONE,
            ),
            entry_maturity=cls._coerce_enum(
                EntryMaturity,
                getattr(record, "entry_maturity", None),
                EntryMaturity.LOW,
            ),
            trade_stage=cls._coerce_enum(
                TradeStage,
                getattr(record, "trade_stage", None),
                TradeStage.WATCH,
            ),
            market_regime=cls._coerce_enum(
                MarketRegime,
                getattr(record, "market_regime", None),
                MarketRegime.BALANCED,
            ),
            risk_level=cls._coerce_enum(
                RiskLevel,
                getattr(record, "risk_level", None),
                RiskLevel.MEDIUM,
            ),
            theme_position=cls._coerce_enum(
                ThemePosition,
                getattr(record, "theme_position", None),
                ThemePosition.NON_THEME,
            ),
            theme_tag=str(getattr(record, "theme_tag", "") or ""),
            theme_score=float(getattr(record, "theme_score", 0.0) or 0.0),
            leader_score=float(getattr(record, "leader_score", 0.0) or 0.0),
            sector_strength=float(getattr(record, "sector_strength", 0.0) or 0.0),
            theme_duration=str(getattr(record, "theme_duration", "unknown") or "unknown"),
            trade_theme_stage=str(getattr(record, "trade_theme_stage", "unknown") or "unknown"),
            leader_stocks=list(getattr(record, "leader_stocks", []) or []),
            front_stocks=list(getattr(record, "front_stocks", []) or []),
            candidate_pool_level=cls._coerce_enum(
                CandidatePoolLevel,
                getattr(record, "candidate_pool_level", None),
                CandidatePoolLevel.WATCHLIST,
            ),
            strategy_family=cls._coerce_optional_enum(
                StrategyFamily,
                getattr(record, "strategy_family", None),
            ),
            setup_freshness=float(getattr(record, "setup_freshness", 0.0) or 0.0),
            setup_hit_reasons=list(getattr(record, "setup_hit_reasons", []) or []),
            trade_plan=cls._coerce_trade_plan(record),
        )

    # Enum values that represent "no real value" — serialized as NULL for DB storage.
    _NONE_ENUM_FIELDS: ClassVar[Dict[str, str]] = {
        "setup_type": SetupType.NONE.value,
    }

    def to_payload(self) -> Dict[str, Any]:
        payload = _serialize_value(self)
        # SetupType.NONE means "no entry_core setup identified" — store as NULL
        # so the backtest aggregator won't create a spurious "none" strategy group.
        for field_name, none_value in self._NONE_ENUM_FIELDS.items():
            if payload.get(field_name) == none_value:
                payload[field_name] = None
        payload.setdefault("primary_strategy", self.primary_strategy)
        payload.setdefault("contributing_strategies", list(self.contributing_strategies))
        payload.setdefault("ai_query_id", None)
        payload.setdefault("ai_summary", None)
        payload.setdefault("ai_operation_advice", None)
        payload.setdefault("ai_trade_stage", None)
        payload.setdefault("ai_reasoning", None)
        payload.setdefault("ai_confidence", None)
        payload.setdefault("ai_environment_ok", None)
        payload.setdefault("ai_theme_alignment", None)
        payload.setdefault("ai_entry_quality", None)
        payload.setdefault("stage_conflict", None)
        payload.setdefault("result_source", None)
        payload.setdefault("is_fallback", False)
        payload.setdefault("fallback_reason", None)
        payload.setdefault("downgrade_reasons", [])
        payload.setdefault("initial_position", None)
        payload.setdefault("stop_loss_rule", None)
        payload.setdefault("take_profit_plan", None)
        payload.setdefault("invalidation_rule", None)
        payload.setdefault("prompt_version", None)
        payload.setdefault("model_name", None)
        payload.setdefault("parse_status", None)
        payload.setdefault("retry_count", 0)
        ai_review = payload.get("ai_review")
        if isinstance(ai_review, dict):
            payload["ai_query_id"] = ai_review.get("ai_query_id")
            payload["ai_summary"] = ai_review.get("ai_summary")
            payload["ai_operation_advice"] = ai_review.get("ai_operation_advice")
            payload["ai_trade_stage"] = ai_review.get("ai_trade_stage")
            payload["ai_reasoning"] = ai_review.get("ai_reasoning")
            payload["ai_confidence"] = ai_review.get("ai_confidence")
            payload["ai_environment_ok"] = ai_review.get("ai_environment_ok")
            payload["ai_theme_alignment"] = ai_review.get("ai_theme_alignment")
            payload["ai_entry_quality"] = ai_review.get("ai_entry_quality")
            payload["stage_conflict"] = ai_review.get("stage_conflict")
            payload["result_source"] = ai_review.get("result_source")
            payload["is_fallback"] = ai_review.get("is_fallback", False)
            payload["fallback_reason"] = ai_review.get("fallback_reason")
            payload["downgrade_reasons"] = ai_review.get("downgrade_reasons", [])
            payload["initial_position"] = ai_review.get("initial_position")
            payload["stop_loss_rule"] = ai_review.get("stop_loss_rule")
            payload["take_profit_plan"] = ai_review.get("take_profit_plan")
            payload["invalidation_rule"] = ai_review.get("invalidation_rule")
            payload["prompt_version"] = ai_review.get("prompt_version")
            payload["model_name"] = ai_review.get("model_name")
            payload["parse_status"] = ai_review.get("parse_status")
            payload["retry_count"] = ai_review.get("retry_count", 0)
        return payload

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "CandidateDecision":
        trade_plan_payload = payload.get("trade_plan")
        ai_review_payload = payload.get("ai_review")
        contributing_strategies = payload.get("contributing_strategies", [])
        if not isinstance(contributing_strategies, list):
            contributing_strategies = []
        if not isinstance(ai_review_payload, dict):
            flat_ai_values = {
                "ai_query_id": payload.get("ai_query_id"),
                "ai_summary": payload.get("ai_summary"),
                "ai_operation_advice": payload.get("ai_operation_advice"),
                "ai_trade_stage": payload.get("ai_trade_stage"),
                "ai_reasoning": payload.get("ai_reasoning"),
                "ai_confidence": payload.get("ai_confidence"),
                "ai_environment_ok": payload.get("ai_environment_ok"),
                "ai_theme_alignment": payload.get("ai_theme_alignment"),
                "ai_entry_quality": payload.get("ai_entry_quality"),
                "stage_conflict": payload.get("stage_conflict"),
                "result_source": payload.get("result_source"),
                "is_fallback": payload.get("is_fallback"),
                "fallback_reason": payload.get("fallback_reason"),
                "downgrade_reasons": payload.get("downgrade_reasons"),
                "initial_position": payload.get("initial_position"),
                "stop_loss_rule": payload.get("stop_loss_rule"),
                "take_profit_plan": payload.get("take_profit_plan"),
                "invalidation_rule": payload.get("invalidation_rule"),
                "prompt_version": payload.get("prompt_version"),
                "model_name": payload.get("model_name"),
                "parse_status": payload.get("parse_status"),
                "retry_count": payload.get("retry_count"),
            }
            if any(value is not None and value != "" for value in flat_ai_values.values()):
                ai_review_payload = flat_ai_values
        return cls(
            code=str(payload.get("code", "")),
            name=str(payload.get("name", "")),
            rank=int(payload.get("rank", 0) or 0),
            selected_for_ai=bool(payload.get("selected_for_ai", False)),
            rule_hits=list(payload.get("rule_hits", []) or []),
            factor_snapshot=dict(payload.get("factor_snapshot", {}) or {}),
            strategy_scores=dict(payload.get("strategy_scores", {}) or {}),
            market_regime=cls._coerce_enum(MarketRegime, payload.get("market_regime"), MarketRegime.BALANCED),
            risk_level=cls._coerce_enum(RiskLevel, payload.get("risk_level"), RiskLevel.MEDIUM),
            environment_ok=bool(payload.get("environment_ok", True)),
            index_price=float(payload.get("index_price", 0.0) or 0.0),
            index_ma100=float(payload.get("index_ma100", 0.0) or 0.0),
            market_message=str(payload.get("market_message", "") or ""),
            theme_tag=str(payload.get("theme_tag", "") or ""),
            theme_score=float(payload.get("theme_score", 0.0) or 0.0),
            theme_position=cls._coerce_enum(ThemePosition, payload.get("theme_position"), ThemePosition.NON_THEME),
            leader_score=float(payload.get("leader_score", 0.0) or 0.0),
            sector_strength=float(payload.get("sector_strength", 0.0) or 0.0),
            theme_duration=str(payload.get("theme_duration", "unknown") or "unknown"),
            trade_theme_stage=str(payload.get("trade_theme_stage", "unknown") or "unknown"),
            leader_stocks=list(payload.get("leader_stocks", []) or []),
            front_stocks=list(payload.get("front_stocks", []) or []),
            candidate_pool_level=cls._coerce_enum(
                CandidatePoolLevel,
                payload.get("candidate_pool_level"),
                CandidatePoolLevel.WATCHLIST,
            ),
            relative_strength_market=float(payload.get("relative_strength_market", 0.0) or 0.0),
            relative_strength_sector=float(payload.get("relative_strength_sector", 0.0) or 0.0),
            setup_type=cls._coerce_enum(SetupType, payload.get("setup_type"), SetupType.NONE),
            entry_maturity=cls._coerce_enum(EntryMaturity, payload.get("entry_maturity"), EntryMaturity.LOW),
            setup_freshness=float(payload.get("setup_freshness", 0.0) or 0.0),
            strategy_family=cls._coerce_optional_enum(StrategyFamily, payload.get("strategy_family")),
            primary_strategy=payload.get("primary_strategy"),
            contributing_strategies=list(contributing_strategies),
            setup_hit_reasons=list(payload.get("setup_hit_reasons", []) or []),
            matched_strategies=list(payload.get("matched_strategies", []) or []),
            trade_stage=cls._coerce_enum(TradeStage, payload.get("trade_stage"), TradeStage.WATCH),
            trade_plan=cls._build_trade_plan(trade_plan_payload),
            ai_review=(
                AiReviewDecision(
                    ai_query_id=ai_review_payload.get("ai_query_id"),
                    ai_summary=ai_review_payload.get("ai_summary"),
                    ai_operation_advice=ai_review_payload.get("ai_operation_advice"),
                    ai_trade_stage=cls._coerce_optional_enum(TradeStage, ai_review_payload.get("ai_trade_stage")),
                    ai_reasoning=str(ai_review_payload.get("ai_reasoning", "") or ""),
                    ai_confidence=float(ai_review_payload.get("ai_confidence", 0.0) or 0.0),
                    ai_environment_ok=ai_review_payload.get("ai_environment_ok"),
                    ai_theme_alignment=ai_review_payload.get("ai_theme_alignment"),
                    ai_entry_quality=ai_review_payload.get("ai_entry_quality"),
                    stage_conflict=bool(ai_review_payload.get("stage_conflict", False)),
                    result_source=ai_review_payload.get("result_source"),
                    is_fallback=bool(ai_review_payload.get("is_fallback", False)),
                    fallback_reason=ai_review_payload.get("fallback_reason"),
                    downgrade_reasons=list(ai_review_payload.get("downgrade_reasons", []) or []),
                    initial_position=ai_review_payload.get("initial_position"),
                    stop_loss_rule=ai_review_payload.get("stop_loss_rule"),
                    take_profit_plan=ai_review_payload.get("take_profit_plan"),
                    invalidation_rule=ai_review_payload.get("invalidation_rule"),
                    prompt_version=ai_review_payload.get("prompt_version"),
                    model_name=ai_review_payload.get("model_name"),
                    parse_status=ai_review_payload.get("parse_status"),
                    retry_count=int(ai_review_payload.get("retry_count", 0) or 0),
                )
                if isinstance(ai_review_payload, dict) else None
            ),
            has_ai_analysis=bool(payload.get("has_ai_analysis", False)),
            news_count=int(payload.get("news_count", 0) or 0),
            news_summary=payload.get("news_summary"),
            recommendation_source=payload.get("recommendation_source"),
            recommendation_reason=payload.get("recommendation_reason"),
            rule_score=float(payload.get("rule_score", 0.0) or 0.0),
            final_score=float(payload.get("final_score", 0.0) or 0.0),
            final_rank=int(payload.get("final_rank", 0) or 0),
        )

    @staticmethod
    def _coerce_enum(enum_cls: Any, raw: Any, default: Any) -> Any:
        if isinstance(raw, enum_cls):
            return raw
        if raw in (None, ""):
            return default
        if isinstance(raw, str):
            raw = _ENUM_ALIASES.get(enum_cls, {}).get(raw, raw)
        try:
            return enum_cls(raw)
        except ValueError:
            return default

    @staticmethod
    def _coerce_optional_enum(enum_cls: Any, raw: Any) -> Optional[Any]:
        if raw in (None, ""):
            return None
        if isinstance(raw, enum_cls):
            return raw
        try:
            return enum_cls(raw)
        except ValueError:
            return None

    @staticmethod
    def _build_trade_plan(payload: Any) -> Optional[TradePlan]:
        if not isinstance(payload, dict):
            return None
        normalized: Dict[str, Any] = {}
        for field_name in TradePlan.__dataclass_fields__.keys():
            if field_name not in payload:
                continue
            value = payload.get(field_name)
            if field_name == "risk_level":
                value = CandidateDecision._coerce_enum(RiskLevel, value, RiskLevel.MEDIUM)
            normalized[field_name] = value
        return TradePlan(**normalized)

    @staticmethod
    def _coerce_trade_plan(record: Any) -> Optional[TradePlan]:
        raw = getattr(record, "trade_plan", None)
        if isinstance(raw, dict):
            return CandidateDecision._build_trade_plan(raw)
        trade_plan_json = getattr(record, "trade_plan_json", None)
        if not trade_plan_json:
            return None
        try:
            payload = json.loads(trade_plan_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return CandidateDecision._build_trade_plan(payload)


def _serialize_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {
            key: _serialize_value(item)
            for key, item in asdict(value).items()
            if item is not None
        }
    if isinstance(value, dict):
        return {key: _serialize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    return value
