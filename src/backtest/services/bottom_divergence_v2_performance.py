# -*- coding: utf-8 -*-
"""Bounded-memory caches and resumable artifacts for validation replay."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from concurrent.futures import ProcessPoolExecutor
import gzip
import hashlib
import multiprocessing
import os
from pathlib import Path
import pickle
from tempfile import TemporaryDirectory
import time
from typing import Any, Callable, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from src.indicators.causal_bottom_divergence_detector import (
    ALGORITHM_VERSION as CAUSAL_ALGORITHM_VERSION,
)
from src.indicators.resistance_zone_detector import (
    ALGORITHM_VERSION as ZONE_ALGORITHM_VERSION,
)
from src.services.adjustment_chain import apply_read_adjustment

from .bottom_divergence_v2_checkpoint import (  # noqa: F401
    DEFAULT_V1_STRATEGY_PATH,
    DEFAULT_V2_STRATEGY_PATH,
    CanonicalCheckpointStore,
    CheckpointCorruptionError,
    CheckpointMismatchError,
    validation_checkpoint_config_hash,
)
from .bottom_divergence_v2_dataset import iter_query_batches
from .bottom_divergence_v2_report import canonical_json_dumps
from .bottom_divergence_v2_validation import canonical_parameter_hash


FROZEN_EVIDENCE_ALGORITHM_VERSION = (
    f"{CAUSAL_ALGORITHM_VERSION}+{ZONE_ALGORITHM_VERSION}"
)

# 冻结分区的磁盘布局版本，进分区身份段。改布局必须 bump，否则新代码会拿旧
# 布局的文件当新布局读——`evaluated` 从共享分区搬进按参数哈希分片的文件后，
# 旧文件里那一份会被当成「没算过」而重算，或更糟：被当成分片读出错的键。
# bump 的代价只是让旧缓存目录整体失效、重算一次。
FROZEN_PARTITION_LAYOUT_VERSION = "sharded-evaluated-v1"

# 分区文件的 gzip 级别。`gzip.open` 默认 9，实测在真实分区上压缩占 dump 总耗时
# 的 83%（0.223s / 0.268s）；降到 6 只让文件大 0.7%（1.757 -> 1.769 MB）却把
# 压缩耗时腰斩（0.223 -> 0.120s）。级别 1 更快（0.057s）但文件大 17.6%
# （2.066 MB），在分片之后 dump 已经不是瓶颈，不值得为它多占磁盘。
# 解压耗时与级别无关（三档实测均为 0.018-0.020s），所以降级别不拖慢读回。
PARTITION_COMPRESS_LEVEL = 6

# base 快照的算法版本，**手工维护**，必须保持文件名安全（只用字母、数字、`-`）。
#
# base 快照的取值由四样东西决定：配置、universe、bar 数据、以及计算它的代码。
# 前三样分别由 `_base_config_hash`、universe 指纹、`data_version` 覆盖；
# 第四样没有任何自动来源，只能靠这个常量。证据层用
# `FROZEN_EVIDENCE_ALGORITHM_VERSION` 解决同一个问题，但那是两个 v2 检测器的
# 版本号，而 base pass 强制关闭 v2、跑的是另一组检测器，对它没有代表性。
#
# 在缓存目录还是进程内临时目录的年代，这个缺口是死条款：目录随进程消失，
# 改了代码也不可能读到旧快照。`cache_directory` 一旦可以指向持久化目录，
# 它立刻变成活漏洞——**改了下列任何一处却没 bump，第二次运行会静默拿旧算法
# 算出的因子当新算法的结果用**。不会报错，只会算错。
#
# 必须手工 bump 的改动（判据：会改变 base 快照内容、却不体现在
# `BASE_FACTOR_CONFIG_FIELDS` / universe / `data_version` 里）：
#
# 1. `FactorService.build_factor_snapshot_from_groups` 在 base pass 上走到的
#    任何计算，包括 `_compute_extended_factors` 及它调用的
#    `_compute_ma100_factors` / `_compute_gap_limit_factors` /
#    `_compute_macd_divergence_factors` / `_compute_trendline_factors` /
#    `_compute_pattern_123_factors` / `_compute_bottom_divergence_factors` /
#    `_compute_shrink_pullback_factors` /
#    `_compute_ma100_low123_combined_factors` /
#    `_compute_ma100_60min_combined_factors`，以及各类打分、风险标记，
#    和输出列的增加、删除、改名、改 dtype。
# 2. base pass 用到的检测器实现：`MABreakoutDetector`、`GapDetector`、
#    `LimitUpDetector`、`DivergenceDetector`、`TrendlineDetector`、
#    `PatternDetector`、`Low123TrendlineDetector`、
#    `BottomDivergenceBreakoutDetector`、`ShrinkPullbackDetector`。
#    （`CausalBottomDivergenceDetector` 与阻力区检测器不在此列——base pass
#    关闭 v2，它们归 `FROZEN_EVIDENCE_ALGORITHM_VERSION` 管。）
# 3. 写死在代码里的阈值与模块级常量，例如 `factor_service.py` 的
#    `_MA100_60MIN_*`。它们不在 `Config` 里，白名单键对它们完全失明。
# 4. `src/services/adjustment_chain.py` 的复权算法。`adj_apply_on_read`
#    只覆盖开关，不覆盖算法本身；复权改了，喂进 base pass 的每个价格都变。
# 5. 本文件里决定喂什么给 base pass 的代码：`_window` 的取窗规则、
#    `_compact_bar_frame` 的列处理、以及 `build_factor_snapshot` 里构造
#    base config 的方式（今天是 `bottom_divergence_v2_enabled=False`）。
# 6. pandas / numpy 等依赖升级到会改变数值输出的版本。
#
# bump 的代价只是让旧缓存目录整体失效、重算一次；不 bump 的代价是错结论。
# 拿不准就 bump。
BASE_SNAPSHOT_ALGORITHM_VERSION = "base-factor-snapshot-v1"

# base 快照路径真正读取的全部配置字段。这是**白名单**而不是黑名单：
# `Config` 有 232 个字段，其中绝大多数（LLM、通知、调度等）与因子计算无关，
# 把它们纳入键只会让无关改动触发全市场重算，跨策略共享因此无法成立。
#
# 白名单的失败模式比黑名单危险：漏登记一个真正影响 base 的字段会导致**过度复用**，
# 也就是拿旧参数的因子当新参数的结果用——算错，不是算慢。
# `tests/test_base_factor_cache_key_whitelist.py` 枚举 `Config` 全部字段逐个
# 变异重算，凡能改变 base 输出却未登记的字段都会让它变红。
#
# 只用于 base 快照。证据两层继续走 `_config_hash`：那两层的输出受
# `bottom_divergence_v2_sync_window` 等字段影响，收窄会静默算错。
BASE_FACTOR_CONFIG_FIELDS = frozenset({
    # 读取时是否施加复权。开关一翻，`_window` 喂给 base pass 的价格与成交量
    # 就换了一套口径，漏登记会让第二次调用读回第一次的 base 快照文件。
    # 变异测试探不到它（那条测试直接调 `build_factor_snapshot_from_groups`，
    # 绕开了 `_window`，而它的夹具没有 `pre_close` 列），所以由
    # `test_adjustment_switch_changes_both_the_window_and_the_base_key` 守。
    "adj_apply_on_read",
    "screening_factor_lookback_days",
    "screening_min_list_days",
    "screening_breakout_lookback_days",
    "low123_max_p1_p2_bars",
    "low123_max_breakout_gap",
    "low123_break_tolerance",
    "bottom_divergence_max_breakout_gap",
    "bottom_divergence_break_tolerance",
})

# universe 里会决定 base 快照取值的全部列，`code` 在最前以便指纹按它排序。
# 只哈希 code 是不够的——同一批 code 配不同元数据是不同的输入：
#   `list_date` -> `days_since_listed`（`factor_service.py:183-188`），
#                  再经 `:207-212` 影响 `risk_flags`，并落在 `:232`
#   `is_st`     -> `:208` 的风险标记与 `:231` 的字段
#   `circ_mv`   -> `:230`
#   `name`      -> `:219`
# 与 `BASE_FACTOR_CONFIG_FIELDS` 一样，这也是白名单，漏登记一列的后果是
# 过度复用（算错），不是过度失效（算慢）。
BASE_SNAPSHOT_UNIVERSE_COLUMNS = (
    "code",
    "name",
    "list_date",
    "is_st",
    "circ_mv",
)


def _universe_cell_fingerprint(value: Any) -> str:
    """把 universe 的单元格渲染成可安全哈希的带类型标签文本。

    不能把原值直接交给 `canonical_json_dumps`：它以 `allow_nan=False` 运行，
    `NaN` 会抛 `ValueError`，而 `np.int64` / `np.bool_` / `Timestamp` /
    `NaT` / `date` 一律抛 `TypeError`（已实测）。

    类型标签的作用是防止不同取值折叠成同一段文本：`1` / `1.0` / `"1"`
    以及 `None` / `NaN` / `False` 都必须互相区分——它们在
    `factor_service.py` 里走的是不同分支，`bool(nan)` 是 `True` 而
    `bool(None)` 是 `False`。
    """
    if value is None:
        return "none:"
    if value is pd.NaT:
        return "nat:"
    # bool 早于 int：`bool` 是 `int` 的子类；`np.bool_` 两者都不是。
    if isinstance(value, (bool, np.bool_)):
        return f"bool:{bool(value)}"
    # datetime 早于 date：`datetime` 是 `date` 的子类。
    if isinstance(value, datetime):
        return f"datetime:{value.isoformat()}"
    if isinstance(value, date):
        return f"date:{value.isoformat()}"
    if isinstance(value, (int, np.integer)):
        return f"int:{int(value)}"
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return "nan:" if number != number else f"float:{number!r}"
    if isinstance(value, str):
        return f"str:{value}"
    return f"{type(value).__name__}:{value!r}"


def _evaluate_factor_task(
    task: tuple[str, Any, pd.DataFrame, Any],
) -> tuple[str, Any, dict[str, Any]]:
    code, config, group, frozen = task
    from src.services.factor_service import FactorService

    service = FactorService(db_manager=object(), config=config)
    if frozen is None:
        frozen = service.freeze_bottom_divergence_v2_evidence(group)
    factors = service.compute_bottom_divergence_v2_factors(
        group,
        frozen_evidence=frozen,
    )
    return code, frozen, factors


def _build_base_factor_task(
    task: tuple[str, Any, pd.DataFrame, dict[str, Any], date],
) -> Optional[dict[str, Any]]:
    code, config, group, info, trade_date = task
    from src.services.factor_service import FactorService

    service = FactorService(db_manager=object(), config=config)
    universe = pd.DataFrame([{"code": code, **info}])
    snapshot = service.build_factor_snapshot_from_groups(
        universe,
        {code: group},
        trade_date=trade_date,
        persist=False,
    )
    return (
        dict(snapshot.iloc[0])
        if not snapshot.empty
        else None
    )


@dataclass(frozen=True)
class FrozenEvidenceCacheKey:
    data_version: str
    code: str
    candidate_version: str
    as_of_index: int
    algorithm_version: str
    config_hash: str
    parameter_hash: Optional[str] = None


class ValidationProgress:
    """Stable elapsed/ETA reporting with a configurable event interval."""

    def __init__(
        self,
        total: int,
        *,
        every: int = 100,
        callback: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        self.total = max(int(total), 0)
        self.every = max(int(every), 1)
        self.callback = callback
        self.completed = 0
        self.started = time.perf_counter()

    def advance(self, amount: int = 1) -> None:
        self.completed += amount
        if (
            self.callback is None
            or (
                self.completed % self.every != 0
                and self.completed < self.total
            )
        ):
            return
        elapsed = time.perf_counter() - self.started
        rate = self.completed / elapsed if elapsed > 0 else 0.0
        remaining = max(self.total - self.completed, 0)
        self.callback({
            "completed": self.completed,
            "total": self.total,
            "elapsed_seconds": round(elapsed, 3),
            "eta_seconds": (
                round(remaining / rate, 3) if rate > 0 else None
            ),
        })


class ValidationFactorCache:
    """Share OHLCV, base factors, and frozen v2 evidence across the grid."""

    def __init__(
        self,
        *,
        data_version: str,
        trade_dates: Sequence[date],
        bar_groups: Mapping[str, pd.DataFrame],
        sql_bar_queries: int,
        progress_every: int = 100,
        progress_callback: Optional[
            Callable[[dict[str, Any]], None]
        ] = None,
        cache_directory: Optional[Path] = None,
        workers: int = 1,
    ) -> None:
        self.data_version = data_version
        self.trade_dates = tuple(sorted(set(trade_dates)))
        self._bar_groups = {
            str(code): self._compact_bar_frame(frame)
            for code, frame in sorted(bar_groups.items())
        }
        # `cache_directory` 为空时退回进程内临时目录，这是既有默认行为：
        # 不传就没有任何跨进程复用，也没有任何跨代码版本的陈旧产物。
        # `ignore_cleanup_errors` 是给 Windows 的：子进程还没完全释放句柄时
        # `cleanup()` 会抛 WinError 32，为了删一个纯派生的缓存目录而让整条
        # 回放链路在 `finally` 里崩掉，不值得。
        self._temporary_directory = (
            TemporaryDirectory(
                prefix="validation-factor-cache-",
                ignore_cleanup_errors=True,
            )
            if cache_directory is None
            else None
        )
        self._cache_directory = (
            Path(self._temporary_directory.name)
            if self._temporary_directory is not None
            else Path(cache_directory)
        )
        self._cache_directory.mkdir(parents=True, exist_ok=True)
        self._frozen: dict[FrozenEvidenceCacheKey, Any] = {}
        self._frozen_lookup: dict[tuple[Any, ...], Any] = {}
        self._evaluated: dict[FrozenEvidenceCacheKey, dict[str, Any]] = {}
        self._active_frozen_date: Optional[date] = None
        self._active_evaluated_hash: Optional[str] = None
        # 没改过的分区不必写回。热运行里三层计数全为 0，也就是每一次写回都在
        # 把刚读进来的字节原样写出去；实测这一项占整趟 62%。
        self._frozen_dirty = False
        self._evaluated_dirty = False
        self.stats = {
            "sql_bar_queries": sql_bar_queries,
            "base_snapshot_builds": 0,
            "frozen_evidence_builds": 0,
            "parameter_evaluations": 0,
            "parameter_evaluations_by_hash": {},
            # 分区换页的可观测量。三层计数只能说明「因子有没有重算」，
            # 说明不了「时间花在哪」——实测热运行三层全零却仍耗时 250s，
            # 差额全在这四个数上。
            "frozen_partition_loads": 0,
            "frozen_partition_load_seconds": 0.0,
            "frozen_partition_dumps": 0,
            "frozen_partition_dump_seconds": 0.0,
        }
        self.progress_every = progress_every
        self.progress_callback = progress_callback
        self.workers = max(int(workers), 1)
        self._executor: Optional[ProcessPoolExecutor] = None

    @staticmethod
    def _compact_bar_frame(frame: pd.DataFrame) -> pd.DataFrame:
        compact = frame.sort_values("date").reset_index(drop=True).copy()
        compact = compact.drop(columns=["code"], errors="ignore")
        compact["date"] = pd.to_datetime(compact["date"])
        for field_name in ("data_source", "adj_factor_source"):
            if field_name in compact:
                compact[field_name] = compact[field_name].astype("category")
        return compact

    @classmethod
    def from_groups(
        cls,
        *,
        data_version: str,
        trade_dates: Sequence[date],
        bar_groups: Mapping[str, pd.DataFrame],
        progress_every: int = 100,
        progress_callback: Optional[
            Callable[[dict[str, Any]], None]
        ] = None,
        cache_directory: Optional[Path] = None,
        workers: int = 1,
    ) -> "ValidationFactorCache":
        return cls(
            data_version=data_version,
            trade_dates=trade_dates,
            bar_groups=bar_groups,
            sql_bar_queries=0,
            progress_every=progress_every,
            progress_callback=progress_callback,
            cache_directory=cache_directory,
            workers=workers,
        )

    @classmethod
    def from_database(
        cls,
        *,
        db_manager: Any,
        data_version: str,
        trade_dates: Sequence[date],
        codes: Sequence[str],
        lookback_days: int,
        progress_every: int = 100,
        progress_callback: Optional[
            Callable[[dict[str, Any]], None]
        ] = None,
        cache_directory: Optional[Path] = None,
        workers: int = 1,
    ) -> "ValidationFactorCache":
        from sqlalchemy import select

        from src.storage import StockDaily

        ordered_dates = tuple(sorted(set(trade_dates)))
        if not ordered_dates:
            raise ValueError("trade_dates must not be empty")
        start = ordered_dates[0] - timedelta(days=lookback_days * 2)
        end = ordered_dates[-1]
        statement = (
            select(*StockDaily.__table__.columns)
            .where(
                StockDaily.code.in_(sorted(set(codes))),
                StockDaily.date >= start,
                StockDaily.date <= end,
            )
            .order_by(StockDaily.code, StockDaily.date)
        )
        groups: dict[str, pd.DataFrame] = {}
        current_code: Optional[str] = None
        current_rows: list[dict[str, Any]] = []

        def flush() -> None:
            nonlocal current_rows
            if current_code is not None and current_rows:
                groups[current_code] = pd.DataFrame(current_rows)
            current_rows = []

        with db_manager.get_session() as session:
            for batch in iter_query_batches(session, statement):
                for row in batch:
                    code = str(row["code"])
                    if current_code is not None and code != current_code:
                        flush()
                    current_code = code
                    row.pop("id", None)
                    current_rows.append(row)
            flush()
        return cls(
            data_version=data_version,
            trade_dates=ordered_dates,
            bar_groups=groups,
            sql_bar_queries=1,
            progress_every=progress_every,
            progress_callback=progress_callback,
            cache_directory=cache_directory,
            workers=workers,
        )

    def close(self) -> None:
        """收工。临时目录连同缓存一起删，持久化目录只落盘不删。

        两种目录的语义相反，必须分开处理：临时目录的全部意义就是「本进程用完
        即弃」，留下就是垃圾；持久化目录的全部意义则是「给下一个进程复用」，
        删掉等于这次改动白做。
        """
        if self._executor is not None:
            # 先等 worker 退干净再动目录：Windows 上还有进程持有文件时
            # 删目录会失败。
            self._executor.shutdown(wait=True)
            self._executor = None
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None
            # 目录已经没了，活动分区也就无处可落。不清掉它，第二次 close()
            # 会走到下面的落盘分支，对着已删除的目录抛 FileNotFoundError。
            self._active_frozen_date = None
            self._active_evaluated_hash = None
            return
        # `_switch_frozen_partition` 只在切日期时写回上一个分区，最后一个
        # 分区不在这里落盘就等于白算，下次运行会从头冻结这一天的证据。
        self._flush_active_frozen_partition()

    def _worker_pool(self) -> ProcessPoolExecutor:
        if self._executor is None:
            self._executor = ProcessPoolExecutor(
                max_workers=self.workers,
                mp_context=multiprocessing.get_context("spawn"),
            )
        return self._executor

    @staticmethod
    def _universe_fingerprint(universe: pd.DataFrame) -> str:
        """指纹化 universe 中真正决定 base 快照取值的那几列。

        只哈希 code 会让「同一批 code、不同元数据」的两个 universe 撞上同一份
        缓存文件，第二次调用直接读回第一次的行——正是本改动要消灭的静默错误。

        缺列与「有这列但值为空」分别记录，因为两者不可互换：
        `info.get("is_st", False)` 在缺列时得到 `False`，在值为 `NaN` 时
        `bool(nan)` 得到 `True`，还会多一个 `st` 风险标记。
        （缺列与 `None` 在生产侧确实等价，这里仍分开记，代价只是多算一次。）
        """
        present = [
            name
            for name in BASE_SNAPSHOT_UNIVERSE_COLUMNS
            if name in universe.columns
        ]
        absent = [
            name
            for name in BASE_SNAPSHOT_UNIVERSE_COLUMNS
            if name not in universe.columns
        ]
        # 行序不该影响指纹：`factor_service.py:167` 以 code 为索引取用元数据。
        # 渲染成字符串后再排序，避免混合类型列在 `sort_values` 上抛异常，
        # 且因 `code` 是首列，排序等价于按 code 排。
        rows = sorted(
            [_universe_cell_fingerprint(record.get(name)) for name in present]
            for record in universe.loc[:, present].to_dict("records")
        )
        return hashlib.sha256(
            canonical_json_dumps({
                "columns": present,
                "absent_columns": absent,
                "rows": rows,
            }).encode("utf-8")
        ).hexdigest()

    def _base_path(
        self,
        trade_date: date,
        config_hash: str,
        *,
        universe: pd.DataFrame,
    ) -> Path:
        # universe、bar 数据、计算代码都是 base 快照的输入，必须全部进文件名。
        # `data_version` 此前只被证据两层的键覆盖（`FrozenEvidenceCacheKey`），
        # 算法版本则谁都没覆盖；缓存目录恒为进程内临时目录时两者都撞不上，
        # 但键的正确性不该依赖这个偶然条件——`cache_directory` 现在可以指向
        # 持久化目录，缓存文件会跨进程、跨代码版本活下来。
        data_version_hash = hashlib.sha256(
            str(self.data_version).encode("utf-8")
        ).hexdigest()[:16]
        return self._cache_directory / (
            f"base-{trade_date.isoformat()}"
            f"-{BASE_SNAPSHOT_ALGORITHM_VERSION}"
            f"-{config_hash[:16]}"
            f"-{self._universe_fingerprint(universe)[:16]}"
            f"-{data_version_hash}.pkl.gz"
        )

    def _frozen_partition_identity(self) -> str:
        """分区文件名里代表「这份证据属于哪次运行」的那一段。

        分区文件的**内容**一直是按完整键存的（`FrozenEvidenceCacheKey` 与
        `_temporary_frozen_key` 都含 `data_version` 与算法版本），所以查表
        不会跨数据集读到错的证据。但文件名此前只有日期，持久化之后两个
        `data_version` 会共用同一个文件，带来两个真实后果：
        `_switch_frozen_partition` 是整份载入、整份写回，后写的一方会抹掉
        另一方的条目（丢工作量）；`frozen_cache_keys` / `evaluation_cache_keys`
        会把别的运行的键当成本次运行的键返回（错答案）。
        文件名与键取同一口径即可一并消掉这两条。
        """
        return hashlib.sha256(
            canonical_json_dumps({
                "algorithm_version": FROZEN_EVIDENCE_ALGORITHM_VERSION,
                "data_version": str(self.data_version),
                "layout_version": FROZEN_PARTITION_LAYOUT_VERSION,
            }).encode("utf-8")
        ).hexdigest()[:16]

    def _frozen_path(self, trade_date: date) -> Path:
        return self._cache_directory / (
            f"frozen-{trade_date.isoformat()}"
            f"-{self._frozen_partition_identity()}.pkl.gz"
        )

    def _evaluated_path(
        self,
        trade_date: date,
        parameter_hash: str,
    ) -> Path:
        """已评估因子按 (日期, 参数哈希) 分片，而不是与冻结证据同住一个文件。

        分片的理由是实测的读写量：一个日期分区解包后 9.84 MB，其中 `evaluated`
        占 9.54 MB（96.9%），而 `frozen` + `lookup` 只有 0.30 MB。网格是
        3×2×3=18 条 leg，`evaluated` 里 18 个参数哈希各占 91 条，**任何一条
        leg 只读写属于自己的那 1/18**——其余 17/18 是被白读白写的。

        合住时每次换页要搬 9.84 MB，分片后只搬 0.30 + 0.53 MB。这条路比给分区
        加内存 LRU 更可取：LRU 要把整个日期分区常驻，实测单个分区常驻 48.5 MB
        RSS（是压缩后 1.76 MB 的 27 倍），32 天就是 1.55 GB，而分片把内存占用
        保持在 O(1)，不随日期数或股池规模增长。
        """
        return self._cache_directory / (
            f"frozen-{trade_date.isoformat()}"
            f"-{self._frozen_partition_identity()}"
            f"-eval-{parameter_hash}.pkl.gz"
        )

    def _write_atomically(
        self,
        path: Path,
        write: Callable[[Path], None],
    ) -> None:
        """先写同目录临时文件再 `os.replace`，避免读到写了一半的缓存。

        临时目录里这是多余的（进程独占且用完即弃），持久化目录里则是必需：
        分批跑会有多个进程指向同一个目录，进程被 Ctrl-C 或 OOM 杀掉时，
        半个 gzip 文件会让下一次运行在 `pickle.load` 上直接崩，而不是重算。
        临时文件名以 `.` 开头、以 `.tmp` 结尾，两个 glob 都匹配不到它。
        """
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            write(temporary)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_partition_file(path: Path) -> Any:
        with gzip.open(path, "rb") as handle:
            return pickle.load(handle)

    def _dump_partition(self, path: Path, payload: Any) -> None:
        def write(target: Path) -> None:
            with gzip.open(
                target,
                "wb",
                compresslevel=PARTITION_COMPRESS_LEVEL,
            ) as handle:
                pickle.dump(payload, handle, protocol=5)

        started = time.perf_counter()
        self._write_atomically(path, write)
        self.stats["frozen_partition_dumps"] += 1
        self.stats["frozen_partition_dump_seconds"] += (
            time.perf_counter() - started
        )

    def _load_partition(self, path: Path) -> Optional[Any]:
        if not path.exists():
            return None
        started = time.perf_counter()
        payload = self._read_partition_file(path)
        self.stats["frozen_partition_loads"] += 1
        self.stats["frozen_partition_load_seconds"] += (
            time.perf_counter() - started
        )
        return payload

    def _flush_active_evaluated_shard(self) -> None:
        if (
            self._active_frozen_date is None
            or self._active_evaluated_hash is None
            or not self._evaluated_dirty
        ):
            return
        self._dump_partition(
            self._evaluated_path(
                self._active_frozen_date,
                self._active_evaluated_hash,
            ),
            self._evaluated,
        )
        self._evaluated_dirty = False

    def _flush_active_frozen_partition(self) -> None:
        # 分片必须先落盘：它的路径要用 `_active_frozen_date`，而调用方随后
        # 就会把这个字段改成新日期。
        self._flush_active_evaluated_shard()
        if self._active_frozen_date is None or not self._frozen_dirty:
            return
        self._dump_partition(
            self._frozen_path(self._active_frozen_date),
            {"frozen": self._frozen, "lookup": self._frozen_lookup},
        )
        self._frozen_dirty = False

    def _switch_frozen_partition(
        self,
        trade_date: date,
        parameter_hash: str,
    ) -> None:
        """把 (日期, 参数哈希) 这一格换进内存，两个轴各自独立换页。

        日期换了，冻结证据与已评估分片都得换；只有参数哈希换了（同一天连着
        跑两条 leg，实际不会发生，但语义上必须成立），只换分片。
        """
        if self._active_frozen_date != trade_date:
            self._flush_active_frozen_partition()
            self._frozen = {}
            self._frozen_lookup = {}
            self._frozen_dirty = False
            payload = self._load_partition(self._frozen_path(trade_date))
            if payload is not None:
                self._frozen = payload["frozen"]
                self._frozen_lookup = payload["lookup"]
            self._active_frozen_date = trade_date
            self._evaluated = {}
            self._evaluated_dirty = False
            self._active_evaluated_hash = None
        if self._active_evaluated_hash == parameter_hash:
            return
        self._flush_active_evaluated_shard()
        self._evaluated = (
            self._load_partition(
                self._evaluated_path(trade_date, parameter_hash)
            )
            or {}
        )
        self._evaluated_dirty = False
        self._active_evaluated_hash = parameter_hash

    @staticmethod
    def _candidate_versions(frozen: Any) -> tuple[str, ...]:
        payload = frozen.decode_payload()
        versions = tuple(sorted({
            str(item["candidate_version"])
            for item in payload.get("candidate_evidence", ())
        }))
        if versions:
            return versions
        return (f"none:{frozen.content_hash}",)

    @staticmethod
    def _temporary_frozen_key(
        *,
        data_version: str,
        code: str,
        as_of_index: int,
        config_hash: str,
    ) -> tuple[Any, ...]:
        return (
            data_version,
            code,
            as_of_index,
            FROZEN_EVIDENCE_ALGORITHM_VERSION,
            config_hash,
        )

    @property
    def frozen_cache_keys(self) -> tuple[FrozenEvidenceCacheKey, ...]:
        # 只看属于本次运行的分区文件：持久化目录里同时躺着别的 `data_version`
        # 与别的算法版本的分区，把它们的键混进来就是在返回错答案。
        keys = set(self._frozen)
        suffix = f"-{self._frozen_partition_identity()}.pkl.gz"
        active = (
            self._active_frozen_date.isoformat()
            if self._active_frozen_date is not None
            else None
        )
        for path in sorted(self._cache_directory.glob(f"frozen-*{suffix}")):
            date_text = path.name[len("frozen-"):-len(suffix)]
            try:
                date.fromisoformat(date_text)
            except ValueError:
                # 分片文件叫 `frozen-<日期>-<身份>-eval-<参数哈希>.pkl.gz`，
                # 参数哈希恰好等于身份段时也会撞进上面的 glob。它没有
                # `frozen` 这个键，读它会直接 KeyError。
                continue
            if date_text == active:
                continue
            keys.update(self._read_partition_file(path)["frozen"])
        return tuple(sorted(keys, key=repr))

    @property
    def evaluation_cache_keys(self) -> tuple[FrozenEvidenceCacheKey, ...]:
        keys = set(self._evaluated)
        identity = self._frozen_partition_identity()
        active = (
            self._evaluated_path(
                self._active_frozen_date,
                self._active_evaluated_hash,
            ).name
            if self._active_frozen_date is not None
            and self._active_evaluated_hash is not None
            else None
        )
        pattern = f"frozen-*-{identity}-eval-*.pkl.gz"
        for path in sorted(self._cache_directory.glob(pattern)):
            if path.name == active:
                continue
            keys.update(self._read_partition_file(path))
        return tuple(sorted(keys, key=repr))

    @staticmethod
    def _config_hash(config: Any) -> str:
        """Hash every config field except the four grid fields.

        The grid fields are covered by `_parameter_hash`, which is a separate
        component of the frozen-evidence and evaluated-factor keys.
        """
        payload = asdict(config)
        for field_name in (
            "bottom_divergence_v2_enabled",
            "bottom_divergence_v2_cluster_pct",
            "bottom_divergence_v2_atr_gap_multiplier",
            "bottom_divergence_v2_zone_score_min",
        ):
            payload.pop(field_name, None)
        return hashlib.sha256(
            canonical_json_dumps(payload).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _base_config_hash(config: Any) -> str:
        """Hash only the fields the base factor pass actually reads.

        `_config_hash` stays as-is and keeps serving the frozen-evidence and
        evaluated-factor layers: their output depends on fields such as
        `bottom_divergence_v2_sync_window`, so narrowing those keys would make
        the cache return one parameter set's evidence as another's.
        """
        payload = asdict(config)
        base_payload = {
            name: payload[name]
            for name in sorted(BASE_FACTOR_CONFIG_FIELDS)
            if name in payload
        }
        return hashlib.sha256(
            canonical_json_dumps(base_payload).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _parameter_hash(config: Any) -> str:
        return canonical_parameter_hash({
            "cluster_pct": config.bottom_divergence_v2_cluster_pct,
            "atr_gap_multiplier": (
                config.bottom_divergence_v2_atr_gap_multiplier
            ),
            "zone_score_min": config.bottom_divergence_v2_zone_score_min,
        })

    def _window(
        self,
        code: str,
        trade_date: date,
        lookback_days: int,
        *,
        adjust: bool = True,
    ) -> pd.DataFrame:
        """回测侧唯一的取窗口，也是回测侧唯一的复权施加点。

        复权必须落在这里而不是 `from_database`：后者一次性拉整段区间，结构上
        不知道单个回放日 D，在那里归一只能除以晚于 D 的因子，等于把 D 之后的
        分红信息带进 D，直接违反时点安全。这里切出来的窗口末行恰好就是 D，
        而 base 快照与 v2 证据两层消费的都是它，一处施加两层同口径。
        """
        frame = self._bar_groups.get(code)
        if frame is None:
            return pd.DataFrame()
        start = pd.Timestamp(
            trade_date - timedelta(days=lookback_days * 2)
        )
        end = pd.Timestamp(trade_date)
        mask = (frame["date"] >= start) & (frame["date"] <= end)
        window = frame.loc[mask].reset_index(drop=True)
        if not adjust:
            return window
        return apply_read_adjustment(window)

    def build_factor_snapshot(
        self,
        *,
        config: Any,
        universe: pd.DataFrame,
        trade_date: date,
    ) -> pd.DataFrame:
        from src.services.factor_service import FactorService

        codes = sorted(str(code) for code in universe["code"].tolist())
        windows = {
            code: self._window(
                code,
                trade_date,
                config.screening_factor_lookback_days,
                adjust=config.adj_apply_on_read,
            )
            for code in codes
        }
        # 两个口径刻意分开：base 快照只依赖白名单里的 8 个字段，而证据两层
        # 仍需 `base_hash` 的全量口径——`bottom_divergence_v2_sync_window` 等
        # 字段只由它覆盖，`_parameter_hash` 不含它们。
        base_snapshot_hash = self._base_config_hash(config)
        base_hash = self._config_hash(config)
        base_path = self._base_path(
            trade_date, base_snapshot_hash, universe=universe
        )
        if base_path.exists():
            base_snapshot = pd.read_pickle(base_path, compression="gzip")
        else:
            base_config = replace(
                config,
                bottom_divergence_v2_enabled=False,
            )
            if self.workers > 1 and len(windows) > 1:
                info_by_code = universe.set_index("code").to_dict("index")
                tasks = [
                    (
                        code,
                        base_config,
                        windows[code],
                        info_by_code.get(code, {}),
                        trade_date,
                    )
                    for code in sorted(windows)
                ]
                rows = [
                    row
                    for row in self._worker_pool().map(
                        _build_base_factor_task,
                        tasks,
                        chunksize=1,
                    )
                    if row is not None
                ]
                base_snapshot = pd.DataFrame(rows)
            else:
                base_service = FactorService(config=base_config)
                base_snapshot = (
                    base_service.build_factor_snapshot_from_groups(
                        universe,
                        windows,
                        trade_date=trade_date,
                        persist=False,
                    )
                )
            base_snapshot = (
                base_snapshot.sort_values("code").reset_index(drop=True)
            )
            self._write_atomically(
                base_path,
                lambda target: base_snapshot.to_pickle(
                    target,
                    compression="gzip",
                ),
            )
            self.stats["base_snapshot_builds"] += 1
        snapshot = base_snapshot.copy(deep=True)
        if not config.bottom_divergence_v2_enabled:
            return snapshot

        parameter_hash = self._parameter_hash(config)
        self._switch_frozen_partition(trade_date, parameter_hash)
        progress = ValidationProgress(
            len(snapshot),
            every=self.progress_every,
            callback=self.progress_callback,
        )
        row_by_code = {
            str(row["code"]): index
            for index, row in snapshot.iterrows()
        }
        tasks = []
        temporary_keys = {}
        ready_results = []
        for code in sorted(row_by_code):
            group = windows[code]
            if len(group) < 60:
                progress.advance()
                continue
            as_of_index = len(group) - 1
            temporary_key = self._temporary_frozen_key(
                data_version=self.data_version,
                code=code,
                as_of_index=as_of_index,
                config_hash=base_hash,
            )
            temporary_keys[code] = temporary_key
            frozen = self._frozen_lookup.get(temporary_key)
            if frozen is not None:
                evaluation_keys = tuple(
                    FrozenEvidenceCacheKey(
                        data_version=self.data_version,
                        code=code,
                        candidate_version=candidate_version,
                        as_of_index=as_of_index,
                        algorithm_version=(
                            FROZEN_EVIDENCE_ALGORITHM_VERSION
                        ),
                        config_hash=base_hash,
                        parameter_hash=parameter_hash,
                    )
                    for candidate_version in self._candidate_versions(frozen)
                )
                cached = self._evaluated.get(evaluation_keys[0])
                if (
                    cached is not None
                    and all(key in self._evaluated for key in evaluation_keys)
                ):
                    ready_results.append((code, frozen, cached, False))
                    continue
            tasks.append((
                code,
                config,
                group,
                frozen,
            ))
        if self.workers > 1 and len(tasks) > 1:
            results = self._worker_pool().map(
                _evaluate_factor_task,
                tasks,
                chunksize=1,
            )
        else:
            results = map(_evaluate_factor_task, tasks)
        computed_results = (
            (code, frozen, factors, True)
            for code, frozen, factors in results
        )
        for code, frozen, factors, was_computed in (
            *ready_results,
            *computed_results,
        ):
            temporary_key = temporary_keys[code]
            as_of_index = temporary_key[2]
            candidate_versions = self._candidate_versions(frozen)
            frozen_keys = tuple(
                FrozenEvidenceCacheKey(
                    data_version=self.data_version,
                    code=code,
                    candidate_version=candidate_version,
                    as_of_index=as_of_index,
                    algorithm_version=FROZEN_EVIDENCE_ALGORITHM_VERSION,
                    config_hash=base_hash,
                )
                for candidate_version in candidate_versions
            )
            if temporary_key not in self._frozen_lookup:
                self._frozen_lookup[temporary_key] = frozen
                for frozen_key in frozen_keys:
                    self._frozen[frozen_key] = frozen
                self._frozen_dirty = True
                self.stats["frozen_evidence_builds"] += 1
            if was_computed:
                for frozen_key in frozen_keys:
                    evaluation_key = replace(
                        frozen_key,
                        parameter_hash=parameter_hash,
                    )
                    self._evaluated[evaluation_key] = factors
                self._evaluated_dirty = True
                self.stats["parameter_evaluations"] += 1
                by_hash = self.stats["parameter_evaluations_by_hash"]
                by_hash[parameter_hash] = by_hash.get(parameter_hash, 0) + 1
            row_index = row_by_code[code]
            for field_name, value in factors.items():
                snapshot.at[row_index, field_name] = value
            progress.advance()
        return snapshot.sort_values("code").reset_index(drop=True)


class CachedValidationFactorService:
    """FactorService-compatible facade backed by a shared validation cache."""

    def __init__(self, config: Any, cache: ValidationFactorCache) -> None:
        self.config = config
        self.cache = cache

    def build_factor_snapshot(
        self,
        universe: pd.DataFrame,
        trade_date: date,
        persist: bool = False,
    ) -> pd.DataFrame:
        if persist:
            raise ValueError("validation factor cache is read-only")
        return self.cache.build_factor_snapshot(
            config=self.config,
            universe=universe,
            trade_date=trade_date,
        )


def _checkpoint_json_value(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _checkpoint_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_checkpoint_json_value(item) for item in value]
    return value


def replay_batch_to_payload(batch: Any) -> dict[str, Any]:
    """Serialize a replay batch into canonical checkpoint-compatible data."""
    return {
        "samples": [
            _checkpoint_json_value(asdict(item)) for item in batch.samples
        ],
        "opportunity_counts": {
            key.isoformat(): value
            for key, value in sorted(batch.opportunity_counts.items())
        },
        "event_evidence": [
            _checkpoint_json_value(asdict(item))
            for item in batch.event_evidence
        ],
    }


def replay_batch_from_payload(payload: Mapping[str, Any]) -> Any:
    """Restore a replay batch without silently mutating selection evidence."""
    from .bottom_divergence_v2_models import (
        CandidateEventEvidence,
        ValidationSample,
    )
    from .bottom_divergence_v2_replay import ReplayBatch

    date_fields = {
        "signal_date",
        "early_event_date",
        "near_cleared_event_date",
        "major_breakout_event_date",
    }
    samples = []
    for raw in payload.get("samples", []):
        item = dict(raw)
        for field_name in date_fields:
            if item.get(field_name):
                item[field_name] = date.fromisoformat(item[field_name])
        item["future_trade_dates_20d"] = tuple(
            date.fromisoformat(value)
            for value in item.get("future_trade_dates_20d", [])
        )
        for field_name in (
            "future_closes_20d",
            "future_highs_20d",
            "future_lows_20d",
        ):
            item[field_name] = tuple(item.get(field_name, []))
        samples.append(ValidationSample(**item))
    evidence = []
    for raw in payload.get("event_evidence", []):
        item = dict(raw)
        for field_name in (
            "near_cleared_event_date",
            "major_breakout_event_date",
        ):
            if item.get(field_name):
                item[field_name] = date.fromisoformat(item[field_name])
        evidence.append(CandidateEventEvidence(**item))
    return ReplayBatch(
        samples=tuple(samples),
        opportunity_counts={
            date.fromisoformat(key): int(value)
            for key, value in payload.get(
                "opportunity_counts",
                {},
            ).items()
        },
        event_evidence=tuple(evidence),
    )
