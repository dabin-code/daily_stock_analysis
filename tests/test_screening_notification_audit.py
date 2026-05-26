"""Tests for audit-level candidate push content (候选池推送内容细化).

TDD Phase 1 (RED): All tests here are written BEFORE the implementation.
They cover the three new helper methods and the modified build_run_notification().

Modules under test:
    src.services.screening_notification_service
        ScreeningNotificationService._build_score_breakdown()
        ScreeningNotificationService._build_factor_snapshot_summary()
        ScreeningNotificationService._format_candidate_audit_block()
        ScreeningNotificationService._format_candidate_summary_block()
        ScreeningNotificationService.build_run_notification()  (extended)
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

from src.services.screening_notification_service import ScreeningNotificationService

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_RULE_HITS = ["trend_aligned", "volume_expanding", "near_breakout", "liquidity_ok"]

_FACTOR = {
    "close": 51.72,
    "ma5": 51.18,
    "ma10": 52.43,
    "ma20": 49.50,
    "volume_ratio": 2.60,
    "breakout_ratio": 1.1739,
    "avg_amount": 830_000_000.0,
    "days_since_listed": 4200,
    "is_st": False,
}

# rule_score = 40+30+20+10 + (1.1739-1)*1000 + min(2.60,3.0)
#            = 100 + 173.9 + 2.6 = 276.5
_RULE_SCORE = 276.5
# AI bonus for 关注 = 4.0, news_bonus = min(2,3) = 2
_FINAL_SCORE = _RULE_SCORE + 4.0 + 2.0  # 282.5


def _make_candidate(**overrides):
    """Return a fully-populated candidate dict for testing."""
    base = {
        "code": "600519",
        "name": "贵州茅台",
        "rank": 1,
        "final_rank": 1,
        "rule_score": _RULE_SCORE,
        "final_score": _FINAL_SCORE,
        "rule_hits_json": json.dumps(_RULE_HITS),
        "factor_snapshot_json": json.dumps(_FACTOR),
        "ai_operation_advice": "关注",
        "ai_summary": "趋势未破坏，继续持有。",
        "news_count": 2,
        "news_summary": "贵州茅台新品上市",
        "has_ai_analysis": True,
        "recommendation_source": "rules_plus_ai",
        "recommendation_reason": f"规则得分 {_RULE_SCORE}；AI 建议 关注；新闻补充 2 条",
    }
    base.update(overrides)
    return base


def _make_service():
    return ScreeningNotificationService(
        screening_task_service=MagicMock(),
        notifier=MagicMock(),
        db_manager=MagicMock(),
    )


# ===========================================================================
# 1. _build_score_breakdown
# ===========================================================================


class TestBuildScoreBreakdown(unittest.TestCase):
    """Tests for ScreeningNotificationService._build_score_breakdown()."""

    def setUp(self):
        self.svc = _make_service()

    # --- result structure ---

    def test_returns_all_required_keys(self):
        result = self.svc._build_score_breakdown(_make_candidate())
        for key in ("rule_score", "ai_bonus", "news_bonus", "final_score", "rule_breakdown"):
            with self.subTest(key=key):
                self.assertIn(key, result)

    def test_rule_breakdown_is_list(self):
        result = self.svc._build_score_breakdown(_make_candidate())
        self.assertIsInstance(result["rule_breakdown"], list)

    # --- AI bonus mapping ---

    def test_ai_bonus_guan_zhu(self):
        result = self.svc._build_score_breakdown(_make_candidate(ai_operation_advice="关注"))
        self.assertAlmostEqual(result["ai_bonus"], 4.0)

    def test_ai_bonus_mai_ru(self):
        result = self.svc._build_score_breakdown(_make_candidate(ai_operation_advice="买入"))
        self.assertAlmostEqual(result["ai_bonus"], 8.0)

    def test_ai_bonus_jia_cang(self):
        result = self.svc._build_score_breakdown(_make_candidate(ai_operation_advice="加仓"))
        self.assertAlmostEqual(result["ai_bonus"], 6.0)

    def test_ai_bonus_chi_you(self):
        result = self.svc._build_score_breakdown(_make_candidate(ai_operation_advice="持有"))
        self.assertAlmostEqual(result["ai_bonus"], 2.0)

    def test_ai_bonus_guan_wang(self):
        result = self.svc._build_score_breakdown(_make_candidate(ai_operation_advice="观望"))
        self.assertAlmostEqual(result["ai_bonus"], 0.0)

    def test_ai_bonus_jian_cang(self):
        result = self.svc._build_score_breakdown(_make_candidate(ai_operation_advice="减仓"))
        self.assertAlmostEqual(result["ai_bonus"], -4.0)

    def test_ai_bonus_mai_chu(self):
        result = self.svc._build_score_breakdown(_make_candidate(ai_operation_advice="卖出"))
        self.assertAlmostEqual(result["ai_bonus"], -8.0)

    def test_ai_bonus_zero_for_unknown_advice(self):
        result = self.svc._build_score_breakdown(_make_candidate(ai_operation_advice="未知操作"))
        self.assertAlmostEqual(result["ai_bonus"], 0.0)

    def test_ai_bonus_zero_for_empty_advice(self):
        result = self.svc._build_score_breakdown(_make_candidate(ai_operation_advice=""))
        self.assertAlmostEqual(result["ai_bonus"], 0.0)

    def test_ai_bonus_zero_for_missing_advice(self):
        item = _make_candidate()
        item.pop("ai_operation_advice", None)
        result = self.svc._build_score_breakdown(item)
        self.assertAlmostEqual(result["ai_bonus"], 0.0)

    # --- news bonus ---

    def test_news_bonus_equals_news_count_when_below_cap(self):
        result = self.svc._build_score_breakdown(_make_candidate(news_count=2))
        self.assertEqual(result["news_bonus"], 2)

    def test_news_bonus_capped_at_three(self):
        result = self.svc._build_score_breakdown(_make_candidate(news_count=10))
        self.assertEqual(result["news_bonus"], 3)

    def test_news_bonus_zero_when_no_news(self):
        result = self.svc._build_score_breakdown(_make_candidate(news_count=0))
        self.assertEqual(result["news_bonus"], 0)

    # --- rule_breakdown items ---

    def test_rule_breakdown_includes_trend_aligned(self):
        result = self.svc._build_score_breakdown(_make_candidate())
        names = [rb["name"] for rb in result["rule_breakdown"]]
        self.assertIn("趋势对齐", names)

    def test_trend_aligned_score_is_40(self):
        result = self.svc._build_score_breakdown(_make_candidate())
        item = next(rb for rb in result["rule_breakdown"] if rb["name"] == "趋势对齐")
        self.assertAlmostEqual(item["score"], 40.0)

    def test_trend_aligned_reason_contains_ma_values(self):
        result = self.svc._build_score_breakdown(_make_candidate())
        item = next(rb for rb in result["rule_breakdown"] if rb["name"] == "趋势对齐")
        # Should reference close and MA20 values from factor snapshot
        self.assertIn("51.72", item["reason"])
        self.assertIn("49.50", item["reason"])

    def test_rule_breakdown_includes_volume_expanding(self):
        result = self.svc._build_score_breakdown(_make_candidate())
        names = [rb["name"] for rb in result["rule_breakdown"]]
        self.assertIn("放量条件", names)

    def test_volume_expanding_score_is_30(self):
        result = self.svc._build_score_breakdown(_make_candidate())
        item = next(rb for rb in result["rule_breakdown"] if rb["name"] == "放量条件")
        self.assertAlmostEqual(item["score"], 30.0)

    def test_volume_expanding_reason_contains_volume_ratio(self):
        result = self.svc._build_score_breakdown(_make_candidate())
        item = next(rb for rb in result["rule_breakdown"] if rb["name"] == "放量条件")
        self.assertIn("2.60", item["reason"])

    def test_rule_breakdown_includes_near_breakout(self):
        result = self.svc._build_score_breakdown(_make_candidate())
        names = [rb["name"] for rb in result["rule_breakdown"]]
        self.assertIn("临近突破", names)

    def test_near_breakout_score_is_20(self):
        result = self.svc._build_score_breakdown(_make_candidate())
        item = next(rb for rb in result["rule_breakdown"] if rb["name"] == "临近突破")
        self.assertAlmostEqual(item["score"], 20.0)

    def test_near_breakout_reason_contains_breakout_ratio(self):
        result = self.svc._build_score_breakdown(_make_candidate())
        item = next(rb for rb in result["rule_breakdown"] if rb["name"] == "临近突破")
        self.assertIn("1.1739", item["reason"])

    def test_rule_breakdown_includes_liquidity_ok(self):
        result = self.svc._build_score_breakdown(_make_candidate())
        names = [rb["name"] for rb in result["rule_breakdown"]]
        self.assertIn("流动性合格", names)

    def test_liquidity_ok_score_is_10(self):
        result = self.svc._build_score_breakdown(_make_candidate())
        item = next(rb for rb in result["rule_breakdown"] if rb["name"] == "流动性合格")
        self.assertAlmostEqual(item["score"], 10.0)

    # --- continuous components ---

    def test_breakout_premium_included_when_ratio_above_one(self):
        result = self.svc._build_score_breakdown(_make_candidate())
        names = [rb["name"] for rb in result["rule_breakdown"]]
        self.assertIn("突破溢价加分", names)

    def test_breakout_premium_score_correct(self):
        # (1.1739 - 1.0) * 1000 = 173.9
        result = self.svc._build_score_breakdown(_make_candidate())
        item = next(rb for rb in result["rule_breakdown"] if rb["name"] == "突破溢价加分")
        self.assertAlmostEqual(item["score"], 173.9, places=1)

    def test_no_breakout_premium_when_ratio_below_one(self):
        factor = {**_FACTOR, "breakout_ratio": 0.95}
        item = _make_candidate(
            factor_snapshot_json=json.dumps(factor),
            rule_hits_json=json.dumps(["trend_aligned", "volume_expanding", "liquidity_ok"]),
        )
        result = self.svc._build_score_breakdown(item)
        names = [rb["name"] for rb in result["rule_breakdown"]]
        self.assertNotIn("突破溢价加分", names)

    def test_volume_supplement_included(self):
        result = self.svc._build_score_breakdown(_make_candidate())
        names = [rb["name"] for rb in result["rule_breakdown"]]
        self.assertIn("量比补充分", names)

    def test_volume_supplement_capped_at_three(self):
        factor = {**_FACTOR, "volume_ratio": 5.0}
        item = _make_candidate(factor_snapshot_json=json.dumps(factor))
        result = self.svc._build_score_breakdown(item)
        entry = next(rb for rb in result["rule_breakdown"] if rb["name"] == "量比补充分")
        self.assertAlmostEqual(entry["score"], 3.0)

    # --- input flexibility ---

    def test_accepts_rule_hits_as_list_not_json(self):
        item = _make_candidate()
        item.pop("rule_hits_json", None)
        item["rule_hits"] = _RULE_HITS
        result = self.svc._build_score_breakdown(item)
        names = [rb["name"] for rb in result["rule_breakdown"]]
        self.assertIn("趋势对齐", names)

    def test_accepts_factor_snapshot_as_dict_not_json(self):
        item = _make_candidate()
        item.pop("factor_snapshot_json", None)
        item["factor_snapshot"] = _FACTOR
        result = self.svc._build_score_breakdown(item)
        names = [rb["name"] for rb in result["rule_breakdown"]]
        self.assertIn("突破溢价加分", names)

    def test_empty_rule_hits_produces_only_continuous_components(self):
        factor = {**_FACTOR, "breakout_ratio": 1.2, "volume_ratio": 2.0}
        item = _make_candidate(
            rule_hits_json=json.dumps([]),
            factor_snapshot_json=json.dumps(factor),
        )
        result = self.svc._build_score_breakdown(item)
        names = [rb["name"] for rb in result["rule_breakdown"]]
        self.assertNotIn("趋势对齐", names)
        self.assertNotIn("放量条件", names)
        self.assertIn("突破溢价加分", names)
        self.assertIn("量比补充分", names)

    def test_handles_missing_factor_snapshot_gracefully(self):
        item = _make_candidate()
        item.pop("factor_snapshot_json", None)
        item.pop("factor_snapshot", None)
        # Should not raise; continuous components should be 0
        result = self.svc._build_score_breakdown(item)
        self.assertIn("rule_breakdown", result)


# ===========================================================================
# 2. _build_factor_snapshot_summary
# ===========================================================================


class TestBuildFactorSnapshotSummary(unittest.TestCase):
    """Tests for ScreeningNotificationService._build_factor_snapshot_summary()."""

    def setUp(self):
        self.svc = _make_service()

    def test_returns_all_required_keys(self):
        result = self.svc._build_factor_snapshot_summary(_make_candidate())
        for key in ("close", "ma5", "ma10", "ma20", "volume_ratio", "breakout_ratio",
                    "avg_amount", "avg_amount_readable", "days_since_listed", "is_st"):
            with self.subTest(key=key):
                self.assertIn(key, result)

    def test_close_value_correct(self):
        result = self.svc._build_factor_snapshot_summary(_make_candidate())
        self.assertAlmostEqual(float(result["close"]), 51.72)

    def test_ma_values_correct(self):
        result = self.svc._build_factor_snapshot_summary(_make_candidate())
        self.assertAlmostEqual(float(result["ma5"]), 51.18)
        self.assertAlmostEqual(float(result["ma10"]), 52.43)
        self.assertAlmostEqual(float(result["ma20"]), 49.50)

    def test_formats_large_avg_amount_as_yi(self):
        # 830_000_000 = 8.30亿
        result = self.svc._build_factor_snapshot_summary(_make_candidate())
        self.assertIn("亿", result["avg_amount_readable"])
        self.assertIn("8.30", result["avg_amount_readable"])

    def test_formats_medium_avg_amount_as_wan(self):
        factor = {**_FACTOR, "avg_amount": 5_000_000.0}  # 500万
        item = _make_candidate(factor_snapshot_json=json.dumps(factor))
        result = self.svc._build_factor_snapshot_summary(item)
        self.assertIn("万", result["avg_amount_readable"])

    def test_returns_na_for_missing_avg_amount(self):
        factor = {k: v for k, v in _FACTOR.items() if k != "avg_amount"}
        item = _make_candidate(factor_snapshot_json=json.dumps(factor))
        result = self.svc._build_factor_snapshot_summary(item)
        self.assertEqual(result["avg_amount_readable"], "N/A")

    def test_handles_completely_empty_snapshot(self):
        item = _make_candidate(factor_snapshot_json=json.dumps({}))
        result = self.svc._build_factor_snapshot_summary(item)
        self.assertIsNone(result["close"])
        self.assertEqual(result["avg_amount_readable"], "N/A")

    def test_handles_missing_snapshot_fields_entirely(self):
        item = _make_candidate()
        item.pop("factor_snapshot_json", None)
        item.pop("factor_snapshot", None)
        # Should not raise
        result = self.svc._build_factor_snapshot_summary(item)
        self.assertIn("close", result)

    def test_accepts_factor_snapshot_as_dict(self):
        item = _make_candidate()
        item.pop("factor_snapshot_json", None)
        item["factor_snapshot"] = _FACTOR
        result = self.svc._build_factor_snapshot_summary(item)
        self.assertAlmostEqual(float(result["close"]), 51.72)

    def test_is_st_value_passed_through(self):
        factor = {**_FACTOR, "is_st": True}
        item = _make_candidate(factor_snapshot_json=json.dumps(factor))
        result = self.svc._build_factor_snapshot_summary(item)
        self.assertTrue(result["is_st"])


# ===========================================================================
# 3. _format_candidate_audit_block
# ===========================================================================


class TestFormatCandidateAuditBlock(unittest.TestCase):
    """Tests for ScreeningNotificationService._format_candidate_audit_block()."""

    def setUp(self):
        self.svc = _make_service()
        self.item = _make_candidate()
        self.block = "\n".join(self.svc._format_candidate_audit_block(self.item))

    # --- section headers ---

    def test_contains_total_overview_section(self):
        self.assertIn("[总览]", self.block)

    def test_contains_score_summary_section(self):
        self.assertIn("[评分汇总]", self.block)

    def test_contains_rule_breakdown_section(self):
        self.assertIn("[规则分拆解]", self.block)

    def test_contains_audit_evidence_section(self):
        self.assertIn("[审计证据]", self.block)

    def test_contains_factor_snapshot_section(self):
        self.assertIn("[原始指标]", self.block)

    def test_contains_ai_section_when_has_ai(self):
        self.assertIn("[AI增强]", self.block)

    def test_contains_news_section_when_has_news(self):
        self.assertIn("[新闻增强]", self.block)

    # --- stock identification ---

    def test_contains_stock_name(self):
        self.assertIn("贵州茅台", self.block)

    def test_contains_stock_code(self):
        self.assertIn("600519", self.block)

    def test_contains_final_rank(self):
        self.assertIn("1.", self.block)

    # --- score values ---

    def test_contains_final_score(self):
        self.assertIn(str(_FINAL_SCORE), self.block)

    def test_contains_rule_score(self):
        self.assertIn(str(_RULE_SCORE), self.block)

    def test_contains_ai_bonus_value(self):
        self.assertIn("4.0", self.block)

    def test_contains_news_bonus_value(self):
        self.assertIn("+2", self.block)

    # --- rule breakdown details ---

    def test_contains_trend_aligned_breakdown(self):
        self.assertIn("趋势对齐", self.block)

    def test_contains_volume_expanding_breakdown(self):
        self.assertIn("放量条件", self.block)

    def test_contains_near_breakout_breakdown(self):
        self.assertIn("临近突破", self.block)

    def test_contains_liquidity_ok_breakdown(self):
        self.assertIn("流动性合格", self.block)

    def test_contains_breakout_premium_breakdown(self):
        self.assertIn("突破溢价加分", self.block)

    def test_contains_volume_supplement_breakdown(self):
        self.assertIn("量比补充分", self.block)

    # --- rule hits Chinese mapping ---

    def test_rule_hits_mapped_to_chinese(self):
        self.assertIn("趋势对齐", self.block)
        self.assertIn("放量", self.block)
        self.assertIn("临近突破", self.block)
        self.assertIn("流动性合格", self.block)

    # --- raw factor values ---

    def test_contains_close_value(self):
        self.assertIn("51.72", self.block)

    def test_contains_ma20_value(self):
        self.assertIn("49.50", self.block)

    def test_contains_volume_ratio_value(self):
        self.assertIn("2.60", self.block)

    def test_contains_breakout_ratio_value(self):
        self.assertIn("1.1739", self.block)

    def test_contains_avg_amount_readable(self):
        # 830_000_000 → 8.30亿
        self.assertIn("亿", self.block)

    def test_contains_days_since_listed(self):
        self.assertIn("4200", self.block)

    # --- AI section content ---

    def test_ai_section_contains_operation_advice(self):
        self.assertIn("关注", self.block)

    def test_ai_section_contains_ai_summary(self):
        self.assertIn("趋势未破坏", self.block)

    # --- news section content ---

    def test_news_section_contains_news_count(self):
        self.assertIn("2", self.block)

    def test_news_section_contains_news_summary(self):
        self.assertIn("贵州茅台新品上市", self.block)

    # --- conditional sections ---

    def test_no_ai_section_when_no_ai_data(self):
        item = _make_candidate(
            ai_operation_advice=None,
            ai_summary=None,
            has_ai_analysis=False,
        )
        block = "\n".join(self.svc._format_candidate_audit_block(item))
        self.assertNotIn("[AI增强]", block)

    def test_no_news_section_when_no_news(self):
        item = _make_candidate(news_count=0, news_summary=None)
        block = "\n".join(self.svc._format_candidate_audit_block(item))
        self.assertNotIn("[新闻增强]", block)

    def test_source_text_ai_enhanced(self):
        item = _make_candidate(recommendation_source="rules_plus_ai")
        block = "\n".join(self.svc._format_candidate_audit_block(item))
        self.assertIn("AI 增强", block)

    def test_source_text_rules_only(self):
        item = _make_candidate(recommendation_source="rules_only")
        block = "\n".join(self.svc._format_candidate_audit_block(item))
        self.assertIn("规则输出", block)

    def test_returns_list_of_strings(self):
        result = self.svc._format_candidate_audit_block(self.item)
        self.assertIsInstance(result, list)
        for line in result:
            self.assertIsInstance(line, str)


# ===========================================================================
# 4. _format_candidate_summary_block
# ===========================================================================


class TestFormatCandidateSummaryBlock(unittest.TestCase):
    """Tests for ScreeningNotificationService._format_candidate_summary_block()."""

    def setUp(self):
        self.svc = _make_service()
        self.item = _make_candidate(final_rank=6)
        self.block = "\n".join(self.svc._format_candidate_summary_block(self.item))

    def test_contains_stock_name(self):
        self.assertIn("贵州茅台", self.block)

    def test_contains_stock_code(self):
        self.assertIn("600519", self.block)

    def test_contains_final_score(self):
        self.assertIn(str(_FINAL_SCORE), self.block)

    def test_contains_source_text(self):
        self.assertIn("AI 增强", self.block)

    def test_does_not_contain_full_audit_sections(self):
        # Summary block must NOT contain the detailed audit headers
        self.assertNotIn("[评分汇总]", self.block)
        self.assertNotIn("[规则分拆解]", self.block)
        self.assertNotIn("[原始指标]", self.block)

    def test_returns_list_of_strings(self):
        result = self.svc._format_candidate_summary_block(self.item)
        self.assertIsInstance(result, list)


# ===========================================================================
# 5. build_run_notification (extended with audit blocks)
# ===========================================================================


class TestBuildRunNotificationAudit(unittest.TestCase):
    """Tests for the extended build_run_notification() with audit-level content."""

    def setUp(self):
        self.svc = _make_service()
        self.run = {
            "run_id": "run-test-001",
            "trade_date": "2026-03-15",
            "mode": "aggressive",
            "status": "completed",
            "universe_size": 5307,
            "candidate_count": 8,
        }

    def _make_candidates(self, count: int):
        return [
            _make_candidate(
                final_rank=i,
                rank=i,
                code=f"60{i:04d}",
                name=f"股票{i}",
            )
            for i in range(1, count + 1)
        ]

    # --- audit blocks for top N ---

    def test_top5_candidates_have_full_audit_blocks(self):
        candidates = self._make_candidates(7)
        content = self.svc.build_run_notification(run=self.run, candidates=candidates, audit_top_n=5)
        # Each of the first 5 should have a full audit block
        for i in range(1, 6):
            with self.subTest(rank=i):
                self.assertIn("[评分汇总]", content)
                self.assertIn("[规则分拆解]", content)
                self.assertIn("[原始指标]", content)

    def test_candidates_beyond_top5_show_summary_only(self):
        candidates = self._make_candidates(7)
        content = self.svc.build_run_notification(run=self.run, candidates=candidates, audit_top_n=5)
        # Candidates 6 and 7 appear as summary: they have the stock name but the block
        # itself should not contain extra audit headers from them exclusively.
        # We verify that the total count of [评分汇总] occurrences == 5 (one per audit block)
        self.assertEqual(content.count("[评分汇总]"), 5)

    def test_single_candidate_uses_audit_block(self):
        candidates = self._make_candidates(1)
        content = self.svc.build_run_notification(run=self.run, candidates=candidates, audit_top_n=5)
        self.assertIn("[评分汇总]", content)
        self.assertIn("[规则分拆解]", content)

    def test_audit_top_n_default_renders_all_candidates(self):
        """build_run_notification() should default to full detail for every candidate."""
        import inspect
        sig = inspect.signature(self.svc.build_run_notification)
        default = sig.parameters.get("audit_top_n")
        self.assertIsNotNone(default, "audit_top_n parameter is missing")
        self.assertIsNone(default.default)

        candidates = self._make_candidates(7)
        content = self.svc.build_run_notification(run=self.run, candidates=candidates)
        self.assertEqual(content.count("[评分汇总]"), 7)

    # --- zero candidates ---

    def test_zero_candidates_does_not_raise(self):
        content = self.svc.build_run_notification(run=self.run, candidates=[])
        self.assertIn("全市场筛选推荐名单", content)

    def test_zero_candidates_shows_no_candidate_message(self):
        content = self.svc.build_run_notification(run=self.run, candidates=[])
        self.assertIn("未产生", content)

    # --- standard header fields ---

    def test_header_contains_trade_date(self):
        content = self.svc.build_run_notification(run=self.run, candidates=self._make_candidates(1))
        self.assertIn("2026-03-15", content)

    def test_header_contains_mode(self):
        content = self.svc.build_run_notification(run=self.run, candidates=self._make_candidates(1))
        self.assertIn("aggressive", content)

    def test_header_contains_universe_size(self):
        content = self.svc.build_run_notification(run=self.run, candidates=self._make_candidates(1))
        self.assertIn("5307", content)

    def test_header_contains_run_id(self):
        content = self.svc.build_run_notification(run=self.run, candidates=self._make_candidates(1))
        self.assertIn("run-test-001", content)

    # --- backward compatibility with existing test data (no rule_hits_json) ---

    def test_backward_compat_candidate_without_snapshot_fields(self):
        """Candidates without rule_hits_json / factor_snapshot_json must not crash."""
        candidates = [
            {
                "code": "600519",
                "name": "贵州茅台",
                "final_rank": 1,
                "rule_score": 91.5,
                "final_score": 96.5,
                "recommendation_source": "rules_plus_ai",
                "recommendation_reason": "规则得分 91.5；AI 建议 关注",
                "ai_summary": "趋势未破坏。",
                "news_summary": "贵州茅台新品上市",
            }
        ]
        content = self.svc.build_run_notification(run=self.run, candidates=candidates)
        self.assertIn("贵州茅台", content)
        self.assertIn("趋势未破坏", content)


# ===========================================================================
# 6. Five-layer decision notification (Phase 3B-2)
# ===========================================================================


def _make_five_layer_candidate(**overrides):
    """Return a candidate dict with five-layer decision fields populated."""
    base = _make_candidate()
    base.update({
        "trade_stage": "probe_entry",
        "setup_type": "trend_breakout",
        "entry_maturity": "high",
        "risk_level": "medium",
        "market_regime": "balanced",
        "theme_position": "main_theme",
        "candidate_pool_level": "focus_list",
        "trade_plan": {
            "initial_position": "1/5仓",
            "stop_loss_rule": "跌破突破K线实体下沿或MA20止损",
            "take_profit_plan": "沿MA10移动止盈;跌破MA10减仓",
            "invalidation_rule": "买入后3个交易日未启动则离场",
            "risk_level": "medium",
            "holding_expectation": "1~2周波段",
            "add_rule": None,
        },
    })
    base.update(overrides)
    return base


class TestFiveLayerAuditBlock(unittest.TestCase):
    """Tests for five-layer decision section in audit blocks (Phase 3B-2)."""

    def setUp(self):
        self.svc = _make_service()

    def test_audit_block_includes_five_layer_section(self):
        """Audit block should contain a [五层决策] section."""
        item = _make_five_layer_candidate()
        block = "\n".join(self.svc._format_candidate_audit_block(item))
        self.assertIn("[五层决策]", block)

    def test_audit_block_probe_entry_shows_stop_loss(self):
        """probe_entry candidate should display stop_loss_rule in audit block."""
        item = _make_five_layer_candidate(trade_stage="probe_entry")
        block = "\n".join(self.svc._format_candidate_audit_block(item))
        self.assertIn("跌破突破K线实体下沿或MA20止损", block)

    def test_audit_block_probe_entry_shows_position(self):
        """probe_entry candidate should display initial_position."""
        item = _make_five_layer_candidate(trade_stage="probe_entry")
        block = "\n".join(self.svc._format_candidate_audit_block(item))
        self.assertIn("1/5仓", block)

    def test_audit_block_add_on_shows_add_rule(self):
        """add_on_strength candidate should display add_rule."""
        item = _make_five_layer_candidate(
            trade_stage="add_on_strength",
            trade_plan={
                "initial_position": "1/3仓",
                "stop_loss_rule": "跌破突破K线实体下沿或MA20止损",
                "take_profit_plan": "沿MA10移动止盈",
                "invalidation_rule": "3个交易日未启动则离场",
                "risk_level": "medium",
                "holding_expectation": "1~2周波段",
                "add_rule": "回踩MA20不破+放量反弹加仓;最多加仓1次;跌破MA20取消",
            },
        )
        block = "\n".join(self.svc._format_candidate_audit_block(item))
        self.assertIn("加仓", block)
        self.assertIn("回踩MA20不破", block)

    def test_audit_block_watch_no_trade_plan(self):
        """watch candidate should NOT display trade plan details."""
        item = _make_five_layer_candidate(trade_stage="watch", trade_plan=None)
        block = "\n".join(self.svc._format_candidate_audit_block(item))
        self.assertIn("[五层决策]", block)
        self.assertNotIn("止损", block)
        self.assertNotIn("仓位", block)

    def test_audit_block_shows_trade_stage_label(self):
        """Five-layer section should show Chinese label for trade_stage."""
        item = _make_five_layer_candidate(trade_stage="probe_entry")
        block = "\n".join(self.svc._format_candidate_audit_block(item))
        self.assertIn("试探进场", block)

    def test_audit_block_shows_setup_type_label(self):
        """Five-layer section should show Chinese label for setup_type."""
        item = _make_five_layer_candidate(setup_type="trend_breakout")
        block = "\n".join(self.svc._format_candidate_audit_block(item))
        self.assertIn("趋势突破", block)

    def test_audit_block_shows_theme_position(self):
        """Five-layer section should show theme_position."""
        item = _make_five_layer_candidate(theme_position="main_theme")
        block = "\n".join(self.svc._format_candidate_audit_block(item))
        self.assertIn("main_theme", block)

    def test_audit_block_no_five_layer_when_no_fields(self):
        """If no five-layer fields present, [五层决策] section should be absent."""
        item = _make_candidate()  # no five-layer fields
        block = "\n".join(self.svc._format_candidate_audit_block(item))
        self.assertNotIn("[五层决策]", block)


class TestFiveLayerSummaryBlock(unittest.TestCase):
    """Tests for trade_stage in summary block (Phase 3B-2)."""

    def setUp(self):
        self.svc = _make_service()

    def test_summary_line_includes_trade_stage(self):
        """Summary block should include trade_stage label."""
        item = _make_five_layer_candidate(final_rank=6, trade_stage="focus")
        block = "\n".join(self.svc._format_candidate_summary_block(item))
        self.assertIn("重点关注", block)

    def test_summary_line_no_stage_when_absent(self):
        """Summary block without trade_stage should not crash."""
        item = _make_candidate(final_rank=6)
        block = "\n".join(self.svc._format_candidate_summary_block(item))
        self.assertIn("贵州茅台", block)


class TestFiveLayerRunNotification(unittest.TestCase):
    """Tests for run-level five-layer features (Phase 3B-2)."""

    def setUp(self):
        self.svc = _make_service()
        self.run = {
            "run_id": "run-test-3b",
            "trade_date": "2026-04-01",
            "mode": "balanced",
            "status": "completed",
            "universe_size": 5000,
            "candidate_count": 3,
        }

    def test_title_includes_regime_label(self):
        """Notification title should include market_regime label."""
        candidates = [
            _make_five_layer_candidate(
                final_rank=1, market_regime="balanced",
            ),
        ]
        content = self.svc.build_run_notification(run=self.run, candidates=candidates)
        self.assertIn("均衡", content)

    def test_title_includes_aggressive_label(self):
        candidates = [
            _make_five_layer_candidate(
                final_rank=1, market_regime="aggressive",
            ),
        ]
        content = self.svc.build_run_notification(run=self.run, candidates=candidates)
        self.assertIn("进攻", content)

    def test_stand_aside_shows_observation_notice(self):
        """stand_aside regime should show observation warning."""
        candidates = [
            _make_five_layer_candidate(
                final_rank=1, market_regime="stand_aside",
                trade_stage="watch", trade_plan=None,
            ),
        ]
        content = self.svc.build_run_notification(run=self.run, candidates=candidates)
        self.assertIn("观望", content)
        # Should contain a notice about observation-only
        self.assertIn("观察", content)

    def test_no_regime_label_when_no_five_layer(self):
        """When candidates have no market_regime, title should be normal."""
        candidates = [_make_candidate(final_rank=1)]
        content = self.svc.build_run_notification(run=self.run, candidates=candidates)
        # Should still have the normal title
        self.assertIn("推荐", content)


if __name__ == "__main__":
    unittest.main()
