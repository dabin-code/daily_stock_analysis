# -*- coding: utf-8 -*-
"""Historical replay services for bottom-divergence v2 validation."""
from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import dataclass, replace
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional, Protocol, Sequence

from src.backtest.evaluators.entry_evaluator import EntrySignalEvaluator
from src.backtest.services.bottom_divergence_v2_validation import (
    CandidateEventEvidence,
    ValidationInputError,
    ValidationSample,
)
from src.config import Config
from .bottom_divergence_v2_report import canonical_json_dumps

ROOT = Path(__file__).resolve().parents[3]
V1_STRATEGY_PATH = ROOT / "strategies" / "bottom_divergence_double_breakout.yaml"
V2_STRATEGY_PATH = ROOT / "strategies" / "bottom_divergence_layered_entry_v2.yaml"


def compute_pre_signal_features(
    prior_bars: Sequence[Any],
) -> tuple[Optional[float], Optional[float]]:
    """Compute frozen 20-session volatility and liquidity before the signal."""
    bars = list(prior_bars)[-20:]
    valid_closes = [
        float(bar.close)
        for bar in bars
        if getattr(bar, "close", None) is not None
        and float(bar.close) > 0.0
    ]
    returns = [
        valid_closes[index] / valid_closes[index - 1] - 1.0
        for index in range(1, len(valid_closes))
    ]
    valid_amounts = [
        float(bar.amount)
        for bar in bars
        if getattr(bar, "amount", None) is not None
    ]
    volatility = statistics.stdev(returns) if len(returns) >= 10 else None
    liquidity = (
        statistics.fmean(valid_amounts)
        if len(valid_amounts) >= 10
        else None
    )
    return volatility, liquidity


@dataclass(frozen=True)
class ReplayBatch:
    samples: tuple[ValidationSample, ...]
    opportunity_counts: dict[date, int]
    event_evidence: tuple[CandidateEventEvidence, ...] = ()


@dataclass(frozen=True)
class ReplayDependencies:
    db_manager: Any
    factor_service_factory: Callable[[Config], Any]
    pipeline: Any
    screener_factory: Callable[[str], tuple[Any, Any]]
    market_context_provider: Callable[[date, Any], tuple[Any, Any]]
    stock_repository: Any


# ── 前瞻 / 前置窗口的复权（gate-3 落点 2）──────────────────────────────
#
# `entry_close` 取自因子快照（已按 D 归一，因此等于 D 当日真实可成交价），而
# `get_forward_bars` / `get_prior_bars` 走的是 `stock_repo.py` 的裸
# `select(StockDaily)`，拿到的是**原始价**。两者混用的后果不是精度问题：前瞻
# 窗口内一次除权就被算成亏损，1145 个送转事件会造成 33%~66% 的假亏损，而回测
# 照常产出样本——「验收通过但数值全错」。
#
# 不改 `stock_repo.py`：那是共享仓储层，五层流水线（`backtest_service`）也在
# 用它，在那里施加会把波及面扩到本计划之外。
#
# `adj_convention` 不参与缩放，但必须随窗口一起进来：它是复权链的口径守卫的
# 输入（`adjustment_chain.convention_reject_reason`），缺了这一列整窗会被判成
# 不可复权。`StockDaily` 本来就有这个属性，`stock_repo.py` 取的是整行 ORM
# 对象，所以这里只是别把它在转 DataFrame 时丢掉。
_ADJUSTABLE_BAR_FIELDS = (
    "date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
    "adj_convention",
)


