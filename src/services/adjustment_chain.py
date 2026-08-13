# -*- coding: utf-8 -*-
"""按窗口现算后复权因子，并在读取时施加到价格与成交量（gate-3 / Task 1）。

要求复权序列复现真实收益率，即 ``close_adj(t)/close_adj(t-1) == close(t)/pre_close(t)``，
解出后复权累乘因子::

    f(t) = f(t-1) * close(t-1) / pre_close(t)      f(窗口首行) = 1

施加时用的不是 f(t) 本身，而是归一到窗口末日 D 的 ``g(t) = f(t)/f(D)``：

- **为什么可以只看窗口**：g(t) 只依赖 t 与 D 之间的比值，窗口起点之前的一切在相除时
  整体约掉。因此「窗口内现算」与「全历史现算」给出同一个 g，稀疏历史与累乘误差的
  风险敞口被自动限制在窗口内。
- **为什么必须归一**：入场价取自因子快照、前瞻 bar 取自原始表，只有 ``g(D) == 1``
  才能保证入场价等于当日真实可成交价、且与前瞻同尺度。

**不落库。** 日常同步的 ``INSERT OR REPLACE`` 列清单不含 ``adj_factor``
（``src/services/market_data_sync_service.py:360-369`` 的注释已明写会被清成 NULL），
回填进去的因子会被逐步擦掉；而 ``pre_close`` 恰好在那份清单里被保住，且有
``scripts/validate_staging_before_promotion.py`` 守着。所以现算比落库稳。

本模块只负责算和施加，不改数据库，也不决定谁可信——下游门禁读
``adj_factor_source``，在读取侧接上之前不要把 ``pre_close_chain`` 加进信任白名单，
否则检测器会在带除权跳空的原始价上算阻力位，从「诚实地拒绝出信号」退化成「静默算错」。

模块分两层，边界不要混：上半部分（``analyze_window`` / ``apply_adjustment``）是
**纯算术**，契约是「调用方给的是同一口径、按日期升序的单只窗口」；下半部分
（``apply_read_adjustment`` 系列）是**读取路径入口**，额外负责准入判定，其中就包括
``adj_convention`` 口径守卫（见 ``convention_reject_reason``）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── 来源标记 ──────────────────────────────────────────────────────────
# 可信段与被作废段必须用不同的字符串：下游白名单只认前者，后者进白名单等于
# 把一段没有复权的价格当成复权过的用。
TRUSTED_SOURCE = "pre_close_chain"
ANOMALOUS_SOURCE = "pre_close_chain_anomalous"

DATE_COLUMN = "date"
CODE_COLUMN = "code"
CLOSE_COLUMN = "close"
PRE_CLOSE_COLUMN = "pre_close"
ADJ_FACTOR_COLUMN = "adj_factor"
ADJ_FACTOR_SOURCE_COLUMN = "adj_factor_source"

# ── 口径 ──────────────────────────────────────────────────────────────
# ``stock_daily.adj_convention`` 标的是**每一行价格自己**的复权口径
# （``raw`` / ``qfq`` / ``unknown``，见 ``src/storage.py`` 的
# ``VALID_ADJ_CONVENTIONS``）。这里不 import 那个常量：本模块被 spawn 出来的
# 回测子进程直接加载，刻意不依赖 ORM 与数据库。
# ``tests/test_adjustment_chain.py`` 钉住两处取值一致，防止各写各的。
ADJ_CONVENTION_COLUMN = "adj_convention"
RAW_CONVENTION = "raw"

CONVENTION_REJECT_MISSING_COLUMN = "adj_convention_column_missing"
CONVENTION_REJECT_NON_RAW = "adj_convention_not_raw"

# 价格列乘 g(t)。
#
# pre_close 也算价格列：它不跟着缩放就会得到一个自相矛盾的窗口——close 已复权而
# pre_close 还是原始价，任何按 close(t)/pre_close(t) 复算当日收益的消费方都会拿到错数。
# 同步缩放后 pre_close_adj(t) == close_adj(t-1) 恰好成立（这正是不变式想表达、却在
# 原始价上不成立的那个等式），并顺带让本函数幂等：对已复权的窗口再算一遍链，比值恒为
# 1，不会二次施加。
PRICE_COLUMNS = ("open", "high", "low", "close", "pre_close")

# volume 是**除以** g(t)，方向和价格相反：g(t) < 1 的是除权前的 bar，它们的成交量以
# 「老股本」计价，要放大到今天的股本口径才可比。1145 个送转事件会让除权日 volume 台阶式
# 跳变，而 volume_ratio 是「最新量 / 前 5 日均量」（``src/services/factor_service.py:196-198``），
# 不处理会被虚增 2~3 倍，直接进 _compute_liquidity_score 与 risk_flags。
VOLUME_COLUMN = "volume"

# amount **不处理**：价 × 量守恒（price*g 乘 volume/g），本就不受复权影响。
# 这条不是省事，是正确性——单独缩放 amount 会把守恒关系破坏掉。

# ── 越界判定 ──────────────────────────────────────────────────────────
# 两个界的性质完全不同，名字刻意不对称，不要读成一对可以对称调节的上下界。
#
# 下界是**数值下界**，不是业务余量。ratio < 1 意味着 pre_close 高于前一日收盘，而分红、
# 送转、拆股只会把 pre_close 往下调，方向上就不成立；这类缺口的真实来源是新三板期转让
# 方式变更或转板股本重组（实测全部落在北交所 920xxx，主板一个都没有）。
#
# 全表 962 万行实测：pre_close 与前一日收盘要么逐位精确相等（9,582,657 行），要么差额
# 来自真实公司行为——``0 < |差| < 0.005`` 的行数是 **0**，``[0.999, 1.0)`` 连续三个分箱
# 全空。也就是说源数据里根本不存在贴着 1 下方的取整噪声。
#
# 写成 ``1.0 - 1e-9`` 而不是 ``1.0``，**纯粹是浮点表示容差**：ratio 由一次除法得到，
# 数学上等于 1 的比值可能落在 1 的最后几个 ulp 上。**这不是业务余量，不是可调参数。**
# 往下放（例如放到 0.99）会重新放进 4 个已知的方向性错误（920718 / 920242 / 920786 /
# 920946，0.19%~0.46%），它们比交叉验证的最大偏差 0.1145% 还大 1.5~4 倍，而且会变成
# 各自序列上一个**永久**的因子水平误差。
RATIO_DIRECTION_FLOOR = 1.0 - 1e-9

# 上界不是业务规则，而是**数据损坏警戒线**。合法的大比例拆股可以走得很远：实测最大合法
# 值 9.7173（920402 约 10:1 拆股），被接受的比值上沿本来就贴着 3.0000（920870）、
# 2.7586（920152）。既然 3.0000 可以接受，就没有任何原则能拒绝 3.0358（002594 的
# 10 送 8 转增 12 派 39.74，1 股变 3 股）或 3.5010（600733）。20 取在观测分布之外并留
# 约 2 倍余量，只用来兜住量级明显不对的脏数据。**不要把它当成一个可以随手收紧的业务
# 参数**：收紧它等于开始按幅度拒绝合法公司行为。
RATIO_CORRUPTION_CEILING = 20.0

# 断链原因。区分「缺数据」与「比值越界」只为让离线抽查能给出画像，处理方式完全一样：
# 都在该行切段，都不 ffill、不按 1.0 补。
BREAK_MISSING_PREV_CLOSE = "missing_prev_close"
BREAK_MISSING_PRE_CLOSE = "missing_pre_close"
BREAK_RATIO_BELOW_FLOOR = "ratio_below_floor"
BREAK_RATIO_ABOVE_CEILING = "ratio_above_ceiling"


@dataclass(frozen=True)
class ChainBreak:
    """一处断链：第 ``position`` 行与它前一行之间的比值不可信。"""

    position: int
    date: Any
    ratio: Optional[float]
    reason: str


@dataclass(frozen=True, eq=False)
class WindowChain:
    """一个窗口的复权链解算结果。

    ``eq=False``：字段是 Series，默认的逐字段 ``==`` 会在比较两个实例时炸成
    「truth value is ambiguous」。需要比较的调用方自己比 Series。
    """

    #: g(t) = f(t)/f(D)，索引与输入窗口一致；被作废段为 NaN，末行恒为 1.0
    factors: pd.Series
    #: 每行的来源标记：可信段 TRUSTED_SOURCE，被作废段 ANOMALOUS_SOURCE
    sources: pd.Series
    #: 单日复权比值 close(t-1)/pre_close(t)，首行为 1；缺数据处为 NaN。
    #: 越界的比值原样保留在这里，离线抽查要靠它给异常画像，而不是自己再算一遍。
    ratios: pd.Series
    #: 按位置升序的断链清单
    breaks: Tuple[ChainBreak, ...]
    #: 可信段的起始位置（0 表示整窗可信）
    segment_start: int

    @property
    def trusted(self) -> bool:
        """整窗可信 —— 一处断链都没有。

        fail-closed 的判据就是它：窗口内只要有一处断链，调用方就该把这只股票在这个
        回放日标成 unknown，而不是拿可信段凑合用。可信段之所以仍然算出因子并施加，
        是为了让「被拒绝的到底是哪几行」在离线抽查里看得见。
        """
        return not self.breaks


def _usable(values: np.ndarray) -> np.ndarray:
    """None / 0 / 负数 / NaN / inf 一律视为不可用，挡在除法之前。"""
    return np.isfinite(values) & (values > 0.0)


def _validate(df: pd.DataFrame) -> None:
    missing = [
        name for name in (CLOSE_COLUMN, PRE_CLOSE_COLUMN) if name not in df.columns
    ]
    if missing:
        raise ValueError(
            f"复权链需要列 {missing}。缺列时宁可报错也不能按 1.0 走过去："
            "那会让调用方拿到一段没有复权、却带着可信来源标记的价格。"
        )

    # 排序由调用方负责，这里只校验不重排：重排会掩盖上游取数顺序出错这类问题，
    # 而顺序错了算出来的因子是错的却看不出来。
    if DATE_COLUMN in df.columns:
        try:
            ordered = bool(df[DATE_COLUMN].is_monotonic_increasing)
        except TypeError:
            # 日期列混了不可比较的类型，交给调用方去发现，不在这里替它决定。
            ordered = True
        if not ordered:
            raise ValueError("复权链要求窗口按日期升序，且末行即回放日 D")

    # 混进第二只股票时算出来的因子是两条序列拼接的产物，数值上完全说得通，
    # 但没有任何意义，必须当场拦下。
    if CODE_COLUMN in df.columns and df[CODE_COLUMN].nunique(dropna=False) > 1:
        raise ValueError("复权链一次只处理单只股票的窗口")


def _break_reason(
    position: int,
    close: np.ndarray,
    pre_close: np.ndarray,
    ratio: float,
) -> str:
    if not (np.isfinite(close[position - 1]) and close[position - 1] > 0.0):
        return BREAK_MISSING_PREV_CLOSE
    if not (np.isfinite(pre_close[position]) and pre_close[position] > 0.0):
        return BREAK_MISSING_PRE_CLOSE
    if ratio < RATIO_DIRECTION_FLOOR:
        return BREAK_RATIO_BELOW_FLOOR
    return BREAK_RATIO_ABOVE_CEILING


def _reject_already_adjusted(df: pd.DataFrame) -> None:
    """已经复权过的窗口不许再复权一次。

    二次施加会把因子平方，而结果依然「有因子、非空、为正」，能顺利通过下游门禁。
    只认本模块自己写下的两个来源值：``stock_daily`` 原生就带 ``adj_factor_source``
    （legacy_assume_one / fetcher_unset 等），按列是否存在判断会把所有正常输入拦掉。

    因为 ``pre_close`` 也跟着缩放，重复施加在数值上其实是无害的（比值恒为 1），
    但那是巧合带来的宽容，不该拿来当接口契约——真正的问题是调用方搞不清谁已经施加过。
    """
    if ADJ_FACTOR_SOURCE_COLUMN not in df.columns or len(df) == 0:
        return
    if df[ADJ_FACTOR_SOURCE_COLUMN].isin((TRUSTED_SOURCE, ANOMALOUS_SOURCE)).any():
        raise ValueError(
            "该窗口已带本模块的 adj_factor_source，拒绝二次施加复权"
        )


def _empty_chain(df: Optional[pd.DataFrame]) -> WindowChain:
    index = df.index if df is not None else pd.Index([])
    return WindowChain(
        factors=pd.Series(np.array([], dtype=float), index=index, dtype=float),
        sources=pd.Series(np.array([], dtype=object), index=index, dtype=object),
        ratios=pd.Series(np.array([], dtype=float), index=index, dtype=float),
        breaks=(),
        segment_start=0,
    )


def analyze_window(df: pd.DataFrame) -> WindowChain:
    """解算一个窗口的复权链。

    ``df`` 是**单只股票、按日期升序**的窗口，末行即回放日 D。

    断链处按位置切段，只作废断链点**之前**的前缀，断链点及之后照常施加。依据是
    g(t) = f(t)/f(D) 的约掉性质：断链点之后的比值不受它影响，而之前的那段与 D 之间
    已经没有可信的换算关系。整只作废会把一次转板重组的代价扩大到八年历史。

    缺 ``pre_close``（非首行）与缺前一日 ``close`` 同样按断链处理。**绝不 ffill、
    绝不按 1.0 补**——那会得到一段部分复权的序列，比拒绝更糟：它有因子、非空、为正，
    能顺利通过下游门禁。
    """
    if df is None or len(df) == 0:
        return _empty_chain(df)

    _validate(df)

    count = len(df)
    close = pd.to_numeric(df[CLOSE_COLUMN], errors="coerce").to_numpy(dtype=float)
    pre_close = pd.to_numeric(
        df[PRE_CLOSE_COLUMN], errors="coerce"
    ).to_numpy(dtype=float)

    # ratio[i] 是第 i 行与第 i-1 行之间的复权增量；首行没有前一行，取 1。
    ratio = np.ones(count, dtype=float)
    broken = np.zeros(count, dtype=bool)
    if count > 1:
        link_ok = _usable(close[:-1]) & _usable(pre_close[1:])
        # 分母先夹住再除：pre_close 不可用时 link_ok 已经是 False，这里只是不让
        # numpy 在一个必然被丢弃的分支上抛除零警告。
        denominator = np.where(_usable(pre_close[1:]), pre_close[1:], 1.0)
        ratio[1:] = np.where(link_ok, close[:-1] / denominator, np.nan)
        out_of_bounds = link_ok & (
            (ratio[1:] < RATIO_DIRECTION_FLOOR)
            | (ratio[1:] > RATIO_CORRUPTION_CEILING)
        )
        broken[1:] = (~link_ok) | out_of_bounds

    positions = np.flatnonzero(broken)
    segment_start = int(positions[-1]) if positions.size else 0

    factors = np.full(count, np.nan, dtype=float)
    segment = ratio[segment_start:].copy()
    segment[0] = 1.0  # 段首重新起链：断掉的那个比值不参与累乘
    cumulative = np.cumprod(segment)
    # 除以段末（即 D）的累乘值，末行因此恒为 1.0，且累乘误差整体约掉。
    factors[segment_start:] = cumulative / cumulative[-1]

    sources = np.where(
        np.arange(count) >= segment_start, TRUSTED_SOURCE, ANOMALOUS_SOURCE
    )
    dates = (
        df[DATE_COLUMN].to_numpy() if DATE_COLUMN in df.columns else df.index.to_numpy()
    )
    breaks = tuple(
        ChainBreak(
            position=int(position),
            date=dates[position],
            ratio=(
                float(ratio[position]) if np.isfinite(ratio[position]) else None
            ),
            reason=_break_reason(int(position), close, pre_close, ratio[position]),
        )
        for position in positions
    )

    return WindowChain(
        factors=pd.Series(factors, index=df.index, dtype=float),
        sources=pd.Series(sources, index=df.index, dtype=object),
        ratios=pd.Series(ratio, index=df.index, dtype=float),
        breaks=breaks,
        segment_start=segment_start,
    )


def compute_window_factors(df: pd.DataFrame) -> pd.Series:
    """算出窗口内每一行的 ``g(t) = f(t)/f(D)``。

    末行恒为 1.0；被作废段为 NaN。索引与输入一致。
    """
    return analyze_window(df).factors


def apply_adjustment(
    df: pd.DataFrame,
    *,
    chain: Optional[WindowChain] = None,
) -> pd.DataFrame:
    """把 ``g(t) = f(t)/f(D)`` 施加到窗口，返回新的 DataFrame（不改入参）。

    价格列乘 g、``volume`` 除以 g、``amount`` 不动，并写入 ``adj_factor`` 与
    ``adj_factor_source``。**只写内存，不落库。**

    被作废段原样保留数值（缩放系数取 1.0），因子留 NaN、来源标 ANOMALOUS_SOURCE ——
    「没复权」和「复权过」必须在同一张表里可区分。

    传 ``chain`` 可以复用已经算好的解算结果，避免离线抽查为了拿画像再算一遍。
    """
    adjusted = df.copy() if df is not None else pd.DataFrame()
    _reject_already_adjusted(adjusted)
    if len(adjusted) == 0:
        adjusted[ADJ_FACTOR_COLUMN] = pd.Series(dtype=float, index=adjusted.index)
        adjusted[ADJ_FACTOR_SOURCE_COLUMN] = pd.Series(
            dtype=object, index=adjusted.index
        )
        return adjusted

    if chain is None:
        chain = analyze_window(adjusted)
    elif len(chain.factors) != len(adjusted):
        raise ValueError("传入的复权链与窗口行数不一致，拒绝按位置硬套")

    factors = chain.factors.to_numpy(dtype=float)
    # 用 ndarray 而不是 Series 做乘除：Series 会按索引对齐，调用方传进来一个未
    # reset_index 的切片就会静默错位。
    scale = np.where(np.isnan(factors), 1.0, factors)

    for column in PRICE_COLUMNS:
        if column in adjusted.columns:
            values = pd.to_numeric(
                adjusted[column], errors="coerce"
            ).to_numpy(dtype=float)
            adjusted[column] = values * scale
    if VOLUME_COLUMN in adjusted.columns:
        values = pd.to_numeric(
            adjusted[VOLUME_COLUMN], errors="coerce"
        ).to_numpy(dtype=float)
        adjusted[VOLUME_COLUMN] = values / scale

    adjusted[ADJ_FACTOR_COLUMN] = factors
    adjusted[ADJ_FACTOR_SOURCE_COLUMN] = chain.sources.to_numpy()
    return adjusted


# ── 读取路径接入（Task 2）────────────────────────────────────────────────
# 下面几个函数是三条读取路径共用的入口。它们不改上面的任何判定规则，只负责把
# 「已经施加过怎么办」「解算不出来怎么办」「这段价格是不是同一个口径」「锚在窗口
# 另一端怎么办」这四件事收敛成一份实现——三条路径各写一遍才是两套口径并存的
# 真正来源。
#
# 口径守卫（``convention_reject_reason``）只在这一层生效，不下沉到
# ``analyze_window``：上面那一层是纯算术，契约是「调用方给的是同一口径的窗口」。


def is_adjusted(df: Optional[pd.DataFrame]) -> bool:
    """窗口是否已经由本模块施加过复权。

    只认本模块写下的两个来源值。``stock_daily`` 原生就带 ``adj_factor_source``
    （``legacy_assume_one`` / ``fetcher_unset`` / ``tushare_native``），按列是否
    存在判断会把所有未施加的窗口误判成已施加。
    """
    if df is None or len(df) == 0:
        return False
    if ADJ_FACTOR_SOURCE_COLUMN not in df.columns:
        return False
    return bool(
        df[ADJ_FACTOR_SOURCE_COLUMN]
        .isin((TRUSTED_SOURCE, ANOMALOUS_SOURCE))
        .any()
    )


def convention_reject_reason(df: Optional[pd.DataFrame]) -> Optional[str]:
    """窗口能不能当作单一 ``raw`` 口径来成链；``None`` 表示可以。

    **为什么读取侧必须看这一列。** 本模块按 ``close(t-1)/pre_close(t)`` 成链，
    这个式子只在两行同口径时才有意义。``data_provider/efinance_fetcher.py``
    的降级路径写死 ``fqt=1``（前复权），它覆写某一行之后，那一行会变成
    「qfq 的 close + 上一次同步留下的 raw ``pre_close``」，而口径标签落
    ``unknown``。混着成链算出来的因子不会报错、不会越界、也不会断链——它只是
    **错**。更糟的是窗口归一到 D：D 恰好是这样一行时，整个窗口的价格水平一起偏。
    今天不出错只因为 staging 晋升后全库恰好都是 ``raw``；三条写入路径小心维护
    的这一列，在读取侧一个消费方都没有，正是「改动前是死条款、改动后变成活漏洞」
    的下一个实例。

    **缺列与 NULL / ``unknown`` / ``qfq`` 同等对待，一律拒绝。** 这个取舍是本
    守卫的核心：

    - 「缺列即放行」会让整道守卫被「忘了 SELECT 这一列」这**一个动作**关掉，
      而且不报错、不留痕，只是悄悄退回混口径成链。守卫要防的失效模式恰好就长
      这个样子，用同样形状的漏洞去实现它没有意义。
    - 「缺列即拒绝」的代价有界且响：命中时走 ``mark_unadjustable``——价格原样
      保留、因子写 NaN、来源写 anomalous，下游按 unknown 处理，也就是退化回
      gate-3 之前那个安全状态；不抛异常、不中断流程，并且首次命中打一条
      WARNING 指名是缺列还是口径不符，不至于只表现为「这只标的永远
      ``adjustment_unknown``」。

    严格只落在**读取路径入口**（``apply_read_adjustment`` /
    ``apply_read_adjustment_from_anchor``）。``analyze_window`` 与
    ``apply_adjustment`` 是纯算术，契约是「调用方给的是同一口径的窗口」，
    不看这一列：离线抽查脚本已经在 SQL 侧 ``WHERE adj_convention='raw'``
    过滤过，内存里人工构造的窗口也不该被迫编造一个口径标签。

    取值按**精确相等**判定，不做 strip / lower 归一化：需要归一化才认得的值
    不可能出自本仓库的三条写入路径，替它猜等于回到「按数据源名字猜口径」。
    """
    if df is None or len(df) == 0:
        return None
    if ADJ_CONVENTION_COLUMN not in df.columns:
        return CONVENTION_REJECT_MISSING_COLUMN
    if bool(df[ADJ_CONVENTION_COLUMN].eq(RAW_CONVENTION).all()):
        return None
    return CONVENTION_REJECT_NON_RAW


# 首次命中才告警：混口径一旦发生就会命中成千上万个 (code, 回放日) 组合，
# 逐次打日志会把有用信息淹掉。这里只求「操作员知道发生了什么、从哪查起」。
_WARNED_CONVENTION_REASONS: set = set()


def _warn_convention_once(reason: str, detail: str) -> None:
    if reason in _WARNED_CONVENTION_REASONS:
        return
    _WARNED_CONVENTION_REASONS.add(reason)
    logger.warning(
        "复权链拒绝该窗口（%s）：%s。这些行按未复权处理，"
        "下游会判 adjustment_unknown，不会静默按原样成链。",
        reason,
        detail,
    )


def _convention_blocks_adjustment(df: pd.DataFrame) -> bool:
    reason = convention_reject_reason(df)
    if reason is None:
        return False
    if reason == CONVENTION_REJECT_MISSING_COLUMN:
        _warn_convention_once(
            reason,
            f"取数结果里没有 {ADJ_CONVENTION_COLUMN} 列，"
            "读取路径需要把它加进取数列清单",
        )
    else:
        values = df[ADJ_CONVENTION_COLUMN]
        offenders = sorted(
            {str(value) for value in values[~values.eq(RAW_CONVENTION)]}
        )
        _warn_convention_once(
            reason,
            f"窗口内出现非 {RAW_CONVENTION} 口径 {offenders}",
        )
    return True


def mark_unadjustable(df: pd.DataFrame) -> pd.DataFrame:
    """解算不出复权链时的 fail-closed 标记，返回新的 DataFrame。

    价格与成交量**原样保留**，但因子写 NaN、来源写 ANOMALOUS_SOURCE。

    不能「什么都不做」：那样窗口会保留取数时带来的 ``adj_factor_source``
    （例如 ``tushare_native``，它已经在 ``factor_service`` 的信任白名单里），
    于是一段没有施加复权的原始价会顶着可信标记穿过门禁——正是本计划第 2 节
    要消灭的「静默算错」。写成 NaN + anomalous 后，下游按 4.5 判为 unknown。
    """
    marked = df.copy()
    marked[ADJ_FACTOR_COLUMN] = np.nan
    marked[ADJ_FACTOR_SOURCE_COLUMN] = ANOMALOUS_SOURCE
    return marked


def apply_read_adjustment(df: pd.DataFrame) -> pd.DataFrame:
    """读取路径入口：窗口按日期升序，**末行即回放日 D**。

    幂等：已施加过的窗口原样返回，不会把因子平方。
    缺 ``pre_close`` 列（只可能是内存里人工构造的窗口——三条取数路径都带这列）
    按 fail-closed 标记，不施加也不打可信标记。

    ``adj_convention`` 不是整窗 ``raw``（含缺列）时同样 fail-closed，理由与
    取舍见 ``convention_reject_reason``：混口径成链算出来的因子只是错，不会
    自己暴露。这里是**整窗**拒绝而不是切段——``adj_convention`` 描述的是这一行
    价格本身的口径，一行口径不对意味着它的 ``close`` 与 ``pre_close`` 之间、
    以及它与相邻行之间的关系全部失效，没有一个「之后照常施加」的安全边界。

    窗口内部的断链不在这里拦：``analyze_window`` 已经按位置切段，被作废段的
    因子是 NaN、来源是 anomalous，下游据此判 unknown。整窗拒绝反而会把一次
    转板重组的代价扩大到整段历史。
    """
    if df is None or len(df) == 0 or is_adjusted(df):
        return df
    if PRE_CLOSE_COLUMN not in df.columns:
        return mark_unadjustable(df)
    if _convention_blocks_adjustment(df):
        return mark_unadjustable(df)
    return apply_adjustment(df)


def apply_read_adjustment_from_anchor(df: pd.DataFrame) -> pd.DataFrame:
    """前瞻窗口入口：窗口按日期升序，**首行即锚 D**。

    前瞻 bar 全部晚于 D，``apply_adjustment`` 归一到自己的末行会得到
    ``g(t) = f(t)/f(D+20)``，与入场价所在的 D 尺度对不上。这里要的是
    ``g(t) = f(t)/f(D)``：先照常解算出 ``f(t)/f(末行)``，再整体除以首行的值，
    末行因子于是约掉——

        [f(t)/f(末行)] / [f(D)/f(末行)] = f(t)/f(D)

    因此首行恒为 1（锚点价不动，入场价仍是当日真实可成交价），除权日之后的
    bar 被乘上 > 1 的因子，与除权前的入场价重新可比。

    与末行归一不同，这里**整窗 fail-closed**：断链会让 ``analyze_window``
    作废包含首行在内的整个前缀，锚点本身的因子成了 NaN，剩下的段再没有任何
    可信的换算关系能接回 D。

    口径守卫同 ``apply_read_adjustment``：``adj_convention`` 不是整窗 ``raw``
    （含缺列）即 fail-closed。
    """
    if df is None or len(df) == 0 or is_adjusted(df):
        return df
    if PRE_CLOSE_COLUMN not in df.columns:
        return mark_unadjustable(df)
    if _convention_blocks_adjustment(df):
        return mark_unadjustable(df)
    chain = analyze_window(df)
    if not chain.trusted:
        return mark_unadjustable(df)
    anchor = float(chain.factors.iloc[0])
    return apply_adjustment(
        df, chain=replace(chain, factors=chain.factors / anchor)
    )
