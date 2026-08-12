# -*- coding: utf-8 -*-
"""交易日历服务：落库、fail-closed 查询、与 exchange_calendars 对照。"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


class CalendarNotCoveredError(RuntimeError):
    """查询的日期不在已落库的日历覆盖范围内。

    刻意设计成异常而非返回默认值：回补与缺口归因场景下，
    「猜一个答案」会静默制造假数据或假缺口。
    """


class TradingCalendarService:
    """统一走裸 sqlite3，不碰 DatabaseManager 单例。

    这样做是为了和 FastBackfillService 保持同一个库文件：后者用
    sqlite3.connect(self.db_path)，而 DatabaseManager 是单例，
    传 URL 构造并不会切库，两边会各读各的。
    统一成一种连接方式也免去了 SQLAlchemy 与 sqlite3 两套占位符语法。
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None:
            from src.config import get_config
            db_path = getattr(get_config(), "database_path", "./data/stock_analysis.db")
        self._db_path = db_path

    @contextmanager
    def _connect(self):
        """连接获取的唯一入口，所有读写方法都必须走这里。"""
        conn = sqlite3.connect(self._db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── 写入 ────────────────────────────────────────────────────────────
    def upsert_days(
        self,
        market: str,
        days: Sequence[Tuple[date, bool]],
        source: str = "akshare",
    ) -> int:
        if not days:
            return 0
        payload = [
            (market, trade_date.isoformat(), 1 if is_open else 0, source)
            for trade_date, is_open in days
        ]
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO trading_calendar "
                "(market, trade_date, is_open, source) VALUES (?, ?, ?, ?)",
                payload,
            )
        return len(payload)

    # ── 查询（fail-closed）──────────────────────────────────────────────
    def is_trading_day(self, check_date: date, market: str = "cn") -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT is_open FROM trading_calendar "
                "WHERE market = ? AND trade_date = ?",
                (market, check_date.isoformat()),
            ).fetchone()
        if row is None:
            raise CalendarNotCoveredError(
                f"trading_calendar has no row for {market} {check_date.isoformat()}; "
                f"run TradingCalendarService.sync() first"
            )
        return bool(row[0])

    def get_trading_days(
        self,
        date_from: date,
        date_to: date,
        market: str = "cn",
    ) -> List[date]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT trade_date FROM trading_calendar "
                "WHERE market = ? AND is_open = 1 "
                "AND trade_date >= ? AND trade_date <= ? "
                "ORDER BY trade_date",
                (market, date_from.isoformat(), date_to.isoformat()),
            ).fetchall()
        return [date.fromisoformat(str(r[0])) for r in rows]

    # ── 抓取 ────────────────────────────────────────────────────────────
    def sync(self, date_from: date, date_to: date, market: str = "cn") -> Dict[str, int]:
        """akshare 主源抓取，exchange_calendars 对照。

        对照不一致时**不自动裁决**，只记录 cross_check='mismatch' 并告警——
        日历是所有缺口归因的基准，静默取其一会让后续全部结论建立在猜测上。
        """
        days = self._fetch_from_akshare(date_from, date_to)
        written = self.upsert_days(market, days, source="akshare")
        mismatches = self._cross_check(market, days)
        if mismatches:
            logger.warning(
                "trading_calendar cross-check mismatch on %d dates: %s",
                len(mismatches),
                [d.isoformat() for d in mismatches[:10]],
            )
        return {"written": written, "mismatch": len(mismatches)}

    def _fetch_from_akshare(self, date_from: date, date_to: date) -> List[Tuple[date, bool]]:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        open_days = {
            d for d in (
                _as_date(v) for v in df["trade_date"].tolist()
            )
            if d is not None and date_from <= d <= date_to
        }
        result: List[Tuple[date, bool]] = []
        cursor = date_from
        while cursor <= date_to:
            result.append((cursor, cursor in open_days))
            cursor += timedelta(days=1)
        return result

    def _cross_check(self, market: str, days: Iterable[Tuple[date, bool]]) -> List[date]:
        try:
            from src.core.trading_calendar import is_market_open
        except Exception:  # noqa: BLE001
            return []

        mismatches: List[date] = []
        with self._connect() as conn:
            for trade_date, is_open in days:
                try:
                    reference = is_market_open(market, trade_date)
                except Exception:  # noqa: BLE001
                    continue
                verdict = "match" if bool(reference) == bool(is_open) else "mismatch"
                if verdict == "mismatch":
                    mismatches.append(trade_date)
                conn.execute(
                    "UPDATE trading_calendar SET cross_check = ? "
                    "WHERE market = ? AND trade_date = ?",
                    (verdict, market, trade_date.isoformat()),
                )
        return mismatches


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
