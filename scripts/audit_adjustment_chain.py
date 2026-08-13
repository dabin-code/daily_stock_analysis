# -*- coding: utf-8 -*-
"""离线抽查 stock_daily 的复权链（gate-3 / Task 1 的只读工具）。

**本脚本不写库。** 复权因子在 v2 里不落库、只在读取时按窗口现算，实现全部在
``src/services/adjustment_chain.py``；这里只是把它跑在全表上，给出四份画像：

1. 单日复权比值 close(t-1)/pre_close(t) 的分布
2. 每只股票的全历史总复权倍数分布
3. 断链统计——多少代码、多少行会被切段作废，按原因分类
4. 断链代码清单及其画像（首次断链的日期、比值、原因）

为什么不落库：日常同步的 ``INSERT OR REPLACE`` 列清单不含 ``adj_factor``
（``src/services/market_data_sync_service.py:360-369``），回填进去会被逐步擦掉；
而 ``pre_close`` 在那份清单里被保住。上一版脚本写 ``stock_daily.adj_factor``，
那条路线已作废。

口径提醒：这里按**全历史**解链（D = 每只股票的最后一根 bar），而生产路径上的窗口
只有几百个交易日。断链统计因此是上界——窗口越短，落在窗口内的断链越少。

用法::

    python scripts/audit_adjustment_chain.py
    python scripts/audit_adjustment_chain.py --codes 002594,920402
    python scripts/audit_adjustment_chain.py --limit 200
    python scripts/audit_adjustment_chain.py --db path/to.db
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.adjustment_chain import (  # noqa: E402
    ChainBreak,
    RATIO_CORRUPTION_CEILING,
    RATIO_DIRECTION_FLOOR,
    analyze_window,
)

TABLE = "stock_daily"

# 只抽查原始价行。生产表里另有指数与个别港股（adj_convention 为 NULL 或 unknown），
# 它们的 pre_close 链路没有做过交叉验证，画像出来无法证伪。
RAW_CONVENTION = "raw"

# 每多少个代码打一行进度。全表 5790 个代码要跑一两分钟，全程静默会让操作员无法
# 区分「在跑」和「卡住」。
PROGRESS_EVERY = 500

# 比值分箱按半开区间 [low, high) 统计。分界点取自实测分布：1.0~1.10 是分红主体、
# 1.5~3.0 是送转、3.0 以上是大比例拆股。
_CEILING_INCLUSIVE = float(np.nextafter(RATIO_CORRUPTION_CEILING, np.inf))
_ONE_INCLUSIVE = float(np.nextafter(1.0, np.inf))
RATIO_BINS: Tuple[Tuple[str, float, float], ...] = (
    ("< 下界（方向性错误）", -np.inf, RATIO_DIRECTION_FLOOR),
    ("== 1（无事件）", RATIO_DIRECTION_FLOOR, _ONE_INCLUSIVE),
    ("(1, 1.01)", _ONE_INCLUSIVE, 1.01),
    ("[1.01, 1.10)", 1.01, 1.10),
    ("[1.10, 1.50)", 1.10, 1.50),
    ("[1.50, 3.00)", 1.50, 3.00),
    ("[3.00, 上界]", 3.00, _CEILING_INCLUSIVE),
    ("> 上界（疑似脏数据）", _CEILING_INCLUSIVE, np.inf),
)

# 总复权倍数 = 1 / g(可信段首行)，即这只股票在可信段里累计的复权幅度。
SPAN_BINS: Tuple[Tuple[str, float, float], ...] = (
    ("== 1（整段无事件）", -np.inf, _ONE_INCLUSIVE),
    ("(1, 1.10)", _ONE_INCLUSIVE, 1.10),
    ("[1.10, 1.50)", 1.10, 1.50),
    ("[1.50, 3.00)", 1.50, 3.00),
    ("[3.00, 10.0)", 3.00, 10.0),
    ("[10.0, +inf)", 10.0, np.inf),
)


@dataclass
class BrokenCode:
    code: str
    rows: int
    invalidated_rows: int
    breaks: Tuple[ChainBreak, ...]


@dataclass
class Audit:
    codes: int = 0
    rows: int = 0
    ratio_counts: List[int] = field(
        default_factory=lambda: [0] * len(RATIO_BINS)
    )
    span_counts: List[int] = field(default_factory=lambda: [0] * len(SPAN_BINS))
    reason_counts: Dict[str, int] = field(default_factory=dict)
    broken: List[BrokenCode] = field(default_factory=list)
    invalidated_rows: int = 0
    missing_ratio_rows: int = 0


def default_db_path() -> str:
    return os.getenv("DATABASE_PATH") or os.path.join("data", "stock_analysis.db")


def connect_readonly(db_path: str) -> sqlite3.Connection:
    """只读连接，由 SQLite 而不是代码分支保证「不写库」。

    上一版脚本是可写的，靠 ``if execute:`` 守卫 dry-run；那种守卫挡不住漏写——
    没有 BEGIN 时漏写的 UPDATE 会被 close() 的隐式回滚吞掉，于是「dry-run 写了库」
    这个 bug 在测试里完全看不出来。现在整个脚本都是只读的，这道兜底保留下来。

    timeout=30：这个库的日常任务和回测会并发访问，默认 5 秒在大写事务面前太短。
    """
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)


def fetch_codes(cur: sqlite3.Cursor) -> List[str]:
    cur.execute(
        f"SELECT DISTINCT code FROM {TABLE} WHERE adj_convention = ? ORDER BY code",
        (RAW_CONVENTION,),
    )
    return [row[0] for row in cur.fetchall()]


def fetch_series(cur: sqlite3.Cursor, code: str) -> pd.DataFrame:
    """一次取完单个代码的序列。

    逐代码取而不是全表扫一遍再分组：962 万行放进内存不划算，而按 (code, date)
    唯一索引逐代码取，峰值内存只有一只股票。
    """
    cur.execute(
        f"SELECT date, close, pre_close FROM {TABLE}"
        f" WHERE code = ? AND adj_convention = ? ORDER BY date",
        (code, RAW_CONVENTION),
    )
    return pd.DataFrame(
        cur.fetchall(), columns=["date", "close", "pre_close"]
    )


def _bin_index(bins: Sequence[Tuple[str, float, float]], value: float) -> int:
    for index, (_, low, high) in enumerate(bins):
        if low <= value < high:
            return index
    return len(bins) - 1


def audit_code(audit: Audit, code: str, frame: pd.DataFrame) -> None:
    if frame.empty:
        return
    chain = analyze_window(frame)
    audit.codes += 1
    audit.rows += len(frame)

    # 首行的比值是构造出来的 1，不是观测值，不该进分布。
    ratios = chain.ratios.to_numpy(dtype=float)[1:]
    finite = np.isfinite(ratios)
    audit.missing_ratio_rows += int((~finite).sum())
    for value in ratios[finite]:
        audit.ratio_counts[_bin_index(RATIO_BINS, float(value))] += 1

    first_trusted = chain.factors.iloc[chain.segment_start]
    audit.span_counts[_bin_index(SPAN_BINS, 1.0 / float(first_trusted))] += 1

    if chain.trusted:
        return
    audit.invalidated_rows += chain.segment_start
    for item in chain.breaks:
        audit.reason_counts[item.reason] = audit.reason_counts.get(item.reason, 0) + 1
    audit.broken.append(
        BrokenCode(
            code=code,
            rows=len(frame),
            invalidated_rows=chain.segment_start,
            breaks=chain.breaks,
        )
    )


def _print_distribution(
    title: str, bins: Sequence[Tuple[str, float, float]], counts: Sequence[int]
) -> None:
    total = sum(counts) or 1
    print(f"\n[dist] {title}（合计 {sum(counts)}）")
    for (label, _, _), count in zip(bins, counts):
        print(f"       {label:<24} {count:>9}  {count / total:7.3%}")


def print_report(audit: Audit, list_limit: int) -> None:
    print(f"\n[info] 已解链 {audit.codes} 个代码 / {audit.rows} 行")
    _print_distribution("单日复权比值分布", RATIO_BINS, audit.ratio_counts)
    if audit.missing_ratio_rows:
        print(
            f"       另有 {audit.missing_ratio_rows} 行缺 close/pre_close，无法取比值"
        )
    _print_distribution("总复权倍数分布（按代码）", SPAN_BINS, audit.span_counts)

    print(
        f"\n[cut] 有断链的代码: {len(audit.broken)} / {audit.codes}"
        f"（{len(audit.broken) / (audit.codes or 1):.3%}）"
    )
    print(
        f"[cut] 被切段作废的行: {audit.invalidated_rows} / {audit.rows}"
        f"（{audit.invalidated_rows / (audit.rows or 1):.3%}）"
        "，注意这是全历史口径的上界，生产窗口只有几百个交易日"
    )
    if audit.reason_counts:
        print("[cut] 断链原因分布：")
        for reason, count in sorted(audit.reason_counts.items()):
            print(f"       {reason:<24} {count:>6}")

    if not audit.broken:
        return
    shown = sorted(
        audit.broken, key=lambda item: item.invalidated_rows, reverse=True
    )[:list_limit]
    print(
        f"\n[cut] 断链代码清单（按被作废行数降序，显示 {len(shown)}/"
        f"{len(audit.broken)}）："
    )
    for item in shown:
        first = item.breaks[0]
        ratio = "n/a" if first.ratio is None else f"{first.ratio:.4f}"
        print(
            f"       {item.code}  {item.rows:>5} 行  作废 {item.invalidated_rows:>5} 行"
            f"  {len(item.breaks)} 处断链  首次 {first.date} ratio={ratio}"
            f" ({first.reason})"
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit the pre_close adjustment chain (read-only)"
    )
    parser.add_argument("--db", default=None, help="database path")
    parser.add_argument(
        "--codes",
        default=None,
        help="只抽查这些代码，逗号分隔；缺省抽查全部原始价代码",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="最多抽查多少个代码"
    )
    parser.add_argument(
        "--list-limit", type=int, default=50, help="断链清单最多打印多少行"
    )
    args = parser.parse_args(argv)

    db_path = args.db or default_db_path()
    print(f"[info] database: {os.path.abspath(db_path)} (read-only)")
    if not os.path.exists(db_path):
        print(f"[error] database not found: {db_path}", file=sys.stderr)
        return 1

    con = connect_readonly(db_path)
    cur = con.cursor()
    try:
        if args.codes:
            codes = [item.strip() for item in args.codes.split(",") if item.strip()]
        else:
            codes = fetch_codes(cur)
        if args.limit is not None:
            codes = codes[: args.limit]

        if not codes:
            print(
                f"[error] {TABLE} 里没有 adj_convention='{RAW_CONVENTION}' 的行",
                file=sys.stderr,
            )
            return 1

        print(f"[info] 待抽查代码: {len(codes)}")
        audit = Audit()
        for index, code in enumerate(codes, start=1):
            audit_code(audit, code, fetch_series(cur, code))
            if index % PROGRESS_EVERY == 0:
                print(f"[info] {index}/{len(codes)} {datetime.now():%H:%M:%S}")
    except sqlite3.Error as exc:
        print(f"[error] cannot read {TABLE}: {exc}", file=sys.stderr)
        return 1
    finally:
        con.close()

    if not audit.codes:
        print("[error] 没有解出任何序列", file=sys.stderr)
        return 1

    print_report(audit, args.list_limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
