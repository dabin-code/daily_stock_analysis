# -*- coding: utf-8 -*-
"""重放引擎的策略描述符与注册表。

重放引擎此前把「跑哪个策略」写死成 v1/v2 的二选一分支，四处接缝各写一遍：
策略 YAML 路径、参数网格覆盖哪些配置字段、事件与阶段抽取读哪些因子列、
证据层调哪对回调。这里把这四件事收进一个描述符，再加一条策略就是多注册一个
描述符，而不是在四处各加一个 `if`。

描述符刻意允许**缺项**：v1 没有证据层、不参与网格、也没有分层事件字段。这些
位置留空而不是补一个假的 v1 版本——补出来的对称性会掩盖两条策略真实的差异，
而下一条策略恰恰要按真实差异来填。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

ROOT = Path(__file__).resolve().parents[3]
STRATEGY_DIRECTORY = ROOT / "strategies"

# 事件键是重放引擎的固定词汇，不属于任何一条策略：`ValidationSample` 的
# early / near_cleared / major_breakout 三个日期字段与它们一一对应。策略要做的
# 是把自己的阶段标签映射到这三个键上，而不是另起一套。
EVENT_EARLY = "early"
EVENT_R1 = "r1"
EVENT_R2 = "r2"


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _prefixed(prefix: str, *logical_names: str) -> dict[str, tuple[str, ...]]:
    """按统一前缀展开逻辑名到因子列名。

    多数策略的输出列共用一个前缀，逐条写出来只会让描述符变长而读不出更多信息。
    列名不成规律的策略照样可以直接写字面映射，两种写法在描述符里等价。
    """
    return {name: (f"{prefix}{name}",) for name in logical_names}


@dataclass(frozen=True)
class EvidenceHooks:
    """证据层的两个回调：冻结一次、按参数评估多次。

    两者都会被 pickle 送进 `ProcessPoolExecutor` 的 worker，因此必须是模块级
    函数——lambda 与闭包在 spawn 模式下发不过去。
    """

    freeze: Callable[[Any, Any], Any]
    compute: Callable[[Any, Any, Any], dict[str, Any]]


@dataclass(frozen=True)
class ReplayStrategy:
    """一条策略要跑通重放必须提供的全部东西。

    读这个类就能知道新增一条策略要填什么；填不出来的位置说明该策略在那一层
    没有对应物，留空即可（v1 就是这么用的）。
    """

    # 重放侧的策略标识，同时是候选版本号的前缀（`v2:代码:日期`）。
    name: str
    # 筛选规则的 YAML，由 `ScreenerService` 按 skill 名消费。
    strategy_path: Path
    # 从候选里抽出 (因子, 阶段, 候选版本, 事件日期表, 本次事件日期)。
    event_context: Callable[..., tuple]
    # 样本的突破下沿；不同策略取的是各自结构里的不同价位。
    breakout_floor: Callable[..., Optional[float]]
    # 参数网格：快照里的键 -> 被覆盖的 `Config` 字段。空表示不参与网格。
    grid_fields: Mapping[str, str] = field(default_factory=dict)
    # 策略开关字段。隔离配置强制置 True，缓存层也据此判断该策略的证据层是否
    # 生效；为 None 表示这条策略没有开关，恒生效。
    enabled_field: Optional[str] = None
    # 事件与阶段抽取读取的因子列：逻辑名 -> 候选列名（按序取第一个有值的）。
    event_fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # 阶段标签 -> 事件键。v1 为空：它只有一个确认事件，没有分层阶段。
    stage_events: Mapping[str, str] = field(default_factory=dict)
    # 证据层回调；没有证据层的策略为 None。
    evidence: Optional[EvidenceHooks] = None
    # 事件是否会在信号日之后继续成熟，决定重放要不要收集事件证据。
    matures_events: bool = False

    def is_enabled(self, config: Any) -> bool:
        if self.enabled_field is None:
            return True
        return bool(getattr(config, self.enabled_field))

    def factor_names(self, logical_name: str) -> tuple[str, ...]:
        return self.event_fields.get(logical_name, ())

    def factor_value(self, factor: Mapping[str, Any], logical_name: str) -> Any:
        """取该策略某个逻辑字段的值；策略没声明这个字段时返回 None。

        返回 None 与「声明了但值为空」不作区分：调用方对两者的处理本来就一致，
        而分开会逼每个消费点各写一遍判断。
        """
        for name in self.factor_names(logical_name):
            value = factor.get(name)
            if value is not None:
                return value
        return None


_REGISTRY: dict[str, ReplayStrategy] = {}


def register_strategy(strategy: ReplayStrategy) -> ReplayStrategy:
    if strategy.name in _REGISTRY:
        raise ValueError(f"重复注册的重放策略：{strategy.name}")
    _REGISTRY[strategy.name] = strategy
    return strategy


def resolve_strategy(name: str) -> ReplayStrategy:
    try:
        return _REGISTRY[str(name)]
    except KeyError:
        raise ValueError(
            f"未注册的重放策略：{name!r}；已注册：{sorted(_REGISTRY)}"
        ) from None


def registered_strategies() -> tuple[ReplayStrategy, ...]:
    return tuple(_REGISTRY[name] for name in sorted(_REGISTRY))


def evidence_strategy_for(config: Any) -> Optional[ReplayStrategy]:
    """本次配置下由哪条策略提供证据层；没有则返回 None。

    因子缓存是全部 leg 共用的一个实例，它拿不到 leg 的策略标识，只能从配置反查。
    多条策略同时打开时不猜一个出来：证据层的产物会按参数哈希写进缓存，猜错的
    后果是把 A 策略的证据当成 B 策略的结果返回——算错，不是算慢。
    """
    matched = [
        strategy
        for strategy in registered_strategies()
        if strategy.evidence is not None and strategy.is_enabled(config)
    ]
    if len(matched) > 1:
        raise ValueError(
            "同一份配置同时打开了多条带证据层的策略："
            f"{[item.name for item in matched]}，无法判定该跑哪一条"
        )
    return matched[0] if matched else None


# ── v1：双突破底背离 ────────────────────────────────────────────────────
#
# v1 的事件模型只有一个「确认」时点，early / r1 / r2 三个键取同一个日期。
# 它没有阻力区、没有分层阶段，因此既不参与网格，也没有可成熟的事件。


def _legacy_confirmation_date(
    factor: dict,
    signal_date: date,
    strategy: "ReplayStrategy",
) -> date:
    # 逐个列名试到能解析为止，而不是取第一个存在的列：某一列存在但值不可解析
    # 时必须继续往后找，否则会把一个解析失败当成「没有确认日期」。
    for field_name in strategy.factor_names("confirmation_date"):
        value = factor.get(field_name)
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass
    for field_name in strategy.factor_names("confirmation_days"):
        value = factor.get(field_name)
        if type(value) is int and value >= 0:
            return signal_date - timedelta(days=value)
    return signal_date


def _legacy_candidate_version(
    factor: dict,
    *,
    code: str,
    strategy: "ReplayStrategy",
) -> str:
    explicit = strategy.factor_value(factor, "candidate_version")
    if explicit:
        return str(explicit)
    for field_name in strategy.factor_names("confirmation_date"):
        frozen_date = factor.get(field_name)
        if frozen_date:
            return f"{strategy.name}:{code}:{frozen_date}"
    structural_payload = {
        "buy_points": strategy.factor_value(factor, "buy_points"),
        "horizontal_resistance": strategy.factor_value(
            factor, "horizontal_resistance"
        ),
        "support": strategy.factor_value(factor, "support"),
        "signal_date": strategy.factor_value(factor, "signal_date"),
    }
    digest = sha256(
        json.dumps(
            structural_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return f"{strategy.name}:{code}:{digest}"


def _v1_breakout_floor(
    factor: dict,
    strategy: Optional["ReplayStrategy"] = None,
) -> Optional[float]:
    """v1 的突破下沿：优先取已触发的水平阻力，否则退回冻结的阻力位。

    `strategy` 缺省解析成 v1 描述符，只为兼容既有的单参调用方。
    """
    strategy = strategy or BOTTOM_DIVERGENCE_V1
    buy_points = strategy.factor_value(factor, "buy_points") or []
    horizontal = [
        item
        for item in buy_points
        if item.get("triggered")
        and (
            "阻力" in str(item.get("label") or "")
            or str(item.get("type") or "").lower() == "horizontal_resistance"
        )
    ]
    if horizontal:
        selected = max(horizontal, key=lambda item: int(item.get("level", 0)))
        return _optional_float(selected.get("trigger_price"))
    return _optional_float(
        strategy.factor_value(factor, "horizontal_resistance")
    )


def confirmation_event_context(
    strategy: "ReplayStrategy",
    *,
    candidate: Any,
    signal_date: date,
    config: Any,
    stock_repository: Any,
) -> tuple[dict, str, str, dict[str, Optional[date]], Optional[date]]:
    del config, stock_repository
    factor = dict(candidate.factor_snapshot or {})
    confirmation_date = _legacy_confirmation_date(
        factor,
        signal_date,
        strategy,
    )
    events = {
        EVENT_EARLY: confirmation_date,
        EVENT_R1: confirmation_date,
        EVENT_R2: confirmation_date,
    }
    candidate_version = _legacy_candidate_version(
        factor,
        code=str(candidate.code),
        strategy=strategy,
    )
    return (
        factor,
        EVENT_R2,
        str(candidate_version),
        events,
        confirmation_date,
    )


def confirmation_breakout_floor(
    strategy: "ReplayStrategy",
    factor: dict,
    stage: str,
) -> Optional[float]:
    del stage
    return _v1_breakout_floor(factor, strategy)


# ── v2：分层建仓底背离 ──────────────────────────────────────────────────


def event_dates(
    *,
    factor: dict,
    code: str,
    signal_date: date,
    config: Any,
    stock_repository: Any,
    strategy: "ReplayStrategy",
) -> dict[str, Optional[date]]:
    if not strategy.is_enabled(config):
        return {
            EVENT_EARLY: signal_date,
            EVENT_R1: signal_date,
            EVENT_R2: signal_date,
        }

    def as_date(value: Any) -> Optional[date]:
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None
        return None

    candidate_version = strategy.factor_value(factor, "candidate_version")
    records = strategy.factor_value(factor, "candidate_records") or ()
    record = next(
        (
            item
            for item in records
            if item.get("candidate_version") == candidate_version
        ),
        None,
    )
    if record is not None:
        early = record.get("early_reversal") or {}
        near = record.get("near_zone_events") or {}
        major = record.get("major_zone_breakout") or {}
        cleared = near.get("cleared_confirmed") or {}
        return {
            EVENT_EARLY: as_date(early.get("date")),
            EVENT_R1: as_date(cleared.get("date")),
            EVENT_R2: (
                as_date(major.get("date"))
                if major.get("confirmed") is True
                else None
            ),
        }

    rows = stock_repository.get_range(
        code,
        signal_date - timedelta(days=config.screening_factor_lookback_days),
        signal_date,
    )
    dates = [row.date for row in sorted(rows, key=lambda item: item.date)]

    def resolve(logical_name: str) -> Optional[date]:
        index = strategy.factor_value(factor, logical_name)
        try:
            normalized = int(index)
        except (TypeError, ValueError, OverflowError):
            return None
        if (
            float(index) == normalized
            and 0 <= normalized < len(dates)
        ):
            return dates[normalized]
        return None

    return {
        EVENT_EARLY: resolve("early_event_index"),
        EVENT_R1: resolve("near_event_index"),
        EVENT_R2: resolve("major_event_index"),
    }


def staged_event_context(
    strategy: "ReplayStrategy",
    *,
    candidate: Any,
    signal_date: date,
    config: Any,
    stock_repository: Any,
) -> tuple[dict, str, str, dict[str, Optional[date]], Optional[date]]:
    factor = dict(candidate.factor_snapshot or {})
    stage = str(strategy.factor_value(factor, "stage") or "")
    events = event_dates(
        factor=factor,
        code=candidate.code,
        signal_date=signal_date,
        config=config,
        stock_repository=stock_repository,
        strategy=strategy,
    )
    candidate_version = (
        strategy.factor_value(factor, "candidate_version")
        or (
            f"{strategy.name}:{candidate.code}:"
            f"{(events[EVENT_EARLY] or signal_date).isoformat()}"
        )
    )
    event_key = strategy.stage_events.get(stage.strip().lower())
    event_date = events[event_key] if event_key is not None else None
    return factor, stage, str(candidate_version), events, event_date


def staged_breakout_floor(
    strategy: "ReplayStrategy",
    factor: dict,
    stage: str,
) -> Optional[float]:
    """按阶段取所在阻力区的下沿。

    这里比的是**原样**的阶段标签，而事件键映射比的是 `strip().lower()` 之后的
    标签。两处口径不同是既有行为，不是笔误：改齐会让大小写异常的阶段换一个
    下沿，进而换一个样本。
    """
    major_stages = {
        label
        for label, event_key in strategy.stage_events.items()
        if event_key == EVENT_R2
    }
    logical_name = (
        "major_zone_lower" if stage in major_stages else "near_zone_lower"
    )
    return _optional_float(strategy.factor_value(factor, logical_name))


def freeze_bottom_divergence_v2_evidence(service: Any, group: Any) -> Any:
    return service.freeze_bottom_divergence_v2_evidence(group)


def compute_bottom_divergence_v2_factors(
    service: Any,
    group: Any,
    frozen: Any,
) -> dict[str, Any]:
    return service.compute_bottom_divergence_v2_factors(
        group,
        frozen_evidence=frozen,
    )


BOTTOM_DIVERGENCE_V1 = register_strategy(ReplayStrategy(
    name="v1",
    strategy_path=(
        STRATEGY_DIRECTORY / "bottom_divergence_double_breakout.yaml"
    ),
    event_context=confirmation_event_context,
    breakout_floor=confirmation_breakout_floor,
    event_fields={
        "candidate_version": ("bottom_divergence_candidate_version",),
        # 两个不带前缀的列名是历史别名，冻结在这里是因为落库的老候选还带着
        # 它们；新策略不必效仿。
        "confirmation_date": (
            "bottom_divergence_confirmation_date",
            "confirmation_date",
        ),
        "confirmation_days": (
            "bottom_divergence_confirmation_days",
            "confirmation_days",
        ),
        "buy_points": ("bottom_divergence_buy_points",),
        "horizontal_resistance": (
            "bottom_divergence_horizontal_resistance",
        ),
        "support": ("bottom_divergence_support",),
        "signal_date": ("bottom_divergence_signal_date",),
    },
))


BOTTOM_DIVERGENCE_V2 = register_strategy(ReplayStrategy(
    name="v2",
    strategy_path=(
        STRATEGY_DIRECTORY / "bottom_divergence_layered_entry_v2.yaml"
    ),
    event_context=staged_event_context,
    breakout_floor=staged_breakout_floor,
    grid_fields={
        "cluster_pct": "bottom_divergence_v2_cluster_pct",
        "atr_gap_multiplier": "bottom_divergence_v2_atr_gap_multiplier",
        "zone_score_min": "bottom_divergence_v2_zone_score_min",
    },
    enabled_field="bottom_divergence_v2_enabled",
    event_fields=_prefixed(
        "bottom_divergence_v2_",
        "candidate_version",
        "candidate_records",
        "stage",
        "early_event_index",
        "near_event_index",
        "major_event_index",
        "near_zone_lower",
        "major_zone_lower",
    ),
    stage_events={
        "early": EVENT_EARLY,
        "near": EVENT_R1,
        "near_cleared": EVENT_R1,
        "r1": EVENT_R1,
        "major": EVENT_R2,
        "major_actionable": EVENT_R2,
        "r2": EVENT_R2,
    },
    evidence=EvidenceHooks(
        freeze=freeze_bottom_divergence_v2_evidence,
        compute=compute_bottom_divergence_v2_factors,
    ),
    matures_events=True,
))
