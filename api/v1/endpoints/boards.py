# -*- coding: utf-8 -*-
"""
===================================
板块数据接口
===================================

职责：
1. GET /api/v1/boards 查询板块列表
2. GET /api/v1/boards/{board_name}/constituents 查询板块成分股
"""

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_database_manager
from api.v1.schemas.boards import (
    BatchBoardConstituentsRequest,
    BatchBoardConstituentsResponse,
    BoardConstituent,
    BoardConstituentGroup,
    BoardConstituentsResponse,
    BoardItem,
    BoardListResponse,
)
from api.v1.schemas.common import ErrorResponse
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

router = APIRouter()


def _build_name_map(
    db: DatabaseManager,
    codes: List[str],
    market: str,
) -> Dict[str, str]:
    """根据代码批量查询股票名称，返回 {规范化代码: 名称}。无记录的代码不会出现在结果中。"""
    if not codes:
        return {}
    instruments = db.list_instruments(codes=codes, market=market)
    return {
        str(item["code"]).strip().upper(): item.get("name")
        for item in instruments
        if item.get("code") and item.get("name")
    }


def _to_constituents(codes: List[str], name_map: Dict[str, str]) -> List[BoardConstituent]:
    """将代码列表组装为成分股明细（带名称）。"""
    return [
        BoardConstituent(code=code, name=name_map.get(str(code).strip().upper()))
        for code in codes
    ]


@router.get(
    "",
    response_model=BoardListResponse,
    responses={
        200: {"description": "板块列表"},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="查询板块列表",
    description="列出指定市场下的活跃板块及其成员股票数量，可按板块类型和最小成员数过滤。",
)
def list_boards(
    market: str = Query("cn", description="市场（cn/hk/us）"),
    board_type: Optional[str] = Query(None, description="板块类型过滤（如 industry/concept）"),
    min_member_count: int = Query(0, ge=0, description="最小成员股票数量"),
    db: DatabaseManager = Depends(get_database_manager),
) -> BoardListResponse:
    """查询板块列表（活跃板块 + 成员数）。"""
    try:
        rows = db.list_active_boards_with_member_count(
            market=market,
            board_type=board_type,
            min_member_count=min_member_count,
        )
        items = [BoardItem(**row) for row in rows]
        return BoardListResponse(
            market=market,
            board_type=board_type,
            total=len(items),
            items=items,
        )
    except Exception as e:
        logger.error(f"查询板块列表失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"查询板块列表失败: {str(e)}"},
        )


@router.get(
    "/{board_name}/constituents",
    response_model=BoardConstituentsResponse,
    responses={
        200: {"description": "板块成分股"},
        404: {"description": "板块不存在或无成分股", "model": ErrorResponse},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="查询板块成分股",
    description="根据板块名称查询该板块下的成分股代码列表。",
)
def get_board_constituents(
    board_name: str,
    market: str = Query("cn", description="市场（cn/hk/us）"),
    db: DatabaseManager = Depends(get_database_manager),
) -> BoardConstituentsResponse:
    """查询指定板块的成分股代码（板块→股票反向查询）。"""
    try:
        mapping = db.batch_get_board_member_codes(board_names=[board_name], market=market)
        codes = mapping.get(board_name, [])
        if not codes:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "not_found",
                    "message": f"未找到板块 {board_name} 的成分股",
                },
            )
        name_map = _build_name_map(db, codes, market)
        return BoardConstituentsResponse(
            market=market,
            board_name=board_name,
            total=len(codes),
            codes=codes,
            items=_to_constituents(codes, name_map),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"查询板块成分股失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"查询板块成分股失败: {str(e)}"},
        )


@router.post(
    "/constituents:batch",
    response_model=BatchBoardConstituentsResponse,
    responses={
        200: {"description": "批量板块成分股"},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="批量查询板块成分股",
    description="根据多个板块名称一次性查询各板块的成分股代码与名称。",
)
def batch_get_board_constituents(
    request: BatchBoardConstituentsRequest,
    db: DatabaseManager = Depends(get_database_manager),
) -> BatchBoardConstituentsResponse:
    """批量查询多个板块的成分股（板块→股票反向查询）。"""
    try:
        mapping = db.batch_get_board_member_codes(
            board_names=request.board_names,
            market=request.market,
        )
        all_codes = sorted({code for codes in mapping.values() for code in codes})
        name_map = _build_name_map(db, all_codes, request.market)

        groups = []
        for board_name in request.board_names:
            codes = mapping.get(board_name, [])
            groups.append(
                BoardConstituentGroup(
                    board_name=board_name,
                    total=len(codes),
                    codes=codes,
                    items=_to_constituents(codes, name_map),
                )
            )
        return BatchBoardConstituentsResponse(market=request.market, boards=groups)
    except Exception as e:
        logger.error(f"批量查询板块成分股失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"批量查询板块成分股失败: {str(e)}"},
        )
