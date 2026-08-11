# -*- coding: utf-8 -*-
"""
快速全市场历史数据回填（按交易日批量获取）

核心策略：
  使用 Tushare daily(trade_date=...) 一次获取全市场某日数据（~5000 行/次），
  200+ 个交易日只需 200+ 次 API 调用，替代逐只股票调用 5000+ 次。

预计耗时：
  250 交易日 × (~0.5s API + ~3s 保存) ≈ 15 分钟

使用方法：
  python scripts/fast_backfill.py
  python scripts/fast_backfill.py --days 250
  python scripts/fast_backfill.py --dry-run
"""

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_provider.base import lots_to_shares, thousand_yuan_to_yuan
from src.services.fast_backfill_service import FastBackfillService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def get_tushare_api() -> Any:
    from src.config import get_config
    config = get_config()
    if not config.tushare_token:
        logger.error("TUSHARE_TOKEN 未配置")
        sys.exit(1)
    import tushare as ts
    ts.set_token(config.tushare_token)
    api = ts.pro_api()
    logger.info("Tushare API 初始化成功")
    return api


def get_db_path() -> str:
    from src.config import get_config
    return getattr(get_config(), "database_path", "./data/stock_analysis.db")


def parse_date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def get_trade_dates_from_db(reference_code: str = "000070") -> list:
    """从已有的 STOCK_LIST 数据中提取交易日历。"""
    conn = sqlite3.connect(get_db_path())
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT date FROM stock_daily WHERE code=? ORDER BY date",
        (reference_code,),
    )
    dates = [r[0] for r in cur.fetchall()]
    conn.close()
    return dates


def get_already_filled_dates() -> set:
    """获取已经有全市场数据的日期（>3000 只股票有数据的日期）。"""
    conn = sqlite3.connect(get_db_path())
    cur = conn.cursor()
    cur.execute(
        "SELECT date, COUNT(DISTINCT code) as cnt FROM stock_daily "
        "GROUP BY date HAVING cnt >= 3000"
    )
    filled = {r[0] for r in cur.fetchall()}
    conn.close()
    return filled


def fetch_daily_all(api: Any, trade_date_yyyymmdd: str, retry: int = 3) -> Any:
    for attempt in range(retry):
        try:
            df = api.daily(trade_date=trade_date_yyyymmdd)
            if df is not None and not df.empty:
                return df
            return pd.DataFrame()
        except Exception as e:
            if attempt < retry - 1:
                wait = 2 ** (attempt + 1)
                logger.warning(f"  {trade_date_yyyymmdd} attempt {attempt+1} 失败: {e}, 等 {wait}s")
                time.sleep(wait)
            else:
                logger.error(f"  {trade_date_yyyymmdd} 最终失败: {e}")
                return pd.DataFrame()


def fetch_index_daily(api: Any, trade_date_yyyymmdd: str) -> Any:
    try:
        df = api.index_daily(ts_code="000001.SH", trade_date=trade_date_yyyymmdd)
        return df if df is not None else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def save_day_data(db_path: str, day_df: Any, index_df: Any) -> int:
    """直接用 SQLite 批量 INSERT OR REPLACE，比 ORM 快 10x+。"""
    if day_df.empty and index_df.empty:
        return 0

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    now = datetime.now().isoformat()
    rows_saved = 0

    for source_df, source_name, code_prefix in [
        (day_df, "TushareFetcher", ""),
        (index_df, "TushareFetcher", "sh"),
    ]:
        if source_df.empty:
            continue
        for _, row in source_df.iterrows():
            ts_code = str(row.get("ts_code", ""))
            code = code_prefix + ts_code.split(".")[0] if ts_code else ""
            td = str(row.get("trade_date", ""))
            date_str = f"{td[:4]}-{td[4:6]}-{td[6:8]}" if len(td) == 8 else td

            cur.execute(
                "INSERT OR REPLACE INTO stock_daily "
                "(code, date, open, high, low, close, volume, amount, pct_chg, data_source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    code,
                    date_str,
                    row.get("open"),
                    row.get("high"),
                    row.get("low"),
                    row.get("close"),
                    lots_to_shares(row.get("vol")),
                    thousand_yuan_to_yuan(row.get("amount")),
                    row.get("pct_chg"),
                    source_name,
                    now,
                    now,
                ),
            )
            rows_saved += 1

    conn.commit()
    conn.close()
    return rows_saved


