# -*- coding: utf-8 -*-
"""上市/退市生命周期服务：回填 instrument_master 的上市与退市日期。

存在的理由是**消除幸存者偏差**。只同步在市股票的话，2018 年上市、2021 年
退市的股票在历史研究里完全缺席，而它们恰恰是亏损样本的主要来源，样本里
只留幸存者会让回测收益虚高。退市日期同时也是缺口归因的依据——没有它，
「停牌」「退市」「抓取失败」三类缺口长得一模一样。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import or_, select

from src.storage import DatabaseManager, InstrumentMaster

logger = logging.getLogger(__name__)

ACTIVE = "active"
DELISTED = "delisted"

# 这些上市状态在全市场范围内不可能一只都没有，返回 0 行只会是抓取出了问题，
# 必须按失败处理。'P'（暂停上市）刻意不在其中：现行退市规则下该状态已基本不
# 再使用，返回 0 行是合法结果，按失败处理会把同批已经抓成功的 L / D 一起作废
# ——而 stock_basic 免费额度约每小时 1 次，代价是三个额度窗口换来零写入。
NON_EMPTY_LIST_STATUSES = frozenset({"L", "D"})


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

    def sync_from_tushare(
        self,
        fetcher: Any = None,
        list_statuses: Sequence[str] = ("L", "D", "P"),
        pause_seconds: float = 0.0,
    ) -> Dict[str, int]:
        """tushare stock_basic 全量拉取上市/退市日期，baostock 不可用时的替代路径。

        **必须逐个 list_status 拉一遍**：stock_basic 的 list_status 默认只给 'L'，
        只拉在市清单等于把这个服务存在的理由（消除幸存者偏差）当场作废。
        'P' 是暂停上市，仍然在市，不能按退市处理。

        **全部拉完才写一次**：任何一个状态失败就抛错、一个字都不写。分状态边拉
        边写的话，'D' 失败会留下一个「只有在市股票」的库——它看起来同步成功，
        实际上带着幸存者偏差，比直接失败更难发现。

        **空结果与抓取失败分开判定**：取数返回 None 一律致命（这次抓取没拿到
        答案）；返回空 DataFrame 只对 NON_EMPTY_LIST_STATUSES 内的状态致命，
        其余状态记一条告警后继续下一个状态。

        pause_seconds 只在状态之间等待：免费额度下 stock_basic 约每小时 1 次
        （`抱歉，您每小时最多访问该接口1次`）。等待与重试策略归调用脚本，这里
        不内建重试循环，否则操作员无法控制一次同步会挂多久。

        Args:
            fetcher: 注入的取数对象，缺省在方法内构造 TushareFetcher
            list_statuses: 要拉取的上市状态，缺省覆盖在市/退市/暂停上市
            pause_seconds: 相邻两次状态抓取之间的等待秒数

        Raises:
            ValueError: list_statuses 为空
            RuntimeError: 任一状态抓取失败，或不可为空的状态返回了 0 行

        Returns:
            {"written": 写入行数, "total": 映射后总行数, "delisted": 退市行数}
        """
        statuses = tuple(list_statuses or ())
        if not statuses:
            # 一个状态都不拉却返回 written=0，调用方会当成同步成功，
            # 与「空结果当失败」是同一类缺陷：拿不到真实结论。
            raise ValueError("sync_from_tushare requires at least one list_status")

        if fetcher is None:
            # 与 sync_from_baostock 的 `import baostock as bs` 同一取法：在方法内
            # 导入，避免服务模块被引入时就拖上数据源依赖。
            from data_provider.tushare_fetcher import TushareFetcher

            fetcher = TushareFetcher()

        rows: List[Dict[str, Any]] = []
        for index, status in enumerate(statuses):
            if index > 0:
                time.sleep(pause_seconds)

            try:
                frame = fetcher.get_stock_lifecycle(list_status=status)
            except Exception as exc:
                raise RuntimeError(
                    f"tushare stock_basic failed for list_status={status}: {exc}"
                ) from exc

            if frame is None:
                # 取数层用 None 表示这次抓取失败（限频、无权限、Token 缺失）。
                raise RuntimeError(
                    f"tushare stock_basic fetch failed for list_status={status}"
                )

            if frame.empty:
                if str(status).strip().upper() in NON_EMPTY_LIST_STATUSES:
                    raise RuntimeError(
                        f"tushare stock_basic returned no rows for list_status={status}"
                    )
                logger.warning(
                    "tushare stock_basic returned no rows for list_status=%s, "
                    "treated as legitimately empty",
                    status,
                )
                continue

            for record in frame.to_dict("records"):
                code = _normalize_tushare_code(record.get("ts_code"))
                if not code:
                    continue
                raw_name = record.get("name")
                # pandas 缺失值是 float('nan')，str() 会得到 'nan' 这种假名字
                name = raw_name.strip() if isinstance(raw_name, str) else ""
                rows.append({
                    "code": code,
                    "name": name or code,
                    "list_date": _as_date(record.get("list_date")),
                    "delist_date": _as_date(record.get("delist_date")),
                    "listing_status": (
                        DELISTED
                        if str(record.get("list_status") or "").strip().upper() == "D"
                        else ACTIVE
                    ),
                })

        written = self.upsert_lifecycle(rows)
        delisted = sum(1 for r in rows if r["listing_status"] == DELISTED)
        logger.info(
            "tushare listing lifecycle synced: total=%d written=%d delisted=%d",
            len(rows),
            written,
            delisted,
        )
        return {"written": written, "total": len(rows), "delisted": delisted}


def _normalize_tushare_code(value: Any) -> str:
    """`600000.SH` / `000001.SZ` → 仓库统一的 `600000` / `000001`。

    **不能复用 `_normalize_baostock_code`**：两家的点号方向是反的。baostock 给
    `sh.600000`（交易所在点号前）所以取 `split('.')[1]`；tushare 给 `600000.SH`
    （交易所在点号后），同一句会取到 `SH`，而 `normalize_stock_code('SH')` 原样
    返回，结果是回填出一批名叫 SH / SZ 的孤儿行。剥后缀的取法与
    `data_provider/tushare_fetcher.py:665` 一致，再交给 `normalize_stock_code`
    做最终归一（北交所 `920748.BJ` 也走同一条路）。
    """
    from data_provider.base import normalize_stock_code

    text = str(value or "").strip()
    if not text:
        return ""
    return normalize_stock_code(text.split('.')[0]).strip().upper()


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
    """空串、None、非法日期统一返回 None——缺失比编造一个日期安全。

    两种源格式都要认：baostock 的 `1991-04-03` 与 tushare 的 `19910403`。
    紧凑形式**必须显式解析**，不能指望 `date.fromisoformat`——它只在 3.11+ 才
    接受无分隔符形式，而 README 声明支持 3.10+，靠它等于让回填结果随解释器
    版本漂移（3.10 上整批上市日期会静默变成 NULL）。

    NaN 单独拦一道：tushare 对在市股票的 delist_date 给的是 float('nan')，
    走到通用分支会被 str() 变成 'nan' 并逐股打一条 WARNING，全市场同步下是
    几千行噪声，真正解析失败的日期会被埋掉。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, float) and value != value:
        return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        return date.fromisoformat(text[:10])
    except ValueError:
        logger.warning("unparsable date from source: %r", value)
        return None
