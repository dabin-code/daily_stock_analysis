# -*- coding: utf-8 -*-
"""Five-layer backtest agent tools — read-only tools for the new run-based system.

Tools:
- get_five_layer_backtest_run_summary: run-level overview
- get_five_layer_group_summary: dimensional group summaries
- get_five_layer_recommendations: graded recommendations
- get_five_layer_candidate_detail: single candidate evaluation detail
"""
from __future__ import annotations

import logging
from typing import Optional

from src.agent.tools.registry import ToolDefinition, ToolParameter

logger = logging.getLogger(__name__)

_service = None


def _get_service():
    """Lazy import + singleton to avoid circular deps."""
    global _service
    if _service is None:
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        _service = FiveLayerBacktestService()
    return _service


# ── get_five_layer_backtest_run_summary ────────────────────────────────────

def _handle_get_run_summary(backtest_run_id: str) -> dict:
    """Get overview of a five-layer backtest run."""
    try:
        svc = _get_service()
        run = svc.get_run(backtest_run_id)
        if run is None:
            return {"info": f"No run found with ID: {backtest_run_id}"}

        result = run.to_dict()

        # Attach overall summary if available
        summaries = svc.summary_repo.get_by_run(backtest_run_id, group_type="overall")
        if summaries:
            s = summaries[0]
            result["overall_summary"] = {
                "sample_count": s.sample_count,
                "avg_return_pct": s.avg_return_pct,
                "median_return_pct": s.median_return_pct,
                "win_rate_pct": s.win_rate_pct,
                "avg_mae": s.avg_mae,
                "avg_mfe": s.avg_mfe,
                "top_k_hit_rate": s.top_k_hit_rate,
                "leader_pool_win_share": s.leader_pool_win_share,
                "excess_return_pct": s.excess_return_pct,
                "ranking_consistency": s.ranking_consistency,
            }

        return result
    except Exception as exc:
        logger.warning("[five_layer_backtest_tools] get_run_summary error: %s", exc)
        return {"error": f"Failed to retrieve run summary: {exc}"}


get_five_layer_backtest_run_summary_tool = ToolDefinition(
    name="get_five_layer_backtest_run_summary",
    description=(
        "Get overview of a five-layer backtest run: status, sample counts, "
        "overall win rate, return metrics, ranking effectiveness. "
        "Read-only, does not trigger new backtests."
    ),
    parameters=[
        ToolParameter(
            name="backtest_run_id",
            type="string",
            description="The backtest run ID (e.g. 'flbt-abc123def456')",
        ),
    ],
    handler=_handle_get_run_summary,
    category="data",
)


# ── get_five_layer_group_summary ───────────────────────────────────────────

def _handle_get_group_summary(
    backtest_run_id: str,
    group_type: Optional[str] = None,
) -> dict:
    """Get dimensional group summaries for a backtest run."""
    try:
        svc = _get_service()
        run = svc.get_run(backtest_run_id)
        if run is None:
            return {"info": f"No run found with ID: {backtest_run_id}"}

        summaries = svc.summary_repo.get_by_run(backtest_run_id, group_type=group_type)
        if not summaries:
            return {"info": f"No summaries found for run {backtest_run_id}"}

        items = []
        for s in summaries:
            items.append({
                "group_type": s.group_type,
                "group_key": s.group_key,
                "sample_count": s.sample_count,
                "avg_return_pct": s.avg_return_pct,
                "median_return_pct": s.median_return_pct,
                "win_rate_pct": s.win_rate_pct,
                "avg_mae": s.avg_mae,
                "avg_mfe": s.avg_mfe,
                "top_k_hit_rate": s.top_k_hit_rate,
                "leader_pool_win_share": s.leader_pool_win_share,
                "excess_return_pct": s.excess_return_pct,
                "ranking_consistency": s.ranking_consistency,
                "p25_return_pct": s.p25_return_pct,
                "p75_return_pct": s.p75_return_pct,
                "time_bucket_stability": s.time_bucket_stability,
            })

        return {"backtest_run_id": backtest_run_id, "summaries": items}
    except Exception as exc:
        logger.warning("[five_layer_backtest_tools] get_group_summary error: %s", exc)
        return {"error": f"Failed to retrieve group summaries: {exc}"}


