"""News digest builder for the screening AI gate."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.schemas.screening_ai_gate import ScreeningAiGateConfig

logger = logging.getLogger(__name__)


@dataclass
class NewsDigest:
    """Typed, compact news summary for one candidate."""

    structural_positive: List[str] = field(default_factory=list)
    structural_negative: List[str] = field(default_factory=list)
    ignored_noise: List[str] = field(default_factory=list)
    source_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "structural_positive": self.structural_positive,
            "structural_negative": self.structural_negative,
            "ignored_noise": self.ignored_noise,
            "source_count": self.source_count,
        }


# Keywords that signal structural risk (hard-veto candidates)
_NEGATIVE_KEYWORDS = [
    "退市", "ST", "财务造假", "立案调查", "重大违法", "暂停上市",
    "连续跌停", "业绩爆雷", "商誉减值", "债务违约",
]

# Keywords that signal structural positive
_POSITIVE_KEYWORDS = [
    "机构调研", "增持", "回购", "业绩预增", "中标", "战略合作",
    "政策利好", "行业景气", "龙头", "市占率提升",
]


class ScreeningAiGateNewsBuilder:
    """Builds a compact, typed news digest from search results.

    The builder classifies each news snippet into structural_positive,
    structural_negative, or ignored_noise based on the strategy's news_focus
    and keyword matching.
    """

    def build(
        self,
        news_items: List[Dict[str, Any]],
        strategy_config: ScreeningAiGateConfig,
    ) -> NewsDigest:
        if not news_items:
            return NewsDigest()

        positives: List[str] = []
        negatives: List[str] = []
        noise: List[str] = []

        for item in news_items:
            title = str(item.get("title", "")).strip()
            snippet = str(item.get("snippet", item.get("content", ""))).strip()
            text = f"{title} {snippet}".strip()
            if not text:
                continue

            if any(kw in text for kw in _NEGATIVE_KEYWORDS):
                negatives.append(title or snippet[:80])
            elif any(kw in text for kw in _POSITIVE_KEYWORDS):
                positives.append(title or snippet[:80])
            else:
                noise.append(title or snippet[:80])

        return NewsDigest(
            structural_positive=positives[:5],
            structural_negative=negatives[:5],
            ignored_noise=noise[:3],
            source_count=len(news_items),
        )