def _bars_to_frame(
    bars: Sequence[Any],
    *,
    leading: Optional[dict] = None,
) -> Optional[Any]:
    """把 bar 对象序列转成复权链要的窗口；缺 close/pre_close 时返回 None。

    返回 None 表示「这批 bar 解算不出复权链」，调用方一律 fail-closed。
    生产侧的 `StockDaily` 行 100% 带 `pre_close`，走到这个分支的只可能是
    内存里构造的桩对象。

    ``leading`` 是拼在最前面的锚点行，前瞻窗口用它把 D 接回序列。和
    `pd.concat` 一个只有 close/pre_close 的单行 DataFrame 不同，这里让锚点
    与 bar 走同一次 `DataFrame` 构造，列的 dtype 由整列共同决定。
    """
    import pandas as pd

    records = []
    if leading is not None:
        records.append({
            name: leading.get(name) for name in _ADJUSTABLE_BAR_FIELDS
        })
    for bar in bars:
        if getattr(bar, "close", None) is None:
            return None
        if getattr(bar, "pre_close", None) is None:
            return None
        records.append({
            name: getattr(bar, name, None)
            for name in _ADJUSTABLE_BAR_FIELDS
        })
    frame = pd.DataFrame(records)
    if frame["date"].isna().any():
        # 日期缺失时不留空列：复权链只在该列存在时校验升序，留个全 NaT 的列
        # 会让它把顺序正常的窗口判成乱序。
        frame = frame.drop(columns=["date"])
    else:
        frame["date"] = pd.to_datetime(frame["date"])
    return frame


def _frame_to_bars(frame: Any) -> list[Any]:
    """把复权后的窗口还原成 bar 对象。

    只回填评估链路真正读的字段。`date` 还原成 `date` 而不是 `Timestamp`：
    `future_trade_dates_20d` 会被成本模型按交易日比较。
    """
    import pandas as pd

    def cell(record: dict, name: str) -> Any:
        value = record.get(name)
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        return float(value)

    bars = []
    for record in frame.to_dict("records"):
        raw_date = record.get("date")
        bars.append(SimpleNamespace(
            date=(
                pd.Timestamp(raw_date).date()
                if raw_date is not None and not pd.isna(raw_date)
                else None
            ),
            open=cell(record, "open"),
            high=cell(record, "high"),
            low=cell(record, "low"),
            close=cell(record, "close"),
            pre_close=cell(record, "pre_close"),
            volume=cell(record, "volume"),
            amount=cell(record, "amount"),
            adj_factor=cell(record, "adj_factor"),
            adj_factor_source=record.get("adj_factor_source"),
        ))
    return bars


def _all_trusted(frame: Any) -> bool:
    from src.services.adjustment_chain import (
        ADJ_FACTOR_SOURCE_COLUMN,
        TRUSTED_SOURCE,
    )

    return bool((frame[ADJ_FACTOR_SOURCE_COLUMN] == TRUSTED_SOURCE).all())


def _adjusted_forward_bars(
    bars: Sequence[Any],
    *,
    signal_date: date,
    anchor_close: float,
    config: Config,
) -> list[Any]:
    """把前瞻 bar 复权到锚 D = ``signal_date``。

    数学上要的是 ``g(t) = f(t)/f(D)``，而 D 本身不在前瞻窗口里
    （`get_forward_bars` 取的是 ``date > analysis_date``）。缺的那一环是
    ``ratio(D+1) = close(D)/pre_close(D+1)``，只需要 ``close(D)``——它就是
    `entry_close`，因为因子快照已按 D 归一、``g(D) == 1``。

    于是把 D 拼成序列首行交给 `apply_read_adjustment_from_anchor`，首行因子恒
    为 1，入场价不动，除权日之后的 bar 被抬到同一尺度。

    fail-closed 时返回空列表而不是原始 bar：跨过一处无法解释的价格跳变算出来
    的收益是个错数，比「这个样本没有前瞻窗口」更糟。
    """
    from src.services.adjustment_chain import (
        RAW_CONVENTION,
        apply_read_adjustment_from_anchor,
    )

    ordered = list(bars)
    if not config.adj_apply_on_read or not ordered:
        return ordered
    if not (math.isfinite(anchor_close) and anchor_close > 0.0):
        return []
    combined = _bars_to_frame(ordered, leading={
        "date": signal_date,
        "close": anchor_close,
        # 首行的 pre_close 从不参与计算（`analyze_window` 的 ratio[0] 恒为
        # 1），填 close 只是为了不给窗口留一个语义不明的空值。
        "pre_close": anchor_close,
        # 锚不是库里的一行，是因子快照给出的 `entry_close`，所以它的口径标签
        # 只能由调用链断言而不能从数据里读出来。标 raw 断言的正是「close(D) 是
        # 原始价」——这是 `ratio(D+1) = close(D)/pre_close(D+1)` 有意义的前提，
        # 而 `pre_close(D+1)` 已由下面的守卫确认是 raw。这个断言是**继承来的**：
        # D 所在的因子窗口走的是同一道守卫，那里出现非 raw 行时整组会被判成
        # 不可复权。缺口在于回放侧并不过滤 `adjustment_unknown` 的候选，见
        # 交付说明里登记的残留风险。
        "adj_convention": RAW_CONVENTION,
    })
    if combined is None:
        return []
    adjusted = apply_read_adjustment_from_anchor(combined)
    if not _all_trusted(adjusted):
        return []
    return _frame_to_bars(adjusted.iloc[1:].reset_index(drop=True))


