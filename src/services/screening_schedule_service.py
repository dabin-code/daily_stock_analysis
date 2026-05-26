from __future__ import annotations

from typing import Any, Dict, Optional

from src.config import Config, get_config
from src.core.trading_calendar import get_open_markets_today
from src.services.screening_task_service import ScreeningTaskService


class ScreeningScheduleService:
    """全市场筛选的手动/定时触发服务。"""

    def __init__(
        self,
        config: Optional[Config] = None,
        screening_task_service: Optional[ScreeningTaskService] = None,
    ) -> None:
        self.config = config or get_config()
        self.screening_task_service = screening_task_service or ScreeningTaskService()

    def run_once(self, force_run: bool = False, market: str = "cn") -> Dict[str, Any]:
        if self._should_skip_for_trading_day(force_run=force_run, market=market):
            return {
                "status": "skipped",
                "reason": "non_trading_day",
                "market": market,
            }

        result = self.screening_task_service.execute_run(
            trade_date=None,
            mode=self.config.screening_default_mode,
            candidate_limit=self.config.screening_candidate_limit,
            ai_top_k=self.config.screening_ai_top_k,
            market=market,
            trigger_type="scheduled",
        )

        return result

    def _should_skip_for_trading_day(self, force_run: bool, market: str) -> bool:
        if force_run or not getattr(self.config, "trading_day_check_enabled", True):
            return False
        open_markets = get_open_markets_today()
        return market not in open_markets
