# -*- coding: utf-8 -*-
"""上市/退市生命周期服务：回填 instrument_master 的上市与退市日期。

存在的理由是**消除幸存者偏差**。只同步在市股票的话，2018 年上市、2021 年
退市的股票在历史研究里完全缺席，而它们恰恰是亏损样本的主要来源，样本里
只留幸存者会让回测收益虚高。退市日期同时也是缺口归因的依据——没有它，
「停牌」「退市」「抓取失败」三类缺口长得一模一样。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import or_, select

from src.storage import DatabaseManager, InstrumentMaster

logger = logging.getLogger(__name__)

ACTIVE = "active"
DELISTED = "delisted"


class ListingLifecycleService:
    """走 DatabaseManager 的 ORM 读写 instrument_master。

    与 TradingCalendarService 的裸 sqlite3 取法不同：instrument_master 的其余
    读写（`upsert_instruments` / `list_instruments`）全在 ORM 层，这里跟着走
    同一层，避免同一张表两套访问方式。

    缺省用 DatabaseManager 单例，也就是**生产库**；测试必须先把
    DATABASE_PATH 指到临时库并重置 Config / DatabaseManager 两个单例。
    """

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self._db = db_manager or DatabaseManager.get_instance()

    # ── 写入 ────────────────────────────────────────────────────────────
    def upsert_lifecycle(self, rows: Sequence[Dict[str, Any]]) -> int:
        """写入 list_date / delist_date / listing_status。

        **缺失值不覆盖已有日期**：`list_date` 可能是别处（
        `scripts/_kline_master_data_treatment.py` 的 Phase A）用 stock_daily
        首个交易日回填出来的，上游某次返回空值就把它清掉，等于用一次抓取
        抖动换掉一份已有证据。唯一的例外是 `listing_status` 为在市时清空
        `delist_date`——在市即无退市日期，误标要能被下一次同步纠正。
        """
        normalized: List[Dict[str, Any]] = []
        for item in rows or []:
            code = str(item.get("code") or "").strip().upper()
            if not code:
                continue
            normalized.append({**item, "code": code})
        if not normalized:
            return 0

        with self._db.session_scope() as session:
            codes = [item["code"] for item in normalized]
            record_map = {
                record.code: record
                for record in session.execute(
                    select(InstrumentMaster).where(InstrumentMaster.code.in_(codes))
                ).scalars().all()
            }

            for item in normalized:
                code = item["code"]
                record = record_map.get(code)
                if record is None:
                    record = InstrumentMaster(
                        code=code,
                        name=str(item.get("name") or code),
                        # market 是 nullable=False，新行必须显式给值
                        market=str(item.get("market") or "cn"),
                    )
                    session.add(record)
                    record_map[code] = record
                elif item.get("name"):
                    record.name = str(item["name"])

                list_date = _as_date(item.get("list_date"))
                if list_date is not None:
                    record.list_date = list_date

                status = str(item.get("listing_status") or "").strip() or None
                delist_date = _as_date(item.get("delist_date"))
                if delist_date is not None:
                    record.delist_date = delist_date
                elif status == ACTIVE:
                    record.delist_date = None

                if status:
                    record.listing_status = status
                record.updated_at = datetime.now()

        return len(normalized)

    # ── 读取 ────────────────────────────────────────────────────────────
    def get_lifecycle(self, codes: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """批量读回生命周期字段，key 为代码；查不到的代码直接缺席。"""
        normalized = [str(code).strip().upper() for code in codes or [] if str(code).strip()]
        if not normalized:
            return {}

        with self._db.get_session() as session:
            records = session.execute(
                select(InstrumentMaster).where(InstrumentMaster.code.in_(normalized))
            ).scalars().all()
            return {
                record.code: {
                    "code": record.code,
                    "name": record.name,
                    "market": record.market,
                    "list_date": record.list_date,
                    "delist_date": record.delist_date,
                    "listing_status": record.listing_status,
                }
                for record in records
            }

    def list_codes_alive_on(
        self,
        as_of: date,
        market: Optional[str] = None,
    ) -> List[str]:
        """某一时点的在市清单，区间口径为 **[list_date, delist_date)**。

        上市日算在内（它是首个交易日），退市日不算（`delist_date` 是终止上市
        生效日，该日证券已不在市、不产生 K 线；算作在市会让审计期望域每只
        退市股多出一天无法归因的缺口）。

        `list_date` 为 NULL 的行一律排除：上市时点未知就无法断言它当天在市，
        与日历查询同样取 fail-closed，不猜。
        """
        with self._db.get_session() as session:
            stmt = (
                select(InstrumentMaster.code)
                .where(
                    InstrumentMaster.list_date.is_not(None),
                    InstrumentMaster.list_date <= as_of,
                    or_(
                        InstrumentMaster.delist_date.is_(None),
                        InstrumentMaster.delist_date > as_of,
                    ),
                )
                .order_by(InstrumentMaster.code)
            )
            if market:
                stmt = stmt.where(InstrumentMaster.market == market)
            return [row[0] for row in session.execute(stmt).all()]

    # ── 抓取 ────────────────────────────────────────────────────────────
    def sync_from_baostock(self) -> Dict[str, int]:
        """baostock 全量拉取上市/退市日期。

        `query_stock_basic()` **不传 code** 时返回全部证券，含已退市者——
        这正是选它的原因。传 code 就只返回那一只，已退市证券整批拉不到，
        幸存者偏差原地复发。
        """
        import baostock as bs

        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"baostock login failed: {login.error_msg}")
        try:
            rs = bs.query_stock_basic()
            if rs.error_code != "0":
                raise RuntimeError(f"query_stock_basic failed: {rs.error_msg}")
            rows: List[Dict[str, Any]] = []
            while rs.next():
                record = dict(zip(rs.fields, rs.get_row_data()))
                if record.get("type") != "1":   # 只要股票，排除指数与基金
                    continue
                code = _normalize_baostock_code(record.get("code", ""))
                if not code:
                    continue
                rows.append({
                    "code": code,
                    "name": record.get("code_name") or code,
                    "list_date": _as_date(record.get("ipoDate")),
                    "delist_date": _as_date(record.get("outDate")),
                    "listing_status": ACTIVE if record.get("status") == "1" else DELISTED,
                })
        finally:
            bs.logout()

        written = self.upsert_lifecycle(rows)
        delisted = sum(1 for r in rows if r["listing_status"] == DELISTED)
        logger.info(
            "baostock listing lifecycle synced: total=%d written=%d delisted=%d",
            len(rows),
            written,
            delisted,
        )
        return {"written": written, "total": len(rows), "delisted": delisted}


def _normalize_baostock_code(value: Any) -> str:
    """`sh.600000` / `sz.000001` → 仓库统一的 `600000` / `000001`。

    **不能直接把 baostock 代码交给 `normalize_stock_code`**：它在
    `data_provider/base.py:119-125` 明确排除了 `SH.` / `SZ.` 形式，
    `sh.600000` 会被原样返回，结果是回填出一批匹配不到任何行情的孤儿代码。
    先按 `data_provider/baostock_fetcher.py:344` 的方式剥前缀，再交给
    `normalize_stock_code` 做最终归一。
    """
    from data_provider.base import normalize_stock_code

    text = str(value or "").strip()
    if not text:
        return ""
    bare = text.split('.')[1] if '.' in text else text
    return normalize_stock_code(bare).strip().upper()


def _as_date(value: Any) -> Optional[date]:
    """空串、None、非法日期统一返回 None——缺失比编造一个日期安全。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        logger.warning("unparsable date from source: %r", value)
        return None
