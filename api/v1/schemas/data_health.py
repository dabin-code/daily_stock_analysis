from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class DataHealthSummaryResponse(BaseModel):
    market: str
    active_instrument_count: int = 0
    expected_universe_count: int = 0
    st_excluded_count: int = 0
    stock_data_start_date: Optional[str] = None
    stock_data_end_date: Optional[str] = None
    stock_data_trade_date_count: int = 0
    latest_trade_date: Optional[str] = None
    latest_complete_date: Optional[str] = None
    latest_audit_passed_date: Optional[str] = None
    latest_trade_date_synced_count: int = 0
    latest_trade_date_coverage_ratio: float = 0.0
    open_gap_count: int = 0
    pending_retry_gap_count: int = 0
    candidate_skip_gap_count: int = 0
    screening_ready: bool = False
    screening_ready_date: Optional[str] = None


class DataHealthCoverageItem(BaseModel):
    trade_date: str
    synced_count: int
    expected_count: int
    coverage_ratio: float
    is_complete: bool


class DataHealthCoverageResponse(BaseModel):
    market: str
    expected_count: int
    items: List[DataHealthCoverageItem] = Field(default_factory=list)
    ma100_ready_count: int = 0
    ma200_ready_count: int = 0


class DataHealthGapItem(BaseModel):
    gap_key: str
    source_run_id: str
    market: str
    gap_scope: str
    code: Optional[str] = None
    trade_date: Optional[str] = None
    missing_date_from: Optional[str] = None
    missing_date_to: Optional[str] = None
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class DataHealthGapListResponse(BaseModel):
    market: str
    total: int
    items: List[DataHealthGapItem] = Field(default_factory=list)


class DataHealthOperationRequest(BaseModel):
    operation_type: Literal["backfill_to_date", "repair_gaps", "rerun_audit", "retry_failed"]
    market: Literal["cn"] = "cn"
    trade_date: Optional[date] = None
    stock_codes: Optional[List[str]] = Field(default=None, max_length=200)

    @field_validator("stock_codes")
    @classmethod
    def validate_stock_codes(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return None
        normalized = []
        for code in value:
            item = str(code).strip().upper()
            if not item:
                continue
            if not item.isdigit() or len(item) != 6:
                raise ValueError("stock_codes must contain 6-digit A-share codes")
            normalized.append(item)
        return normalized or None

    @model_validator(mode="after")
    def validate_operation_scope(self) -> "DataHealthOperationRequest":
        if self.trade_date is not None and self.trade_date > date.today():
            raise ValueError("trade_date cannot be in the future")
        if self.operation_type in {"backfill_to_date", "rerun_audit"} and self.trade_date is None:
            raise ValueError(f"{self.operation_type} requires trade_date")
        if self.operation_type == "retry_failed" and self.trade_date is not None and not self.stock_codes:
            raise ValueError("retry_failed with trade_date requires stock_codes")
        return self


class DataHealthTaskResponse(BaseModel):
    task_id: str
    operation_type: str
    market: str
    status: str
    progress: int
    message: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class DataHealthTaskListResponse(BaseModel):
    total: int
    items: List[DataHealthTaskResponse] = Field(default_factory=list)
