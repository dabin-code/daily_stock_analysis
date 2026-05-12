from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy import distinct, func, select

from src.storage import (
    DatabaseManager,
    KlineAuditGap,
    KlineAuditTradeDate,
    StockDaily,
)


class DataHealthService:
    """Read-only data health projections for the local stock database."""

    UNRESOLVED_GAP_STATUSES = {"open", "pending_retry", "candidate_skip"}

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        *,
        min_full_count: int = 3000,
    ) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.min_full_count = max(1, int(min_full_count))

    def get_summary(self, *, market: str = "cn") -> Dict[str, Any]:
        active_instruments = self.db.list_instruments(market=market, listing_status="active")
        expected_instruments = self.db.list_instruments(
            market=market,
            listing_status="active",
            exclude_st=True,
        )
        expected_codes = [item["code"] for item in expected_instruments]
        expected_count = len(expected_codes)

        latest_trade_date = self._latest_stock_date(expected_codes)
        stock_data_start_date, stock_data_end_date, stock_data_trade_date_count = self._stock_date_span(
            expected_codes
        )
        latest_synced_count = (
            self._synced_count_for_date(latest_trade_date, expected_codes)
            if latest_trade_date is not None
            else 0
        )
        latest_complete_date = self._latest_complete_date(expected_codes)
        latest_audit_passed_date = self._latest_audit_passed_date(market)
        screening_ready_date = self._resolve_screening_ready_date(
            latest_complete_date=latest_complete_date,
            latest_audit_passed_date=latest_audit_passed_date,
        )

        return {
            "market": market,
            "active_instrument_count": len(active_instruments),
            "expected_universe_count": expected_count,
            "st_excluded_count": max(0, len(active_instruments) - expected_count),
            "stock_data_start_date": self._date_to_str(stock_data_start_date),
            "stock_data_end_date": self._date_to_str(stock_data_end_date),
            "stock_data_trade_date_count": stock_data_trade_date_count,
            "latest_trade_date": self._date_to_str(latest_trade_date),
            "latest_complete_date": self._date_to_str(latest_complete_date),
            "latest_audit_passed_date": self._date_to_str(latest_audit_passed_date),
            "latest_trade_date_synced_count": latest_synced_count,
            "latest_trade_date_coverage_ratio": self._ratio(latest_synced_count, expected_count),
            "open_gap_count": self._gap_count(market=market, status="open"),
            "pending_retry_gap_count": self._gap_count(market=market, status="pending_retry"),
            "candidate_skip_gap_count": self._gap_count(market=market, status="candidate_skip"),
            "screening_ready": screening_ready_date is not None,
            "screening_ready_date": self._date_to_str(screening_ready_date),
        }

    def get_coverage(
        self,
        *,
        market: str = "cn",
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> Dict[str, Any]:
        expected_codes = [
            item["code"]
            for item in self.db.list_instruments(
                market=market,
                listing_status="active",
                exclude_st=True,
            )
        ]
        expected_count = len(expected_codes)
        daily_counts = self._daily_synced_counts(expected_codes, start=start, end=end)
        row_counts = self._symbol_row_counts(expected_codes)

        return {
            "market": market,
            "expected_count": expected_count,
            "items": [
                {
                    "trade_date": self._date_to_str(trade_date),
                    "synced_count": synced_count,
                    "expected_count": expected_count,
                    "coverage_ratio": self._ratio(synced_count, expected_count),
                    "is_complete": synced_count >= self.min_full_count,
                }
                for trade_date, synced_count in daily_counts
            ],
            "ma100_ready_count": sum(1 for count in row_counts.values() if count >= 100),
            "ma200_ready_count": sum(1 for count in row_counts.values() if count >= 200),
        }

    def list_gaps(
        self,
        *,
        market: str = "cn",
        status: Optional[str] = None,
        start: Optional[date] = None,
        end: Optional[date] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        safe_limit = max(1, min(int(limit), 500))
        normalized_status = str(status or "unresolved").strip().lower()
        if normalized_status == "all":
            gaps = self.db.list_kline_audit_gaps(market=market)
        elif normalized_status == "unresolved":
            gaps = [
                gap
                for gap in self.db.list_kline_audit_gaps(market=market)
                if gap.status in self.UNRESOLVED_GAP_STATUSES
            ]
        else:
            gaps = self.db.list_kline_audit_gaps(market=market, status=normalized_status)
        gaps = [
            gap
            for gap in gaps
            if self._gap_within_date_range(gap, start=start, end=end)
        ]
        items = [self._gap_to_dict(gap) for gap in gaps[:safe_limit]]
        return {
            "market": market,
            "total": len(gaps),
            "items": items,
        }

    def _latest_stock_date(self, expected_codes: List[str]) -> Optional[date]:
        if not expected_codes:
            return None
        with self.db.session_scope() as session:
            return session.execute(
                select(func.max(StockDaily.date)).where(StockDaily.code.in_(expected_codes))
            ).scalar_one_or_none()

    def _stock_date_span(self, expected_codes: List[str]) -> tuple[Optional[date], Optional[date], int]:
        if not expected_codes:
            return None, None, 0
        with self.db.session_scope() as session:
            row = session.execute(
                select(
                    func.min(StockDaily.date),
                    func.max(StockDaily.date),
                    func.count(distinct(StockDaily.date)),
                ).where(StockDaily.code.in_(expected_codes))
            ).one()
        return row[0], row[1], int(row[2] or 0)

    def _latest_complete_date(self, expected_codes: List[str]) -> Optional[date]:
        if not expected_codes:
            return None
        with self.db.session_scope() as session:
            row = session.execute(
                select(StockDaily.date)
                .where(StockDaily.code.in_(expected_codes))
                .group_by(StockDaily.date)
                .having(func.count(distinct(StockDaily.code)) >= self.min_full_count)
                .order_by(StockDaily.date.desc())
                .limit(1)
            ).first()
        return row[0] if row else None

    def _latest_audit_passed_date(self, market: str) -> Optional[date]:
        with self.db.session_scope() as session:
            return session.execute(
                select(func.max(KlineAuditTradeDate.trade_date)).where(
                    KlineAuditTradeDate.market == market,
                    KlineAuditTradeDate.pass_status == "passed",
                )
            ).scalar_one_or_none()

    def _synced_count_for_date(self, trade_date: date, expected_codes: List[str]) -> int:
        if not expected_codes:
            return 0
        with self.db.session_scope() as session:
            value = session.execute(
                select(func.count(distinct(StockDaily.code))).where(
                    StockDaily.date == trade_date,
                    StockDaily.code.in_(expected_codes),
                )
            ).scalar_one()
        return int(value or 0)

    def _daily_synced_counts(
        self,
        expected_codes: List[str],
        *,
        start: Optional[date],
        end: Optional[date],
    ) -> List[tuple[date, int]]:
        if not expected_codes:
            return []
        with self.db.session_scope() as session:
            stmt = (
                select(StockDaily.date, func.count(distinct(StockDaily.code)))
                .where(StockDaily.code.in_(expected_codes))
                .group_by(StockDaily.date)
            )
            if start is not None:
                stmt = stmt.where(StockDaily.date >= start)
            if end is not None:
                stmt = stmt.where(StockDaily.date <= end)
            if start is None and end is None:
                stmt = stmt.order_by(StockDaily.date.desc()).limit(250)
            else:
                stmt = stmt.order_by(StockDaily.date.asc())
            rows = session.execute(stmt).all()
        items = [(row[0], int(row[1] or 0)) for row in rows]
        if start is None and end is None:
            items.reverse()
        return items

    def _symbol_row_counts(self, expected_codes: List[str]) -> Dict[str, int]:
        if not expected_codes:
            return {}
        with self.db.session_scope() as session:
            rows = session.execute(
                select(StockDaily.code, func.count(StockDaily.id))
                .where(StockDaily.code.in_(expected_codes))
                .group_by(StockDaily.code)
            ).all()
        counts = {code: 0 for code in expected_codes}
        counts.update({row[0]: int(row[1] or 0) for row in rows})
        return counts

    def _gap_count(self, *, market: str, status: str) -> int:
        with self.db.session_scope() as session:
            value = session.execute(
                select(func.count(KlineAuditGap.id)).where(
                    KlineAuditGap.market == market,
                    KlineAuditGap.status == status,
                )
            ).scalar_one()
        return int(value or 0)

    @staticmethod
    def _gap_within_date_range(
        gap: KlineAuditGap,
        *,
        start: Optional[date],
        end: Optional[date],
    ) -> bool:
        if start is None and end is None:
            return True
        gap_start = gap.trade_date or gap.missing_date_from
        gap_end = gap.trade_date or gap.missing_date_to
        if gap_start is None or gap_end is None:
            return True
        if start is not None and gap_start < start:
            return False
        if end is not None and gap_end > end:
            return False
        return True

    @staticmethod
    def _resolve_screening_ready_date(
        *,
        latest_complete_date: Optional[date],
        latest_audit_passed_date: Optional[date],
    ) -> Optional[date]:
        if latest_complete_date is None or latest_audit_passed_date is None:
            return None
        return min(latest_complete_date, latest_audit_passed_date)

    @staticmethod
    def _gap_to_dict(gap: KlineAuditGap) -> Dict[str, Any]:
        return {
            "gap_key": gap.gap_key,
            "source_run_id": gap.source_run_id,
            "market": gap.market,
            "gap_scope": gap.gap_scope,
            "code": gap.code,
            "trade_date": DataHealthService._date_to_str(gap.trade_date),
            "missing_date_from": DataHealthService._date_to_str(gap.missing_date_from),
            "missing_date_to": DataHealthService._date_to_str(gap.missing_date_to),
            "status": gap.status,
            "created_at": gap.created_at.isoformat() if gap.created_at else None,
            "updated_at": gap.updated_at.isoformat() if gap.updated_at else None,
        }

    @staticmethod
    def _date_to_str(value: Optional[date]) -> Optional[str]:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> float:
        if denominator <= 0:
            return 0.0
        return numerator / denominator
