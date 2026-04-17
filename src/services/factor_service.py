from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from sqlalchemy import func, select

from src.config import get_config
from src.storage import DatabaseManager, StockDaily
from src.indicators.ma_breakout_detector import MABreakoutDetector
from src.indicators.gap_detector import GapDetector
from src.indicators.limit_up_detector import LimitUpDetector
from src.indicators.divergence_detector import DivergenceDetector
from src.indicators.trendline_detector import TrendlineDetector
from src.indicators.pattern_detector import PatternDetector
from src.indicators.low_123_trendline_detector import Low123TrendlineDetector
from src.indicators.bottom_divergence_breakout_detector import (
    BottomDivergenceBreakoutDetector,
)

logger = logging.getLogger(__name__)


# ─── ma100_60min_combined gating constants ────────────────────────────
# Maximum bars since the most recent *real* upward crossing of MA100 that
# still counts as a fresh breakout (inclusive; 0 = crossed on latest bar).
_MA100_60MIN_FRESH_BARS_MAX = 5
# Upper bound on distance from MA100 at selection time (percent).  Beyond
# this the stock is considered to have already left the best-buy zone.
_MA100_60MIN_DISTANCE_PCT_MAX = 6.0
# Minimum ratio of pre-breakout bars (within the detector's pre-breakout
# window) whose close was at-or-below MA100.  Guards against labelling
# noise flips on an already-elevated stock as a fresh breakout.
_MA100_60MIN_PRE_BELOW_RATIO_MIN = 0.6
# Fallback signal: minimum number of bars *immediately* before the
# crossing that closed at-or-below MA100.  Either ratio OR consecutive
# count satisfies the pre-breakout background condition.
_MA100_60MIN_PRE_CONSECUTIVE_BELOW_MIN = 3


