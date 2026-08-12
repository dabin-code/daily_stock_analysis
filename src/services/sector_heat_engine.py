# -*- coding: utf-8 -*-
"""L2 本地板块热度引擎。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from src.services._debug_session_logger import write_debug_log
from src.services.board_strength_ranker import BoardStrengthRanker

logger = logging.getLogger(__name__)

MIN_SECTOR_STOCK_COUNT = 5
FRONT_TOP_N = 10
HOT_MIN_BREADTH = 0.30
HOT_MIN_PERSISTENCE = 20.0
HOT_REQUIRE_LEADER = True
EXPAND_WARM_PROMOTION_SCORE = 50.0
EXPAND_WARM_MIN_BREADTH = 0.40
EXPAND_WARM_MIN_STRENGTH = 0.60


@dataclass
class SectorHeatResult:
    board_name: str
    board_type: str = "concept"
    breadth_score: float = 0.0
    strength_score: float = 0.0
    persistence_score: float = 0.0
    leadership_score: float = 0.0
    sector_hot_score: float = 0.0
    sector_status: str = "neutral"
    sector_stage: str = "fade"
    stock_count: int = 0
    up_count: int = 0
    limit_up_count: int = 0
    avg_pct_chg: float = 0.0
    median_pct: float = 0.0
    leader_codes: List[str] = field(default_factory=list)
    front_codes: List[str] = field(default_factory=list)
    reason: str = ""
    board_strength_score: float = 0.0
    board_strength_rank: int = 0
    board_strength_percentile: float = 0.0
    leader_candidate_count: int = 0
    quality_flags: Dict[str, Any] = field(default_factory=dict)


class SectorHeatEngine:
    """Rank-driven L2 board heat engine."""

    def __init__(self, db_manager) -> None:
        self._db = db_manager
        self._ranker = BoardStrengthRanker()

    def compute_all_sectors(
        self,
        snapshot_df: pd.DataFrame,
        trade_date: date,
    ) -> List[SectorHeatResult]:
        if snapshot_df is None or snapshot_df.empty or "code" not in snapshot_df.columns:
            write_debug_log(
                location="src/services/sector_heat_engine.py:compute_all_sectors",
                message="L2 sector heat skipped due to empty snapshot",
                hypothesis_id="H1",
                run_id=trade_date.isoformat(),
                data={
                    "snapshot_empty": bool(snapshot_df is None or getattr(snapshot_df, "empty", True)),
                    "has_code_column": bool(snapshot_df is not None and "code" in snapshot_df.columns),
                },
            )
            logger.warning(
                "L2 板块热度快照不可用（snapshot_empty=%s，has_code_column=%s），本日 L2 整层降级为空结果",
                bool(snapshot_df is None or getattr(snapshot_df, "empty", True)),
                bool(snapshot_df is not None and "code" in snapshot_df.columns),
            )
            return []

        boards = self._db.list_active_boards_with_member_count(
            market="cn",
            min_member_count=MIN_SECTOR_STOCK_COUNT,
        )
        if not boards:
            write_debug_log(
                location="src/services/sector_heat_engine.py:compute_all_sectors",
                message="L2 sector heat found no active boards",
                hypothesis_id="H1",
                run_id=trade_date.isoformat(),
                data={
                    "min_sector_stock_count": MIN_SECTOR_STOCK_COUNT,
                    "snapshot_rows": int(len(snapshot_df)),
                },
            )
            logger.warning(
                "L2 板块热度无可用板块（min_member_count=%s，快照 %s 行），本日 L2 整层降级为空结果",
                MIN_SECTOR_STOCK_COUNT,
                len(snapshot_df),
            )
            return []

        snapshot_index = snapshot_df.set_index("code") if "code" in snapshot_df.columns else snapshot_df
        board_names = [item["board_name"] for item in boards]
        board_type_map = {item["board_name"]: item["board_type"] for item in boards}
        member_map = self._db.batch_get_board_member_codes(board_names, market="cn")

        raw_results: List[SectorHeatResult] = []
        raw_metrics: List[Dict[str, Any]] = []
        debug_board_summaries: List[Dict[str, Any]] = []
        boards_below_snapshot_threshold = 0

        for board_name in board_names:
            member_codes = member_map.get(board_name, [])
            sector_df = snapshot_index.loc[snapshot_index.index.isin(member_codes)].copy()
            if len(sector_df) < MIN_SECTOR_STOCK_COUNT:
                boards_below_snapshot_threshold += 1
                continue

            result, metrics = self._compute_single_sector(
                board_name=board_name,
                board_type=board_type_map.get(board_name, "concept"),
                sector_df=sector_df,
                trade_date=trade_date,
            )
            raw_results.append(result)
            raw_metrics.append(metrics)
            debug_board_summaries.append({
                "board_name": board_name,
                "matched_count": int(len(sector_df)),
                "avg_pct_chg": round(float(result.avg_pct_chg), 2),
                "median_pct": round(float(result.median_pct), 2),
                "breadth": round(float(result.breadth_score), 4),
                "strength": round(float(result.strength_score), 4),
                "persistence": round(float(result.persistence_score), 4),
                "leadership": round(float(result.leadership_score), 4),
                "leader_count": int(result.leader_candidate_count),
                "limit_up_count": int(result.limit_up_count),
            })

        ranked_metrics = {
            str(item["board_name"]): item
            for item in self._ranker.rank(raw_metrics)
        }

        results: List[SectorHeatResult] = []
        for result in raw_results:
            ranked = ranked_metrics.get(result.board_name, {})
            result.board_strength_score = float(ranked.get("board_strength_score", 0.0) or 0.0)
            result.board_strength_rank = int(ranked.get("board_strength_rank", 0) or 0)
            result.board_strength_percentile = float(ranked.get("board_strength_percentile", 0.0) or 0.0)
            result.sector_hot_score = result.board_strength_score
            result.sector_status = str(ranked.get("status_bucket") or "cold")
            result.sector_stage = self._classify_stage(
                board_name=result.board_name,
                trade_date=trade_date,
                current_result=result,
            )
            result.reason = self._build_reason(result)
            results.append(result)

        results.sort(
            key=lambda item: (
                item.board_strength_rank if item.board_strength_rank > 0 else 9999,
                -item.board_strength_score,
                item.board_name,
            )
        )

        raw_status_counts: Dict[str, int] = {}
        for item in results:
            raw_status_counts[item.sector_status] = raw_status_counts.get(item.sector_status, 0) + 1

        write_debug_log(
            location="src/services/sector_heat_engine.py:compute_all_sectors",
            message="L2 sector heat computed board summaries",
            hypothesis_id="H1,H2,H3",
            run_id=trade_date.isoformat(),
            data={
                "snapshot_rows": int(len(snapshot_df)),
                "active_board_count": int(len(board_names)),
                "boards_below_snapshot_threshold": int(boards_below_snapshot_threshold),
                "computed_sector_count": int(len(results)),
                "raw_status_counts": raw_status_counts,
                "top_ranked_candidates": [
                    {
                        "board_name": item.board_name,
                        "board_strength_score": round(float(item.board_strength_score), 2),
                        "board_strength_rank": int(item.board_strength_rank),
                        "sector_status": item.sector_status,
                        "sector_stage": item.sector_stage,
                        "leader_candidate_count": int(item.leader_candidate_count),
                    }
                    for item in results[:8]
                ],
                "board_debug_preview": debug_board_summaries[:8],
            },
        )

        self._persist_results(results, trade_date)
        return results

    def _compute_single_sector(
        self,
        board_name: str,
        board_type: str,
        sector_df: pd.DataFrame,
        trade_date: date,
    ) -> tuple[SectorHeatResult, Dict[str, Any]]:
        pct_chg = sector_df["pct_chg"].astype(float) if "pct_chg" in sector_df.columns else pd.Series(dtype=float)
        n = len(sector_df)

        up_count = int((pct_chg > 0).sum())
        limit_up_count = int(sector_df["is_limit_up"].sum()) if "is_limit_up" in sector_df.columns else 0
        avg_pct = float(pct_chg.mean()) if len(pct_chg) > 0 else 0.0
        median_pct = float(pct_chg.median()) if len(pct_chg) > 0 else 0.0
        top3_avg = float(pct_chg.nlargest(min(3, len(pct_chg))).mean()) if len(pct_chg) > 0 else 0.0

        breadth = self._calc_breadth(sector_df, pct_chg, n)
        strength = self._calc_strength(sector_df, pct_chg, n)
        leadership, leader_codes, front_codes, front_concentration = self._calc_leadership(sector_df, pct_chg)
        persistence, history = self._calc_persistence(board_name, trade_date)

        strong_stock_ratio_3pct = float((pct_chg > 3.0).sum() / n) if n else 0.0
        strong_stock_ratio_5pct = float((pct_chg > 5.0).sum() / n) if n else 0.0
        limit_up_ratio = float(limit_up_count / n) if n else 0.0
        up_ratio = float(up_count / n) if n else 0.0
        quality_flags = self._build_quality_flags(
            leader_codes=leader_codes,
            front_concentration=front_concentration,
            persistence_score=persistence,
            limit_up_count=limit_up_count,
            stock_count=n,
        )

        result = SectorHeatResult(
            board_name=board_name,
            board_type=board_type,
            breadth_score=breadth,
            strength_score=strength,
            persistence_score=persistence,
            leadership_score=leadership,
            sector_stage="fade",
            stock_count=n,
            up_count=up_count,
            limit_up_count=limit_up_count,
            avg_pct_chg=round(avg_pct, 2),
            median_pct=round(median_pct, 2),
            leader_codes=leader_codes,
            front_codes=front_codes,
            leader_candidate_count=len(leader_codes),
            quality_flags=quality_flags,
        )
        result.reason = self._build_reason(result)

        metrics = {
            "board_name": board_name,
            "avg_pct_chg": avg_pct,
            "median_pct": median_pct,
            "strong_stock_ratio_3pct": strong_stock_ratio_3pct,
            "strong_stock_ratio_5pct": strong_stock_ratio_5pct,
            "limit_up_count": limit_up_count,
            "limit_up_ratio": limit_up_ratio,
            "up_ratio": up_ratio,
            "top3_avg": top3_avg,
            "front_concentration": front_concentration,
            "breadth_score": breadth,
            "strength_score": strength,
            "persistence_score": persistence,
            "leadership_score": leadership,
            "history": history,
        }
        return result, metrics

    @staticmethod
    def _build_quality_flags(
        leader_codes: List[str],
        front_concentration: float,
        persistence_score: float,
        limit_up_count: int,
        stock_count: int,
    ) -> Dict[str, Any]:
        return {
            "has_leader_candidate": bool(leader_codes),
            "leader_candidate_count": len(leader_codes),
            "front_concentration_high": front_concentration >= 0.65,
            "persistence_ok": persistence_score >= 0.35,
            "limit_up_cluster": (limit_up_count >= 2) or (stock_count > 0 and (limit_up_count / stock_count) >= 0.08),
        }

    @staticmethod
    def _build_reason(result: SectorHeatResult) -> str:
        flags = result.quality_flags or {}
        quality_fragments = [
            f"leader_candidate_count={int(flags.get('leader_candidate_count', 0) or 0)}",
            f"front_concentration_high={bool(flags.get('front_concentration_high', False))}",
            f"persistence_ok={bool(flags.get('persistence_ok', False))}",
            f"limit_up_cluster={bool(flags.get('limit_up_cluster', False))}",
        ]
        return (
            f"strength_rank={result.board_strength_rank} "
            f"strength_score={result.board_strength_score:.1f} "
            f"percentile={result.board_strength_percentile:.2f} "
            f"avg_pct={result.avg_pct_chg:.2f} "
            f"stage={result.sector_stage} "
            f"{' '.join(quality_fragments)}"
        )

    @staticmethod
    def _apply_hard_filters(result: SectorHeatResult) -> SectorHeatResult:
        if result.sector_status == "hot":
            if result.breadth_score < HOT_MIN_BREADTH:
                result.sector_status = "warm"
                result.reason += " | downgraded:low_breadth"
            elif HOT_REQUIRE_LEADER and not result.leader_codes:
                result.sector_status = "warm"
                result.reason += " | downgraded:no_leader"
            elif result.persistence_score < (HOT_MIN_PERSISTENCE / 100.0):
                result.sector_status = "warm"
                result.reason += " | downgraded:low_persistence"
        return result

    @staticmethod
    def _apply_expand_warm_promotion(result: SectorHeatResult) -> SectorHeatResult:
        if result.sector_status != "neutral":
            return result
        if result.sector_stage != "expand":
            return result
        if result.sector_hot_score < EXPAND_WARM_PROMOTION_SCORE:
            return result
        if result.breadth_score < EXPAND_WARM_MIN_BREADTH:
            return result
        if result.strength_score < EXPAND_WARM_MIN_STRENGTH:
            return result
        result.sector_status = "warm"
        result.reason += " | promoted:expand_momentum"
        return result

    def _calc_breadth(self, df: pd.DataFrame, pct_chg: pd.Series, n: int) -> float:
        if n == 0:
            return 0.0
        up_ratio = (pct_chg > 0).sum() / n
        strong_3pct = (pct_chg > 3.0).sum() / n
        strong_5pct = (pct_chg > 5.0).sum() / n

        ma_aligned = 0.0
        if "close" in df.columns and "ma20" in df.columns and "ma60" in df.columns:
            close = df["close"].astype(float)
            ma20 = df["ma20"].astype(float)
            ma60 = df["ma60"].astype(float)
            ma_aligned = float(((close > ma20) & (close > ma60)).sum() / n)

        return float(0.35 * up_ratio + 0.25 * strong_3pct + 0.20 * strong_5pct + 0.20 * ma_aligned)

    def _calc_strength(self, df: pd.DataFrame, pct_chg: pd.Series, n: int) -> float:
        if n == 0 or len(pct_chg) == 0:
            return 0.0
        avg_pct = float(pct_chg.mean())
        median_pct = float(pct_chg.median())
        top3_avg = float(pct_chg.nlargest(min(3, len(pct_chg))).mean())
        limit_up_count = int(df["is_limit_up"].sum()) if "is_limit_up" in df.columns else 0
        limit_score = min(limit_up_count / 3.0, 1.0)
        excess_return = avg_pct

        return float(
            0.30 * _normalize(avg_pct, -2.0, 6.0)
            + 0.20 * _normalize(median_pct, -2.0, 5.0)
            + 0.20 * _normalize(top3_avg, 0.0, 10.0)
            + 0.15 * limit_score
            + 0.15 * _normalize(excess_return, -2.0, 4.0)
        )

    def _calc_leadership(
        self,
        df: pd.DataFrame,
        pct_chg: pd.Series,
    ) -> tuple[float, List[str], List[str], float]:
        leader_codes: List[str] = []
        front_codes: List[str] = []

        if "is_limit_up" in df.columns:
            leaders = df[df["is_limit_up"].fillna(False).astype(bool)].copy()
            if not leaders.empty:
                if "circ_mv" in leaders.columns:
                    leaders["_circ_mv_missing"] = leaders["circ_mv"].isna()
                    leaders["_circ_mv_value"] = leaders["circ_mv"].fillna(float("inf")).astype(float)
                    leaders = leaders.sort_values(
                        by=["_circ_mv_missing", "_circ_mv_value"],
                        ascending=[True, True],
                    )
                leader_codes = [str(code) for code in leaders.index.tolist()]

        if len(pct_chg) > 0:
            top_n = min(FRONT_TOP_N, len(pct_chg))
            front_idx = pct_chg.nlargest(top_n).index
            front_codes = [str(code) for code in front_idx if pct_chg.loc[code] > 0]

        leader_count = len(leader_codes)
        front_count = len(front_codes)
        front_concentration = 0.0
        if front_count >= 3:
            front_pcts = pct_chg.loc[[code for code in front_codes if code in pct_chg.index]]
            positive_sum = float(front_pcts.clip(lower=0).sum())
            total_positive = float(pct_chg.clip(lower=0).sum())
            if total_positive > 0:
                front_concentration = positive_sum / total_positive

        score = float(
            0.40 * min(leader_count / 2.0, 1.0)
            + 0.35 * front_concentration
            + 0.25 * min(front_count / 5.0, 1.0)
        )
        return score, leader_codes, front_codes, front_concentration

    def _calc_persistence(self, board_name: str, trade_date: date) -> tuple[float, List[Dict[str, Any]]]:
        history = self._db.list_sector_heat_history(
            board_name=board_name,
            end_date=trade_date - timedelta(days=1),
            lookback_days=5,
        )
        if not history:
            return 0.0, history

        scores = [
            float(item.get("board_strength_score", item.get("sector_hot_score", 0.0)) or 0.0)
            for item in history
        ]
        if len(scores) < 3:
            return float(_normalize(float(np.mean(scores)), 20.0, 80.0) * 0.5), history
        if len(scores) < 5:
            return float(_normalize(float(np.mean(scores)), 20.0, 80.0)), history

        hot_3d = float(np.mean(scores[-3:]))
        hot_5d = float(np.mean(scores[-5:]))
        slope = (scores[-1] - scores[0]) / max(len(scores) - 1, 1)
        return float(
            0.40 * _normalize(hot_3d, 20.0, 80.0)
            + 0.30 * _normalize(hot_5d, 20.0, 80.0)
            + 0.30 * _sigmoid(slope)
        ), history

    def _classify_stage(
        self,
        board_name: str,
        trade_date: date,
        current_result: SectorHeatResult,
    ) -> str:
        history = self._db.list_sector_heat_history(
            board_name=board_name,
            end_date=trade_date - timedelta(days=1),
            lookback_days=5,
        )
        current_score = current_result.board_strength_score
        current_status = current_result.sector_status
        if current_status not in {"hot", "warm"}:
            return "fade"

        if not history:
            return "launch"

        previous_scores = [
            float(item.get("board_strength_score", item.get("sector_hot_score", 0.0)) or 0.0)
            for item in history
        ]
        previous_statuses = {str(item.get("sector_status") or "") for item in history}
        previous_avg = float(np.mean(previous_scores)) if previous_scores else 0.0

        if "hot" not in previous_statuses and "warm" not in previous_statuses:
            return "launch"
        if current_result.quality_flags.get("limit_up_cluster") and current_result.persistence_score >= 0.45:
            return "climax"
        if current_score >= previous_avg * 1.03:
            return "expand"
        if current_score < previous_avg * 0.9:
            return "fade"
        return "expand"

    def _persist_results(self, results: List[SectorHeatResult], trade_date: date) -> None:
        if not results:
            return
        records = []
        for item in results:
            records.append({
                "trade_date": trade_date,
                "board_name": item.board_name,
                "board_type": item.board_type,
                "breadth_score": item.breadth_score,
                "strength_score": item.strength_score,
                "persistence_score": item.persistence_score,
                "leadership_score": item.leadership_score,
                "sector_hot_score": item.sector_hot_score,
                "sector_status": item.sector_status,
                "sector_stage": item.sector_stage,
                "stock_count": item.stock_count,
                "up_count": item.up_count,
                "limit_up_count": item.limit_up_count,
                "avg_pct_chg": item.avg_pct_chg,
                "leader_codes_json": json.dumps(item.leader_codes, ensure_ascii=False),
                "front_codes_json": json.dumps(item.front_codes, ensure_ascii=False),
                "reason": item.reason,
                "board_strength_score": item.board_strength_score,
                "board_strength_rank": item.board_strength_rank,
                "board_strength_percentile": item.board_strength_percentile,
                "leader_candidate_count": item.leader_candidate_count,
                "quality_flags_json": json.dumps(item.quality_flags, ensure_ascii=False),
            })
        self._db.save_sector_heat_batch(trade_date, records)


def _normalize(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


def _sigmoid(x: float, k: float = 0.1) -> float:
    return float(1.0 / (1.0 + np.exp(-k * x)))