def _adjusted_prior_bars(
    bars: Sequence[Any],
    *,
    config: Config,
) -> list[Any]:
    """把信号日之前的 bar 复权，供 `compute_pre_signal_features` 使用。

    这里按窗口**末行**归一，而不是像前瞻窗口那样接到 D 上，是因为拿不到
    ``pre_close(D)``：前置窗口止于 ``signal_date - 1``，而因子快照只给
    ``close(D)``。两种锚只差一个常数——
    ``f(t)/f(D) = [f(t)/f(末行)] · [f(末行)/f(D)]``——
    而这两个消费方都对常数免疫：波动率是收益率的标准差（比值里常数约掉），
    流动性用 `amount`（复权模块按价×量守恒刻意不动它）。真正要修的是窗口
    **内部**的除权跳空，那一段两种锚给出的结果逐位相同。

    整窗 fail-closed：`apply_adjustment` 对被作废段按 1.0 原样保留，混着算
    标准差会得到一个两种尺度拼出来的数，比没有更糟。
    """
    from src.services.adjustment_chain import apply_read_adjustment

    ordered = list(bars)
    if not config.adj_apply_on_read or not ordered:
        return ordered
    frame = _bars_to_frame(ordered)
    if frame is None:
        return []
    adjusted = apply_read_adjustment(frame)
    if not _all_trusted(adjusted):
        return []
    return _frame_to_bars(adjusted)


def _parse_position_weight(trade_plan_json: Optional[str]) -> Optional[float]:
    if not trade_plan_json:
        return None
    try:
        position = str(
            (json.loads(trade_plan_json) or {}).get("initial_position") or ""
        )
    except (TypeError, ValueError):
        return None
    compact = position.replace(" ", "")
    fractions = {
        "1/10仓": 0.1,
        "1/5仓": 0.2,
        "1/3仓": 1.0 / 3.0,
        "1/2仓": 0.5,
    }
    for label, weight in fractions.items():
        if label in compact:
            return weight
    match = re.search(r"目标仓位(20|50|100)%", compact)
    if match:
        weight = float(match.group(1)) / 100.0
        if math.isfinite(weight) and 0.0 < weight <= 1.0:
            return weight
    return None


def _event_dates(
    *,
    factor: dict,
    code: str,
    signal_date: date,
    config: Config,
    stock_repository: Any,
) -> dict[str, Optional[date]]:
    if not config.bottom_divergence_v2_enabled:
        return {"early": signal_date, "r1": signal_date, "r2": signal_date}

    def as_date(value: Any) -> Optional[date]:
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None
        return None

    candidate_version = factor.get(
        "bottom_divergence_v2_candidate_version"
    )
    records = factor.get("bottom_divergence_v2_candidate_records") or ()
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
            "early": as_date(early.get("date")),
            "r1": as_date(cleared.get("date")),
            "r2": (
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

    def resolve(field: str) -> Optional[date]:
        index = factor.get(field)
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
        "early": resolve("bottom_divergence_v2_early_event_index"),
        "r1": resolve("bottom_divergence_v2_near_event_index"),
        "r2": resolve("bottom_divergence_v2_major_event_index"),
    }


def _v1_breakout_floor(factor: dict) -> Optional[float]:
    buy_points = factor.get("bottom_divergence_buy_points") or []
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
        factor.get("bottom_divergence_horizontal_resistance")
    )


def _legacy_confirmation_date(factor: dict, signal_date: date) -> date:
    for field_name in (
        "bottom_divergence_confirmation_date",
        "confirmation_date",
    ):
        value = factor.get(field_name)
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass
    for field_name in (
        "bottom_divergence_confirmation_days",
        "confirmation_days",
    ):
        value = factor.get(field_name)
        if type(value) is int and value >= 0:
            return signal_date - timedelta(days=value)
    return signal_date


