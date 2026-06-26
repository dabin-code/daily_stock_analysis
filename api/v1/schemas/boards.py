# -*- coding: utf-8 -*-
"""
===================================
板块数据相关模型
===================================

职责：
1. 定义板块列表项与列表响应模型
2. 定义板块成分股响应模型
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class BoardItem(BaseModel):
    """板块列表项"""

    board_id: int = Field(..., description="板块 ID")
    board_name: str = Field(..., description="板块名称")
    board_type: Optional[str] = Field(None, description="板块类型（industry/concept/unknown）")
    member_count: int = Field(..., description="成员股票数量")


class BoardListResponse(BaseModel):
    """板块列表响应"""

    market: str = Field(..., description="市场（cn/hk/us）")
    board_type: Optional[str] = Field(None, description="板块类型过滤条件")
    total: int = Field(..., description="板块总数")
    items: List[BoardItem] = Field(default_factory=list, description="板块列表")

    class Config:
        json_schema_extra = {
            "example": {
                "market": "cn",
                "board_type": "industry",
                "total": 2,
                "items": [
                    {"board_id": 1, "board_name": "白酒", "board_type": "industry", "member_count": 45},
                    {"board_id": 2, "board_name": "锂电池", "board_type": "industry", "member_count": 60},
                ],
            }
        }


class BoardConstituent(BaseModel):
    """板块成分股明细项"""

    code: str = Field(..., description="股票代码")
    name: Optional[str] = Field(None, description="股票名称（股票池中无记录时为 None）")


class BoardConstituentsResponse(BaseModel):
    """板块成分股响应"""

    market: str = Field(..., description="市场（cn/hk/us）")
    board_name: str = Field(..., description="板块名称")
    total: int = Field(..., description="成分股数量")
    codes: List[str] = Field(default_factory=list, description="成分股代码列表（向后兼容）")
    items: List[BoardConstituent] = Field(default_factory=list, description="成分股明细（代码+名称）")

    class Config:
        json_schema_extra = {
            "example": {
                "market": "cn",
                "board_name": "白酒",
                "total": 2,
                "codes": ["600519", "000858"],
                "items": [
                    {"code": "600519", "name": "贵州茅台"},
                    {"code": "000858", "name": "五粮液"},
                ],
            }
        }


class BatchBoardConstituentsRequest(BaseModel):
    """批量查询板块成分股请求"""

    board_names: List[str] = Field(..., min_length=1, description="板块名称列表")
    market: str = Field("cn", description="市场（cn/hk/us）")

    class Config:
        json_schema_extra = {
            "example": {
                "board_names": ["白酒", "锂电池"],
                "market": "cn",
            }
        }


class BoardConstituentGroup(BaseModel):
    """单个板块的成分股分组"""

    board_name: str = Field(..., description="板块名称")
    total: int = Field(..., description="成分股数量")
    codes: List[str] = Field(default_factory=list, description="成分股代码列表（向后兼容）")
    items: List[BoardConstituent] = Field(default_factory=list, description="成分股明细（代码+名称）")


class BatchBoardConstituentsResponse(BaseModel):
    """批量查询板块成分股响应"""

    market: str = Field(..., description="市场（cn/hk/us）")
    boards: List[BoardConstituentGroup] = Field(default_factory=list, description="各板块成分股分组")

    class Config:
        json_schema_extra = {
            "example": {
                "market": "cn",
                "boards": [
                    {
                        "board_name": "白酒",
                        "total": 1,
                        "codes": ["600519"],
                        "items": [{"code": "600519", "name": "贵州茅台"}],
                    }
                ],
            }
        }
