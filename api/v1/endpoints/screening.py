from __future__ import annotations

from datetime import date, datetime, timezone
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from typing import List, Optional

from api.v1.schemas.screening import (
    ScreeningCandidateDetailResponse,
    ScreeningBackfillToDateRequest,
    ScreeningBackfillToDateResponse,
    CreateScreeningRunRequest,
    ScreeningNotifyRequest,
    ScreeningCandidateListResponse,
    ScreeningRunListResponse,
    ScreeningRunResponse,
    ScreeningStrategyListResponse,
)
from api.v1.schemas.common import SuccessResponse
from src.services.screening_notification_service import (
    ScreeningNotificationService,
    ScreeningRunNotFoundError,
    ScreeningRunNotReadyError,
)
from src.services.screening_task_service import ScreeningTaskService, ScreeningTradeDateNotReadyError
from src.services.fast_backfill_service import FastBackfillService

router = APIRouter()


# OpenClaw 接口模型
class OpenClawEvidence(BaseModel):
    title: str
    source: str
    url: Optional[str] = None
    published_at: Optional[str] = None


class OpenClawTheme(BaseModel):
    name: str
    heat_score: float = Field(..., ge=0, le=100)
    confidence: float = Field(..., ge=0, le=1)
    catalyst_summary: str
    keywords: List[str]
    evidence: List[OpenClawEvidence] = Field(default_factory=list)


class OpenClawScreeningOptions(BaseModel):
    candidate_limit: int = Field(50, ge=1, le=200)
    ai_top_k: int = Field(10, ge=0, le=50)
    force_refresh: bool = False


class OpenClawThemeRunRequest(BaseModel):
    trade_date: str
    market: str = "cn"
    themes: List[OpenClawTheme]
    options: Optional[OpenClawScreeningOptions] = None


class OpenClawThemeRunResponse(BaseModel):
    run_id: str
    status: str
    strategy_names: List[str]
    accepted_theme_count: int
    created_at: str


def get_screening_strategies() -> list[dict]:
    """Load available strategies from SkillManager."""
    from src.agent.skills.base import SkillManager
    mgr = SkillManager()
    mgr.load_builtin_strategies()
    return [
        {
            "name": s.name,
            "display_name": s.display_name,
            "description": s.description,
            "category": s.category,
            "has_screening_rules": s.screening is not None,
            "system_role": s.system_role,
            "strategy_family": s.strategy_family,
            "applicable_market": list(s.applicable_market or []),
            "applicable_theme": list(s.applicable_theme or []),
            "setup_type": s.setup_type,
        }
        for s in mgr.list_skills()
    ]


@router.get("/strategies", response_model=ScreeningStrategyListResponse, summary="获取可用筛选策略列表")
def list_screening_strategies() -> ScreeningStrategyListResponse:
    strategies = get_screening_strategies()
    return ScreeningStrategyListResponse(strategies=strategies)


@router.post(
    "/backfill-to-date",
    response_model=ScreeningBackfillToDateResponse,
    summary="快速回填股票数据到目标交易日",
)
def backfill_screening_data_to_date(
    request: ScreeningBackfillToDateRequest,
) -> ScreeningBackfillToDateResponse:
    service = FastBackfillService()
    try:
        result = service.backfill_to_trade_date(
            target_trade_date=request.trade_date,
            market=request.market,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": str(exc),
            },
        ) from exc
    return ScreeningBackfillToDateResponse(**result)


@router.post("/runs", response_model=ScreeningRunResponse, summary="执行一次全市场筛选")
def create_screening_run(request: CreateScreeningRunRequest) -> ScreeningRunResponse:
    from src.agent.skills.base import SkillManager
    skill_manager = SkillManager()
    skill_manager.load_builtin_strategies()
    service = ScreeningTaskService(skill_manager=skill_manager)
    normalized_stock_codes = [
        str(code).strip()
        for code in (request.stock_codes or [])
        if str(code).strip()
    ]
    if request.stock_codes == [] or (request.stock_codes is not None and not normalized_stock_codes):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": "stock_codes cannot be empty",
            },
        )
    runtime_config = service.resolve_run_config(
        mode=request.mode,
        candidate_limit=request.candidate_limit,
        ai_top_k=request.ai_top_k,
    )
    if runtime_config.ai_top_k > runtime_config.candidate_limit:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": "ai_top_k cannot be greater than candidate_limit",
            },
        )
    try:
        result = service.execute_run(
            trade_date=request.trade_date,
            stock_codes=request.stock_codes,
            mode=request.mode,
            candidate_limit=request.candidate_limit,
            ai_top_k=request.ai_top_k,
            market=request.market,
            rerun_failed=request.rerun_failed,
            resume_from=request.resume_from,
            strategy_names=request.strategies,
        )
    except ScreeningTradeDateNotReadyError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": exc.error_code,
                "message": str(exc),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": str(exc),
            },
        ) from exc
    return ScreeningRunResponse(**result)


@router.get("/runs", response_model=ScreeningRunListResponse, summary="查询筛选任务列表")
def list_screening_runs(limit: int = Query(20, ge=1, le=100)) -> ScreeningRunListResponse:
    service = ScreeningTaskService()
    items = [ScreeningRunResponse(**item) for item in service.list_runs(limit=limit)]
    return ScreeningRunListResponse(total=len(items), items=items)


@router.delete("/runs", response_model=SuccessResponse, summary="清除所有筛选历史记录")
def clear_screening_runs() -> SuccessResponse:
    service = ScreeningTaskService()
    count = service.clear_runs()
    return SuccessResponse(success=True, message=f"已清除 {count} 条筛选记录")


