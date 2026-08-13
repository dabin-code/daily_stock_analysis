# -*- coding: utf-8 -*-
"""逐日收益引擎：把设计稿 8.2c 的三组公式落成纯函数。

本模块只做算术，不查库、不读配置。持仓集合 `H(d,k)`、交易日历、上市主数据都由
调用方传入——8.2b 的持仓状态机依赖 `daily_price_limit` 与 `suspension_history`，
这两张表本部署没有，状态机因此**不在这里实现**。

## 三段公式与自检恒等式

入场 `open(T+1)`，出场 `open(T+1+k)`，两端都用开盘价：

| 持有日 | 公式 |
| --- | --- |
| 首日 `d = T+1` | `close(T+1) / open(T+1) - 1` |
| 延续日 `T+2 <= d <= T+k` | `close(d) / close(d-1) - 1` |
| 出场日 `d = T+1+k` | `open(d) / close(d-1) - 1` |
| 停牌日 | `0` |

三段连乘恰好等于 `open(T+1+k) / open(T+1) - 1`，这是实现的自检条件。
首日**不能**写成 `close(T+1)/close(T) - 1`：信号日收盘到次日开盘那段跳空在下单
时点尚不可得，用它等于把未来信息计入收益，恒等式也会随之破掉。

## 为什么必须先复权

`close(d)/close(d-1)` 会跨过分红与送转日。原始价上每个除权日都会凭空出现一段
假跌，而它不会报错、不会越界，只是错。持仓窗口因此整体复权到**入场日**的尺度：
窗口从入场日向后推进，窗口内所有价格都要与入场价可比，这正是
`apply_read_adjustment_from_anchor` 的契约（首行即锚，锚行因子恒为 1）。

复权守卫拒绝窗口时（`adj_convention` 非整窗 `raw`、缺 `pre_close`、链路断裂），
本模块抛 `UnadjustableWindowError` 而不是退回原始价：带着假跌的收益序列在数值上
完全说得通，静默返回它比没有收益更糟。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.services.adjustment_chain import (
    ADJ_FACTOR_SOURCE_COLUMN,
    TRUSTED_SOURCE,
    apply_read_adjustment,
    apply_read_adjustment_from_anchor,
)

CODE_COLUMN = "code"
DATE_COLUMN = "date"
OPEN_COLUMN = "open"
CLOSE_COLUMN = "close"
VOLUME_COLUMN = "volume"
LIST_DATE_COLUMN = "list_date"
DELIST_DATE_COLUMN = "delist_date"

#: `U(d)` 的「上市满 N 个交易日」阈值。新股上市初期波动极端，会污染中位数。
DEFAULT_MIN_LISTED_TRADING_DAYS = 60


class UnadjustableWindowError(RuntimeError):
    """窗口无法复权到入场日尺度——fail-closed 出口。

    抛异常而不是返回一个「带不可信标记的值」：后者要靠每个消费方记得去看那个
    标记，而复权算错的收益序列本身完全通得过下游门禁。
    """


def _numeric(frame: pd.DataFrame, column: str) -> np.ndarray:
    return pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float)


def _require_trusted(frame: pd.DataFrame, detail: str) -> None:
    source = frame.get(ADJ_FACTOR_SOURCE_COLUMN)
    if source is None or not bool(source.eq(TRUSTED_SOURCE).all()):
        raise UnadjustableWindowError(
            f"{detail}：复权守卫拒绝了该窗口，拒绝在原始价上给出逐日收益"
        )


def _suspension_mask(frame: pd.DataFrame) -> np.ndarray:
    """停牌代理：当日 `volume` 缺失或为 0 即视为停牌。

    **这是替代口径，不是设计稿的原意。** 8.2b 要求用 `suspension_history` 判定，
    那张表属 Tushare 付费档，本部署没有。成交量为 0 与停牌并不等价（也可能是
    数据缺失），沿用这条判据前先确认数据源是否已经能给出权威停牌区间。
    """
    if VOLUME_COLUMN not in frame.columns:
        raise ValueError(f"缺少 {VOLUME_COLUMN} 列，无法判定停牌日")
    volume = _numeric(frame, VOLUME_COLUMN)
    return ~(np.isfinite(volume) & (volume > 0.0))


def _as_timestamp(value: Any) -> Optional[pd.Timestamp]:
    if value is None:
        return None
    stamp = pd.Timestamp(value)
    return None if pd.isna(stamp) else stamp


@dataclass(frozen=True)
class HoldingDailyReturns:
    """一笔持仓从 T+1 到 T+1+k 的逐日收益。"""

    dates: Tuple[Any, ...]
    returns: Tuple[float, ...]
    #: 复权到入场日尺度后的两端开盘价，恒等式自检用
    entry_open: float
    exit_open: float
    suspended_days: int

    @property
    def open_to_open_return(self) -> float:
        return self.exit_open / self.entry_open - 1.0


def compute_holding_daily_returns(
    window: pd.DataFrame,
    *,
    holding_days: int,
) -> HoldingDailyReturns:
    """算出一笔持仓的逐日收益 `r_i(d)`。

    `window` 是**单只股票、按日期升序**的持仓窗口，首行即入场日 T+1、末行即出场日
    T+1+k，因此行数恒为 `holding_days + 1`。首行是入场日这件事无法从数据里验证，
    由调用方保证。

    窗口先整体复权到首行（入场日）尺度，之后三段公式都在复权价上计算，
    延续日的 `close(d)/close(d-1)` 因此不再包含除权假跌。
    """
    if holding_days < 1:
        raise ValueError("holding_days 至少为 1")
    expected_rows = holding_days + 1
    if window is None or len(window) != expected_rows:
        raise ValueError(
            f"持仓窗口应恰好有 holding_days + 1 = {expected_rows} 行"
            "（T+1 到 T+1+k），少一行或多一行都意味着调用方的窗口取错了"
        )
    missing = [
        name
        for name in (DATE_COLUMN, OPEN_COLUMN, CLOSE_COLUMN, VOLUME_COLUMN)
        if name not in window.columns
    ]
    if missing:
        raise ValueError(f"持仓窗口缺少列 {missing}")

    ordered = window.reset_index(drop=True)
    adjusted = apply_read_adjustment_from_anchor(ordered)
    _require_trusted(adjusted, "持仓窗口")

    opens = _numeric(adjusted, OPEN_COLUMN)
    closes = _numeric(adjusted, CLOSE_COLUMN)
    for name, values in ((OPEN_COLUMN, opens), (CLOSE_COLUMN, closes)):
        if not bool((np.isfinite(values) & (values > 0.0)).all()):
            raise ValueError(f"持仓窗口的 {name} 存在缺失或非正值，无法给出逐日收益")

    count = len(adjusted)
    returns = np.empty(count, dtype=float)
    returns[0] = closes[0] / opens[0] - 1.0
    if count > 2:
        returns[1:count - 1] = closes[1:count - 1] / closes[0:count - 2] - 1.0
    returns[count - 1] = opens[count - 1] / closes[count - 2] - 1.0

    suspended = _suspension_mask(adjusted)
    returns[suspended] = 0.0

    return HoldingDailyReturns(
        dates=tuple(adjusted[DATE_COLUMN].tolist()),
        returns=tuple(float(value) for value in returns),
        entry_open=float(opens[0]),
        exit_open=float(opens[count - 1]),
        suspended_days=int(suspended.sum()),
    )


@dataclass(frozen=True)
class PortfolioDailyReturns:
    """`{R(d,k)}`：等权组合的逐日收益序列。"""

    dates: Tuple[Any, ...]
    returns: Tuple[float, ...]
    position_counts: Tuple[int, ...]
    empty_days: int

    @property
    def empty_day_share(self) -> float:
        return self.empty_days / len(self.dates) if self.dates else 0.0


def compute_portfolio_daily_returns(
    holdings: Mapping[Any, Iterable[str]],
    stock_returns: Mapping[Any, Mapping[str, float]],
    *,
    dates: Optional[Sequence[Any]] = None,
) -> PortfolioDailyReturns:
    """`R(d,k) = mean_{i in H(d,k)} r_i(d)`，等权。

    `H(d,k)` 由调用方给出并在这里**去重**：同一只股票在 k 日窗口内多次发出信号
    只算一份仓位，按信号等权等价于给高频复选的股票加杠杆。

    空仓日记 `R(d,k) = 0`（等价于持有现金）并保留在序列里，不剔除——剔除会让样本
    系统性偏向有信号的时段，而信号密度本身与市场环境相关。空仓日占比由
    `empty_day_share` 报出。

    持仓缺当日收益时抛错而不是跳过：跳过一只等于当天把它的权重悄悄摊给其余持仓，
    相当于一次没有发生的调仓。
    """
    axis = list(holdings.keys()) if dates is None else list(dates)
    if dates is None:
        axis.sort()

    values: list = []
    counts: list = []
    for day in axis:
        members = sorted(set(holdings.get(day, ())))
        if not members:
            values.append(0.0)
            counts.append(0)
            continue
        day_returns = stock_returns.get(day) or {}
        total = 0.0
        for code in members:
            if code not in day_returns:
                raise ValueError(f"{day} 的持仓 {code} 没有逐日收益，拒绝按缺失处理")
            value = float(day_returns[code])
            if not math.isfinite(value):
                raise ValueError(f"{day} 的持仓 {code} 逐日收益非有限值")
            total += value
        values.append(total / len(members))
        counts.append(len(members))

    return PortfolioDailyReturns(
        dates=tuple(axis),
        returns=tuple(values),
        position_counts=tuple(counts),
        empty_days=sum(1 for count in counts if count == 0),
    )


@dataclass(frozen=True)
class MarketMedianBenchmark:
    """`{B(d)}`：市场中位数基准序列。"""

    dates: Tuple[Any, ...]
    #: `U(d)` 为空的日子收益未定义（None），不记 0——记 0 等于捏造一个走平的市场
    returns: Tuple[Optional[float], ...]
    universe_sizes: Tuple[int, ...]
    #: 在 `stock_daily` 有价但 `instrument_master` 无主数据的代码，整只剔除
    excluded_unmastered_codes: frozenset
    #: 被复权守卫判为不可信、因而不参与中位数的行数
    untrusted_rows: int


def _listing_windows(listings: pd.DataFrame) -> dict:
    missing = [
        name
        for name in (CODE_COLUMN, LIST_DATE_COLUMN, DELIST_DATE_COLUMN)
        if name not in listings.columns
    ]
    if missing:
        raise ValueError(f"上市主数据缺少列 {missing}")
    windows = {}
    for record in listings.to_dict("records"):
        list_date = _as_timestamp(record[LIST_DATE_COLUMN])
        if list_date is None:
            # `list_date` 为空时条件 1 无从判定，与无主数据同等对待。
            continue
        windows[str(record[CODE_COLUMN])] = (
            list_date,
            _as_timestamp(record[DELIST_DATE_COLUMN]),
        )
    return windows


def _listed_trading_days(
    calendar: np.ndarray,
    list_date: pd.Timestamp,
    day: pd.Timestamp,
) -> int:
    """`[list_date, d]` 区间内的交易日数量，两端闭合。"""
    start = int(np.searchsorted(calendar, np.datetime64(list_date), side="left"))
    end = int(np.searchsorted(calendar, np.datetime64(day), side="right"))
    return max(end - start, 0)


def compute_market_median_benchmark(
    prices: pd.DataFrame,
    listings: pd.DataFrame,
    *,
    trading_days: Sequence[Any],
    dates: Optional[Sequence[Any]] = None,
    min_listed_trading_days: int = DEFAULT_MIN_LISTED_TRADING_DAYS,
) -> MarketMedianBenchmark:
    """`B(d) = median_{i in U(d)} [ close(d)/close(d-1) - 1 ]`。

    **基准逐日都用延续日公式，没有入场日与出场日的分段。** 基准是一个持续持有的
    参照物，不存在成交时点。这是策略腿与基准腿**唯一**允许的公式差异，代价是首日
    与出场日的比较不完全同口径（量级为两个半日收益）。除此之外两腿必须逐字对齐，
    包括都在复权价上计算——归一常数在相邻日比值里约掉，因此基准腿按窗口末行归一
    与策略腿按入场日归一给出逐位相同的逐日收益，选前者只是为了让一处断链作废前缀
    而不是整只股票。

    `U(d)` 三条缺一不可：

    1. `d` 日在市：`list_date <= d` 且（`delist_date` 为空或 `d < delist_date`）。
    2. `d` 日有成交：`stock_daily` 有行且非停牌（停牌判据见 `_suspension_mask`）。
    3. 上市满 `min_listed_trading_days` 个交易日。

    `close(d-1)` 取该股票在 `prices` 里的**上一行**，即上一个有行情的交易日。
    """
    if min_listed_trading_days < 1:
        raise ValueError("min_listed_trading_days 至少为 1")
    missing = [
        name
        for name in (CODE_COLUMN, DATE_COLUMN, CLOSE_COLUMN, VOLUME_COLUMN)
        if name not in prices.columns
    ]
    if missing:
        raise ValueError(f"行情数据缺少列 {missing}")

    calendar = np.array(
        sorted({pd.Timestamp(day).to_datetime64() for day in trading_days}),
        dtype="datetime64[ns]",
    )
    windows = _listing_windows(listings)

    all_codes = {str(code) for code in prices[CODE_COLUMN].unique()}
    # `stock_daily` 有价但主数据缺失的代码没有 `list_date`，条件 1 无从判定。
    # 不从行情首行倒推上市日：那是猜，且新股上市初期恰恰是条件 3 要挡的那段。
    unmastered = frozenset(code for code in all_codes if code not in windows)

    daily_returns: dict = {}
    untrusted_rows = 0
    for code, group in prices.groupby(CODE_COLUMN, sort=True):
        code = str(code)
        if code not in windows:
            continue
        ordered = group.sort_values(DATE_COLUMN).reset_index(drop=True)
        if ordered.empty:
            continue
        adjusted = apply_read_adjustment(ordered)
        source = adjusted.get(ADJ_FACTOR_SOURCE_COLUMN)
        trusted = (
            source.eq(TRUSTED_SOURCE).to_numpy(dtype=bool)
            if source is not None
            else np.zeros(len(adjusted), dtype=bool)
        )
        untrusted_rows += int((~trusted).sum())

        closes = _numeric(adjusted, CLOSE_COLUMN)
        suspended = _suspension_mask(adjusted)
        list_date, delist_date = windows[code]
        day_values = adjusted[DATE_COLUMN].tolist()

        for position in range(1, len(adjusted)):
            # 前一行不可信时这一步的比值跨过了一处无法解释的跳变，整格丢弃。
            if not (trusted[position] and trusted[position - 1]):
                continue
            if suspended[position]:
                continue
            previous = closes[position - 1]
            current = closes[position]
            if not (np.isfinite(previous) and previous > 0.0 and np.isfinite(current)):
                continue
            day = _as_timestamp(day_values[position])
            if day is None or day < list_date:
                continue
            if delist_date is not None and day >= delist_date:
                continue
            if _listed_trading_days(calendar, list_date, day) < min_listed_trading_days:
                continue
            daily_returns.setdefault(day_values[position], []).append(
                float(current / previous - 1.0)
            )

    axis = (
        sorted(prices[DATE_COLUMN].unique().tolist())
        if dates is None
        else list(dates)
    )
    values: list = []
    sizes: list = []
    for day in axis:
        members = daily_returns.get(day, [])
        sizes.append(len(members))
        values.append(float(np.median(members)) if members else None)

    return MarketMedianBenchmark(
        dates=tuple(axis),
        returns=tuple(values),
        universe_sizes=tuple(sizes),
        excluded_unmastered_codes=unmastered,
        untrusted_rows=untrusted_rows,
    )