def _legacy_candidate_version(
    factor: dict,
    *,
    code: str,
) -> str:
    explicit = factor.get("bottom_divergence_candidate_version")
    if explicit:
        return str(explicit)
    for field_name in (
        "bottom_divergence_confirmation_date",
        "confirmation_date",
    ):
        frozen_date = factor.get(field_name)
        if frozen_date:
            return f"v1:{code}:{frozen_date}"
    structural_payload = {
        "buy_points": factor.get("bottom_divergence_buy_points"),
        "horizontal_resistance": factor.get(
            "bottom_divergence_horizontal_resistance"
        ),
        "support": factor.get("bottom_divergence_support"),
        "signal_date": factor.get("bottom_divergence_signal_date"),
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
    return f"v1:{code}:{digest}"


def _candidate_event_context(
    *,
    candidate: Any,
    signal_date: date,
    strategy_version: str,
    config: Config,
    stock_repository: Any,
) -> tuple[
    dict,
    str,
    str,
    dict[str, Optional[date]],
    Optional[date],
]:
    factor = dict(candidate.factor_snapshot or {})
    if strategy_version == "v1":
        confirmation_date = _legacy_confirmation_date(factor, signal_date)
        events = {
            "early": confirmation_date,
            "r1": confirmation_date,
            "r2": confirmation_date,
        }
        stage = "r2"
        candidate_version = _legacy_candidate_version(
            factor,
            code=str(candidate.code),
        )
        return factor, stage, str(candidate_version), events, confirmation_date

    stage = str(factor.get("bottom_divergence_v2_stage") or "")
    events = _event_dates(
        factor=factor,
        code=candidate.code,
        signal_date=signal_date,
        config=config,
        stock_repository=stock_repository,
    )
    candidate_version = (
        factor.get("bottom_divergence_v2_candidate_version")
        or (
            f"v2:{candidate.code}:"
            f"{(events['early'] or signal_date).isoformat()}"
        )
    )
    event_key = {
        "early": "early",
        "near": "r1",
        "near_cleared": "r1",
        "r1": "r1",
        "major": "r2",
        "major_actionable": "r2",
        "r2": "r2",
    }.get(stage.strip().lower())
    event_date = events[event_key] if event_key is not None else None
    return factor, stage, str(candidate_version), events, event_date


def _build_validation_sample(
    *,
    candidate: Any,
    signal_date: date,
    strategy_version: str,
    config: Config,
    stock_repository: Any,
    event_context: Optional[
        tuple[
            dict,
            str,
            str,
            dict[str, Optional[date]],
            Optional[date],
        ]
    ] = None,
) -> ValidationSample:
    context = event_context or _candidate_event_context(
        candidate=candidate,
        signal_date=signal_date,
        strategy_version=strategy_version,
        config=config,
        stock_repository=stock_repository,
    )
    factor, stage, candidate_version, events, _ = context
    trade_stage = str(candidate.trade_stage or "")
    position_weight = _parse_position_weight(candidate.trade_plan_json)
    is_executable = (
        trade_stage in {"probe_entry", "add_on_strength"}
        and candidate.trade_plan_json is not None
        and position_weight is not None
        and math.isfinite(position_weight)
        and 0.0 < position_weight <= 1.0
    )
    # entry_close 必须先算：它就是 close(D)（因子快照已按 D 归一，g(D)==1），
    # 也是前瞻窗口接回 D 所缺的那一个数。
    entry_close = float(factor.get("close") or 0.0)
    forward_bars = _adjusted_forward_bars(
        stock_repository.get_forward_bars(
            code=candidate.code,
            analysis_date=signal_date,
            eval_window_days=20,
        ),
        signal_date=signal_date,
        anchor_close=entry_close,
        config=config,
    )
    prior_bars = _adjusted_prior_bars(
        stock_repository.get_prior_bars(
            code=candidate.code,
            signal_date=signal_date,
            count=20,
        ),
        config=config,
    )
    volatility, liquidity = compute_pre_signal_features(prior_bars)
    closes = tuple(float(bar.close) for bar in forward_bars)
    highs = tuple(float(bar.high) for bar in forward_bars)
    lows = tuple(float(bar.low) for bar in forward_bars)
    full_evaluation = EntrySignalEvaluator.evaluate(
        entry_price=entry_close,
        forward_bars=list(forward_bars),
    )
    first_five_evaluation = (
        EntrySignalEvaluator.evaluate(
            entry_price=entry_close,
            forward_bars=list(forward_bars[:5]),
        )
        if len(forward_bars) >= 5
        else None
    )
    if strategy_version == "v1":
        breakout_floor = _v1_breakout_floor(factor)
    elif stage in {"major", "major_actionable", "r2"}:
        breakout_floor = _optional_float(
            factor.get("bottom_divergence_v2_major_zone_lower")
        )
    else:
        breakout_floor = _optional_float(
            factor.get("bottom_divergence_v2_near_zone_lower")
        )
    return ValidationSample(
        code=str(candidate.code),
        signal_date=signal_date,
        candidate_version=str(candidate_version),
        strategy_version=strategy_version,
        stage=stage,
        entry_close=entry_close,
        near_zone_lower=_optional_float(
            factor.get("bottom_divergence_v2_near_zone_lower")
        ),
        major_zone_lower=_optional_float(
            factor.get("bottom_divergence_v2_major_zone_lower")
        ),
        early_event_date=events["early"],
        near_cleared_event_date=events["r1"],
        major_breakout_event_date=events["r2"],
        close_5d=closes[4] if len(closes) >= 5 else None,
        close_10d=closes[9] if len(closes) >= 10 else None,
        close_20d=closes[19] if len(closes) >= 20 else None,
        future_closes_20d=closes,
        future_highs_20d=highs,
        future_lows_20d=lows,
        max_high_20d=(
            entry_close * (1.0 + full_evaluation.mfe / 100.0)
            if full_evaluation.mfe is not None
            else None
        ),
        min_low_20d=(
            entry_close * (1.0 + full_evaluation.mae / 100.0)
            if full_evaluation.mae is not None
            else None
        ),
        market_regime=str(candidate.market_regime or "unknown"),
        volatility=volatility,
        liquidity=liquidity,
        future_trade_dates_20d=tuple(bar.date for bar in forward_bars),
        breakout_floor=breakout_floor,
        position_weight=position_weight,
        is_executable=is_executable,
        evaluator_return_5d=full_evaluation.forward_return_5d,
        evaluator_return_10d=full_evaluation.forward_return_10d,
        mae_5d=(
            first_five_evaluation.mae
            if first_five_evaluation is not None
            else None
        ),
        mae_20d=full_evaluation.mae,
        mfe_20d=full_evaluation.mfe,
    )


def replay_historical_dates(
    *,
    strategy_version: str,
    config: Config,
    trade_dates: Sequence[date],
    universe: Any,
    dependencies: ReplayDependencies,
) -> ReplayBatch:
    """Replay production factors, YAML screening and the five-layer pipeline."""
    factor_service = dependencies.factor_service_factory(config)
    screener, skill_manager = dependencies.screener_factory(strategy_version)
    samples: list[ValidationSample] = []
    opportunities: dict[date, int] = {}
    seen_stage_events: set[tuple[str, str, str]] = set()
    evidence_by_key: dict[
        tuple[str, str],
        CandidateEventEvidence,
    ] = {}
    for trade_date in sorted(set(trade_dates)):
        snapshot_df = factor_service.build_factor_snapshot(
            universe,
            trade_date=trade_date,
            persist=False,
        )
        opportunities[trade_date] = len(snapshot_df)
        market_env, guard_result = dependencies.market_context_provider(
            trade_date,
            snapshot_df,
        )
        pipeline_result = dependencies.pipeline.run(
            snapshot_df=snapshot_df,
            trade_date=trade_date,
            market_env=market_env,
            guard_result=guard_result,
            screener_service=screener,
            candidate_limit=max(1, len(snapshot_df)),
            db_manager=dependencies.db_manager,
            skill_manager=skill_manager,
            lock_universe=False,
        )
        candidates_with_context = [
            (
                candidate,
                _candidate_event_context(
                    candidate=candidate,
                    signal_date=trade_date,
                    strategy_version=strategy_version,
                    config=config,
                    stock_repository=dependencies.stock_repository,
                ),
            )
            for candidate in pipeline_result.candidates
        ]
        for candidate, context in sorted(
            candidates_with_context,
            key=lambda item: (
                str(item[0].code),
                item[1][2],
                item[1][1],
            ),
        ):
            _, stage, candidate_version, events, event_date = context
            if strategy_version == "v2":
                evidence_key = (str(candidate.code), candidate_version)
                current = evidence_by_key.get(evidence_key)
                evidence_by_key[evidence_key] = CandidateEventEvidence(
                    code=evidence_key[0],
                    candidate_version=evidence_key[1],
                    near_cleared_event_date=_earliest_date(
                        current.near_cleared_event_date if current else None,
                        events["r1"],
                    ),
                    major_breakout_event_date=_earliest_date(
                        current.major_breakout_event_date if current else None,
                        events["r2"],
                    ),
                )
            position_weight = _parse_position_weight(
                candidate.trade_plan_json
            )
            executable = (
                str(candidate.trade_stage or "")
                in {"probe_entry", "add_on_strength"}
                and candidate.trade_plan_json is not None
                and position_weight is not None
            )
            stage_key = (
                str(candidate.code),
                candidate_version,
                stage.strip().lower(),
            )
            if (
                not executable
                or event_date != trade_date
                or stage_key in seen_stage_events
            ):
                continue
            samples.append(
                _build_validation_sample(
                    candidate=candidate,
                    signal_date=trade_date,
                    strategy_version=strategy_version,
                    config=config,
                    stock_repository=dependencies.stock_repository,
                    event_context=context,
                )
            )
            seen_stage_events.add(stage_key)
    return ReplayBatch(
        tuple(samples),
        opportunities,
        tuple(evidence_by_key[key] for key in sorted(evidence_by_key)),
    )


def replay_maturation_events(
    *,
    config: Config,
    maturation_dates: Sequence[date],
    universe: Any,
    target_candidates: set[tuple[str, str]],
    dependencies: ReplayDependencies,
) -> tuple[CandidateEventEvidence, ...]:
    """Replay only locked-v2 event evidence without signals or opportunities."""
    factor_service = dependencies.factor_service_factory(config)
    evidence_by_key: dict[
        tuple[str, str],
        CandidateEventEvidence,
    ] = {}
    for trade_date in maturation_dates:
        snapshot_df = factor_service.build_factor_snapshot(
            universe,
            trade_date=trade_date,
            persist=False,
        )
        for factor in snapshot_df.to_dict(orient="records"):
            candidate_version = factor.get(
                "bottom_divergence_v2_candidate_version"
            )
            key = (str(factor.get("code") or ""), str(candidate_version or ""))
            if key not in target_candidates:
                continue
            events = _event_dates(
                factor=factor,
                code=key[0],
                signal_date=trade_date,
                config=config,
                stock_repository=dependencies.stock_repository,
            )
            current = evidence_by_key.get(key)
            near_date = events["r1"]
            major_date = events["r2"]
            evidence_by_key[key] = CandidateEventEvidence(
                code=key[0],
                candidate_version=key[1],
                near_cleared_event_date=_earliest_date(
                    current.near_cleared_event_date if current else None,
                    near_date,
                ),
                major_breakout_event_date=_earliest_date(
                    current.major_breakout_event_date if current else None,
                    major_date,
                ),
            )
    return tuple(
        evidence_by_key[key] for key in sorted(evidence_by_key)
    )


def _earliest_date(
    first: Optional[date],
    second: Optional[date],
) -> Optional[date]:
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)