def main():
    parser = argparse.ArgumentParser(description="快速全市场历史数据回填")
    parser.add_argument("--days", type=int, default=250, help="回填最近 N 个交易日")
    parser.add_argument("--to-date", dest="to_date", type=parse_date, help="回填到指定交易日（YYYY-MM-DD）")
    parser.add_argument("--market", default="cn", help="市场，默认 cn")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-filled", action="store_true", default=True,
                        help="跳过已有全市场数据的日期")
    args = parser.parse_args()

    if args.to_date:
        if args.dry_run:
            logger.info(f"[DRY RUN] 计划快速回填到 {args.to_date.isoformat()} market={args.market}")
            return
        result = FastBackfillService().backfill_to_trade_date(
            target_trade_date=args.to_date,
            market=args.market,
        )
        logger.info(
            "回填到目标日期完成: status=%s target=%s saved_rows=%s failed_dates=%s audit=%s/%s",
            result.get("status"),
            result.get("target_trade_date"),
            result.get("saved_rows"),
            len(result.get("failed_dates") or []),
            (result.get("governance_result") or {}).get("run_result"),
            (result.get("governance_result") or {}).get("pass_status"),
        )
        return

    api = get_tushare_api()
    db_path = get_db_path()

    all_trade_dates = get_trade_dates_from_db()
    if not all_trade_dates:
        logger.error("数据库中无交易日历参考数据，请先运行 data_reset_and_backfill.py --backfill-watchlist")
        return

    target_dates = all_trade_dates[-args.days:] if len(all_trade_dates) > args.days else all_trade_dates
    logger.info(f"目标交易日: {len(target_dates)} 个 ({target_dates[0]} ~ {target_dates[-1]})")

    if args.skip_filled:
        already_filled = get_already_filled_dates()
        todo_dates = [d for d in target_dates if d not in already_filled]
        logger.info(f"已有全市场数据的日期: {len(already_filled)}, 待回填: {len(todo_dates)}")
    else:
        todo_dates = target_dates

    if not todo_dates:
        logger.info("所有日期已回填完成，无需操作")
        return

    if args.dry_run:
        logger.info(f"[DRY RUN] 计划回填 {len(todo_dates)} 个交易日")
        logger.info(f"  预计 API 调用: ~{len(todo_dates) * 2} 次 (stock + index)")
        logger.info(f"  预计耗时: ~{len(todo_dates) * 4 / 60:.0f} 分钟")
        return

    total_saved = 0
    failed = []
    start = time.time()

    call_count = 0
    minute_start = time.time()

    for i, td in enumerate(todo_dates):
        td_fmt = td.replace("-", "")

        if call_count >= 45:
            elapsed_in_minute = time.time() - minute_start
            if elapsed_in_minute < 65:
                wait = 65 - elapsed_in_minute
                logger.info(f"  [限速] 已调用 {call_count} 次/分钟，等待 {wait:.0f}s...")
                time.sleep(wait)
            call_count = 0
            minute_start = time.time()

        df = fetch_daily_all(api, td_fmt)
        call_count += 1

        idx_df = pd.DataFrame()

        if df.empty:
            failed.append(td)
        else:
            saved = save_day_data(db_path, df, idx_df)
            total_saved += saved

        elapsed = time.time() - start
        if (i + 1) % 20 == 0 or i == len(todo_dates) - 1:
            rate = (i + 1) / elapsed * 60 if elapsed > 0 else 0
            eta = (len(todo_dates) - i - 1) / rate if rate > 0 else 0
            logger.info(
                f"[{i+1}/{len(todo_dates)}] 保存={total_saved}, "
                f"失败={len(failed)}, 速率={rate:.0f} 日/min, "
                f"剩余~{eta:.0f}min, 已用时={elapsed:.0f}s"
            )

        time.sleep(1.3)

    elapsed = time.time() - start
    logger.info("=" * 60)
    logger.info("回填完成!")
    logger.info(f"  交易日: {len(todo_dates)}")
    logger.info(f"  保存行数: {total_saved}")
    logger.info(f"  失败: {len(failed)}")
    logger.info(f"  耗时: {elapsed:.0f}s ({elapsed/60:.1f}min)")
    if failed:
        logger.warning(f"  失败列表: {failed[:20]}")

    logger.info("=" * 60)
    logger.info("数据验证...")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM stock_daily")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT code) FROM stock_daily")
    distinct = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM (SELECT code FROM stock_daily GROUP BY code HAVING COUNT(*) >= 200)")
    good = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM (SELECT code FROM stock_daily GROUP BY code HAVING COUNT(*) < 100)")
    bad = cur.fetchone()[0]
    cur.execute("SELECT MIN(date), MAX(date) FROM stock_daily")
    dr = cur.fetchone()
    conn.close()
    logger.info(f"  总行数: {total}")
    logger.info(f"  股票数: {distinct}")
    logger.info(f"  日期范围: {dr[0]} ~ {dr[1]}")
    logger.info(f"  200+ 行(满足 MA100): {good} 只")
    logger.info(f"  < 100 行(数据不足): {bad} 只")


if __name__ == "__main__":
    main()