class FactorService:
    """从本地日线数据构建筛选输入。

    注意：
    - 本地因子快照只基于本地市场数据构建。
    - `theme_context` 仅为兼容过渡字段，当前不会驱动题材增强或外部板块补全。
    """

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        lookback_days: Optional[int] = None,
        breakout_lookback_days: Optional[int] = None,
        min_list_days: Optional[int] = None,
        theme_context: Optional[object] = None,
        fetcher_manager: Optional[Any] = None,
    ) -> None:
        config = get_config()
        self.db = db_manager or DatabaseManager.get_instance()
        self.lookback_days = (
            lookback_days if lookback_days is not None else config.screening_factor_lookback_days
        )
        self.min_list_days = (
            min_list_days if min_list_days is not None else config.screening_min_list_days
        )
        self.breakout_lookback_days = (
            breakout_lookback_days
            if breakout_lookback_days is not None
            else config.screening_breakout_lookback_days
        )
        # 保留 theme_context 仅为兼容旧调用方；主链路因子构建已与外部题材上下文解耦。
        self.theme_context = theme_context
        self.fetcher_manager = fetcher_manager

    def build_factor_snapshot(
        self,
        universe_df: pd.DataFrame,
        trade_date: Optional[date] = None,
        persist: bool = False,
    ) -> pd.DataFrame:
        if universe_df is None or universe_df.empty:
            return pd.DataFrame()

        if trade_date is None:
            trade_date = datetime.now().date()

        codes = [str(code) for code in universe_df["code"].dropna().tolist()]
        if not codes:
            return pd.DataFrame()

        start_date = trade_date - timedelta(days=self.lookback_days * 2)

        # 分批加载日线数据，防止一次性 OOM（5000 只 × 400 天 ≈ 200 万行）
        _FACTOR_BATCH_SIZE = 500
        all_bar_dicts: list = []
        for batch_start in range(0, len(codes), _FACTOR_BATCH_SIZE):
            batch_codes = codes[batch_start:batch_start + _FACTOR_BATCH_SIZE]
            with self.db.get_session() as session:
                rows = session.execute(
                    select(StockDaily)
                    .where(
                        StockDaily.code.in_(batch_codes),
                        StockDaily.date >= start_date,
                        StockDaily.date <= trade_date,
                    )
                    .order_by(StockDaily.code, StockDaily.date)
                ).scalars().all()
            all_bar_dicts.extend(
                {
                    "code": row.code,
                    "date": row.date,
                    "open": row.open,
                    "high": row.high,
                    "low": row.low,
                    "close": row.close,
                    "volume": row.volume,
                    "amount": row.amount,
                    "pct_chg": row.pct_chg,
                }
                for row in rows
            )

        if not all_bar_dicts:
            return pd.DataFrame()

        bars = pd.DataFrame(all_bar_dicts)

        snapshots = []
        universe_map = universe_df.set_index("code").to_dict("index")
        for code, group in bars.groupby("code"):
            group = group.sort_values("date").reset_index(drop=True)
            if len(group) < 20:
                continue

            latest_trade_date = pd.to_datetime(group.iloc[-1]["date"]).date()
            if latest_trade_date != trade_date:
                continue

            latest = group.iloc[-1]
            close_series = group["close"].astype(float)
            volume_series = group["volume"].astype(float).fillna(0.0)
            amount_series = group["amount"].astype(float).fillna(0.0)
            info = universe_map.get(code, {})
            list_date = info.get("list_date")
            if list_date:
                list_date = pd.to_datetime(list_date).date()
                days_since_listed = max((trade_date - list_date).days, 0)
            else:
                days_since_listed = 9999

            prior_bars = group.iloc[:-1]
            rolling_high = float(prior_bars["high"].tail(self.breakout_lookback_days).max()) if not prior_bars.empty else 0.0
            breakout_ratio = float(latest["close"]) / rolling_high if rolling_high else 0.0
            prior_amount = amount_series.iloc[:-1]
            prior_volume = volume_series.iloc[:-1]
            avg_amount = float(prior_amount.tail(5).mean()) if not prior_amount.empty else 0.0
            avg_volume = float(prior_volume.tail(5).mean()) if not prior_volume.empty else 0.0
            volume_ratio = float(latest["volume"]) / avg_volume if avg_volume else 0.0
            turnover_rate = round(min(volume_ratio * 2.0, 100.0), 4)
            trend_score = self._compute_trend_score(
                close=float(latest["close"]),
                ma5=float(close_series.tail(5).mean()),
                ma10=float(close_series.tail(10).mean()),
                ma20=float(close_series.tail(20).mean()),
                breakout_ratio=breakout_ratio,
            )
            liquidity_score = self._compute_liquidity_score(avg_amount=avg_amount, volume_ratio=volume_ratio)
            risk_flags = self._build_risk_flags(
                is_st=bool(info.get("is_st", False)),
                days_since_listed=int(days_since_listed),
                volume_ratio=volume_ratio,
                breakout_ratio=breakout_ratio,
            )

            extended = self._compute_extended_factors(group, latest, close_series)

            snapshots.append(
                {
                    "code": code,
                    "name": info.get("name") or code,
                    "close": float(latest["close"]),
                    "ma5": float(close_series.tail(5).mean()),
                    "ma10": float(close_series.tail(10).mean()),
                    "ma20": float(close_series.tail(20).mean()),
                    "ma60": float(close_series.tail(min(len(close_series), 60)).mean()),
                    "ma100": float(close_series.tail(min(len(close_series), 100)).mean()) if len(close_series) >= 100 else 0.0,
                    "volume_ratio": round(volume_ratio, 4),
                    "avg_amount": round(avg_amount, 2),
                    "breakout_ratio": round(breakout_ratio, 4),
                    "pct_chg": float(latest["pct_chg"] or 0.0),
                    "circ_mv": info.get("circ_mv"),
                    "is_st": bool(info.get("is_st", False)),
                    "days_since_listed": int(days_since_listed),
                    "turnover_rate": turnover_rate,
                    "trend_score": trend_score,
                    "liquidity_score": liquidity_score,
                    "risk_flags": risk_flags,
                    **extended,
                }
            )

        # 无条件计算基础 leader_score / extreme_strength_score（纯市场数据）
        self._enrich_base_scores(snapshots)

        snapshot_df = pd.DataFrame(snapshots)

        if persist and not snapshot_df.empty:
            self.db.replace_factor_snapshots(trade_date=trade_date, snapshots=snapshot_df.to_dict("records"))
        return snapshot_df

    @staticmethod
    def _enrich_base_scores(snapshots: List[Dict[str, Any]]) -> None:
        """无条件计算基础 leader_score / extreme_strength_score（纯市场数据）。

        使用 theme_match_score=0 / theme_heat_score=0 计算基础分数。
        Phase 1 约定：
        - `base_leader_score` / `base_extreme_strength_score` 始终代表纯市场数据基础分
        - `leader_score` / `extreme_strength_score` 暂时保留为兼容别名，在题材增强前先指向基础分
        - 若后续 HotThemeFactorEnricher 运行，可再生成题材增强分并切换兼容别名语义
        """
        from src.services.leader_score_calculator import LeaderScoreCalculator
        from src.services.extreme_strength_scorer import ExtremeStrengthScorer

        calc = LeaderScoreCalculator()
        scorer = ExtremeStrengthScorer()

        for s in snapshots:
            leader = calc.calculate_leader_score(
                theme_match_score=0.0,
                circ_mv=s.get("circ_mv"),
                turnover_rate=s.get("turnover_rate"),
                is_limit_up=s.get("is_limit_up", False),
                gap_breakaway=s.get("gap_breakaway", False),
                above_ma100=s.get("above_ma100", False),
                ma100_breakout_days=s.get("ma100_breakout_days", 0),
            )
            extreme = scorer.calculate_extreme_strength_score(
                above_ma100=s.get("above_ma100", False),
                gap_breakaway=s.get("gap_breakaway", False),
                pattern_123_low_trendline=s.get("pattern_123_low_trendline", False),
                pattern_123_watchlist=s.get("pattern_123_watchlist", False),
                is_limit_up=s.get("is_limit_up", False),
                bottom_divergence_double_breakout=s.get(
                    "bottom_divergence_double_breakout", False,
                ),
                theme_heat_score=0.0,
                leader_score=leader,
                volume_ratio=s.get("volume_ratio", 0.0) or 0.0,
                turnover_rate=s.get("turnover_rate"),
                circ_mv=s.get("circ_mv"),
                breakout_ratio=s.get("breakout_ratio", 0.0) or 0.0,
            )
            s["base_leader_score"] = leader
            s["base_extreme_strength_score"] = extreme
            s["theme_leader_score"] = s.get("theme_leader_score", 0.0) or 0.0
            s["theme_extreme_strength_score"] = s.get("theme_extreme_strength_score", 0.0) or 0.0
            s["leader_score"] = leader
            s["extreme_strength_score"] = extreme
            s["leader_score_source"] = "base"
            s["extreme_strength_score_source"] = "base"

    def _resolve_board_names_for_codes(self, codes: List[str]) -> Dict[str, List[str]]:
        board_map: Dict[str, List[str]] = {}

        normalized_codes = [str(item).strip().upper() for item in codes if str(item).strip()]
        if not normalized_codes:
            return board_map

        # 始终从 DB 读取板块数据（不依赖 theme_context）
        board_map = self.db.batch_get_instrument_board_names(normalized_codes)

        # 仅在有 theme_context 时调用外部 API 补全缺失板块
        if self.theme_context:
            missing_codes = [code for code in normalized_codes if not board_map.get(code)]
            for code in missing_codes:
                resolved = self._resolve_board_names(code)
                board_map[code] = resolved
                if resolved:
                    self.db.replace_instrument_board_memberships(
                        instrument_code=code,
                        memberships=[
                            {
                                "board_name": name,
                                "board_type": "unknown",
                                "market": "cn",
                                "source": "efinance",
                            }
                            for name in resolved
                        ],
                        market="cn",
                        source="efinance",
                    )
        return board_map

    def _resolve_board_names(self, code: str) -> List[str]:
        manager = self._get_fetcher_manager()
        if manager is None:
            return []
        try:
            boards = manager.get_belong_boards(code)
        except Exception:
            return []
        if not isinstance(boards, list):
            return []

        normalized: List[str] = []
        for item in boards:
            name = ""
            if isinstance(item, dict):
                name = str(
                    item.get("name")
                    or item.get("board_name")
                    or item.get("所属板块")
                    or item.get("industry")
                    or item.get("concept")
                    or ""
                ).strip()
            elif item is not None:
                name = str(item).strip()
            if name and name not in normalized:
                normalized.append(name)
        return normalized

    def _get_fetcher_manager(self) -> Optional[Any]:
        if self.fetcher_manager is not None:
            return self.fetcher_manager
        try:
            from data_provider.base import DataFetcherManager
        except Exception:
            return None
        self.fetcher_manager = DataFetcherManager()
        return self.fetcher_manager

    def get_latest_trade_date(
        self,
        universe_df: pd.DataFrame,
        min_coverage_ratio: float = 0.5,
    ) -> Optional[date]:
        """
        返回本地日线表中覆盖率足够的最新交易日。

        避免被少量"毒数据"（如部分同步导致的零星未来日期记录）污染：
        只有当某个日期拥有 >= universe 数量 * min_coverage_ratio 条记录时，
        才认定该日期为可用交易日。

        Args:
            universe_df: 股票池 DataFrame，需含 code 列
            min_coverage_ratio: 覆盖率下限，默认 0.5（至少覆盖一半股票池）
        """
        if universe_df is None or universe_df.empty:
            return None

        codes = [str(code) for code in universe_df["code"].dropna().tolist()]
        if not codes:
            return None

        min_count = max(1, int(len(codes) * min_coverage_ratio))

        with self.db.get_session() as session:
            # 按日期倒序查找，取第一个覆盖率达标的日期
            from sqlalchemy import desc
            row = session.execute(
                select(StockDaily.date, func.count(StockDaily.code.distinct()).label("cnt"))
                .where(StockDaily.code.in_(codes))
                .group_by(StockDaily.date)
                .having(func.count(StockDaily.code.distinct()) >= min_count)
                .order_by(desc(StockDaily.date))
                .limit(1)
            ).first()
        return row[0] if row else None

    @staticmethod
    def _compute_trend_score(close: float, ma5: float, ma10: float, ma20: float, breakout_ratio: float) -> float:
        score = 0.0
        if close >= ma20:
            score += 25.0
        if ma5 >= ma10 >= ma20:
            score += 45.0
        if breakout_ratio >= 1.0:
            score += 15.0
        elif breakout_ratio >= 0.995:
            score += 10.0
        return round(min(score, 100.0), 2)

    @staticmethod
    def _compute_liquidity_score(avg_amount: float, volume_ratio: float) -> float:
        amount_score = min(avg_amount / 1_000_000, 80.0)
        volume_score = min(volume_ratio * 10, 20.0)
        return round(min(amount_score + volume_score, 100.0), 2)

    def _build_risk_flags(
        self,
        is_st: bool,
        days_since_listed: int,
        volume_ratio: float,
        breakout_ratio: float,
    ) -> list[str]:
        flags = []
        if is_st:
            flags.append("st")
        if days_since_listed < self.min_list_days:
            flags.append("new_listing")
        if volume_ratio < 1.0:
            flags.append("low_volume")
        if breakout_ratio < 0.98:
            flags.append("far_from_breakout")
        return flags

    def _compute_extended_factors(
        self,
        group: pd.DataFrame,
        latest: pd.Series,
        close_series: pd.Series,
    ) -> dict:
        """Compute additional factor dimensions used by strategy screening."""
        close = float(latest["close"])
        ma5 = float(close_series.tail(5).mean()) if len(close_series) >= 5 else close

        pct_chg_5d = 0.0
        if len(close_series) >= 6:
            prev_5 = float(close_series.iloc[-6])
            pct_chg_5d = round((close - prev_5) / prev_5 * 100.0, 4) if prev_5 else 0.0

        pct_chg_20d = 0.0
        if len(close_series) >= 21:
            prev_20 = float(close_series.iloc[-21])
            pct_chg_20d = round((close - prev_20) / prev_20 * 100.0, 4) if prev_20 else 0.0

        ma5_distance_pct = round(abs(close - ma5) / ma5 * 100.0, 4) if ma5 else 0.0

        high = float(latest.get("high", close))
        low = float(latest.get("low", close))
        prev_close = float(close_series.iloc[-2]) if len(close_series) >= 2 else close
        amplitude = round((high - low) / prev_close * 100.0, 4) if prev_close else 0.0

        # Close strength: position of close within day's range (0=at low, 1=at high)
        # Used by volume_breakout to filter out false breakouts (冲高回落)
        if high > low:
            close_strength = round((close - low) / (high - low), 4)
        else:
            close_strength = 0.5

        tail_bars = group.tail(5) if len(group) >= 5 else group.tail(1)
        candle_pattern = self._detect_candle_pattern(tail_bars)

        # MA100 strategy factors
        ma100_factors = self._compute_ma100_factors(group, close_series, close)

        # Gap / limit-up factors
        gap_limit_factors = self._compute_gap_limit_factors(group)

        # MACD divergence factors
        macd_factors = self._compute_macd_divergence_factors(group)

        # Trendline factors
        trendline_factors = self._compute_trendline_factors(group)

        # 123 pattern factors
        pattern_123_factors, pattern_123_raw = self._compute_pattern_123_factors(group)

        # Bottom divergence double breakout factors
        bottom_div_factors = self._compute_bottom_divergence_factors(group)

        # Trend pullback freshness / support confirmation
        pullback_touched_ma = self._compute_pullback_touched_ma(group, close_series)

        # MA100 + Low-123 combined factors (Strategy 2)
        ma100_low123_factors = self._compute_ma100_low123_combined_factors(
            ma100_factors, pattern_123_factors, pattern_123_raw, group
        )

        # MA100 + 60-min combined factors (Strategy 3)
        ma100_60min_factors = self._compute_ma100_60min_combined_factors(ma100_factors)

        return {
            "pct_chg_5d": pct_chg_5d,
            "pct_chg_20d": pct_chg_20d,
            "ma5_distance_pct": ma5_distance_pct,
            "amplitude": amplitude,
            "close_strength": close_strength,
            "candle_pattern": candle_pattern,
            "pullback_touched_ma": pullback_touched_ma,
            **ma100_factors,
            **gap_limit_factors,
            **macd_factors,
            **trendline_factors,
            **pattern_123_factors,
            **bottom_div_factors,
            **ma100_low123_factors,
            **ma100_60min_factors,
        }

    @staticmethod
    def _compute_ma100_factors(group: pd.DataFrame, close_series: pd.Series, close: float) -> dict:
        """Compute MA100-related factors for screening strategies C/D."""
        n = len(close_series)
        ma100 = float(close_series.tail(min(n, 100)).mean()) if n >= 100 else 0.0
        above_ma100 = close > ma100 if ma100 > 0 else False
        ma100_distance_pct = round((close - ma100) / ma100 * 100.0, 4) if ma100 > 0 else 0.0

        ma100_breakout = MABreakoutDetector.detect_breakout(group, ma_period=100) if n >= 100 else {}
        breakout_days = ma100_breakout.get("breakout_days", 0)
        bars_since_breakout_raw = ma100_breakout.get("bars_since_breakout")
        breakout_bar_index_raw = ma100_breakout.get("breakout_bar_index")
        pre_breakout_below_ratio = float(ma100_breakout.get("pre_breakout_below_ratio", 0.0))
        pre_breakout_consecutive_below_bars = int(
            ma100_breakout.get("pre_breakout_consecutive_below_bars", 0)
        )

        pullback_ma100 = MABreakoutDetector.detect_pullback_support(group, ma_period=100) if n >= 100 else {}
        pullback_ma20 = MABreakoutDetector.detect_pullback_support(group, ma_period=20) if n >= 20 else {}

        # Stop-loss: highest MA below price
        ma20 = float(close_series.tail(min(n, 20)).mean()) if n >= 20 else 0.0
        stop_loss_price = 0.0
        stop_loss_ma = ""
        if ma20 > 0 and ma20 < close:
            stop_loss_price = round(ma20, 4)
            stop_loss_ma = "MA20"
        if ma100 > 0 and ma100 < close and ma100 > stop_loss_price:
            stop_loss_price = round(ma100, 4)
            stop_loss_ma = "MA100"

        # Use -1 sentinel when no real crossing was found — keeps the snapshot
        # schema JSON/DB friendly and lets downstream consumers explicitly
        # detect "no breakout" without None-handling.
        bars_since_breakout = (
            int(bars_since_breakout_raw) if bars_since_breakout_raw is not None else -1
        )
        breakout_bar_index = (
            int(breakout_bar_index_raw) if breakout_bar_index_raw is not None else -1
        )

        return {
            "ma100": round(ma100, 4),
            "above_ma100": above_ma100,
            "ma100_distance_pct": ma100_distance_pct,
            "ma100_breakout_days": breakout_days,
            # ── New: real-crossing semantics (see MABreakoutDetector docstring) ──
            "ma100_breakout_bar_index": breakout_bar_index,
            "ma100_bars_since_breakout": bars_since_breakout,
            "ma100_pre_breakout_below_ratio": round(pre_breakout_below_ratio, 4),
            "ma100_pre_breakout_consecutive_below_bars": pre_breakout_consecutive_below_bars,
            "pullback_ma100": pullback_ma100.get("is_pullback_support", False),
            "pullback_ma20": pullback_ma20.get("is_pullback_support", False),
            "stop_loss_price": stop_loss_price,
            "stop_loss_ma": stop_loss_ma,
        }

    @staticmethod
    def _compute_gap_limit_factors(group: pd.DataFrame) -> dict:
        """Compute gap and limit-up factors for screening strategy C."""
        gap_result = GapDetector.detect_breakaway_gap(group)
        pct_chg = float(group.iloc[-1].get("pct_chg", 0)) if not group.empty else 0.0
        limit_result = LimitUpDetector.is_breakout_limit_up(group)

        return {
            "gap_up": gap_result.get("is_gap_up", False),
            "gap_breakaway": gap_result.get("is_breakaway", False),
            "gap_exhaustion_risk": gap_result.get("is_exhaustion_risk", False),
            "is_limit_up": limit_result.get("is_limit_up", False),
            "limit_up_breakout": limit_result.get("is_breakout_high", False),
        }

    @staticmethod
    def _compute_macd_divergence_factors(group: pd.DataFrame) -> dict:
        """Compute MACD divergence factors from daily data."""
        if len(group) < 35:
            return {
                "macd_bull_divergence": False,
                "macd_bear_divergence": False,
            }
        df_for_div = group[["close"]].copy()
        if "high" in group.columns:
            df_for_div["high"] = group["high"].values
        if "low" in group.columns:
            df_for_div["low"] = group["low"].values
        bull = DivergenceDetector.detect_bullish(df_for_div)
        bear = DivergenceDetector.detect_bearish(df_for_div)
        return {
            "macd_bull_divergence": bull.get("found", False),
            "macd_bear_divergence": bear.get("found", False),
        }

    @staticmethod
    def _compute_trendline_factors(group: pd.DataFrame) -> dict:
        """Compute trendline breakout factors for screening strategy A."""
        if len(group) < 20:
            return {"trendline_breakout": False, "trendline_touch_count": 0}
        tl_result = TrendlineDetector.detect_trendline_breakout(group)
        is_breakout = tl_result.get("breakout", False) and tl_result.get("direction") == "up"
        downtrend = tl_result.get("downtrend") or {}
        touch_count = downtrend.get("touch_count", 0) if is_breakout else 0
        return {
            "trendline_breakout": is_breakout,
            "trendline_touch_count": touch_count,
        }

    @staticmethod
    def _compute_pattern_123_factors(group: pd.DataFrame) -> tuple[dict, dict]:
        """Compute 123 bottom pattern factors for screening strategy B.

        Returns:
            (factors_dict, raw_detector_result) — the raw result is used by
            downstream combined-strategy methods to build detailed hit_reasons.
        """
        empty_raw: dict = {}
        if len(group) < 40:
            return {
                # Legacy fields (kept for backward compatibility)
                "pattern_123_bottom": False,
                "pattern_123_breakout": False,
                "pattern_123_higher_low_pct": 0.0,
                # New joint-detector fields
                "pattern_123_low_trendline": False,
                "pattern_123_watchlist": False,
                "pattern_123_breakout_ready": False,
                "pattern_123_state": "rejected",
                "pattern_123_entry_price": None,
                "pattern_123_stop_loss": None,
                "pattern_123_signal_strength": 0.0,
                "pattern_123_rejection_reason": "insufficient_data",
                "pattern_123_pullback_reentry": False,
                "pattern_123_pullback_support_price": None,
                "pattern_123_trailing_stop_price": None,
                "pattern_123_trailing_stop_upgrades": 0,
            }, empty_raw

        # Legacy (PatternDetector) — kept for backward compat
        legacy = PatternDetector.detect_123_bottom(group)
        found_legacy = legacy.get("found", False)
        confirmed_legacy = legacy.get("breakout_confirmed", False)
        higher_low_pct = 0.0
        if found_legacy and legacy.get("point1") and legacy.get("point3"):
            p1 = legacy["point1"]["price"]
            p3 = legacy["point3"]["price"]
            if p1 > 0:
                higher_low_pct = round((p3 - p1) / p1 * 100.0, 4)

        # Joint detector
        joint = Low123TrendlineDetector.detect(group)
        state = joint.get("state", "rejected")
        is_breakout_ready = state == "breakout_ready"
        is_watching = state == "watching"

        factors = {
            # Legacy fields
            "pattern_123_bottom": found_legacy,
            "pattern_123_breakout": confirmed_legacy,
            "pattern_123_higher_low_pct": higher_low_pct,
            # New detector fields
            "pattern_123_low_trendline": is_breakout_ready,
            "pattern_123_watchlist": is_watching,
            "pattern_123_breakout_ready": is_breakout_ready,
            "pattern_123_state": state,
            "pattern_123_entry_price": joint.get("entry_price"),
            "pattern_123_stop_loss": joint.get("stop_loss_price"),
            "pattern_123_signal_strength": joint.get("signal_strength", 0.0),
            "pattern_123_rejection_reason": joint.get("rejection_reason"),
            # 回踩支撑 + 动态止损
            "pattern_123_pullback_reentry": bool(
                joint.get("pullback_reentry") and joint["pullback_reentry"].get("detected")
            ),
            "pattern_123_pullback_support_price": (
                joint["pullback_reentry"]["support_price"]
                if joint.get("pullback_reentry") and joint["pullback_reentry"].get("detected")
                else None
            ),
            "pattern_123_trailing_stop_price": (
                joint["trailing_stop"]["price"]
                if joint.get("trailing_stop")
                else None
            ),
            "pattern_123_trailing_stop_upgrades": (
                joint["trailing_stop"]["upgrades"]
                if joint.get("trailing_stop")
                else 0
            ),
        }
        return factors, joint

    @staticmethod
    def _compute_bottom_divergence_factors(group: pd.DataFrame) -> dict:
        """Compute bottom divergence double breakout factors."""
        if len(group) < 60:
            return {
                "bottom_divergence_double_breakout": False,
                "bottom_divergence_state": "rejected",
                "bottom_divergence_pattern_code": None,
                "bottom_divergence_pattern_label": None,
                "bottom_divergence_signal_strength": 0.0,
                "bottom_divergence_entry_price": None,
                "bottom_divergence_stop_loss": None,
                "bottom_divergence_horizontal_breakout": False,
                "bottom_divergence_trendline_breakout": False,
                "bottom_divergence_sync_breakout": False,
                "bottom_divergence_confirmation_days": None,
                "bottom_divergence_hit_reasons": [],
                "bottom_divergence_buy_points": [],
                "bottom_divergence_exit_plan": None,
                "bottom_divergence_buy_point_count": 0,
            }

        result = BottomDivergenceBreakoutDetector.detect(group)
        state = result.get("state", "rejected")
        confirmation_days = FactorService._compute_bottom_divergence_confirmation_days(group, result)

        return {
            "bottom_divergence_double_breakout": state == "confirmed",
            "bottom_divergence_state": state,
            "bottom_divergence_pattern_code": result.get("pattern_code"),
            "bottom_divergence_pattern_label": result.get("pattern_label"),
            "bottom_divergence_signal_strength": result.get("signal_strength", 0.0),
            "bottom_divergence_entry_price": result.get("entry_price"),
            "bottom_divergence_stop_loss": result.get("stop_loss_price"),
            "bottom_divergence_horizontal_breakout": result.get(
                "horizontal_breakout_confirmed", False
            ),
            "bottom_divergence_trendline_breakout": result.get(
                "trendline_breakout_confirmed", False
            ),
            "bottom_divergence_sync_breakout": result.get("double_breakout_sync", False),
            "bottom_divergence_confirmation_days": confirmation_days,
            "bottom_divergence_hit_reasons": result.get("hit_reasons", []),
            "bottom_divergence_buy_points": result.get("buy_points", []),
            "bottom_divergence_exit_plan": result.get("exit_plan"),
            "bottom_divergence_buy_point_count": len([
                bp for bp in result.get("buy_points", []) if bp.get("triggered")
            ]),
        }

    @staticmethod
    def _compute_bottom_divergence_confirmation_days(
        group: pd.DataFrame,
        detector_result: dict,
    ) -> Optional[int]:
        confirmation_bar = detector_result.get("confirmation_bar_index")
        if confirmation_bar is None:
            downtrend_line = detector_result.get("downtrend_line") or {}
            confirmation_bar = downtrend_line.get("breakout_bar_index")
        if confirmation_bar is None:
            return None
        try:
            latest_bar = len(group) - 1
            return max(latest_bar - int(confirmation_bar), 0)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _compute_pullback_touched_ma(group: pd.DataFrame, close_series: pd.Series) -> bool:
        if len(group) < 10:
            return False

        recent = group.tail(3).copy()
        recent_closes = close_series.astype(float)
        ma5_series = recent_closes.rolling(5).mean()
        ma10_series = recent_closes.rolling(10).mean()

        for idx, row in recent.iterrows():
            low = float(row.get("low", row.get("close", 0.0)) or 0.0)
            ma5 = ma5_series.iloc[idx] if idx < len(ma5_series) else np.nan
            ma10 = ma10_series.iloc[idx] if idx < len(ma10_series) else np.nan

            if pd.notna(ma5) and ma5 > 0 and abs(low - float(ma5)) / float(ma5) <= 0.01:
                return True
            if pd.notna(ma10) and ma10 > 0 and abs(low - float(ma10)) / float(ma10) <= 0.02:
                return True

        return False

    @staticmethod
    def _compute_ma100_low123_combined_factors(
        ma100_factors: dict, pattern_123_factors: dict, pattern_123_raw: dict,
        group: pd.DataFrame,
    ) -> dict:
        """Combine MA100 + Low-123 pattern into a single gate with hit reasons.

        Hard freshness gate: bars_since_entry > 3 → reject entirely.
        Shadow observability mode: if `breakout_bar_index` is missing, keep the
        legacy confirmation result but emit explicit validation status/reason so
        downstream screening, backtest attribution, and monitoring can isolate
        these samples before a future fail-closed rollout.
        Freshness is derived from `pattern_123_raw["downtrend_line"]["breakout_bar_index"]`
        relative to the latest bar in `group`.
        """
        above_ma100 = bool(ma100_factors.get("above_ma100", False))
        p123_state = str(pattern_123_factors.get("pattern_123_state", "rejected") or "rejected")
        p123_breakout_ready = bool(
            pattern_123_factors.get("pattern_123_breakout_ready", False)
            or pattern_123_factors.get("pattern_123_low_trendline", False)
            or p123_state == "breakout_ready"
        )
        p123_watching = bool(
            pattern_123_factors.get("pattern_123_watchlist", False)
            or p123_state == "watching"
        )

        # bars_since_entry: how many bars since the 123 breakout confirmation
        entry_price = pattern_123_factors.get("pattern_123_entry_price")
        signal_strength = float(pattern_123_factors.get("pattern_123_signal_strength", 0.0))
        bars_since_entry = FactorService._compute_low123_confirmation_days(group, pattern_123_raw)
        data_complete = p123_breakout_ready and bars_since_entry is not None

        # Low123 detector and MA100 gate both need to be fresh enough to stay
        # actionable. We suppress stale breakouts once the P2 breakout
        # is more than 3 bars old. Watching state is routed into a dedicated
        # watchlist channel instead of the breakout-confirmed channel.
        validation_status = "confirmed"
        validation_reason: Optional[str] = None
        if not above_ma100:
            validation_status = "below_ma100"
            validation_reason = "below_ma100"
        elif p123_watching:
            validation_status = "watching"
            validation_reason = "watching"
        elif not p123_breakout_ready:
            validation_status = "low123_not_ready"
            validation_reason = "low123_not_ready"
        elif bars_since_entry is None:
            validation_status = "confirmed_missing_breakout_bar_index"
            validation_reason = "missing_breakout_bar_index"
        elif bars_since_entry > 3:
            validation_status = "stale_breakout"
            validation_reason = "stale_breakout"

        confirmed = (
            p123_breakout_ready
            and above_ma100
            and (bars_since_entry is None or bars_since_entry <= 3)
        )
        watchlist = above_ma100 and p123_watching

        # ── MA score (breakout recency + distance) ──
        breakout_days = int(ma100_factors.get("ma100_breakout_days", 0))
        distance_pct = abs(float(ma100_factors.get("ma100_distance_pct", 0.0)))

        if breakout_days <= 5:
            recency_score = 1.0
        elif breakout_days <= 10:
            recency_score = 0.7
        else:
            recency_score = 0.4

        if distance_pct <= 5.0:
            dist_score = 1.0 - (distance_pct / 5.0) * 0.7  # 0%→1.0, 5%→0.3
        else:
            dist_score = 0.3

        ma_score = round(recency_score * 0.6 + dist_score * 0.4, 4)

        # ── Hit reasons (Chinese 【标题】描述 format) ──
        hit_reasons: list[str] = []
        watch_hit_reasons: list[str] = []
        if confirmed:
            hit_reasons = _build_ma100_low123_hit_reasons(
                ma100_factors, pattern_123_factors, pattern_123_raw, group,
                breakout_days, distance_pct, signal_strength,
            )
            if validation_status == "confirmed_missing_breakout_bar_index":
                hit_reasons.insert(
                    0,
                    "【数据校验】缺少 breakout_bar_index，当前按观察模式保留，建议单独统计并人工复核",
                )
        elif watchlist:
            watch_hit_reasons = _build_ma100_low123_watch_hit_reasons(
                ma100_factors, pattern_123_raw, group, distance_pct, signal_strength,
            )

        return {
            "ma100_low123_confirmed": confirmed,
            "ma100_low123_watchlist": watchlist,
            "ma100_low123_state": p123_state,
            "ma100_low123_data_complete": data_complete,
            "ma100_low123_pattern_strength": signal_strength if (confirmed or watchlist) else 0.0,
            "ma100_low123_ma_score": ma_score if (confirmed or watchlist) else 0.0,
            "ma100_low123_validation_status": validation_status,
            "ma100_low123_validation_reason": validation_reason,
            "ma100_low123_hit_reasons": hit_reasons,
            "ma100_low123_watch_hit_reasons": watch_hit_reasons,
        }

    @staticmethod
    def _compute_low123_confirmation_days(
        group: pd.DataFrame,
        detector_result: dict,
    ) -> Optional[int]:
        """Compute bars since Low123 P2 breakout confirmation.

        Only breakout_p2_bar_index is valid for freshness.
        Trendline breakout is now only a bonus signal and cannot replace the
        actual P2 breakout timing.
        """
        confirmation_bar = detector_result.get("breakout_p2_bar_index")
        if confirmation_bar is None:
            return None
        try:
            latest_bar = len(group) - 1
            return max(latest_bar - int(confirmation_bar), 0)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _compute_ma100_60min_combined_factors(ma100_factors: dict) -> dict:
        """MA100+60分钟线联合策略因子（Strategy 3）。

        选股门控（全部满足才算入选）：

        1. ``above_ma100``：当前收盘位于 MA100 上方。
        2. ``ma100_bars_since_breakout`` 在 ``[0, _MA100_60MIN_FRESH_BARS_MAX]``：
           最近一次真实上穿 MA100（前一日收盘 ≤ MA100、当日收盘 > MA100）
           发生在最近 ``_MA100_60MIN_FRESH_BARS_MAX`` 根 K 线内。
        3. 突破前背景过滤：``pre_breakout_below_ratio`` ≥ 0.6
           或 ``pre_breakout_consecutive_below_bars`` ≥ 3，避免把已在 MA100
           上方运行的普通波动误认为新的趋势突破。
        4. ``ma100_distance_pct`` 绝对值 ≤ ``_MA100_60MIN_DISTANCE_PCT_MAX``：
           防止已经明显脱离最佳买点区间的滞后股票入选。

        注意：不再使用 ``ma100_breakout_days``（连续站上天数）作为门控依据，
        该旧字段仍保留在 ma100_factors 中供其它模块（leader_score、stock_analyzer
        报告等）按其连续站上语义复用。
        """
        above_ma100 = bool(ma100_factors.get("above_ma100", False))
        bars_since_breakout = int(ma100_factors.get("ma100_bars_since_breakout", -1))
        pre_below_ratio = float(ma100_factors.get("ma100_pre_breakout_below_ratio", 0.0))
        pre_consecutive_below = int(
            ma100_factors.get("ma100_pre_breakout_consecutive_below_bars", 0)
        )
        distance_pct_signed = float(ma100_factors.get("ma100_distance_pct", 0.0))
        distance_pct = abs(distance_pct_signed)
        ma100_val = float(ma100_factors.get("ma100", 0.0))

        has_fresh_crossing = (
            bars_since_breakout is not None
            and bars_since_breakout >= 0
            and bars_since_breakout <= _MA100_60MIN_FRESH_BARS_MAX
        )
        pre_breakout_ok = (
            pre_below_ratio >= _MA100_60MIN_PRE_BELOW_RATIO_MIN
            or pre_consecutive_below >= _MA100_60MIN_PRE_CONSECUTIVE_BELOW_MIN
        )
        distance_ok = distance_pct <= _MA100_60MIN_DISTANCE_PCT_MAX

        confirmed = bool(
            above_ma100 and has_fresh_crossing and pre_breakout_ok and distance_ok
        )

        # ── Freshness score: b=0→1.0, b=1→0.9, … b=5→0.5 ──
        freshness_score = (
            max(1.0 - max(bars_since_breakout, 0) * 0.1, 0.0) if confirmed else 0.0
        )

        # ── MA score (recency × distance) ──
        if distance_pct <= _MA100_60MIN_DISTANCE_PCT_MAX:
            dist_score = 1.0 - (distance_pct / _MA100_60MIN_DISTANCE_PCT_MAX) * 0.7
        else:
            dist_score = 0.3
        ma_score = (
            round(freshness_score * 0.6 + dist_score * 0.4, 4) if confirmed else 0.0
        )

        # ── Hit reasons with 60-min operational guidance ──
        hit_reasons: list[str] = []
        if confirmed:
            hit_reasons.append(
                f"【MA100站稳确认】{bars_since_breakout}根K线前真实上穿，"
                f"MA100={ma100_val:.2f}，距离{distance_pct:.1f}%"
            )
            hit_reasons.append(
                f"【60分钟入场提示】建议关注次日60分钟线，"
                f"突破60分钟MA20或站稳MA100({ma100_val:.2f})时买入"
            )

        return {
            "ma100_60min_confirmed": confirmed,
            "ma100_60min_freshness_score": freshness_score,
            "ma100_60min_ma_score": ma_score,
            "ma100_60min_hit_reasons": hit_reasons,
        }

    @staticmethod
    def _detect_candle_pattern(bars: pd.DataFrame) -> str:
        """Detect basic candlestick pattern from recent bars.

        Returns a pattern identifier string. Operates on the last row for single-bar
        patterns, and on all rows for multi-bar patterns.
        """
        if bars.empty:
            return "unknown"

        latest = bars.iloc[-1]
        o, h, l, c = float(latest["open"]), float(latest["high"]), float(latest["low"]), float(latest["close"])
        pct = float(latest.get("pct_chg", 0.0) or 0.0)
        body = abs(c - o)
        total_range = h - l if h > l else 0.001

        body_ratio = body / total_range

        if body_ratio < 0.15 and total_range / max(l, 0.01) > 0.02:
            return "doji"

        if pct >= 5.0 and body_ratio > 0.6 and c > o:
            return "big_yang"

        if pct <= -5.0 and body_ratio > 0.6 and c < o:
            return "big_yin"

        if len(bars) >= 5:
            pattern = _detect_one_yang_three_yin(bars)
            if pattern:
                return pattern

        return "normal"


