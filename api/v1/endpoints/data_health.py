from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from api.v1.schemas.data_health import (
    DataHealthCoverageResponse,
    DataHealthGapListResponse,
    DataHealthOperationRequest,
    DataHealthSummaryResponse,
    DataHealthTaskListResponse,
    DataHealthTaskResponse,
)
from src.services.data_health_service import DataHealthService
from src.services.data_health_task_service import get_data_health_task_service

router = APIRouter()


@router.get("/summary", response_model=DataHealthSummaryResponse, summary="查询本地股票数据健康摘要")
def get_data_health_summary(market: str = Query("cn")) -> DataHealthSummaryResponse:
    result = DataHealthService().get_summary(market=market)
    return DataHealthSummaryResponse(**result)


@router.get("/coverage", response_model=DataHealthCoverageResponse, summary="查询本地股票数据覆盖率")
def get_data_health_coverage(
    market: str = Query("cn"),
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
) -> DataHealthCoverageResponse:
    if from_date is not None and to_date is not None and (to_date - from_date).days > 370:
        raise HTTPException(
            status_code=422,
            detail={"error": "validation_error", "message": "coverage date range cannot exceed 370 days"},
        )
    result = DataHealthService().get_coverage(
        market=market,
        start=from_date,
        end=to_date,
    )
    return DataHealthCoverageResponse(**result)


@router.get("/gaps", response_model=DataHealthGapListResponse, summary="查询本地股票数据缺口")
def list_data_health_gaps(
    market: str = Query("cn"),
    status: Optional[str] = Query(None),
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    limit: int = Query(100, ge=1, le=500),
) -> DataHealthGapListResponse:
    result = DataHealthService().list_gaps(
        market=market,
        status=status,
        start=from_date,
        end=to_date,
        limit=limit,
    )
    return DataHealthGapListResponse(**result)


@router.post("/operations", response_model=DataHealthTaskResponse, summary="提交数据健康后台操作")
def submit_data_health_operation(request: DataHealthOperationRequest) -> DataHealthTaskResponse:
    try:
        task = get_data_health_task_service().submit_operation(
            operation_type=request.operation_type,
            market=request.market,
            trade_date=request.trade_date,
            stock_codes=request.stock_codes,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "validation_error", "message": str(exc)},
        ) from exc
    return DataHealthTaskResponse(**task)


@router.get("/tasks", response_model=DataHealthTaskListResponse, summary="查询数据健康后台任务列表")
def list_data_health_tasks(limit: int = Query(20, ge=1, le=100)) -> DataHealthTaskListResponse:
    items = [
        DataHealthTaskResponse(**item)
        for item in get_data_health_task_service().list_tasks(limit=limit)
    ]
    return DataHealthTaskListResponse(total=len(items), items=items)


@router.get("/tasks/{task_id}", response_model=DataHealthTaskResponse, summary="查询数据健康后台任务")
def get_data_health_task(task_id: str) -> DataHealthTaskResponse:
    task = get_data_health_task_service().get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "数据健康任务不存在"},
        )
    return DataHealthTaskResponse(**task)