@router.delete("/runs/{run_id}", response_model=SuccessResponse, summary="删除单条筛选任务")
def delete_screening_run(run_id: str) -> SuccessResponse:
    service = ScreeningTaskService()
    deleted = service.delete_run(run_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "筛选任务不存在"})
    return SuccessResponse(success=True, message=f"已删除筛选任务 {run_id}")


@router.get("/runs/{run_id}", response_model=ScreeningRunResponse, summary="查询单次筛选任务")
def get_screening_run(run_id: str) -> ScreeningRunResponse:
    service = ScreeningTaskService()
    result = service.get_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "筛选任务不存在"})
    return ScreeningRunResponse(**result)


@router.get(
    "/runs/{run_id}/candidates",
    response_model=ScreeningCandidateListResponse,
    summary="查询筛选候选结果",
)
def list_screening_candidates(
    run_id: str,
    limit: int = Query(100, ge=1, le=500),
    with_ai_only: bool = Query(False),
) -> ScreeningCandidateListResponse:
    service = ScreeningTaskService()
    run = service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "筛选任务不存在"})
    if run.get("status") not in {"completed", "completed_with_ai_degraded"}:
        raise HTTPException(
            status_code=409,
            detail={"error": "run_not_ready", "message": "筛选任务尚未完成，候选结果不可用", "run_id": run_id},
        )
    items = service.list_candidates(run_id=run_id, limit=limit, with_ai_only=with_ai_only)
    return ScreeningCandidateListResponse(total=len(items), items=items)


@router.get(
    "/runs/{run_id}/candidates/{code}",
    response_model=ScreeningCandidateDetailResponse,
    summary="查询单只筛选候选详情",
)
def get_screening_candidate_detail(run_id: str, code: str) -> ScreeningCandidateDetailResponse:
    service = ScreeningTaskService()
    run = service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "筛选任务不存在"})
    if run.get("status") not in {"completed", "completed_with_ai_degraded"}:
        raise HTTPException(
            status_code=409,
            detail={"error": "run_not_ready", "message": "筛选任务尚未完成，候选详情不可用", "run_id": run_id},
        )

    result = service.get_candidate_detail(run_id=run_id, code=code)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "候选不存在", "run_id": run_id, "code": code},
        )
    return ScreeningCandidateDetailResponse(**result)


@router.post("/runs/{run_id}/notify", response_model=SuccessResponse, summary="推送筛选推荐名单（幂等，支持 force 补发）")
def notify_screening_run(run_id: str, request: ScreeningNotifyRequest) -> SuccessResponse:
    service = ScreeningNotificationService()
    try:
        result = service.notify_run(
            run_id=run_id,
            force=request.force,
        )
    except ScreeningRunNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": str(exc), "run_id": run_id},
        ) from exc
    except ScreeningRunNotReadyError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "conflict", "message": str(exc), "run_id": run_id},
        ) from exc

    success = bool(result.get("success")) and not result.get("skipped")
    message = result.get("reason") or result.get("notification_status") or "ok"
    return SuccessResponse(success=success, message=message, data=result)


@router.post(
    "/openclaw-theme-run",
    response_model=OpenClawThemeRunResponse,
    summary="OpenClaw 热点题材筛选接口"
)
def openclaw_theme_run(request: OpenClawThemeRunRequest) -> OpenClawThemeRunResponse:
    """
    OpenClaw 专用接口：触发热点题材筛选运行。

    - 验证请求参数
    - 创建筛选任务
    - 返回运行 ID 和状态
    """
    # 验证市场
    if request.market != "cn":
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_market",
                "message": "market must be 'cn' in phase 1"
            }
        )

    # 验证题材
    if not request.themes:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_themes",
                "message": "themes cannot be empty"
            }
        )

    # 验证日期格式
    try:
        from datetime import datetime as dt
        parsed_trade_date = dt.strptime(request.trade_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_date_format",
                "message": "trade_date must be in YYYY-MM-DD format"
            }
        )

    # 创建筛选任务
    from src.agent.skills.base import SkillManager
    from src.services.theme_context_ingest_service import ExternalTheme, OpenClawThemeContext

    themes = [
        ExternalTheme(
            name=t.name,
            heat_score=t.heat_score,
            confidence=t.confidence,
            catalyst_summary=t.catalyst_summary,
            keywords=t.keywords,
            evidence=[{"title": e.title, "source": e.source, "url": e.url, "published_at": e.published_at} for e in t.evidence]
        )
        for t in request.themes
    ]

    theme_context = OpenClawThemeContext(
        source="openclaw",
        trade_date=request.trade_date,
        market=request.market,
        themes=themes,
        accepted_at=datetime.now(timezone.utc).isoformat()
    )

    try:
        skill_manager = SkillManager()
        skill_manager.load_builtin_strategies()
        service = ScreeningTaskService(skill_manager=skill_manager)
        options = request.options or OpenClawScreeningOptions()
        if options.ai_top_k > options.candidate_limit:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "validation_error",
                    "message": "ai_top_k cannot be greater than candidate_limit",
                },
            )

        result = service.execute_run(
            trade_date=parsed_trade_date,
            stock_codes=None,
            mode="balanced",
            candidate_limit=options.candidate_limit,
            ai_top_k=options.ai_top_k,
            market=request.market,
            trigger_type="openclaw",
            strategy_names=["extreme_strength_combo"],
            theme_context=theme_context,
        )

        return OpenClawThemeRunResponse(
            run_id=result["run_id"],
            status=result["status"],
            strategy_names=["extreme_strength_combo"],
            accepted_theme_count=len(request.themes),
            created_at=datetime.now(timezone.utc).isoformat()
        )
    except ScreeningTradeDateNotReadyError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": exc.error_code,
                "message": str(exc),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": str(exc),
            },
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Internal server error"
            }
        ) from exc
