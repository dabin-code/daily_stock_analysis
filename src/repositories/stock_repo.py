# -*- coding: utf-8 -*-
"""
===================================
股票数据访问层
===================================

职责：
1. 封装股票数据的数据库操作
2. 提供日线数据查询接口
"""

import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Optional, List, Dict, Any, Tuple

import pandas as pd
from sqlalchemy import and_, desc, select

from src.storage import DatabaseManager, StockDaily

logger = logging.getLogger(__name__)


# B5: minimum bars / window-span tolerance for forward-window suspension
# detection. ``DEFAULT_GAP_TOLERANCE_FACTOR`` allows a 5-day evaluation
# window to span up to 10 calendar days (covers weekends + small Chinese
# holidays); 1.0 would be too tight (a single weekend already bumps a 5d
# window to 7-day span). ``MIN_BAR_RATIO`` sets the floor for how many of
# the requested bars must come back before the window is treated as
# under-resourced.
DEFAULT_GAP_TOLERANCE_FACTOR = 2.0
MIN_BAR_RATIO = 0.6


@dataclass(frozen=True)
class ForwardBarsMeta:
    """Metadata describing the forward-bar window quality.

    Used by the backtest pipeline to detect suspended-trading samples
    (where the requested 5-day window actually spans several weeks because
    the stock was halted) and under-resourced samples (where the data
    source returned fewer bars than the window asks for, e.g. listing
    delisted mid-window or data sync gap).

    Fields:
        requested_window_days:  Window the caller asked for (eval_window_days).
        actual_bar_count:       Number of bars actually returned.
        actual_span_days:       Calendar days between first and last bar
                                (None when fewer than 2 bars came back).
        gap_threshold_days:     Calendar-day cutoff used for the gap_too_long
                                check (= ceil(window * tolerance_factor)).
        gap_too_long:           True if actual_span_days > gap_threshold_days.
        insufficient_bars:      True if actual_bar_count < ceil(window * MIN_BAR_RATIO).
        tolerance_factor:       Factor applied to compute gap_threshold_days,
                                preserved here so callers can audit the cut.
    """
    requested_window_days: int
    actual_bar_count: int
    actual_span_days: Optional[int]
    gap_threshold_days: int
    gap_too_long: bool
    insufficient_bars: bool
    tolerance_factor: float


