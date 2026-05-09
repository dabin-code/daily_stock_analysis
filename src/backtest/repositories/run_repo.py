# -*- coding: utf-8 -*-
"""Repository for FiveLayerBacktestRun CRUD operations."""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from src.backtest.models.backtest_models import FiveLayerBacktestRun
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)


class RunRepository:
    """CRUD for five_layer_backtest_runs."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def create_run(
        self,
        backtest_run_id: str,
        evaluation_mode: str,
        execution_model: str,
        trade_date_from: date,
        trade_date_to: date,
        market: str = "cn",
        **kwargs: Any,
    ) -> FiveLayerBacktestRun:
        with self.db.get_session() as session:
            run = FiveLayerBacktestRun(
                backtest_run_id=backtest_run_id,
                evaluation_mode=evaluation_mode,
                execution_model=execution_model,
                trade_date_from=trade_date_from,
                trade_date_to=trade_date_to,
                market=market,
                **kwargs,
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            return run

    def get_run(self, backtest_run_id: str) -> Optional[FiveLayerBacktestRun]:
        with self.db.get_session() as session:
            return (
                session.query(FiveLayerBacktestRun)
                .filter(FiveLayerBacktestRun.backtest_run_id == backtest_run_id)
                .first()
            )

    def update_run_status(
        self,
        backtest_run_id: str,
        status: Optional[str] = None,
        sample_count: Optional[int] = None,
        completed_count: Optional[int] = None,
        error_count: Optional[int] = None,
        config_json: Optional[str] = None,
        candidate_filter_json: Optional[str] = None,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        data_version: Optional[str] = None,
    ) -> Optional[FiveLayerBacktestRun]:
        with self.db.get_session() as session:
            run = (
                session.query(FiveLayerBacktestRun)
                .filter(FiveLayerBacktestRun.backtest_run_id == backtest_run_id)
                .first()
            )
            if run is None:
                return None
            if status is not None:
                run.status = status
            if sample_count is not None:
                run.sample_count = sample_count
            if completed_count is not None:
                run.completed_count = completed_count
            if error_count is not None:
                run.error_count = error_count
            if config_json is not None:
                run.config_json = config_json
            if candidate_filter_json is not None:
                run.candidate_filter_json = candidate_filter_json
            if started_at is not None:
                run.started_at = started_at
            if completed_at is not None:
                run.completed_at = completed_at
            if data_version is not None:
                run.data_version = data_version
            session.commit()
            session.refresh(run)
            return run

    def list_runs(
        self,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        market: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[FiveLayerBacktestRun]:
        with self.db.get_session() as session:
            q = session.query(FiveLayerBacktestRun)
            if date_from is not None:
                q = q.filter(FiveLayerBacktestRun.trade_date_from >= date_from)
            if date_to is not None:
                q = q.filter(FiveLayerBacktestRun.trade_date_to <= date_to)
            if market is not None:
                q = q.filter(FiveLayerBacktestRun.market == market)
            if status is not None:
                q = q.filter(FiveLayerBacktestRun.status == status)
            return q.order_by(FiveLayerBacktestRun.created_at.desc()).all()
