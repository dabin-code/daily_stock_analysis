"""Registry that loads ai_gate configs from strategy YAML assets."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from src.schemas.screening_ai_gate import ScreeningAiGateConfig

logger = logging.getLogger(__name__)

# Strategies that participate in screening (have a `screening:` block in YAML).
_SCREENING_STRATEGY_NAMES = [
    "bottom_divergence_double_breakout",
    "ma100_low123_combined",
    "ma100_60min_combined",
    "extreme_strength_combo",
]

_STRATEGIES_DIR = Path(__file__).resolve().parent.parent.parent / "strategies"


class ScreeningAiGateRegistry:
    """Loads and caches ai_gate config from strategy YAML files."""

    def __init__(self, configs: Dict[str, ScreeningAiGateConfig]) -> None:
        self._configs = configs

    def get(self, strategy_name: str) -> Optional[ScreeningAiGateConfig]:
        return self._configs.get(strategy_name)

    def list_all(self) -> List[str]:
        return list(self._configs.keys())

    def list_supported(self) -> List[str]:
        return [name for name, cfg in self._configs.items() if cfg.supported]

    @classmethod
    def from_builtin_strategies(
        cls,
        strategies_dir: Optional[Path] = None,
    ) -> "ScreeningAiGateRegistry":
        base_dir = strategies_dir or _STRATEGIES_DIR
        configs: Dict[str, ScreeningAiGateConfig] = {}
        for strategy_name in _SCREENING_STRATEGY_NAMES:
            yaml_path = base_dir / f"{strategy_name}.yaml"
            if not yaml_path.exists():
                logger.warning("ai_gate_registry: YAML not found for %s", strategy_name)
                continue
            try:
                with open(yaml_path, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
                ai_gate_block = data.get("ai_gate") if isinstance(data, dict) else None
                if ai_gate_block and isinstance(ai_gate_block, dict):
                    configs[strategy_name] = ScreeningAiGateConfig(
                        strategy_name=strategy_name,
                        version=ai_gate_block.get("version", "v1"),
                        ai_priority=ai_gate_block.get("ai_priority", 99),
                        supported=ai_gate_block.get("supported", True),
                        playbook=ai_gate_block.get("playbook", {}),
                        stage_definitions=ai_gate_block.get("stage_definitions", {}),
                        hard_veto_rules=ai_gate_block.get("hard_veto_rules", []),
                        news_focus=ai_gate_block.get("news_focus", []),
                        payload_fields=ai_gate_block.get("payload_fields", []),
                    )
                else:
                    # Strategy exists but has no ai_gate block → unsupported stub
                    configs[strategy_name] = ScreeningAiGateConfig(
                        strategy_name=strategy_name,
                        version="v1",
                        ai_priority=99,
                        supported=False,
                    )
            except Exception:
                logger.exception("ai_gate_registry: failed to load %s", strategy_name)
                configs[strategy_name] = ScreeningAiGateConfig(
                    strategy_name=strategy_name,
                    version="v1",
                    ai_priority=99,
                    supported=False,
                )
        return cls(configs)