class StockRepository:
    """
    股票数据访问层
    
    封装 StockDaily 表的数据库操作
    """
    
    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        """
        初始化数据访问层
        
        Args:
            db_manager: 数据库管理器（可选，默认使用单例）
        """
        self.db = db_manager or DatabaseManager.get_instance()
    
    def get_latest(self, code: str, days: int = 2) -> List[StockDaily]:
        """
        获取最近 N 天的数据
        
        Args:
            code: 股票代码
            days: 获取天数
            
        Returns:
            StockDaily 对象列表（按日期降序）
        """
        try:
            return self.db.get_latest_data(code, days)
        except Exception as e:
            logger.error(f"获取最新数据失败: {e}")
            return []
    
    def get_range(
        self,
        code: str,
        start_date: date,
        end_date: date
    ) -> List[StockDaily]:
        """
        获取指定日期范围的数据
        
        Args:
            code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            StockDaily 对象列表
        """
        try:
            return self.db.get_data_range(code, start_date, end_date)
        except Exception as e:
            logger.error(f"获取日期范围数据失败: {e}")
            return []
    
    def save_dataframe(
        self,
        df: pd.DataFrame,
        code: str,
        data_source: str = "Unknown"
    ) -> int:
        """
        保存 DataFrame 到数据库
        
        Args:
            df: 包含日线数据的 DataFrame
            code: 股票代码
            data_source: 数据来源
            
        Returns:
            保存的记录数
        """
        try:
            return self.db.save_daily_data(df, code, data_source)
        except Exception as e:
            logger.error(f"保存日线数据失败: {e}")
            return 0
    
    def has_today_data(self, code: str, target_date: Optional[date] = None) -> bool:
        """
        检查是否有指定日期的数据
        
        Args:
            code: 股票代码
            target_date: 目标日期（默认今天）
            
        Returns:
            是否存在数据
        """
        try:
            return self.db.has_today_data(code, target_date)
        except Exception as e:
            logger.error(f"检查数据存在失败: {e}")
            return False
    
    def get_analysis_context(
        self, 
        code: str, 
        target_date: Optional[date] = None
    ) -> Optional[Dict[str, Any]]:
        """
        获取分析上下文
        
        Args:
            code: 股票代码
            target_date: 目标日期
            
        Returns:
            分析上下文字典
        """
        try:
            return self.db.get_analysis_context(code, target_date)
        except Exception as e:
            logger.error(f"获取分析上下文失败: {e}")
            return None

    def get_start_daily(self, *, code: str, analysis_date: date) -> Optional[StockDaily]:
        """Return StockDaily for analysis_date (preferred) or nearest previous date."""
        with self.db.get_session() as session:
            row = session.execute(
                select(StockDaily)
                .where(and_(StockDaily.code == code, StockDaily.date <= analysis_date))
                .order_by(desc(StockDaily.date))
                .limit(1)
            ).scalar_one_or_none()
            return row

    def get_forward_bars(self, *, code: str, analysis_date: date, eval_window_days: int) -> List[StockDaily]:
        """Return forward daily bars after analysis_date, up to eval_window_days.

        Backwards-compatible facade — drops the metadata. New callers should
        prefer :meth:`get_forward_bars_with_meta` so they can detect
        suspended-trading samples (B5).
        """
        bars, _ = self.get_forward_bars_with_meta(
            code=code,
            analysis_date=analysis_date,
            eval_window_days=eval_window_days,
        )
        return bars

    def get_forward_bars_with_meta(
        self,
        *,
        code: str,
        analysis_date: date,
        eval_window_days: int,
        tolerance_factor: float = DEFAULT_GAP_TOLERANCE_FACTOR,
    ) -> Tuple[List[StockDaily], ForwardBarsMeta]:
        """Return forward bars + a metadata block describing the window quality.

        The metadata is intentionally separate from the bars list so callers
        that only want the prices keep the simpler list shape (via
        :meth:`get_forward_bars`), while the backtest pipeline can read the
        ``ForwardBarsMeta`` to detect:

          * suspended-trading samples — the 5d window actually spans several
            weeks because the stock was halted (``gap_too_long=True``)
          * under-resourced samples — the data source returned fewer bars
            than the window asks for (``insufficient_bars=True``)

        Both conditions should suppress evaluation in the backtest service so
        the ``forward_return_5d`` denominator stays meaningful.
        """
        with self.db.get_session() as session:
            rows = session.execute(
                select(StockDaily)
                .where(and_(StockDaily.code == code, StockDaily.date > analysis_date))
                .order_by(StockDaily.date)
                .limit(eval_window_days)
            ).scalars().all()
            bars = list(rows)

        actual_bar_count = len(bars)
        if actual_bar_count >= 2:
            actual_span_days: Optional[int] = (bars[-1].date - bars[0].date).days
        else:
            actual_span_days = None

        gap_threshold_days = max(1, math.ceil(eval_window_days * tolerance_factor))
        gap_too_long = (
            actual_span_days is not None
            and actual_span_days > gap_threshold_days
        )

        # Floor for "enough bars" — 5d window allows down to 3 bars before
        # we treat it as under-resourced. Plays nicely with EntryEvaluator's
        # forward_return_5d requirement (needs the 5th bar) without
        # punishing samples that only lost the last day to a half-day close.
        min_bars_required = max(1, math.ceil(eval_window_days * MIN_BAR_RATIO))
        insufficient_bars = actual_bar_count < min_bars_required

        meta = ForwardBarsMeta(
            requested_window_days=eval_window_days,
            actual_bar_count=actual_bar_count,
            actual_span_days=actual_span_days,
            gap_threshold_days=gap_threshold_days,
            gap_too_long=gap_too_long,
            insufficient_bars=insufficient_bars,
            tolerance_factor=tolerance_factor,
        )
        return bars, meta
