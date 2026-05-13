# -*- coding: utf-8 -*-
"""Tests for backward-compatible config env aliases and fallbacks."""

import os
import unittest
from unittest.mock import patch

from src.config import Config
from src.core.config_registry import get_field_definition


class ConfigEnvCompatibilityTestCase(unittest.TestCase):
    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_board_sync_schedule_uses_safe_defaults(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = Config._load_from_env()

        self.assertFalse(config.board_sync_schedule_enabled)
        self.assertEqual(config.board_sync_schedule_time, "15:05")
        self.assertFalse(config.board_sync_run_immediately)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_board_sync_schedule_reads_explicit_env_values(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "BOARD_SYNC_SCHEDULE_ENABLED": "true",
            "BOARD_SYNC_SCHEDULE_TIME": "15:10",
            "BOARD_SYNC_RUN_IMMEDIATELY": "true",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertTrue(config.board_sync_schedule_enabled)
        self.assertEqual(config.board_sync_schedule_time, "15:10")
        self.assertTrue(config.board_sync_run_immediately)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_screening_schedule_uses_safe_defaults(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = Config._load_from_env()

        self.assertFalse(config.screening_schedule_enabled)
        self.assertEqual(config.screening_schedule_time, "07:00")
        self.assertFalse(config.screening_schedule_run_immediately)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_screening_schedule_reads_explicit_env_values(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "SCREENING_SCHEDULE_ENABLED": "true",
            "SCREENING_SCHEDULE_TIME": "07:05",
            "SCREENING_SCHEDULE_RUN_IMMEDIATELY": "true",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertTrue(config.screening_schedule_enabled)
        self.assertEqual(config.screening_schedule_time, "07:05")
        self.assertTrue(config.screening_schedule_run_immediately)

        enabled_field = get_field_definition("SCREENING_SCHEDULE_ENABLED")
        time_field = get_field_definition("SCREENING_SCHEDULE_TIME")

        self.assertEqual(enabled_field["category"], "screening")
        self.assertEqual(enabled_field["data_type"], "boolean")
        self.assertEqual(time_field["data_type"], "time")

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_schedule_run_immediately_falls_back_to_legacy_run_immediately(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "RUN_IMMEDIATELY": "false",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertFalse(config.schedule_run_immediately)
        self.assertFalse(config.run_immediately)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_schedule_run_immediately_prefers_schedule_specific_setting(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "RUN_IMMEDIATELY": "false",
            "SCHEDULE_RUN_IMMEDIATELY": "true",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertTrue(config.schedule_run_immediately)
        self.assertFalse(config.run_immediately)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_empty_legacy_run_immediately_stays_false_when_schedule_alias_is_unset(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "RUN_IMMEDIATELY": "",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertFalse(config.schedule_run_immediately)
        self.assertFalse(config.run_immediately)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_empty_schedule_run_immediately_stays_false_without_falling_back(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "RUN_IMMEDIATELY": "true",
            "SCHEDULE_RUN_IMMEDIATELY": "",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertFalse(config.schedule_run_immediately)
        self.assertTrue(config.run_immediately)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_kline_governance_config_uses_safe_defaults(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = Config._load_from_env()

        self.assertFalse(config.kline_governance_enabled)
        self.assertEqual(config.kline_governance_schedule_time, "17:00")
        self.assertFalse(config.kline_governance_run_immediately)
        self.assertEqual(config.kline_audit_lookback_days, 30)
        self.assertEqual(config.kline_deep_audit_lookback_days, 365)
        self.assertFalse(config.kline_deep_audit_schedule_enabled)
        self.assertEqual(config.kline_deep_audit_schedule_time, "17:00")
        self.assertEqual(config.kline_retry_max_attempts, 3)
        self.assertEqual(config.kline_skip_candidate_failure_threshold, 3)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_kline_governance_config_reads_env_values_and_registers_fields(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "KLINE_GOVERNANCE_ENABLED": "true",
            "KLINE_GOVERNANCE_SCHEDULE_TIME": "17:05",
            "KLINE_GOVERNANCE_RUN_IMMEDIATELY": "true",
            "KLINE_AUDIT_LOOKBACK_DAYS": "45",
            "KLINE_DEEP_AUDIT_LOOKBACK_DAYS": "540",
            "KLINE_DEEP_AUDIT_SCHEDULE_ENABLED": "true",
            "KLINE_DEEP_AUDIT_SCHEDULE_TIME": "18:30",
            "KLINE_RETRY_MAX_ATTEMPTS": "4",
            "KLINE_SKIP_CANDIDATE_FAILURE_THRESHOLD": "5",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertTrue(config.kline_governance_enabled)
        self.assertEqual(config.kline_governance_schedule_time, "17:05")
        self.assertTrue(config.kline_governance_run_immediately)
        self.assertEqual(config.kline_audit_lookback_days, 45)
        self.assertEqual(config.kline_deep_audit_lookback_days, 540)
        self.assertTrue(config.kline_deep_audit_schedule_enabled)
        self.assertEqual(config.kline_deep_audit_schedule_time, "18:30")
        self.assertEqual(config.kline_retry_max_attempts, 4)
        self.assertEqual(config.kline_skip_candidate_failure_threshold, 5)

        governance_field = get_field_definition("KLINE_GOVERNANCE_ENABLED")
        deep_audit_time_field = get_field_definition("KLINE_DEEP_AUDIT_SCHEDULE_TIME")

        self.assertEqual(governance_field["category"], "system")
        self.assertEqual(governance_field["data_type"], "boolean")
        self.assertEqual(deep_audit_time_field["data_type"], "time")


if __name__ == "__main__":
    unittest.main()