class HistoricalReplayService(Protocol):
    """Dependency boundary used by the CLI and deterministic unit tests."""

    def resolve_universe(
        self,
        market: str,
        codes: Optional[list[str]],
    ) -> Any:
        ...

    def list_trade_dates(
        self,
        *,
        date_from: date,
        date_to: date,
        market: str,
        universe: Any,
    ) -> list[date]:
        ...

    def replay(
        self,
        *,
        strategy_version: str,
        config: Config,
        trade_dates: list[date],
        universe: Any,
    ) -> ReplayBatch:
        ...

    def list_future_trade_dates(
        self,
        *,
        after_date: date,
        count: int,
        universe: Any,
    ) -> list[date]:
        ...

    def mature_events(
        self,
        *,
        config: Config,
        maturation_dates: list[date],
        universe: Any,
        target_candidates: set[tuple[str, str]],
    ) -> tuple[CandidateEventEvidence, ...]:
        ...

    def data_version(self) -> str:
        ...

    def universe_identity(self, universe: Any) -> dict:
        ...


def build_isolated_config(
    base_config: Config,
    parameter_snapshot: dict[str, float],
) -> Config:
    """Copy all runtime settings and override only the three v2 grid knobs."""
    return replace(
        base_config,
        bottom_divergence_v2_enabled=True,
        bottom_divergence_v2_cluster_pct=parameter_snapshot["cluster_pct"],
        bottom_divergence_v2_atr_gap_multiplier=parameter_snapshot[
            "atr_gap_multiplier"
        ],
        bottom_divergence_v2_zone_score_min=parameter_snapshot[
            "zone_score_min"
        ],
    )