get_five_layer_group_summary_tool = ToolDefinition(
    name="get_five_layer_group_summary",
    description=(
        "Get dimensional group summaries for a backtest run. "
        "Can filter by group_type (overall, signal_family, setup_type, "
        "market_regime, theme_position, candidate_pool_level, entry_maturity, combo). "
        "Read-only."
    ),
    parameters=[
        ToolParameter(
            name="backtest_run_id",
            type="string",
            description="The backtest run ID",
        ),
        ToolParameter(
            name="group_type",
            type="string",
            description="Filter by group type (e.g. 'signal_family', 'setup_type', 'combo')",
            required=False,
        ),
    ],
    handler=_handle_get_group_summary,
    category="data",
)


# ── get_five_layer_recommendations ─────────────────────────────────────────

def _handle_get_recommendations(
    backtest_run_id: str,
    recommendation_level: Optional[str] = None,
) -> dict:
    """Get graded recommendations for a backtest run."""
    try:
        svc = _get_service()
        run = svc.get_run(backtest_run_id)
        if run is None:
            return {"info": f"No run found with ID: {backtest_run_id}"}

        recs = svc.recommendation_repo.get_by_run(
            backtest_run_id, recommendation_level=recommendation_level,
        )
        if not recs:
            return {"info": f"No recommendations found for run {backtest_run_id}"}

        items = []
        for r in recs:
            items.append({
                "recommendation_type": r.recommendation_type,
                "target_scope": r.target_scope,
                "target_key": r.target_key,
                "recommendation_level": r.recommendation_level,
                "suggested_change": r.suggested_change,
                "sample_count": r.sample_count,
                "confidence": r.confidence,
                "validation_status": r.validation_status,
            })

        return {"backtest_run_id": backtest_run_id, "recommendations": items}
    except Exception as exc:
        logger.warning("[five_layer_backtest_tools] get_recommendations error: %s", exc)
        return {"error": f"Failed to retrieve recommendations: {exc}"}


get_five_layer_recommendations_tool = ToolDefinition(
    name="get_five_layer_recommendations",
    description=(
        "Get graded recommendations from a backtest run: observation, hypothesis, "
        "or actionable suggestions with evidence chains. "
        "Recommendations NEVER modify production rules — they are suggestions only. "
        "Read-only."
    ),
    parameters=[
        ToolParameter(
            name="backtest_run_id",
            type="string",
            description="The backtest run ID",
        ),
        ToolParameter(
            name="recommendation_level",
            type="string",
            description="Filter by level: observation / hypothesis / actionable",
            required=False,
            enum=["observation", "hypothesis", "actionable"],
        ),
    ],
    handler=_handle_get_recommendations,
    category="data",
)


# ── get_five_layer_candidate_detail ────────────────────────────────────────

def _handle_get_candidate_detail(
    backtest_run_id: str,
    code: str,
) -> dict:
    """Get evaluation detail for a specific stock in a backtest run."""
    try:
        svc = _get_service()
        run = svc.get_run(backtest_run_id)
        if run is None:
            return {"info": f"No run found with ID: {backtest_run_id}"}

        evaluations = svc.eval_repo.get_by_run(backtest_run_id)
        matches = [e for e in evaluations if e.code == code]
        if not matches:
            return {"info": f"No evaluation found for {code} in run {backtest_run_id}"}

        items = [e.to_dict() for e in matches]
        return {"backtest_run_id": backtest_run_id, "code": code, "evaluations": items}
    except Exception as exc:
        logger.warning("[five_layer_backtest_tools] get_candidate_detail error: %s", exc)
        return {"error": f"Failed to retrieve candidate detail: {exc}"}


get_five_layer_candidate_detail_tool = ToolDefinition(
    name="get_five_layer_candidate_detail",
    description=(
        "Get detailed backtest evaluation for a specific stock in a run. "
        "Shows snapshot fields, execution result, forward returns, MAE/MFE, "
        "signal quality score, plan success. Read-only."
    ),
    parameters=[
        ToolParameter(
            name="backtest_run_id",
            type="string",
            description="The backtest run ID",
        ),
        ToolParameter(
            name="code",
            type="string",
            description="Stock code (e.g. '600519')",
        ),
    ],
    handler=_handle_get_candidate_detail,
    category="data",
)


# ── Exported tool list ─────────────────────────────────────────────────────

ALL_FIVE_LAYER_BACKTEST_TOOLS = [
    get_five_layer_backtest_run_summary_tool,
    get_five_layer_group_summary_tool,
    get_five_layer_recommendations_tool,
    get_five_layer_candidate_detail_tool,
]
