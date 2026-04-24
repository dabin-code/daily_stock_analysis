"""Pydantic models for the page-scoped screening AI gate."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field


class ScreeningAiGateConfig(BaseModel):
    """One strategy's AI gate configuration loaded from its YAML asset."""

    strategy_name: str
    version: str = "v1"
    ai_priority: int = Field(default=99, description="Lower = higher priority for active-strategy resolution")
    supported: bool = True
    playbook: Dict[str, Any] = Field(default_factory=dict)
    stage_definitions: Dict[str, Any] = Field(default_factory=dict)
    hard_veto_rules: List[str] = Field(default_factory=list)
    news_focus: List[str] = Field(default_factory=list)
    payload_fields: List[str] = Field(default_factory=list)