def _resolve_local_universe(
    db_manager: Any,
    market: str,
    codes: Optional[list[str]],
) -> Any:
    import pandas as pd

    from src.services.universe_service import (
        LocalUniverseNotReadyError,
        UniverseService,
    )

    service = UniverseService(db_manager=db_manager)
    if codes is not None:
        if not codes:
            raise ValidationInputError("EMPTY_UNIVERSE", "universe is empty")
        try:
            resolved = service.resolve_universe(codes)
        except LocalUniverseNotReadyError as exc:
            raise ValidationInputError(
                "EMPTY_UNIVERSE",
                "none of the requested universe codes are available",
            ) from exc
        if getattr(resolved, "empty", False):
            raise ValidationInputError(
                "EMPTY_UNIVERSE",
                "none of the requested universe codes are available",
            )
        return resolved
    rows = db_manager.list_instruments(
        market=market,
        listing_status="active",
        exclude_st=True,
    )
    if not rows:
        raise LocalUniverseNotReadyError(
            f"本地 instrument_master 没有 {market} 活跃股票"
        )
    return service._normalize_universe(pd.DataFrame(rows))


def _list_local_trade_dates(
    db_manager: Any,
    *,
    date_from: date,
    date_to: date,
    universe: Any,
) -> list[date]:
    from sqlalchemy import select

    from src.storage import StockDaily

    codes = sorted(str(code) for code in universe["code"].tolist())
    with db_manager.get_session() as session:
        values = list(session.execute(
            select(StockDaily.date)
            .where(
                StockDaily.code.in_(codes),
                StockDaily.date >= date_from,
                StockDaily.date <= date_to,
            )
            .distinct()
            .order_by(StockDaily.date)
        ).scalars())
    return values


