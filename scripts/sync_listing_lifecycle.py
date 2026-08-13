# -*- coding: utf-8 -*-
"""回填 instrument_master 的上市/退市日期，消除历史研究里的幸存者偏差。

本脚本只是 ``ListingLifecycleService`` 的运维壳：抓取、多状态编排、原子写、
「空结果 vs 抓取失败」判定全在服务层，这里只负责两件事——动手之前把本次要
花掉的 API 额度摆到台面上，跑完把库里的实际覆盖度读回来。

**为什么必须先跑 --dry-run**：tushare ``stock_basic`` 在本账号的免费额度
（120 积分）下实测被限到 **5 次/天**，另有约 1 次/分钟、1 次/小时的次级上限，
观测到的拒绝文案是 ``抱歉，您访问接口(stock_basic)频率超限(5次/天)``。一次
完整同步要 3 次调用（L / D / P），只有一次都不浪费才塞得进 5 次/天。
**每一次失败的尝试同样烧额度**，所以本脚本不重试、不做任何试探性调用，并在
真正发起调用之前把次数打出来让操作员确认。

失败后立刻重跑只会再烧一次额度，什么都换不回来——限频类失败会额外打一条
``[hint]`` 说明这一点。

Usage:
    python scripts/sync_listing_lifecycle.py --dry-run
    python scripts/sync_listing_lifecycle.py
    python scripts/sync_listing_lifecycle.py --list-statuses L,D
    python scripts/sync_listing_lifecycle.py --pause-seconds 90
    python scripts/sync_listing_lifecycle.py --source baostock

库路径沿用主程序约定：优先 ``DATABASE_PATH``，否则回落到仓库内
``data/stock_analysis.db``；解析结果每次运行都会回显，用来确认动的是哪个文件。

幂等性：重复运行只是用同一份上游数据覆盖同样的字段，不会产生重复行；但每跑
一次就再花一遍额度，所以「重跑一次看看」在这个脚本上不是免费动作。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.listing_lifecycle_service import ListingLifecycleService

FALLBACK_DB_PATH = PROJECT_ROOT / "data" / "stock_analysis.db"

SOURCE_TUSHARE = "tushare"
SOURCE_BAOSTOCK = "baostock"

DEFAULT_LIST_STATUSES = "L,D,P"

# 默认 65 秒而不是 0：stock_basic 免费额度除了 5 次/天，还有约 1 次/分钟的次级
# 上限。状态之间不留够一分钟，第二个状态会直接撞限频，而撞一次就少一次今天
# 可用的额度。这是「状态之间的间隔」，不是重试等待——本脚本没有重试。
DEFAULT_PAUSE_SECONDS = 65.0

# 限频原因在服务层被包进 RuntimeError 的文案里，拿不到结构化错误码，只能按
# 子串识别。宁可多认几个词也不能漏：漏判的代价是操作员立刻重跑再烧一次额度。
RATE_LIMIT_MARKERS = (
    "超限",
    "限频",
    "最多访问",
    "频率",
    "配额",
    "积分",
    "rate limit",
    "quota",
)


def default_db_path() -> Path:
    """默认库路径复用主程序约定：优先 DATABASE_PATH，否则仓库内 data/。

    读环境变量而不是固定仓库内路径：库放在独立卷 / 托管实例上时，写死仓库内
    路径会让脚本对着一个同名空壳库报告覆盖度，而真正被服务写入的是另一个文件。
    """
    env_path = os.environ.get("DATABASE_PATH")
    if env_path:
        return Path(env_path)
    return FALLBACK_DB_PATH


def parse_list_statuses(raw: str) -> List[str]:
    """``"L,D,P"`` → ``["L", "D", "P"]``；空片段丢掉。

    统一大写后交给服务层：服务层的「不可为空状态」判定按大写比对，这里放过
    小写会让 ``--list-statuses l,d`` 的空结果被当成合法，退市样本无声缺席。
    """
    return [item.strip().upper() for item in str(raw or "").split(",") if item.strip()]


def planned_api_calls(source: str, list_statuses: List[str]) -> int:
    """本次运行要消耗的上游调用次数。

    tushare 必须按 list_status 逐个拉（stock_basic 一次只返回一种状态），
    所以次数等于状态数；baostock 的 query_stock_basic 不传 code 时一次就返回
    含已退市证券的全市场，固定 1 次。
    """
    if source == SOURCE_BAOSTOCK:
        return 1
    return len(list_statuses)


def looks_rate_limited(message: str) -> bool:
    """错误文案是否指向限频/额度耗尽。"""
    text = str(message or "").lower()
    return any(marker.lower() in text for marker in RATE_LIMIT_MARKERS)


def lifecycle_coverage(db_path: Path) -> Optional[Tuple[int, int]]:
    """读回 list_date / delist_date 的非空行数，读不到返回 None。

    刻意独立查一次库而不是从服务返回的计数推算：``written`` 只说明这次提交了
    多少行，证明幸存者偏差能被纠正的是「库里此刻真的存着多少个退市日期」。
    读不到（库不存在、表还没建、库被锁）不能让已经成功的同步变成失败退出码，
    所以这里返回 None 由调用方降级成一条告警。
    """
    if not db_path.exists():
        return None
    try:
        # 只读打开：同步已经结束，这里只是取证，没有理由再持有写锁。
        # as_uri() 要求绝对路径，而 DATABASE_PATH 允许是相对路径。
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except (sqlite3.Error, ValueError, OSError):
        return None
    try:
        row = conn.execute(
            "SELECT SUM(list_date IS NOT NULL), SUM(delist_date IS NOT NULL) "
            "FROM instrument_master"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return int(row[0] or 0), int(row[1] or 0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill instrument_master list_date / delist_date so historical "
            "research is not restricted to survivors."
        ),
    )
    parser.add_argument(
        "--source",
        choices=(SOURCE_TUSHARE, SOURCE_BAOSTOCK),
        default=SOURCE_TUSHARE,
        help=(
            f"Upstream data source (default: {SOURCE_TUSHARE}). "
            "baostock is kept because the code path still exists and the service "
            "may come back, but it is currently down at the service level."
        ),
    )
    # default=None 而不是 DEFAULT_LIST_STATUSES：只有区分「没传」和「传了」才能
    # 在 --source baostock 时对显式传入报错，而不是静默忽略。
    parser.add_argument(
        "--list-statuses",
        default=None,
        help=(
            f"Comma-separated tushare list_status values (default: {DEFAULT_LIST_STATUSES}; "
            "L=listed D=delisted P=suspended). One API call per status. "
            "Only meaningful with --source tushare."
        ),
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=DEFAULT_PAUSE_SECONDS,
        help=(
            f"Seconds to wait *between* status fetches (default: {DEFAULT_PAUSE_SECONDS:g}). "
            "This is spacing to stay under the ~1-call-per-minute secondary cap, "
            "NOT a retry delay — this script never retries."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the fetch plan and the number of API calls it would consume, "
            "then exit 0 without making any API call or writing to the database."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.source == SOURCE_BAOSTOCK and args.list_statuses is not None:
        # 静默忽略比报错危险：操作员会以为自己限定了抓取范围，实际拿到的是
        # 另一回事，而这个脚本的产出正是「样本范围」本身。
        print(
            "[error] --list-statuses only applies to --source tushare; "
            "baostock returns listed and delisted securities in a single call.",
            file=sys.stderr,
        )
        return 2

    statuses = parse_list_statuses(
        DEFAULT_LIST_STATUSES if args.list_statuses is None else args.list_statuses
    )
    if args.source == SOURCE_TUSHARE and not statuses:
        print(
            "[error] --list-statuses parsed to an empty list; nothing to fetch.",
            file=sys.stderr,
        )
        return 2

    db_path = default_db_path()
    api_calls = planned_api_calls(args.source, statuses)

    # 预检输出必须在任何调用之前，且额度代价要显眼：额度是本任务最稀缺的资源。
    print(f"[info] Target database: {db_path}")
    print(f"[info] Source: {args.source}")
    if args.source == SOURCE_TUSHARE:
        print(f"[info] List statuses to fetch: {', '.join(statuses)}")
        print(f"[info] Pause between statuses: {args.pause_seconds:g}s")
    else:
        print(
            "[info] List statuses: n/a "
            "(baostock returns the whole market, delisted included, in one call)"
        )
    print(f"[info] API QUOTA COST: this run will consume {api_calls} API call(s).")
    if args.source == SOURCE_TUSHARE:
        print(
            "[info] tushare stock_basic free-tier caps observed on this account: "
            "5 calls/DAY, ~1/minute, ~1/hour. Failed attempts burn quota too, "
            "and this script never retries."
        )

    if args.dry_run:
        print(
            "[dry-run] No API call made and no database write performed. "
            "Re-run without --dry-run to spend the quota above."
        )
        return 0

    service = ListingLifecycleService()
    try:
        if args.source == SOURCE_BAOSTOCK:
            stats = service.sync_from_baostock()
        else:
            stats = service.sync_from_tushare(
                list_statuses=statuses,
                pause_seconds=args.pause_seconds,
            )
    except (RuntimeError, ValueError) as exc:
        print(f"[error] Listing lifecycle sync failed: {exc}", file=sys.stderr)
        if looks_rate_limited(str(exc)):
            print(
                "[hint] This looks like a rate-limit rejection — the daily quota may "
                "already be exhausted. Retrying now will only burn more of it; wait "
                "for the cap window to roll over before the next attempt.",
                file=sys.stderr,
            )
        return 1

    print(
        "[ok] Listing lifecycle synced: "
        f"total={stats.get('total')} written={stats.get('written')} "
        f"delisted={stats.get('delisted')}"
    )

    coverage = lifecycle_coverage(db_path)
    if coverage is None:
        print(
            f"[warn] Could not read instrument_master from {db_path}; "
            "coverage report skipped (the sync itself succeeded).",
            file=sys.stderr,
        )
    else:
        rows_with_list_date, rows_with_delist_date = coverage
        print(f"[ok] instrument_master rows with list_date: {rows_with_list_date}")
        print(f"[ok] instrument_master rows with delist_date: {rows_with_delist_date}")
        print(
            "[info] The delist_date count is the one that proves survivor bias can be "
            "corrected — a zero there means historical universes are still "
            "survivors-only."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