def _idx_to_date(group: pd.DataFrame, idx: int) -> str:
    """Convert a bar index to its date string (YYYY-MM-DD)."""
    if idx is None or idx < 0 or idx >= len(group):
        return "N/A"
    raw = group.iloc[idx]["date"]
    return str(pd.to_datetime(raw).date())


def _build_ma100_low123_hit_reasons(
    ma100_factors: dict,
    pattern_123_factors: dict,
    raw: dict,
    group: pd.DataFrame,
    breakout_days: int,
    distance_pct: float,
    signal_strength: float,
) -> list[str]:
    """Build detailed hit reasons for the MA100+Low123 combined strategy.

    Extracts structural info from the raw Low123TrendlineDetector result to
    produce Chinese-formatted 【标题】描述 strings that fully describe the
    detected 123 structure, trendline, breakout synchronisation and MA100
    confirmation.  All bar indices are converted to dates for user readability.
    """
    reasons: list[str] = []

    # 1. 123 结构关键点
    p1 = raw.get("point1") or {}
    p2 = raw.get("point2") or {}
    p3 = raw.get("point3") or {}
    if p1 and p2 and p3:
        p1_price = p1.get("price", 0)
        p2_price = p2.get("price", 0)
        p3_price = p3.get("price", 0)
        higher_low_pct = round((p3_price - p1_price) / p1_price * 100, 2) if p1_price else 0
        bounce_pct = round((p2_price - p1_price) / p1_price * 100, 2) if p1_price else 0
        retrace_pct = round((p2_price - p3_price) / (p2_price - p1_price) * 100, 2) if (p2_price - p1_price) else 0
        p1_date = _idx_to_date(group, p1.get("idx"))
        p2_date = _idx_to_date(group, p2.get("idx"))
        p3_date = _idx_to_date(group, p3.get("idx"))
        reasons.append(
            f"【123结构】P1({p1_date},价格{p1_price:.2f}) → "
            f"P2({p2_date},价格{p2_price:.2f}) → "
            f"P3({p3_date},价格{p3_price:.2f})，"
            f"反弹{bounce_pct}%，回撤{retrace_pct}%，P3抬高{higher_low_pct}%"
        )

    # 2. 下降趋势线
    dtl = raw.get("downtrend_line") or {}
    if dtl.get("found"):
        touch_count = dtl.get("touch_count", 0)
        slope = dtl.get("slope", 0)
        touch_pts = dtl.get("touch_points", [])
        touch_desc = "、".join(
            f"{_idx_to_date(group, tp['idx'])}({tp['price']:.2f})"
            for tp in touch_pts[:4]
        )
        bo_bar = dtl.get("breakout_bar_index")
        proj_val = dtl.get("projected_value_at_breakout")
        tl_status = "已突破" if dtl.get("breakout_confirmed") else "未突破"
        tl_detail = ""
        if bo_bar is not None and proj_val is not None:
            bo_date = _idx_to_date(group, bo_bar)
            tl_detail = f"，突破于{bo_date}(趋势线投影{proj_val:.2f})"
        reasons.append(
            f"【下降趋势线】斜率{slope:.6f}，{touch_count}个触点（{touch_desc}），"
            f"{tl_status}{tl_detail}"
        )

    # 3. P2 突破确认（趋势线为加分项）
    bo_p2 = raw.get("breakout_point2_confirmed", False)
    bo_tl = raw.get("breakout_trendline_confirmed", False)
    if bo_p2 and bo_tl:
        reasons.append("【P2突破】已突破P2高点确认买入信号，趋势线同步突破（加分项）")
    elif bo_p2:
        reasons.append("【P2突破】已突破P2高点确认买入信号")

    # 4. MA100 站上确认
    ma100_val = ma100_factors.get("ma100", 0)
    reasons.append(
        f"【MA100站上确认】突破{breakout_days}天，"
        f"MA100={ma100_val:.2f}，距离{distance_pct:.1f}%"
    )

    # 5. 回踩支撑 + 动态止损
    pb = raw.get("pullback_reentry") or {}
    if pb.get("detected"):
        reasons.append(
            f"【回踩加仓】回踩P2支撑位{pb['support_price']:.2f}，"
            f"深度{pb['depth_pct']:.1f}%，企稳确认"
        )
    ts = raw.get("trailing_stop") or {}
    if ts.get("upgrades", 0) > 0:
        reasons.append(
            f"【动态止损】止损已从P3上移{ts['upgrades']}次至{ts['price']:.2f}"
        )

    # 6. 信号强度
    entry_price = pattern_123_factors.get("pattern_123_entry_price")
    stop_loss = pattern_123_factors.get("pattern_123_stop_loss")
    reasons.append(
        f"【信号强度】综合评分{signal_strength:.2f}，"
        f"入场价{entry_price}，止损价{stop_loss}"
    )

    return reasons