def _list_future_local_trade_dates(
    db_manager: Any,
    *,
    after_date: date,
    count: int,
    universe: Any,
) -> list[date]:
    from sqlalchemy import select

    from src.storage import StockDaily

    codes = sorted(str(code) for code in universe["code"].tolist())
    with db_manager.get_session() as session:
        values = list(session.execute(
            select(StockDaily.date)
            .where(
                StockDaily.code.in_(codes),
                StockDaily.date > after_date,
            )
            .distinct()
            .order_by(StockDaily.date)
            .limit(count)
        ).scalars())
    return values


def _local_data_version(
    db_manager: Any,
    universe: Any,
    date_from: date,
    date_to: date,
) -> str:
    from sqlalchemy import func, select

    from src.storage import StockDaily

    codes = sorted(str(code) for code in universe["code"].tolist())
    with db_manager.get_session() as session:
        count = session.execute(
            select(func.count(StockDaily.id)).where(
                StockDaily.code.in_(codes),
                StockDaily.date >= date_from,
                StockDaily.date <= date_to,
            )
        ).scalar_one()
    return (
        f"stock_daily|first:{date_from.isoformat()}|"
        f"last:{date_to.isoformat()}|bars:{count}"
    )


def _universe_identity(universe: Any) -> dict:
    codes = sorted(str(code) for code in universe["code"].tolist())
    digest = sha256(
        canonical_json_dumps(codes).encode("utf-8")
    ).hexdigest()
    return {"count": len(codes), "codes_sha256": digest}


