from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional
from uuid import uuid4

from sqlalchemy import and_, select

from src.core.trading_calendar import is_market_open
from src.storage import DatabaseManager, InstrumentMaster, KlineAuditGap, StockDaily

logger = logging.getLogger(__name__)


class KlineAuditService:
    """基于 instrument_master 与 stock_daily 生成 K 线审计结果。"""

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        rule_version: str = "kline_audit_v1",
    ) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.rule_version = rule_version

    def audit_trade_date(
        self,
        *,
        market: str,
        trade_date: date,
        window_start: date,
        window_end: date,
        run_type: str = "daily",
        trigger_type: str = "manual",
    ) -> Dict[str, object]:
        run_id = f"kline-audit-{trade_date.isoformat()}-{uuid4().hex[:8]}"
        expected_universe = self._resolve_expected_universe(market=market, window_end=window_end)
        fail_closed_empty_universe = len(expected_universe) == 0
        if fail_closed_empty_universe:
            logger.warning(
                "K line audit fail-closed because expected universe is empty: market=%s trade_date=%s",
                market,
                trade_date,
            )
        observed_by_code = self._load_observed_dates(
            codes=[item["code"] for item in expected_universe],
            window_start=window_start,
            window_end=window_end,
        )
        market_dates = self._build_market_dates(
            market=market,
            window_start=window_start,
            window_end=window_end,
        )
        target_is_market_session = self._is_market_session(market=market, trade_date=trade_date)
        approved_skips = self._load_approved_skips(market=market)

        market_day_gap_dates: List[date] = []
        symbol_gap_ranges: List[Dict[str, object]] = []

        for market_date in market_dates:
            expected_codes = [
                item["code"]
                for item in expected_universe
                if item["list_date"] is None or item["list_date"] <= market_date
            ]
            missing_codes = [
                code
                for code in expected_codes
                if market_date not in observed_by_code.get(code, set())
                and not self._is_date_skipped(
                    approved_skips=approved_skips,
                    code=code,
                    trade_date=market_date,
                )
            ]
            if missing_codes:
                market_day_gap_dates.append(market_date)

        for item in expected_universe:
            code = item["code"]
            missing_dates = [
                market_date
                for market_date in market_dates
                if item["list_date"] is None or item["list_date"] <= market_date
                if market_date not in observed_by_code.get(code, set())
                and not self._is_date_skipped(
                    approved_skips=approved_skips,
                    code=code,
                    trade_date=market_date,
                )
            ]
            if missing_dates:
                symbol_gap_ranges.extend(
                    self._collapse_dates_to_ranges(
                        code=code,
                        missing_dates=missing_dates,
                        market_dates=market_dates,
                    )
                )
        run_result = "degraded" if market_day_gap_dates or symbol_gap_ranges else "succeeded"
        if fail_closed_empty_universe:
            run_result = "degraded"
        if not target_is_market_session:
            run_result = "degraded"
        pass_status = "passed" if run_result == "succeeded" else "not_passed"
        passed_at = datetime.now() if pass_status == "passed" else None

        gap_payloads: List[Dict[str, object]] = []
        event_payloads: List[Dict[str, object]] = []
        for gap_date in market_day_gap_dates:
            gap_key = KlineAuditGap.build_gap_key(
                market=market,
                gap_scope="market_day_gap",
                trade_date=gap_date,
            )
            gap_payloads.append(
                {
                    "market": market,
                    "gap_scope": "market_day_gap",
                    "trade_date": gap_date,
                    "source_run_id": run_id,
                    "status": "open",
                }
            )
            event_payloads.append(
                {
                    "source_run_id": run_id,
                    "gap_key": gap_key,
                    "event_type": "gap_detected",
                    "event_status": "open",
                    "payload": {
                        "market": market,
                        "gap_scope": "market_day_gap",
                        "trade_date": gap_date.isoformat(),
                    },
                }
            )

        for gap_range in symbol_gap_ranges:
            gap_key = KlineAuditGap.build_gap_key(
                market=market,
                gap_scope="symbol_range_gap",
                code=str(gap_range["code"]),
                missing_date_from=gap_range["missing_date_from"],
                missing_date_to=gap_range["missing_date_to"],
            )
            gap_payloads.append(
                {
                    "market": market,
                    "gap_scope": "symbol_range_gap",
                    "code": str(gap_range["code"]),
                    "missing_date_from": gap_range["missing_date_from"],
                    "missing_date_to": gap_range["missing_date_to"],
                    "source_run_id": run_id,
                    "status": "open",
                }
            )
            event_payloads.append(
                {
                    "source_run_id": run_id,
                    "gap_key": gap_key,
                    "event_type": "gap_detected",
                    "event_status": "open",
                    "payload": {
                        "market": market,
                        "gap_scope": "symbol_range_gap",
                        "code": gap_range["code"],
                        "missing_date_from": gap_range["missing_date_from"].isoformat(),
                        "missing_date_to": gap_range["missing_date_to"].isoformat(),
                    },
                }
            )

        completed_payload: Dict[str, object] = {
            "market": market,
            "trade_date": trade_date.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "run_result": run_result,
            "pass_status": pass_status,
            "market_day_gap_count": len(market_day_gap_dates),
            "symbol_range_gap_count": len(symbol_gap_ranges),
            "expected_universe_count": len(expected_universe),
        }
        if fail_closed_empty_universe:
            completed_payload["fail_closed_reason"] = "empty_expected_universe"
        if not target_is_market_session:
            completed_payload["skip_reason"] = "non_trading_trade_date"

        event_payloads.append(
            {
                "source_run_id": run_id,
                "event_type": "audit_completed",
                "event_status": run_result,
                "payload": completed_payload,
            }
        )

        self.db.persist_kline_audit_result(
            run_payload={
                "run_id": run_id,
                "market": market,
                "trade_date": trade_date,
                "run_type": run_type,
                "trigger_type": trigger_type,
                "run_result": run_result,
                "pass_status": pass_status,
                "rule_version": self.rule_version,
                "window_start": window_start,
                "window_end": window_end,
            },
            trade_date_payload={
                "market": market,
                "trade_date": trade_date,
                "pass_status": pass_status,
                "window_start": window_start,
                "window_end": window_end,
                "rule_version": self.rule_version,
                "source_run_id": run_id,
                "passed_at": passed_at,
            },
            gap_payloads=gap_payloads,
            event_payloads=event_payloads,
        )

        logger.info(
            "K 线审计完成: market=%s trade_date=%s run_result=%s pass_status=%s",
            market,
            trade_date,
            run_result,
            pass_status,
        )
        return {
            "run_id": run_id,
            "trade_date": trade_date,
            "window_start": window_start,
            "window_end": window_end,
            "run_result": run_result,
            "pass_status": pass_status,
        }

    def _resolve_expected_universe(self, *, market: str, window_end: date) -> List[Dict[str, object]]:
        with self.db.session_scope() as session:
            rows = session.execute(
                select(InstrumentMaster).where(
                    and_(
                        InstrumentMaster.market == market,
                        InstrumentMaster.listing_status == "active",
                    )
                )
            ).scalars().all()
            expected = []
            for row in rows:
                if row.list_date is not None and row.list_date > window_end:
                    continue
                if market == "cn" and bool(getattr(row, "is_st", False)):
                    continue
                expected.append(
                    {
                        "code": row.code,
                        "list_date": row.list_date,
                    }
                )
            return expected

    def _load_observed_dates(
        self,
        *,
        codes: List[str],
        window_start: date,
        window_end: date,
    ) -> Dict[str, set[date]]:
        observed_by_code: Dict[str, set[date]] = {code: set() for code in codes}
        if not codes:
            return observed_by_code

        with self.db.session_scope() as session:
            rows = session.execute(
                select(StockDaily.code, StockDaily.date).where(
                    and_(
                        StockDaily.code.in_(codes),
                        StockDaily.date >= window_start,
                        StockDaily.date <= window_end,
                    )
                )
            ).all()
        for code, trade_date in rows:
            observed_by_code.setdefault(str(code), set()).add(trade_date)
        return observed_by_code

    def _load_approved_skips(self, *, market: str) -> List[Dict[str, object]]:
        rows = self.db.list_kline_skip_registry(
            market=market,
            status="approved_skip",
        )
        return [
            {
                "gap_scope": row.gap_scope,
                "code": row.code,
                "trade_date": row.trade_date,
                "missing_date_from": row.missing_date_from,
                "missing_date_to": row.missing_date_to,
            }
            for row in rows
        ]

    def _build_market_dates(
        self,
        *,
        market: str,
        window_start: date,
        window_end: date,
    ) -> List[date]:
        if window_start > window_end:
            return []

        market_dates: List[date] = []
        candidate = window_start
        while candidate <= window_end:
            if self._is_market_session(market=market, trade_date=candidate):
                market_dates.append(candidate)
            candidate += timedelta(days=1)
        return market_dates

    @staticmethod
    def _is_market_session(*, market: str, trade_date: date) -> bool:
        if trade_date.weekday() >= 5:
            return False
        return is_market_open(market, trade_date)

    @staticmethod
    def _is_date_skipped(
        *,
        approved_skips: List[Dict[str, object]],
        code: str,
        trade_date: date,
    ) -> bool:
        for row in approved_skips:
            gap_scope = row["gap_scope"]
            if gap_scope == "market_day_gap" and row["trade_date"] == trade_date:
                return True
            if gap_scope == "symbol_range_gap" and row["code"] == code:
                missing_date_from = row["missing_date_from"]
                missing_date_to = row["missing_date_to"]
                if missing_date_from is not None and missing_date_to is not None:
                    if missing_date_from <= trade_date <= missing_date_to:
                        return True
        return False

    @staticmethod
    def _collapse_dates_to_ranges(
        *,
        code: str,
        missing_dates: List[date],
        market_dates: List[date],
    ) -> List[Dict[str, object]]:
        if not missing_dates:
            return []

        ordered_dates = sorted(set(missing_dates))
        market_index = {market_date: idx for idx, market_date in enumerate(market_dates)}
        ranges: List[Dict[str, object]] = []
        range_start = ordered_dates[0]
        previous = ordered_dates[0]

        for current in ordered_dates[1:]:
            if market_index.get(current) == market_index.get(previous, -1) + 1:
                previous = current
                continue
            ranges.append(
                {
                    "code": code,
                    "missing_date_from": range_start,
                    "missing_date_to": previous,
                }
            )
            range_start = current
            previous = current

        ranges.append(
            {
                "code": code,
                "missing_date_from": range_start,
                "missing_date_to": previous,
            }
        )
        return ranges