def _build_ma100_low123_watch_hit_reasons(
    ma100_factors: dict,
    raw: dict,
    group: pd.DataFrame,
    distance_pct: float,
    signal_strength: float,
) -> list[str]:
    """Build watchlist reasons for MA100 + Low123 watching state."""
    reasons: list[str] = []

    p1 = raw.get("point1") or {}
    p2 = raw.get("point2") or {}
    p3 = raw.get("point3") or {}
    if p1 and p2 and p3:
        p1_price = p1.get("price", 0)
        p2_price = p2.get("price", 0)
        p3_price = p3.get("price", 0)
        p1_date = _idx_to_date(group, p1.get("idx"))
        p2_date = _idx_to_date(group, p2.get("idx"))
        p3_date = _idx_to_date(group, p3.get("idx"))
        reasons.append(
            f"【123观察结构】P1({p1_date},价格{p1_price:.2f}) → "
            f"P2({p2_date},价格{p2_price:.2f}) → "
            f"P3({p3_date},价格{p3_price:.2f})，当前已重新站上P3"
        )

    reasons.append("【观察池】最新收盘价已大于P3但尚未突破P2，纳入重点观察")

    ma100_val = ma100_factors.get("ma100", 0)
    breakout_days = int(ma100_factors.get("ma100_breakout_days", 0))
    reasons.append(
        f"【MA100站上确认】突破{breakout_days}天，"
        f"MA100={ma100_val:.2f}，距离{distance_pct:.1f}%"
    )

    reasons.append(f"【信号强度】观察评分{signal_strength:.2f}，等待突破P2触发成熟买点")
    return reasons


def _detect_one_yang_three_yin(bars: pd.DataFrame) -> str | None:
    """Detect the one-yang-three-yin pattern across last 5 bars."""
    if len(bars) < 5:
        return None

    last5 = bars.tail(5).reset_index(drop=True)
    day1 = last5.iloc[0]
    day5 = last5.iloc[4]

    d1_o, d1_c = float(day1["open"]), float(day1["close"])
    d5_o, d5_c = float(day5["open"]), float(day5["close"])

    if d1_c <= d1_o or (d1_c - d1_o) / max(d1_o, 0.01) < 0.02:
        return None

    for i in range(1, 4):
        bar = last5.iloc[i]
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])
        if bar_low < d1_o:
            return None
        if bar_close > d1_c:
            return None

    if d5_c <= d5_o or d5_c < d1_c:
        return None

    return "one_yang_three_yin"