def _build_default_replay_dependencies(
    config: Config,
    *,
    db_manager: Any = None,
    factor_cache: Any = None,
) -> ReplayDependencies:
    import pandas as pd

    from src.agent.skills.base import SkillManager, load_skill_from_yaml
    from src.core.market_guard import MarketGuardResult
    from src.repositories.stock_repo import StockRepository
    from src.services.factor_service import FactorService
    from src.services.five_layer_pipeline import FiveLayerPipeline
    from src.services.market_environment_engine import MarketEnvironmentEngine
    from src.services.screener_service import ScreenerService
    from src.storage import DatabaseManager

    db_manager = db_manager or DatabaseManager.get_instance()
    repository = StockRepository(db_manager)

    def screener_factory(strategy_version: str) -> tuple[Any, Any]:
        path = (
            V1_STRATEGY_PATH
            if strategy_version == "v1"
            else V2_STRATEGY_PATH
        )
        skill = load_skill_from_yaml(path)
        manager = SkillManager()
        manager.register(skill)
        return (
            ScreenerService(
                skill_manager=manager,
                strategy_names=[skill.name],
            ),
            manager,
        )

    def prior_bars_loader(
        *,
        code: str,
        signal_date: date,
        count: int,
    ) -> list[Any]:
        rows = repository.get_range(
            code,
            signal_date - timedelta(days=90),
            signal_date - timedelta(days=1),
        )
        return sorted(rows, key=lambda item: item.date)[-count:]

    stock_history = SimpleNamespace(
        get_forward_bars=repository.get_forward_bars,
        get_prior_bars=prior_bars_loader,
        get_range=repository.get_range,
    )

    def market_context(
        trade_date: date,
        snapshot_df: Any,
    ) -> tuple[Any, Any]:
        index_code = config.screening_market_guard_index
        rows = repository.get_range(
            index_code,
            trade_date - timedelta(days=240),
            trade_date,
        )
        ordered = sorted(rows, key=lambda item: item.date)
        index_bars = pd.DataFrame(
            [
                {"date": item.date, "close": float(item.close)}
                for item in ordered
            ]
        )
        if len(ordered) >= 100:
            index_price = float(ordered[-1].close)
            index_ma100 = statistics.fmean(
                float(item.close) for item in ordered[-100:]
            )
            is_safe = index_price >= index_ma100
            message = "local stock_daily MA100"
        else:
            index_price = float(ordered[-1].close) if ordered else 0.0
            index_ma100 = 0.0
            is_safe = False
            message = "local index history insufficient; fail closed"
        guard = MarketGuardResult(
            is_safe=is_safe,
            index_code=index_code,
            index_price=index_price,
            index_ma100=index_ma100,
            message=message,
        )
        pct = snapshot_df.get("pct_chg")
        market_stats = {
            "up_count": int((pct > 0).sum()) if pct is not None else 0,
            "down_count": int((pct < 0).sum()) if pct is not None else 0,
            "limit_up_count": int(
                snapshot_df.get("is_limit_up", []).sum()
            )
            if "is_limit_up" in snapshot_df
            else 0,
            "limit_down_count": int(
                snapshot_df.get("is_limit_down", []).sum()
            )
            if "is_limit_down" in snapshot_df
            else 0,
        }
        environment = MarketEnvironmentEngine().assess(
            guard,
            index_bars,
            market_stats,
        )
        return environment, guard

    if factor_cache is not None:
        from .bottom_divergence_v2_performance import (
            CachedValidationFactorService,
        )

        factor_service_factory = lambda isolated: (  # noqa: E731
            CachedValidationFactorService(isolated, factor_cache)
        )
    else:
        factor_service_factory = lambda isolated: FactorService(  # noqa: E731
            db_manager=db_manager,
            config=isolated,
        )

    return ReplayDependencies(
        db_manager=db_manager,
        factor_service_factory=factor_service_factory,
        pipeline=FiveLayerPipeline(),
        screener_factory=screener_factory,
        market_context_provider=market_context,
        stock_repository=stock_history,
    )


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None
