# -*- coding: utf-8 -*-
"""Tests for simplified candidate-pool classification rules."""

import unittest

from src.schemas.trading_types import CandidatePoolLevel, ThemePosition
from src.services.candidate_pool_classifier import CandidatePoolClassifier


class CandidatePoolClassifierSimplifiedTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self.classifier = CandidatePoolClassifier()

    def test_main_theme_limit_up_stock_can_enter_leader_pool(self) -> None:
        result = self.classifier.classify(
            leader_score=0.0,
            extreme_strength_score=0.0,
            theme_position=ThemePosition.MAIN_THEME,
            is_limit_up=True,
        )
        self.assertEqual(result, CandidatePoolLevel.LEADER_POOL)

    def test_secondary_theme_limit_up_stock_can_enter_leader_pool(self) -> None:
        result = self.classifier.classify(
            leader_score=0.0,
            extreme_strength_score=0.0,
            theme_position=ThemePosition.SECONDARY_THEME,
            is_limit_up=True,
        )
        self.assertEqual(result, CandidatePoolLevel.LEADER_POOL)

    def test_follower_theme_limit_up_stock_stays_in_focus_list(self) -> None:
        result = self.classifier.classify(
            leader_score=99.0,
            extreme_strength_score=99.0,
            theme_position=ThemePosition.FOLLOWER_THEME,
            is_limit_up=True,
        )
        self.assertEqual(result, CandidatePoolLevel.FOCUS_LIST)

    def test_non_limit_up_main_theme_stock_does_not_enter_leader_pool(self) -> None:
        result = self.classifier.classify(
            leader_score=99.0,
            extreme_strength_score=99.0,
            theme_position=ThemePosition.MAIN_THEME,
            is_limit_up=False,
        )
        self.assertEqual(result, CandidatePoolLevel.FOCUS_LIST)

    def test_non_theme_stock_cannot_enter_leader_pool_even_if_limit_up(self) -> None:
        result = self.classifier.classify(
            leader_score=99.0,
            extreme_strength_score=99.0,
            theme_position=ThemePosition.NON_THEME,
            is_limit_up=True,
        )
        self.assertEqual(result, CandidatePoolLevel.WATCHLIST)

    def test_extended_do_not_chase_downgrades_leader_pool_to_focus_list(self) -> None:
        """A3/C：阶段标签 extended_do_not_chase 时 LEADER_POOL 降级为 FOCUS_LIST。"""
        result = self.classifier.classify(
            leader_score=0.0,
            extreme_strength_score=0.0,
            theme_position=ThemePosition.MAIN_THEME,
            is_limit_up=True,
            stage_label="extended_do_not_chase",
        )
        self.assertEqual(result, CandidatePoolLevel.FOCUS_LIST)

    def test_other_stage_labels_do_not_change_classification(self) -> None:
        """非 extended_do_not_chase 的阶段标签不改变原有分级结果。"""
        for label in ("pool_only", "watch_only", "breakout_day", "retest_entry", "none", None):
            with self.subTest(stage_label=label):
                result = self.classifier.classify(
                    leader_score=0.0,
                    extreme_strength_score=0.0,
                    theme_position=ThemePosition.MAIN_THEME,
                    is_limit_up=True,
                    stage_label=label,
                )
                self.assertEqual(result, CandidatePoolLevel.LEADER_POOL)

    def test_extended_do_not_chase_does_not_affect_non_leader_pool(self) -> None:
        """非 LEADER_POOL 档位不被进一步降级，避免把已是观察池的样本再踢出。"""
        result = self.classifier.classify(
            leader_score=0.0,
            extreme_strength_score=0.0,
            theme_position=ThemePosition.FOLLOWER_THEME,
            is_limit_up=True,
            stage_label="extended_do_not_chase",
        )
        self.assertEqual(result, CandidatePoolLevel.FOCUS_LIST)

        watchlist_result = self.classifier.classify(
            leader_score=0.0,
            extreme_strength_score=0.0,
            theme_position=ThemePosition.NON_THEME,
            is_limit_up=True,
            stage_label="extended_do_not_chase",
        )
        self.assertEqual(watchlist_result, CandidatePoolLevel.WATCHLIST)
