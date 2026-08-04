from __future__ import annotations

import logging
import sqlite3
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from src.config import get_config
from src.core.trading_calendar import is_market_open
from src.services.kline_governance_schedule_service import KlineGovernanceScheduleService

logger = logging.getLogger(__name__)


class FastBackfillService:
    """按交易日批量回填全市场 K 线到目标日期。"""

    def __init__(
        self,
        *,
        db_path: Optional[str] = None,
        tushare_api: Optional[Any] = None,
        governance_service: Optional[KlineGovernanceScheduleService] = None,
        min_full_count: int = 3000,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = get_config()
        self.db_path = db_path or getattr(self.config, "database_path", "./data/stock_analysis.db")
        self._tushare_api = tushare_api
        self.governance_service = governance_service or KlineGovernanceScheduleService(config=self.config)
        self.min_full_count = max(1, int(min_full_count))
        self.sleep = sleep

    def backfill_to_trade_date(self, target_trade_date: date, market: str = "cn") -> Dict[str, Any]:
        if target_trade_date > date.today():
            raise ValueError("目标交易日不能是未来日期")

        latest_complete_date = self._get_latest_complete_date()
        if latest_complete_date is None:
            raise ValueError("stock_daily 中没有达到全市场覆盖阈值的基准日期，无法计算回填起点")

        if target_trade_date <= latest_complete_date:
            target_dates = (
                [target_trade_date]
                if is_market_open(market, target_trade_date) and not self._is_date_complete(target_trade_date)
                else []
            )
        else:
            target_dates = self._build_target_dates(
                start_date=latest_complete_date + timedelta(days=1),
                target_trade_date=target_trade_date,
                market=market,
            )

        if not target_dates:
            governance_result = self._run_target_governance(target_trade_date, market)
            return {
                "status": "already_complete",
                "target_trade_date": target_trade_date.isoformat(),
                "from_trade_date": latest_complete_date.isoformat(),
                "backfilled_dates": [],
                "saved_rows": 0,
                "failed_dates": [],
                "governance_result": governance_result,
            }

        api = self._get_tushare_api()
        saved_rows = 0
        failed_dates: List[str] = []
        call_count = 0
        minute_started_at = time.time()

        for current_date in target_dates:
            if call_count >= 45:
                elapsed_in_minute = time.time() - minute_started_at
                if elapsed_in_minute < 65:
                    self.sleep(65 - elapsed_in_minute)
                call_count = 0
                minute_started_at = time.time()

            trade_date_yyyymmdd = current_date.strftime("%Y%m%d")
            day_df = self._fetch_daily_all(api, trade_date_yyyymmdd)
            call_count += 1
            if day_df.empty:
                failed_dates.append(current_date.isoformat())
                continue
            saved_rows += self._save_day_data(day_df)
            self.sleep(1.3)

        governance_result = self._run_target_governance(target_trade_date, market)
        return {
            "status": "completed" if not failed_dates else "completed_with_errors",
            "target_trade_date": target_trade_date.isoformat(),
            "from_trade_date": latest_complete_date.isoformat(),
            "backfilled_dates": [item.isoformat() for item in target_dates],
            "saved_rows": saved_rows,
            "failed_dates": failed_dates,
            "governance_result": governance_result,
        }

    def _get_latest_complete_date(self) -> Optional[date]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT date
                FROM stock_daily
                GROUP BY date
                HAVING COUNT(DISTINCT code) >= ?
                ORDER BY date DESC
                LIMIT 1
                """,
                (self.min_full_count,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return datetime.strptime(str(row[0]), "%Y-%m-%d").date()

    def _build_target_dates(self, *, start_date: date, target_trade_date: date, market: str) -> List[date]:
        if start_date > target_trade_date:
            return []
        dates: List[date] = []
        current_date = start_date
        while current_date <= target_trade_date:
            if is_market_open(market, current_date) and not self._is_date_complete(current_date):
                dates.append(current_date)
            current_date += timedelta(days=1)
        return dates

    def _is_date_complete(self, trade_date: date) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT code) FROM stock_daily WHERE date=?",
                (trade_date.isoformat(),),
            ).fetchone()
        return int(row[0] if row else 0) >= self.min_full_count

    def _get_tushare_api(self) -> Any:
        if self._tushare_api is not None:
            return self._tushare_api
        if not getattr(self.config, "tushare_token", None):
            raise ValueError("TUSHARE_TOKEN 未配置，无法执行快速回填")
        import tushare as ts

        ts.set_token(self.config.tushare_token)
        self._tushare_api = ts.pro_api()
        return self._tushare_api

    @staticmethod
    def _fetch_daily_all(api: Any, trade_date_yyyymmdd: str, retry: int = 3) -> pd.DataFrame:
        for attempt in range(retry):
            try:
                df = api.daily(trade_date=trade_date_yyyymmdd)
                return df if df is not None and not df.empty else pd.DataFrame()
            except Exception as exc:
                if attempt >= retry - 1:
                    logger.error("fast backfill failed for %s: %s", trade_date_yyyymmdd, exc)
                    return pd.DataFrame()
                time.sleep(2 ** (attempt + 1))
        return pd.DataFrame()

    def _save_day_data(self, day_df: pd.DataFrame) -> int:
        if day_df.empty:
            return 0

        now = datetime.now().isoformat()
        rows_saved = 0
        with sqlite3.connect(self.db_path) as conn:
            for _, row in day_df.iterrows():
                ts_code = str(row.get("ts_code", ""))
                code = ts_code.split(".")[0] if ts_code else ""
                trade_date_value = str(row.get("trade_date", ""))
                date_str = (
                    f"{trade_date_value[:4]}-{trade_date_value[4:6]}-{trade_date_value[6:8]}"
                    if len(trade_date_value) == 8
                    else trade_date_value
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO stock_daily
                    (code, date, open, high, low, close, volume, amount, pct_chg, data_source, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        code,
                        date_str,
                        row.get("open"),
                        row.get("high"),
                        row.get("low"),
                        row.get("close"),
                        row.get("vol"),
                        row.get("amount"),
                        row.get("pct_chg"),
                        "TushareFetcher",
                        now,
                        now,
                    ),
                )
                rows_saved += 1
            conn.commit()
        return rows_saved

    def _run_target_governance(self, target_trade_date: date, market: str) -> Dict[str, Any]:
        # 优先逐日补跑治理：把审计通过日从最近通过日推进到目标日，避免中间被跳过的
        # 交易日停牌缺口长期滞留窗口内、使目标日始终 not_passed 而无法选股。
        catch_up = getattr(self.governance_service, "run_daily_governance_with_catch_up", None)
        if callable(catch_up):
            result = catch_up(trade_date=target_trade_date, market=market)
        else:
            result = self.governance_service.run_daily_governance(
                trade_date=target_trade_date,
                market=market,
            )
        return {
            "trade_date": result.get("trade_date").isoformat()
            if hasattr(result.get("trade_date"), "isoformat")
            else result.get("trade_date"),
            "run_result": result.get("run_result"),
            "pass_status": result.get("pass_status"),
            "reason": result.get("reason"),
        }
