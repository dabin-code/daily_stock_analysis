# -*- coding: utf-8 -*-
"""交易日历服务：落库、fail-closed 查询、与 exchange_calendars 对照。"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 与 DatabaseManager 的忙等超时对齐（秒）
_SQLITE_TIMEOUT_SECONDS = 30


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
        """连接获取的唯一入口，所有读写方法都必须走这里。

        显式传 timeout：WAL 是文件级持久设置能自动继承，busy_timeout 是连接级的，
        不传就只有 sqlite3 默认的 5 秒，而 DatabaseManager 配的是 30 秒。
        本服务在数小时的历史回补期间与其他写入方并发，最容易撞锁。
        """
        conn = sqlite3.connect(self._db_path, timeout=_SQLITE_TIMEOUT_SECONDS)
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
    def sync(self, date_from: date, date_to: date, market: str = "cn") -> Dict[str, object]:
        """akshare 主源抓取，exchange_calendars 对照。

        对照不一致时**不自动裁决**，只记录 cross_check='mismatch' 并告警——
        日历是所有缺口归因的基准，静默取其一会让后续全部结论建立在猜测上。

        请求区间超出信源覆盖时按信源裁剪，返回值里的 `covered_to` 给出实际
        写到哪天。同理由：源里「不在开市日集合中」在覆盖范围内表示休市，
        范围外只表示未知，两者混同会让 fail-closed 查询对未知日期给出确定答案。
        """
        source_days = self._fetch_source_open_days()
        days = self._to_calendar_rows(source_days, date_from, date_to)
        written = self.upsert_days(market, days, source="akshare")
        mismatches = self._cross_check(market, days)
        if mismatches:
            logger.warning(
                "trading_calendar cross-check mismatch on %d dates: %s",
                len(mismatches),
                [d.isoformat() for d in mismatches[:10]],
            )
        covered_to = days[-1][0] if days else None
        return {
            "written": written,
            "mismatch": len(mismatches),
            "requested_from": date_from.isoformat(),
            "requested_to": date_to.isoformat(),
            "covered_from": days[0][0].isoformat() if days else None,
            "covered_to": covered_to.isoformat() if covered_to else None,
        }

    def _fetch_source_open_days(self) -> List[date]:
        """主源的全部开市日，不做区间过滤——覆盖边界由调用方判定。"""
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        return sorted(
            d for d in (_as_date(v) for v in df["trade_date"].tolist()) if d is not None
        )

    def _to_calendar_rows(
        self,
        source_days: Sequence[date],
        date_from: date,
        date_to: date,
    ) -> List[Tuple[date, bool]]:
        if not source_days:
            logger.warning("trading_calendar source returned no open days, nothing to write")
            return []

        open_days = set(source_days)
        start = max(date_from, min(source_days))
        end = min(date_to, max(source_days))
        if end < date_to:
            logger.warning(
                "trading_calendar source only covers up to %s, "
                "requested %s; dates beyond are left unknown rather than closed",
                end.isoformat(),
                date_to.isoformat(),
            )
        if start > date_from:
            logger.warning(
                "trading_calendar source starts at %s, requested %s",
                start.isoformat(),
                date_from.isoformat(),
            )

        result: List[Tuple[date, bool]] = []
        cursor = start
        while cursor <= end:
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
