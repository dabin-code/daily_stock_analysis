# -*- coding: utf-8 -*-
"""TDD: Integration test for OpenClaw endpoint with strategy filtering."""

import pytest
from datetime import date
from unittest.mock import MagicMock, patch
from src.services.screening_task_service import ScreeningTaskService
from src.services.theme_context_ingest_service import ExternalTheme, OpenClawThemeContext
from src.agent.skills.base import SkillManager


class TestOpenClawEndpointIntegration:
    """Test that OpenClaw endpoint correctly filters strategies end-to-end."""

    def test_openclaw_run_fails_closed_without_theme_memberships(self):
        """OpenClaw must not silently fall back to the full market."""
        # Setup
        skill_manager = SkillManager()
        skill_manager.load_builtin_strategies()
        service = ScreeningTaskService(
            db_manager=MagicMock(),
            skill_manager=skill_manager,
        )

        # Create theme context (simulating OpenClaw input)
        theme_context = OpenClawThemeContext(
            source="openclaw",
            trade_date=date.today().isoformat(),
            market="cn",
            themes=[
                ExternalTheme(
                    name="AI芯片",
                    heat_score=85.0,
                    confidence=0.9,
                    catalyst_summary="AI芯片板块受政策利好刺激",
                    keywords=["AI", "芯片", "算力"],
                    evidence=[],
                )
            ],
            accepted_at="2026-03-27T15:00:00",
        )

        # Inject theme context
        service._theme_context = theme_context

        with patch.object(
            service,
            "_resolve_theme_universe_codes",
            return_value=[],
        ):
            with pytest.raises(ValueError, match="题材未匹配到任何板块成分股"):
                service.execute_run(
                    trade_date=None,
                    stock_codes=None,
                    mode="balanced",
                    candidate_limit=50,
                    ai_top_k=10,
                    market="cn",
                    trigger_type="openclaw",
                    strategy_names=["extreme_strength_combo"],
                )
