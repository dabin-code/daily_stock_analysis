"""Page-scoped AI gate service for screening candidates."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.schemas.screening_ai_gate import ScreeningAiGateConfig
from src.services.screening_ai_gate_evidence_builder import EvidencePackage
from src.services.screening_ai_gate_news_builder import NewsDigest
from src.services.screening_ai_gate_prompt_builder import ScreeningAiGatePromptBuilder

logger = logging.getLogger(__name__)

_VALID_VERDICTS = {"buy", "watch", "reject"}


@dataclass
class AiGateDecision:
    """Normalized result of one AI gate evaluation."""

    strategy_name: str
    verdict: str  # "buy" | "watch" | "reject"
    confidence: float = 0.0
    ai_status: str = "pending"  # "completed" | "failed" | "insufficient_data" | "hard_veto" | "skipped"
    stage: Optional[str] = None
    primary_action: Optional[str] = None
    reasoning: Optional[str] = None
    watch_levels: Dict[str, Any] = field(default_factory=dict)
    hard_vetoes: List[str] = field(default_factory=list)
    decision_summary: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "ai_status": self.ai_status,
            "stage": self.stage,
            "primary_action": self.primary_action,
            "reasoning": self.reasoning,
            "watch_levels": self.watch_levels,
            "hard_vetoes": self.hard_vetoes,
            "decision_summary": self.decision_summary,
        }


class ScreeningAiGateService:
    """Evaluates one candidate against its active strategy using a dedicated AI gate."""

    def __init__(self, llm_adapter: Optional[Any] = None) -> None:
        self._llm_adapter = llm_adapter
        self._prompt_builder = ScreeningAiGatePromptBuilder()

    def evaluate(
        self,
        config: ScreeningAiGateConfig,
        evidence: EvidencePackage,
        news_digest: NewsDigest,
    ) -> AiGateDecision:
        strategy_name = config.strategy_name

        # 1. Check hard vetoes deterministically
        vetoes = self._check_hard_vetoes(config, evidence)
        if vetoes:
            return AiGateDecision(
                strategy_name=strategy_name,
                verdict="reject",
                ai_status="hard_veto",
                hard_vetoes=vetoes,
                decision_summary=f"硬性否决: {'; '.join(vetoes)}",
            )

        # 2. Check evidence quality
        if evidence.data_quality != "sufficient":
            return AiGateDecision(
                strategy_name=strategy_name,
                verdict="watch",
                ai_status="insufficient_data",
                decision_summary=f"证据不足: 缺失 {', '.join(evidence.missing_fields)}",
            )

        # 3. Call LLM for strategy evaluation
        try:
            prompt = self._prompt_builder.build(config, evidence, news_digest)
            raw_response = self._call_llm(prompt)
            if raw_response is None:
                return AiGateDecision(
                    strategy_name=strategy_name,
                    verdict="watch",
                    ai_status="failed",
                    decision_summary="AI 模型未返回有效响应",
                )
            return self._normalize_response(strategy_name, raw_response)
        except Exception as exc:
            logger.warning("ai_gate: LLM call failed for %s: %s", strategy_name, exc)
            return AiGateDecision(
                strategy_name=strategy_name,
                verdict="watch",
                ai_status="failed",
                decision_summary=f"AI 调用失败: {str(exc)[:80]}",
            )

    def _call_llm(self, prompt: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """Call the LLM and parse the JSON response."""
        if self._llm_adapter is None:
            from src.agent.llm_adapter import LLMToolAdapter
            self._llm_adapter = LLMToolAdapter()

        messages = [
            {"role": "system", "content": prompt["system"]},
            {"role": "user", "content": prompt["user"]},
        ]
        response = self._llm_adapter.call_text(messages, temperature=0.3, max_tokens=1024)
        if not response or not response.content:
            return None

        return self._parse_json_response(response.content)

    @staticmethod
    def _parse_json_response(text: str) -> Optional[Dict[str, Any]]:
        """Extract JSON from LLM response text."""
        cleaned = text.strip()
        if "```json" in cleaned:
            cleaned = cleaned.split("```json", 1)[1]
            if "```" in cleaned:
                cleaned = cleaned.split("```", 1)[0]
        elif "```" in cleaned:
            cleaned = cleaned.split("```", 1)[1]
            if "```" in cleaned:
                cleaned = cleaned.split("```", 1)[0]

        # Find JSON object
        json_start = cleaned.find("{")
        json_end = cleaned.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            json_str = cleaned[json_start:json_end]
            # Fix common issues
            json_str = re.sub(r",\s*}", "}", json_str)
            json_str = re.sub(r",\s*]", "]", json_str)
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                pass
        return None

    @staticmethod
    def _normalize_response(strategy_name: str, raw: Dict[str, Any]) -> AiGateDecision:
        """Normalize LLM output into a stable AiGateDecision."""
        verdict = str(raw.get("verdict", "watch")).strip().lower()
        if verdict not in _VALID_VERDICTS:
            verdict = "watch"

        confidence = float(raw.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))

        return AiGateDecision(
            strategy_name=strategy_name,
            verdict=verdict,
            confidence=confidence,
            ai_status="completed",
            stage=raw.get("stage"),
            primary_action=raw.get("primary_action"),
            reasoning=raw.get("reasoning"),
            watch_levels=raw.get("watch_levels") or {},
            hard_vetoes=raw.get("hard_vetoes") or [],
            decision_summary=raw.get("reasoning"),
        )

    @staticmethod
    def _check_hard_vetoes(
        config: ScreeningAiGateConfig,
        evidence: EvidencePackage,
    ) -> List[str]:
        """Deterministic hard-veto checks. Returns list of triggered veto reasons."""
        vetoes: List[str] = []
        is_st = evidence.market_filter_snapshot.get("is_st", False)
        if is_st:
            vetoes.append("ST或*ST股票")
        return vetoes
