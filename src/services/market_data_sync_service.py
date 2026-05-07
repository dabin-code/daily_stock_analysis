from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import date
from typing import Callable, Dict, List, Optional, Set, Tuple

from data_provider.base import DataFetchError, DataFetcherManager
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 可调常量
# ---------------------------------------------------------------------------
# 单只股票 API 调用超时（秒）
_PER_STOCK_FETCH_TIMEOUT: int = 60
# Tushare 批量接口超时（秒）
_BULK_FETCH_TIMEOUT: int = 60
# 心跳回调触发间隔（每同步 N 只后触发一次）
_HEARTBEAT_INTERVAL: int = 10
# 逐只同步数量上限，超出部分直接标记为无数据跳过
_MAX_INDIVIDUAL_SYNC: int = 200
_SOURCE_ATTEMPT_PATTERN = re.compile(
    r"^\[(?P<source>[^\]]+)\]\s*(?:\((?P<error_type>[^)]+)\))?\s*(?P<detail>.*)$"
)


class MarketDataSyncService:
    """全市场日线增量同步服务。"""

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        fetcher_manager: Optional[DataFetcherManager] = None,
    ) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.fetcher_manager = fetcher_manager or DataFetcherManager()

    # ------------------------------------------------------------------
    # 核心入口
    # ------------------------------------------------------------------
    def sync_trade_date(
        self,
        trade_date: date,
        stock_codes: Optional[List[str]] = None,
        force: bool = False,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        min_available_ratio: float = 0.8,
    ) -> Dict[str, object]:
        """
        同步指定交易日的日线数据。

        四层防御策略：
        1. 数据库已有目标交易日数据 → 跳过该股票（非按覆盖率短路）
        2. 先尝试 bulk sync，仍缺失的股票继续进入后续流程
           - bulk 哨兵：bulk 已确认不存在的代码（如已退市但 instrument_master 未及时同步）
             直接标 skip_eligible，避免逐只兜底浪费时间
        3. 逐只同步有上限 _MAX_INDIVIDUAL_SYNC
        4. 每次 API 调用有 _PER_STOCK_FETCH_TIMEOUT 超时
        """
        _ = min_available_ratio  # 保留现有方法签名兼容性，不再参与健康判定或短路逻辑。
        if stock_codes:
            codes = [str(code).strip().upper() for code in stock_codes if str(code).strip()]
        else:
            instruments = self.db.list_instruments(market="cn", listing_status="active", exclude_st=True)
            codes = [item["code"] for item in instruments]

        if not codes:
            return self._build_sync_result(trade_date, codes=[], synced=0, skipped=0, errors=[])

        synced = 0
        skipped = 0
        errors: List[Dict[str, object]] = []

        # 批量查询已有数据的股票
        if not force:
            existing_codes = self.db.batch_has_today_data(codes, target_date=trade_date)
        else:
            existing_codes = set()
        codes_needing_sync = [c for c in codes if c not in existing_codes]
        skipped = len(codes) - len(codes_needing_sync)

        # ── bulk sync 阶段 ──
        bulk_returned_universe: Optional[Set[str]] = None
        if codes_needing_sync:
            bulk_result = self._try_bulk_sync(trade_date, set(codes_needing_sync))
            if bulk_result is not None:
                bulk_synced, bulk_returned_universe = bulk_result
                synced += bulk_synced
                # 批量复查哪些已被 bulk sync 覆盖
                still_existing = self.db.batch_has_today_data(codes_needing_sync, target_date=trade_date)
                codes_needing_sync = [c for c in codes_needing_sync if c not in still_existing]

        # ── bulk 哨兵：bulk 当日 universe 之外的代码视为无源数据，立即标 skip_eligible ──
        if (
            bulk_returned_universe is not None
            and codes_needing_sync
            and self._is_bulk_sentinel_enabled()
        ):
            not_in_bulk = [c for c in codes_needing_sync if c not in bulk_returned_universe]
            if not_in_bulk:
                logger.info(
                    f"[SyncTradeDate] bulk 哨兵命中 {len(not_in_bulk)} 只缺失代码不在 Tushare bulk universe，"
                    f"跳过逐只兜底"
                )
                errors.extend(
                    self._build_service_failure_record(
                        code=c,
                        trade_date=trade_date,
                        reason="not_in_bulk_universe",
                        detail="code absent from tushare bulk daily snapshot for target trade date",
                        reason_class="skip_eligible",
                        source="tushare_bulk_sentinel",
                    )
                    for c in not_in_bulk
                )
                codes_needing_sync = [c for c in codes_needing_sync if c in bulk_returned_universe]

        # ── 第三层防御：缺失数量超过100只时，改用批量同步 ──
        if len(codes_needing_sync) > 100:
            logger.info(
                f"[SyncTradeDate] 缺失 {len(codes_needing_sync)} 只 > 100，"
                f"改用批量同步而非逐只同步"
            )
            bulk_retry_result = self._try_bulk_sync(trade_date, set(codes_needing_sync))
            if bulk_retry_result is not None:
                bulk_synced_retry, _retry_universe = bulk_retry_result
                synced += bulk_synced_retry
                still_existing = self.db.batch_has_today_data(codes_needing_sync, target_date=trade_date)
                codes_needing_sync = [c for c in codes_needing_sync if c not in still_existing]

            # 批量同步后仍有缺失，标记为无数据
            if codes_needing_sync:
                logger.warning(
                    f"[SyncTradeDate] 批量同步后仍缺失 {len(codes_needing_sync)} 只，标记为可重试未完成"
                )
                errors.extend(
                    self._build_service_failure_record(
                        code=c,
                        trade_date=trade_date,
                        reason="bulk_sync_incomplete",
                        detail="bulk sync completed but target trade date is still missing",
                        reason_class="retryable",
                        source="bulk_sync_guardrail",
                    )
                    for c in codes_needing_sync
                )
                codes_needing_sync = []

        # ── 第四层防御：逐只同步数量上限 ──
        elif len(codes_needing_sync) > _MAX_INDIVIDUAL_SYNC:
            overflow = codes_needing_sync[_MAX_INDIVIDUAL_SYNC:]
            logger.warning(
                f"[SyncTradeDate] 需逐只同步 {len(codes_needing_sync)} 只，"
                f"超出上限 {_MAX_INDIVIDUAL_SYNC}，截断 {len(overflow)} 只"
            )
            errors.extend(
                self._build_service_failure_record(
                    code=c,
                    trade_date=trade_date,
                    reason="individual_sync_overflow",
                    detail=f"individual sync limit reached: {_MAX_INDIVIDUAL_SYNC}",
                    reason_class="retryable",
                    source="individual_sync_guardrail",
                )
                for c in overflow
            )
            codes_needing_sync = codes_needing_sync[:_MAX_INDIVIDUAL_SYNC]

        # ── 第四层防御：逐只同步，每只有超时保护 ──
        total_to_sync = len(codes_needing_sync)
        if total_to_sync > 0:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="sync_single") as executor:
                for idx, code in enumerate(codes_needing_sync):
                    try:
                        result = self._fetch_single_with_timeout(code, trade_date, executor=executor)
                        if result is None:
                            errors.append(
                                self._build_service_failure_record(
                                    code=code,
                                    trade_date=trade_date,
                                    reason="empty_data",
                                    detail="fetch returned empty data for target trade date",
                                    reason_class="skip_eligible",
                                    source="unknown_fetcher",
                                )
                            )
                            continue
                        df, source_name = result
                        saved = self.db.save_daily_data(df, code, data_source=source_name)
                        if saved > 0 or self.db.has_today_data(code, target_date=trade_date):
                            synced += 1
                        else:
                            errors.append(
                                self._build_service_failure_record(
                                    code=code,
                                    trade_date=trade_date,
                                    reason="save_failed",
                                    detail="save_daily_data returned 0 and target trade date is still missing",
                                    reason_class="blocking",
                                    source="database",
                                )
                            )
                    except Exception as exc:
                        if isinstance(exc, DataFetchError):
                            errors.append(
                                self._build_fetch_error_record(
                                    code=code,
                                    trade_date=trade_date,
                                    detail=str(exc),
                                )
                            )
                        else:
                            detail = str(exc) or exc.__class__.__name__
                            errors.append(
                                self._build_service_failure_record(
                                    code=code,
                                    trade_date=trade_date,
                                    reason="unexpected_error",
                                    detail=detail,
                                    reason_class="blocking",
                                    source="market_data_sync_service",
                                )
                            )

                    # 心跳回调
                    if progress_callback and (idx + 1) % _HEARTBEAT_INTERVAL == 0:
                        try:
                            progress_callback(synced, total_to_sync)
                        except TimeoutError:
                            raise  # 全局 deadline 超时，必须向上传播
                        except Exception:
                            pass

        return self._build_sync_result(trade_date, codes, synced, skipped, errors)

    # ------------------------------------------------------------------
    # 单只获取（带超时）
    # ------------------------------------------------------------------
    def _fetch_single_with_timeout(
        self,
        code: str,
        trade_date: date,
        executor: Optional[ThreadPoolExecutor] = None,
    ) -> Optional[tuple]:
        """带超时保护的单只股票数据获取，防止单只挂死阻塞整个同步流程。"""
        own_executor = executor is None
        _executor = executor or ThreadPoolExecutor(max_workers=1)
        try:
            future = _executor.submit(
                self.fetcher_manager.get_daily_data,
                code,
                start_date=trade_date.isoformat(),
                end_date=trade_date.isoformat(),
                days=1,
            )
            try:
                df, source_name = future.result(timeout=_PER_STOCK_FETCH_TIMEOUT)
            except FuturesTimeoutError:
                future.cancel()
                raise DataFetchError(f"获取 {code} 超时（{_PER_STOCK_FETCH_TIMEOUT}s）")
            if df is None or df.empty:
                return None
            return df, source_name
        finally:
            if own_executor:
                _executor.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Tushare 批量同步（带超时，统一 SQLAlchemy）
    # ------------------------------------------------------------------
    def _try_bulk_sync(
        self, trade_date: date, needed_codes: set
    ) -> Optional[Tuple[int, Set[str]]]:
        """
        尝试用 Tushare daily(trade_date=...) 批量获取全市场当日数据。

        一次 API 调用覆盖全市场，比逐只调用快 1000x。

        返回：
            None  → Tushare 不可用 / 调用失败，由调用方降级到逐只模式。
            (synced_count, bulk_returned_codes) → bulk 调用成功；
                synced_count 是写入 stock_daily 的行数；
                bulk_returned_codes 是 Tushare 当日 daily 返回的全部 6 位代码集合，
                可被调用方用作 "当日真正存在数据的股票" 哨兵。
        """
        from src.config import get_config
        config = get_config()
        if not config.tushare_token:
            return None

        import tushare as ts

        td_str = trade_date.strftime("%Y%m%d")

        # 重试机制：最多 3 次，失败后指数退避
        for attempt in range(3):
            try:
                ts.set_token(config.tushare_token)
                api = ts.pro_api()

                # Tushare API 超时保护：防止 api.daily() 无限阻塞
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(api.daily, trade_date=td_str)
                    try:
                        df = future.result(timeout=_BULK_FETCH_TIMEOUT)
                    except FuturesTimeoutError:
                        future.cancel()
                        if attempt < 2:
                            wait = 2 ** (attempt + 1)
                            logger.warning(f"[BulkSync] Tushare daily({td_str}) 超时，{wait}s 后重试...")
                            time.sleep(wait)
                            continue
                        logger.warning(f"[BulkSync] Tushare daily({td_str}) 超时（{_BULK_FETCH_TIMEOUT}s）")
                        return None

                if df is None or df.empty:
                    logger.info(f"[BulkSync] Tushare daily({td_str}) 返回空，可能非交易日")
                    return 0, set()

                # 统一使用 SQLAlchemy 写入，继承 busy_timeout + 连接池
                synced = 0
                bulk_returned: Set[str] = set()
                now_str = time.strftime("%Y-%m-%d %H:%M:%S")
                from sqlalchemy import text
                with self.db._engine.begin() as conn:
                    for _, row in df.iterrows():
                        ts_code = str(row.get("ts_code", ""))
                        code = ts_code.split(".")[0] if ts_code else ""
                        if not code:
                            continue
                        bulk_returned.add(code)
                        if code not in needed_codes:
                            continue

                        td = str(row.get("trade_date", ""))
                        date_str = f"{td[:4]}-{td[4:6]}-{td[6:8]}" if len(td) == 8 else td

                        conn.execute(
                            text(
                                "INSERT OR REPLACE INTO stock_daily "
                                "(code, date, open, high, low, close, volume, amount, pct_chg, "
                                "data_source, created_at) "
                                "VALUES (:code, :date, :open, :high, :low, :close, :volume, :amount, :pct_chg, "
                                ":data_source, :created_at)"
                            ),
                            {
                                "code": code, "date": date_str,
                                "open": row.get("open"), "high": row.get("high"),
                                "low": row.get("low"), "close": row.get("close"),
                                "volume": row.get("vol"), "amount": row.get("amount"),
                                "pct_chg": row.get("pct_chg"),
                                "data_source": "TushareFetcher(bulk)",
                                "created_at": now_str,
                            },
                        )
                        synced += 1
                    # commit 由 begin() 上下文管理器自动处理

                logger.info(
                    f"[BulkSync] Tushare 批量同步 {trade_date}: {synced}/{len(needed_codes)} 只 "
                    f"(bulk universe={len(bulk_returned)} 只)"
                )
                return synced, bulk_returned

            except Exception as e:
                if attempt < 2:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"[BulkSync] attempt {attempt+1} 失败: {e}, {wait}s 后重试...")
                    time.sleep(wait)
                else:
                    logger.warning(f"[BulkSync] Tushare 批量同步失败，降级到逐只模式: {e}")
                    return None

    @staticmethod
    def _is_bulk_sentinel_enabled() -> bool:
        """读取 Config.kline_sync_bulk_sentinel_enabled，默认 True。"""
        try:
            from src.config import get_config
            return bool(getattr(get_config(), "kline_sync_bulk_sentinel_enabled", True))
        except Exception:  # 任何配置读取异常都退化为启用，保守不阻塞主流程
            return True

    # ------------------------------------------------------------------
    # 历史回填
    # ------------------------------------------------------------------
    def backfill_history(
        self,
        stock_codes: Optional[List[str]] = None,
        days: int = 300,
        min_rows: int = 150,
        batch_size: int = 50,
    ) -> Dict[str, object]:
        """批量回填历史日线数据，确保每只股票有足够行数支撑策略计算。"""
        if stock_codes:
            codes = [str(c).strip().upper() for c in stock_codes if str(c).strip()]
        else:
            instruments = self.db.list_instruments(
                market="cn", listing_status="active", exclude_st=True
            )
            codes = [item["code"] for item in instruments]

        if not codes:
            return {"total": 0, "backfilled": 0, "skipped": 0, "failed": 0, "elapsed_seconds": 0}

        existing_counts = self._get_row_counts(codes)
        needs_backfill = [c for c in codes if existing_counts.get(c, 0) < min_rows]
        skip_count = len(codes) - len(needs_backfill)

        logger.info(
            f"[Backfill] 总共 {len(codes)} 只, 需回填 {len(needs_backfill)}, "
            f"跳过 {skip_count} (已有 {min_rows}+ 行)"
        )

        backfilled = 0
        failed = 0
        start = time.time()

        for i in range(0, len(needs_backfill), batch_size):
            batch = needs_backfill[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (len(needs_backfill) + batch_size - 1) // batch_size

            for code in batch:
                try:
                    df, source = self.fetcher_manager.get_daily_data(code, days=days)
                    if df is not None and not df.empty:
                        self.db.save_daily_data(df, code, data_source=source)
                        backfilled += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1

            elapsed = time.time() - start
            rate = (backfilled + failed) / elapsed * 60 if elapsed > 0 else 0
            logger.info(
                f"[Backfill] 批次 {batch_num}/{total_batches}: "
                f"已完成 {backfilled + failed}/{len(needs_backfill)}, "
                f"成功={backfilled}, 失败={failed}, "
                f"速率={rate:.0f}/min, 已用时={elapsed:.0f}s"
            )

            if i + batch_size < len(needs_backfill):
                time.sleep(0.3)

        elapsed = time.time() - start
        result = {
            "total": len(codes),
            "backfilled": backfilled,
            "skipped": skip_count,
            "failed": failed,
            "elapsed_seconds": round(elapsed, 1),
        }
        logger.info(f"[Backfill] 完成: {result}")
        return result

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _get_row_counts(self, codes: List[str]) -> Dict[str, int]:
        """查询指定股票在 stock_daily 中的行数（统一使用 SQLAlchemy）。"""
        from sqlalchemy import text
        with self.db._engine.connect() as conn:
            rows = conn.execute(text("SELECT code, COUNT(*) FROM stock_daily GROUP BY code")).fetchall()
            return {row[0]: row[1] for row in rows}

    def _build_sync_result(
        self,
        trade_date: date,
        codes: List[str],
        synced: int,
        skipped: int,
        errors: List[Dict[str, object]],
    ) -> Dict[str, object]:
        """统一构建同步返回结果，确保所有路径结构一致。"""
        # 批量查询最终已有数据的股票
        final_existing = self.db.batch_has_today_data(codes, target_date=trade_date) if codes else set()
        missing_codes = [code for code in codes if code not in final_existing]
        available_count = len(codes) - len(missing_codes)
        success_rate = round((available_count / len(codes)), 4) if codes else 0.0
        refresh_success_rate = round((synced / len(codes)), 4) if codes else 0.0
        reason_class_counts = {
            "blocking": 0,
            "retryable": 0,
            "skip_eligible": 0,
        }
        for error in errors:
            reason_class = str(error.get("reason_class", "blocking"))
            if reason_class not in reason_class_counts:
                reason_class_counts[reason_class] = 0
            reason_class_counts[reason_class] += 1
        is_healthy = available_count == len(codes) and len(errors) == 0
        return {
            "trade_date": trade_date.isoformat(),
            "target_trade_date": trade_date.isoformat(),
            "total": len(codes),
            "synced": synced,
            "skipped": skipped,
            "errors": errors,
            "health_report": {
                "target_trade_date": trade_date.isoformat(),
                "expected_count": len(codes),
                "available_count": available_count,
                "missing_count": len(missing_codes),
                "error_count": len(errors),
                "missing_codes": missing_codes,
                "success_rate": success_rate,
                "refresh_success_count": synced,
                "refresh_success_rate": refresh_success_rate,
                "reason_class_counts": reason_class_counts,
                "is_healthy": is_healthy,
                "status": "healthy" if is_healthy else "degraded",
                "summary": (
                    f"{trade_date.isoformat()} sync health: status={'healthy' if is_healthy else 'degraded'}, "
                    f"available={available_count}/{len(codes)}, refreshed={synced}/{len(codes)}, "
                    f"missing={len(missing_codes)}, errors={len(errors)}"
                ),
            },
        }

    @classmethod
    def _build_fetch_error_record(
        cls,
        code: str,
        trade_date: date,
        detail: str,
    ) -> Dict[str, object]:
        source_attempts = cls._parse_source_attempts(detail)
        reason_class = cls._summarize_reason_class(source_attempts)
        return {
            "code": code,
            "target_trade_date": trade_date.isoformat(),
            "reason": "empty_data" if reason_class == "skip_eligible" else "fetch_failed",
            "reason_class": reason_class,
            "detail": detail,
            "source_attempts": source_attempts,
            "candidate_skip": cls._build_candidate_skip_info(source_attempts),
        }

    @staticmethod
    def _classify_reason_class(detail: str) -> str:
        normalized = str(detail or "").strip().lower()
        if not normalized:
            return "blocking"
        retryable_markers = [
            "timeout", "timed out", "超时", "connect failed", "connection",
            "ssl", "proxy", "network", "503", "502", "429",
            "temporarily unavailable", "rate limit", "reset by peer",
        ]
        if any(marker in normalized for marker in retryable_markers):
            return "retryable"
        skip_markers = [
            "未获取到", "未找到", "no data", "not found", "empty", "查无",
            "停牌", "suspend", "suspended", "delist", "退市", "未上市",
        ]
        if any(marker in normalized for marker in skip_markers):
            return "skip_eligible"
        return "blocking"

    @classmethod
    def _build_service_failure_record(
        cls,
        code: str,
        trade_date: date,
        reason: str,
        detail: str,
        reason_class: str,
        source: str,
    ) -> Dict[str, object]:
        source_attempts = [
            {
                "source": source,
                "detail": detail,
                "reason_class": reason_class,
                "candidate_skip_signal": reason_class == "skip_eligible",
            }
        ]
        return {
            "code": code,
            "target_trade_date": trade_date.isoformat(),
            "reason": reason,
            "reason_class": reason_class,
            "detail": detail,
            "source_attempts": source_attempts,
            "candidate_skip": cls._build_candidate_skip_info(source_attempts),
        }

    @classmethod
    def _parse_source_attempts(cls, detail: str) -> List[Dict[str, object]]:
        attempts: List[Dict[str, object]] = []
        for raw_line in str(detail or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = _SOURCE_ATTEMPT_PATTERN.match(line)
            if not match:
                continue
            error_type = (match.group("error_type") or "").strip()
            attempt_detail = (match.group("detail") or "").strip()
            reason_class = cls._classify_reason_class(f"{error_type} {attempt_detail}".strip())
            attempts.append(
                {
                    "source": match.group("source").strip(),
                    "error_type": error_type,
                    "detail": attempt_detail or error_type or line,
                    "reason_class": reason_class,
                    "candidate_skip_signal": reason_class == "skip_eligible",
                }
            )
        if attempts:
            return attempts
        fallback_reason_class = cls._classify_reason_class(detail)
        return [
            {
                "source": "market_data_sync_service",
                "detail": detail,
                "reason_class": fallback_reason_class,
                "candidate_skip_signal": fallback_reason_class == "skip_eligible",
            }
        ]

    @staticmethod
    def _summarize_reason_class(source_attempts: List[Dict[str, object]]) -> str:
        attempt_classes = {str(item.get("reason_class", "blocking")) for item in source_attempts}
        if attempt_classes == {"skip_eligible"}:
            return "skip_eligible"
        if attempt_classes == {"retryable"}:
            return "retryable"
        return "blocking"

    @staticmethod
    def _build_candidate_skip_info(source_attempts: List[Dict[str, object]]) -> Dict[str, object]:
        total_attempts = len(source_attempts)
        skip_eligible_attempts = sum(
            1 for item in source_attempts if item.get("reason_class") == "skip_eligible"
        )
        retryable_attempts = sum(
            1 for item in source_attempts if item.get("reason_class") == "retryable"
        )
        blocking_attempts = sum(
            1 for item in source_attempts if item.get("reason_class") == "blocking"
        )
        eligible = total_attempts > 0 and skip_eligible_attempts == total_attempts
        return {
            "eligible": eligible,
            "signal": "all_attempts_skip_eligible" if eligible else "needs_manual_review",
            "total_attempts": total_attempts,
            "skip_eligible_attempts": skip_eligible_attempts,
            "retryable_attempts": retryable_attempts,
            "blocking_attempts": blocking_attempts,
        }
