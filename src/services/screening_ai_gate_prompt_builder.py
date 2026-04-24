"""Prompt builder for the page-scoped screening AI gate."""

from __future__ import annotations

import json
from typing import Any, Dict

from src.schemas.screening_ai_gate import ScreeningAiGateConfig
from src.services.screening_ai_gate_evidence_builder import EvidencePackage
from src.services.screening_ai_gate_news_builder import NewsDigest

_SYSTEM_PROMPT = """\
你是一个专业的A股选股 AI 判关助手。你的唯一任务是根据给定的策略playbook、技术证据和新闻摘要，\
对一只候选股票做出结构化的 buy/watch/reject 判定。

硬性规则：
1. 严格按照策略 playbook 中的阶段定义和判定标准评估。
2. 如果存在硬性否决条件（hard_veto_rules），必须优先执行否决。
3. 不要发明策略中未定义的判断标准。
4. 输出必须是一个合法的 JSON 对象，不要包含其他文字。

输出 JSON 格式：
{
  "verdict": "buy" | "watch" | "reject",
  "confidence": 0.0-1.0,
  "stage": "阶段标签",
  "primary_action": "下一步操作建议（一句话）",
  "reasoning": "结构化判断依据（2-3句）",
  "watch_levels": {
    "entry_price": null,
    "stop_loss": null,
    "target_price": null
  },
  "hard_vetoes": []
}
"""


class ScreeningAiGatePromptBuilder:
    """Builds a strategy-specific prompt for the AI gate evaluation."""

    def build(
        self,
        config: ScreeningAiGateConfig,
        evidence: EvidencePackage,
        news_digest: NewsDigest,
    ) -> Dict[str, str]:
        sections = []

        # Strategy playbook
        sections.append("## 策略 Playbook")
        sections.append(f"策略: {config.strategy_name}")
        sections.append(f"目标: {config.playbook.get('goal', '未定义')}")
        focus_items = config.playbook.get("focus", [])
        if focus_items:
            sections.append("评估重点:")
            for item in focus_items:
                sections.append(f"  - {item}")

        # Stage definitions
        sections.append("\n## 阶段定义 (stage_definitions)")
        for stage_key, stage_def in config.stage_definitions.items():
            label = stage_def.get("label", stage_key) if isinstance(stage_def, dict) else stage_key
            typical = stage_def.get("typical_verdict", "N/A") if isinstance(stage_def, dict) else "N/A"
            desc = stage_def.get("description", "") if isinstance(stage_def, dict) else ""
            sections.append(f"  - {stage_key}: {label} (典型判定: {typical})")
            if desc:
                sections.append(f"    {desc}")

        # Hard veto rules
        if config.hard_veto_rules:
            sections.append("\n## 硬性否决规则 (hard_veto_rules)")
            for rule in config.hard_veto_rules:
                sections.append(f"  - {rule}")

        # Candidate evidence
        sections.append("\n## 候选股票信息")
        meta = evidence.candidate_meta
        sections.append(f"代码: {meta.get('code', 'N/A')}")
        sections.append(f"名称: {meta.get('name', 'N/A')}")
        sections.append(f"收盘价: {meta.get('close', 'N/A')}")

        sections.append("\n### 策略快照")
        sections.append(json.dumps(evidence.strategy_snapshot, ensure_ascii=False, indent=2, default=str))

        sections.append("\n### 原始技术证据")
        sections.append(json.dumps(evidence.strategy_raw_evidence, ensure_ascii=False, indent=2, default=str))

        sections.append("\n### 市场筛选指标")
        sections.append(json.dumps(evidence.market_filter_snapshot, ensure_ascii=False, indent=2, default=str))

        sections.append(f"\n数据质量: {evidence.data_quality}")
        if evidence.missing_fields:
            sections.append(f"缺失字段: {', '.join(evidence.missing_fields)}")

        # News digest
        sections.append("\n## 新闻摘要")
        if news_digest.structural_positive:
            sections.append("利好:")
            for item in news_digest.structural_positive:
                sections.append(f"  + {item}")
        if news_digest.structural_negative:
            sections.append("利空:")
            for item in news_digest.structural_negative:
                sections.append(f"  - {item}")
        if not news_digest.structural_positive and not news_digest.structural_negative:
            sections.append("无显著结构性新闻")

        # Output contract reminder
        sections.append("\n## 输出要求")
        sections.append("请严格按照系统提示中的 JSON 格式输出 verdict (buy/watch/reject)。")

        user_content = "\n".join(sections)

        return {
            "system": _SYSTEM_PROMPT,
            "user": user_content,
        }
