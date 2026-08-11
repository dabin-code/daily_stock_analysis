# -*- coding: utf-8 -*-
"""修复 stock_daily 历史数据中的成交量/成交额单位错误。

背景：
  stock_daily.volume 的口径是「股」、amount 的口径是「元」，但历史上有三条写入
  路径未做单位换算：
    - src/services/market_data_sync_service.py  每日 bulk 同步（Tushare：手 + 千元）
    - src/services/fast_backfill_service.py     批量回补（Tushare：手 + 千元）
    - scripts/fast_backfill.py                  回补脚本（Tushare：手 + 千元）
    - data_provider/efinance_fetcher.py         efinance 历史 K 线（东财：手）
  写入侧已修复，本脚本负责修正存量数据。

判定方式：
  用 amount / (volume * close) 的量级判定，该比值的物理含义是「成交均价 / 收盘价」，
  单位正确时必然接近 1（受涨跌停限制，实测全库落在 0.89 ~ 1.19）。
  单位错误会让该比值偏离一到两个数量级，因此分类没有歧义：
    ~1    → 单位正确，跳过
    ~0.1  → 成交量为手、成交额为千元 → volume x100, amount x1000
    ~100  → 成交量为手、成交额为元   → volume x100
  落在三个区间之外的行不做任何修改，仅登记，交由人工确认。

幂等性：
  修复后所有行都落入「正确」区间，重复执行不会二次放大。

用法：
  python scripts/repair_volume_amount_units.py              # dry-run，只报告
  python scripts/repair_volume_amount_units.py --apply      # 真正写库
"""

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# 判定区间。正确值实测落在 [0.89, 1.19]，错误值落在 ~0.1 与 ~100，
# 边界留有充足余量。
BAND_OK = (0.5, 2.0)
BAND_LOTS_AND_THOUSAND = (0.05, 0.2)   # volume x100, amount x1000
BAND_LOTS_ONLY = (50.0, 200.0)         # volume x100

_EVALUABLE = "volume > 0 AND amount > 0 AND close > 0"
_RATIO = "amount / (volume * close)"


def _count(cur, where: str) -> int:
    return cur.execute(
        f"SELECT COUNT(*) FROM stock_daily WHERE {_EVALUABLE} AND {where}"
    ).fetchone()[0]


def _band(name: str, lo: float, hi: float) -> str:
    return f"{_RATIO} BETWEEN {lo} AND {hi}"


def report(cur, title: str) -> dict:
    logger.info("=== %s ===", title)
    stats = {
        "ok": _count(cur, _band("ok", *BAND_OK)),
        "lots_and_thousand": _count(cur, _band("lt", *BAND_LOTS_AND_THOUSAND)),
        "lots_only": _count(cur, _band("lo", *BAND_LOTS_ONLY)),
    }
    stats["outlier"] = _count(
        cur,
        f"{_RATIO} NOT BETWEEN {BAND_OK[0]} AND {BAND_OK[1]}"
        f" AND {_RATIO} NOT BETWEEN {BAND_LOTS_AND_THOUSAND[0]} AND {BAND_LOTS_AND_THOUSAND[1]}"
        f" AND {_RATIO} NOT BETWEEN {BAND_LOTS_ONLY[0]} AND {BAND_LOTS_ONLY[1]}",
    )
    stats["unevaluable"] = cur.execute(
        f"SELECT COUNT(*) FROM stock_daily WHERE NOT ({_EVALUABLE})"
    ).fetchone()[0]

    logger.info("  单位正确            : %d", stats["ok"])
    logger.info("  需 vol x100 amt x1000: %d", stats["lots_and_thousand"])
    logger.info("  需 vol x100          : %d", stats["lots_only"])
    logger.info("  区间外（不修改）     : %d", stats["outlier"])
    logger.info("  无法判定（量/额为零）: %d", stats["unevaluable"])
    return stats


def list_outliers(cur, limit: int = 20) -> None:
    rows = cur.execute(
        f"""SELECT code, date, data_source, close, volume, amount,
                   ROUND({_RATIO}, 4)
            FROM stock_daily
            WHERE {_EVALUABLE}
              AND {_RATIO} NOT BETWEEN {BAND_OK[0]} AND {BAND_OK[1]}
              AND {_RATIO} NOT BETWEEN {BAND_LOTS_AND_THOUSAND[0]} AND {BAND_LOTS_AND_THOUSAND[1]}
              AND {_RATIO} NOT BETWEEN {BAND_LOTS_ONLY[0]} AND {BAND_LOTS_ONLY[1]}
            ORDER BY code, date LIMIT ?""",
        (limit,),
    ).fetchall()
    if not rows:
        return
    logger.warning("区间外样本（保持原样，需人工确认）：")
    for r in rows:
        logger.warning("  %s %s %-22s close=%s vol=%s amt=%s ratio=%s", *r)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真正写库；不加则仅报告")
    parser.add_argument("--db", default=None, help="数据库路径，默认取配置")
    args = parser.parse_args()

    db_path = args.db or get_config().database_path
    if not Path(db_path).exists():
        logger.error("数据库不存在：%s", db_path)
        return 1

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    before = report(cur, "修复前")
    list_outliers(cur)

    need_fix = before["lots_and_thousand"] + before["lots_only"]
    if need_fix == 0:
        logger.info("没有需要修复的行，退出。")
        conn.close()
        return 0

    if not args.apply:
        logger.info("")
        logger.info("dry-run：将修复 %d 行。加 --apply 真正执行。", need_fix)
        logger.info("执行前请先备份：copy %s %s.bak", db_path, db_path)
        conn.close()
        return 0

    logger.info("开始修复 %d 行...", need_fix)
    lo, hi = BAND_LOTS_AND_THOUSAND
    cur.execute(
        f"""UPDATE stock_daily
            SET volume = volume * 100, amount = amount * 1000,
                updated_at = datetime('now')
            WHERE {_EVALUABLE} AND {_RATIO} BETWEEN {lo} AND {hi}"""
    )
    n1 = cur.rowcount

    lo, hi = BAND_LOTS_ONLY
    cur.execute(
        f"""UPDATE stock_daily
            SET volume = volume * 100,
                updated_at = datetime('now')
            WHERE {_EVALUABLE} AND {_RATIO} BETWEEN {lo} AND {hi}"""
    )
    n2 = cur.rowcount
    conn.commit()
    logger.info("已修复：vol x100 amt x1000 = %d 行，vol x100 = %d 行", n1, n2)

    after = report(cur, "修复后")
    conn.close()

    remaining = after["lots_and_thousand"] + after["lots_only"]
    if remaining:
        logger.error("仍有 %d 行处于错误区间，请检查。", remaining)
        return 1
    if after["outlier"] != before["outlier"]:
        logger.error(
            "区间外行数发生变化（%d -> %d），说明修复越界，请从备份恢复。",
            before["outlier"], after["outlier"],
        )
        return 1
    logger.info("修复完成，全部可判定行已落入正确区间。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
