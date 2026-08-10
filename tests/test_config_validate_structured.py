# -*- coding: utf-8 -*-
"""Tests for Config.validate_structured() and backward-compatible validate().

Covers:
- ConfigIssue dataclass basics
- validate_structured() severity classifications
- LLM availability check honours all three config tiers (YAML / channels /
  legacy keys) via llm_model_list
- validate() backward-compat: still returns List[str] with the same messages
"""
import pytest
from unittest.mock import patch

from src.config import Config, ConfigIssue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**kwargs) -> Config:
    """Build a minimal Config object with sensible defaults for testing.

    Any keyword argument overrides the corresponding dataclass field so tests
    only have to specify the fields that matter for their scenario.
    """
    defaults = dict(
        stock_list=["600519"],
        tushare_token=None,
        # Populate llm_model_list as the three-tier signal
        llm_model_list=[{"model_name": "gemini/gemini-2.0-flash", "litellm_params": {"model": "gemini/gemini-2.0-flash", "api_key": "sk-test"}}],
        litellm_model="gemini/gemini-2.0-flash",
        gemini_api_keys=[],
        anthropic_api_keys=[],
        openai_api_keys=[],
        deepseek_api_keys=[],
        bocha_api_keys=[],
        tavily_api_keys=[],
        brave_api_keys=[],
        serpapi_keys=[],
        searxng_base_urls=[],
        wechat_webhook_url="https://example.com/webhook",
        feishu_webhook_url=None,
        telegram_bot_token=None,
        telegram_chat_id=None,
        email_sender=None,
        email_password=None,
        pushover_user_key=None,
        pushover_api_token=None,
        pushplus_token=None,
        serverchan3_sendkey=None,
        custom_webhook_urls=[],
        discord_bot_token=None,
        discord_main_channel_id=None,
        discord_webhook_url=None,
        llm_channels=[],
        litellm_config_path=None,
        gemini_api_key=None,
        anthropic_api_key=None,
        openai_api_key=None,
        openai_base_url=None,
        openai_vision_model=None,
    )
    defaults.update(kwargs)
    return Config(**defaults)


def _severities(issues):
    return [i.severity for i in issues]


def _fields(issues):
    return [i.field for i in issues]


# ---------------------------------------------------------------------------
# ConfigIssue basics
# ---------------------------------------------------------------------------

class TestConfigIssue:
    def test_str_equals_message(self):
        issue = ConfigIssue(severity="error", message="something went wrong", field="FOO")
        assert str(issue) == "something went wrong"

    def test_severity_values(self):
        for sev in ("error", "warning", "info"):
            issue = ConfigIssue(severity=sev, message="test", field="F")
            assert issue.severity == sev

    def test_default_field(self):
        issue = ConfigIssue(severity="info", message="hello")
        assert issue.field == ""


# ---------------------------------------------------------------------------
# validate_structured() — happy path (all good)
# ---------------------------------------------------------------------------

class TestValidateStructuredHappyPath:
    def test_no_issues_when_fully_configured(self):
        cfg = _make_config()
        issues = cfg.validate_structured()
        # No errors or warnings; only possible info about tushare / search
        errors = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]
        assert errors == []
        assert warnings == []


# ---------------------------------------------------------------------------
# validate_structured() — stock list
# ---------------------------------------------------------------------------

class TestValidateStructuredStockList:
    def test_empty_stock_list_is_error(self):
        cfg = _make_config(stock_list=[])
        issues = cfg.validate_structured()
        errors = [i for i in issues if i.severity == "error"]
        assert any("STOCK_LIST" in i.field for i in errors)

    def test_configured_stock_list_no_stock_error(self):
        cfg = _make_config(stock_list=["600519", "000001"])
        issues = cfg.validate_structured()
        assert not any(i.field == "STOCK_LIST" for i in issues if i.severity == "error")


# ---------------------------------------------------------------------------
# validate_structured() — LLM availability (three-tier check)
# ---------------------------------------------------------------------------

class TestValidateStructuredLLM:
    def test_no_llm_is_error(self):
        """Empty llm_model_list must produce an error regardless of legacy keys."""
        cfg = _make_config(llm_model_list=[])
        issues = cfg.validate_structured()
        assert any(i.severity == "error" and "LLM" in i.message for i in issues)

    def test_llm_channels_only_no_error(self):
        """LLM_CHANNELS populated via llm_model_list must NOT trigger an error.

        This is the primary regression guard: a user who only configures
        LLM_CHANNELS (no legacy *_API_KEY) should not see 'AI 功能不可用'.
        """
        channel_model_list = [
            {"model_name": "openai/gpt-4o-mini", "litellm_params": {"api_key": "sk-chan", "api_base": "https://aihubmix.com/v1"}},
        ]
        cfg = _make_config(
            llm_model_list=channel_model_list,
            litellm_model="openai/gpt-4o-mini",
            gemini_api_keys=[],
            anthropic_api_keys=[],
            openai_api_keys=[],
            deepseek_api_keys=[],
        )
        issues = cfg.validate_structured()
        assert not any(i.severity == "error" and "LLM" in i.message for i in issues)

    def test_yaml_config_only_no_error(self):
        """LITELLM_CONFIG (YAML) path: populated llm_model_list = no error."""
        yaml_model_list = [
            {"model_name": "gemini/gemini-2.5-flash", "litellm_params": {"api_key": "sk-yaml"}},
        ]
        cfg = _make_config(
            llm_model_list=yaml_model_list,
            litellm_model="gemini/gemini-2.5-flash",
            litellm_config_path="/tmp/litellm.yaml",
            gemini_api_keys=[],
            anthropic_api_keys=[],
            openai_api_keys=[],
        )
        issues = cfg.validate_structured()
        assert not any(i.severity == "error" and "LLM" in i.message for i in issues)

    def test_legacy_gemini_key_no_error(self):
        """Legacy GEMINI_API_KEY path: llm_model_list populated = no error."""
        model_list = [
            {"model_name": "__legacy_gemini__", "litellm_params": {"model": "__legacy_gemini__", "api_key": "sk-gem"}},
        ]
        cfg = _make_config(llm_model_list=model_list, gemini_api_keys=["sk-gem"])
        issues = cfg.validate_structured()
        assert not any(i.severity == "error" and "LLM" in i.message for i in issues)

    def test_deepseek_only_no_error(self):
        """DEEPSEEK_API_KEY path (was missing in old validate()): no error."""
        model_list = [
            {"model_name": "__legacy_deepseek__", "litellm_params": {"model": "__legacy_deepseek__", "api_key": "sk-ds"}},
        ]
        cfg = _make_config(
            llm_model_list=model_list,
            deepseek_api_keys=["sk-ds"],
            gemini_api_keys=[],
            anthropic_api_keys=[],
            openai_api_keys=[],
        )
        issues = cfg.validate_structured()
        assert not any(i.severity == "error" and "LLM" in i.message for i in issues)

    def test_missing_litellm_model_is_info_not_error(self):
        """llm_model_list present but litellm_model unset = info, not error."""
        cfg = _make_config(litellm_model="")
        issues = cfg.validate_structured()
        llm_issues = [i for i in issues if "LITELLM_MODEL" in i.field]
        assert llm_issues, "Expected an info issue about LITELLM_MODEL"
        assert all(i.severity == "info" for i in llm_issues)

    def test_direct_env_provider_model_without_model_list_no_error(self):
        """Direct LiteLLM env providers should count as configured for runtime."""
        cfg = _make_config(
            llm_model_list=[],
            litellm_model="cohere/command-r-plus",
        )
        issues = cfg.validate_structured()
        assert not any(i.severity == "error" and "LLM" in i.message for i in issues)

    def test_configured_primary_model_missing_from_channels_is_error(self):
        cfg = _make_config(
            llm_model_list=[
                {"model_name": "openai/gpt-4o-mini", "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "sk-test"}},
            ],
            litellm_model="openai/gpt-4o",
        )
        issues = cfg.validate_structured()
        assert any(i.severity == "error" and i.field == "LITELLM_MODEL" for i in issues)

    def test_configured_vision_model_missing_from_channels_is_warning(self):
        cfg = _make_config(
            llm_model_list=[
                {"model_name": "openai/gpt-4o-mini", "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "sk-test"}},
            ],
            vision_model="openai/gpt-4o",
        )
        issues = cfg.validate_structured()
        assert any(i.severity == "warning" and i.field == "VISION_MODEL" for i in issues)


class TestValidateStructuredScreening:
    def test_ai_top_k_greater_than_candidate_limit_is_error(self):
        cfg = _make_config(
            screening_candidate_limit=10,
            screening_ai_top_k=12,
        )
        issues = cfg.validate_structured()
        screening_issues = [i for i in issues if i.field == "SCREENING_AI_TOP_K"]
        assert screening_issues
        assert screening_issues[0].severity == "error"

    @patch.dict(
        "os.environ",
        {
            "SCREENING_DEFAULT_MODE": "aggressive",
            "SCREENING_CANDIDATE_LIMIT": "40",
            "SCREENING_AI_TOP_K": "8",
            "SCREENING_MIN_LIST_DAYS": "180",
            "SCREENING_MIN_VOLUME_RATIO": "1.6",
            "SCREENING_BREAKOUT_LOOKBACK_DAYS": "30",
            "SCREENING_FACTOR_LOOKBACK_DAYS": "120",
            "SCREENING_INGEST_FAILURE_THRESHOLD": "0.15",
        },
        clear=True,
    )
    @patch("src.config.load_dotenv")
    def test_screening_fields_load_from_env(self, _mock_dotenv):
        Config._instance = None
        cfg = Config._load_from_env()
        assert cfg.screening_default_mode == "aggressive"
        assert cfg.screening_candidate_limit == 40
        assert cfg.screening_ai_top_k == 8
        assert cfg.screening_min_list_days == 180
        assert cfg.screening_min_volume_ratio == 1.6
        assert cfg.screening_breakout_lookback_days == 30
        assert cfg.screening_factor_lookback_days == 120
        assert cfg.screening_ingest_failure_threshold == 0.15
        assert not hasattr(cfg, "screening_min_avg_amount")

    @patch.dict(
        "os.environ",
        {
            "SCREENING_CANDIDATE_LIMIT": "",
            "SCREENING_AI_TOP_K": "",
            "SCREENING_MIN_VOLUME_RATIO": "",
        },
        clear=True,
    )
    @patch("src.config.load_dotenv")
    def test_blank_screening_numeric_fields_fallback_to_defaults(self, _mock_dotenv):
        Config._instance = None
        cfg = Config._load_from_env()
        assert cfg.screening_candidate_limit == 30
        assert cfg.screening_ai_top_k == 10
        assert cfg.screening_min_volume_ratio == 1.2
        assert cfg.screening_ingest_failure_threshold == 0.20

    @patch.dict(
        "os.environ",
        {
            "SCREENING_CANDIDATE_LIMIT": "10000",
            "SCREENING_AI_TOP_K": "500",
            "SCREENING_MIN_VOLUME_RATIO": "99",
        },
        clear=True,
    )
    @patch("src.config.load_dotenv")
    def test_screening_out_of_range_values_are_preserved_and_reported(self, _mock_dotenv):
        Config._instance = None
        cfg = Config._load_from_env()
        assert cfg.screening_candidate_limit == 10000
        assert cfg.screening_ai_top_k == 500
        assert cfg.screening_min_volume_ratio == 99.0
        assert cfg._screening_parse_errors == ()
        error_fields = {
            issue.field
            for issue in cfg.validate_structured()
            if issue.severity == "error"
        }
        assert {
            "SCREENING_CANDIDATE_LIMIT",
            "SCREENING_AI_TOP_K",
            "SCREENING_MIN_VOLUME_RATIO",
        }.issubset(error_fields)

    @patch.dict(
        "os.environ",
        {
            "SCREENING_AI_TOP_K": "abc",
            "SCREENING_MIN_VOLUME_RATIO": "nan",
        },
        clear=True,
    )
    @patch("src.config.load_dotenv")
    def test_invalid_screening_numeric_fields_fallback_to_defaults(self, _mock_dotenv):
        Config._instance = None
        cfg = Config._load_from_env()
        assert cfg.screening_ai_top_k == 10
        assert cfg.screening_min_volume_ratio == 1.2
        assert cfg._screening_parse_errors
        error_fields = {
            issue.field
            for issue in cfg.validate_structured()
            if issue.severity == "error"
        }
        assert {
            "SCREENING_AI_TOP_K",
            "SCREENING_MIN_VOLUME_RATIO",
        }.issubset(error_fields)

    @patch.dict(
        "os.environ",
        {
            "SCREENING_DEFAULT_MODE": "unknown",
        },
        clear=True,
    )
    @patch("src.config.load_dotenv")
    def test_invalid_screening_default_mode_falls_back_to_balanced(self, _mock_dotenv):
        Config._instance = None
        cfg = Config._load_from_env()
        assert cfg.screening_default_mode == "balanced"
        assert any(
            issue.severity == "error"
            and issue.field == "SCREENING_DEFAULT_MODE"
            for issue in cfg.validate_structured()
        )


# ---------------------------------------------------------------------------
# Bottom-divergence v2 config
# ---------------------------------------------------------------------------

class TestBottomDivergenceV2Config:
    def test_bottom_divergence_v2_defaults_are_disabled_and_zero_cost(self):
        cfg = _make_config()

        assert cfg.bottom_divergence_v2_enabled is False
        assert cfg.bottom_divergence_v2_cluster_pct == 0.015
        assert cfg.bottom_divergence_v2_atr_gap_multiplier == 0.5
        assert cfg.bottom_divergence_v2_zone_score_min == 0.45
        assert cfg.bottom_divergence_v2_breakout_buffer_pct == 0.003
        assert cfg.bottom_divergence_v2_sync_window == 3
        assert cfg.bottom_divergence_v2_retention_bars == 20
        assert cfg.bottom_divergence_v2_r1_weights == (
            0.30, 0.25, 0.15, 0.15, 0.10, 0.05,
        )
        assert cfg.bottom_divergence_v2_r2_weights == (
            0.35, 0.15, 0.15, 0.15, 0.10, 0.10,
        )
        assert cfg.backtest_buy_cost_bps == 0.0
        assert cfg.backtest_sell_cost_bps == 0.0
        assert cfg.backtest_slippage_bps == 0.0
        assert cfg._bottom_divergence_v2_parse_errors == ()
        assert "_bottom_divergence_v2_parse_errors" not in repr(cfg)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_bottom_divergence_v2_fields_load_from_env(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ):
        with patch.dict(
            "os.environ",
            {
                "BOTTOM_DIVERGENCE_V2_ENABLED": "true",
                "BOTTOM_DIVERGENCE_V2_CLUSTER_PCT": "0.02",
                "BOTTOM_DIVERGENCE_V2_ATR_GAP_MULTIPLIER": "0.7",
                "BOTTOM_DIVERGENCE_V2_ZONE_SCORE_MIN": "0.6",
                "BOTTOM_DIVERGENCE_V2_BREAKOUT_BUFFER_PCT": "0.004",
                "BOTTOM_DIVERGENCE_V2_SYNC_WINDOW": "4",
                "BOTTOM_DIVERGENCE_V2_RETENTION_BARS": "25",
                "BOTTOM_DIVERGENCE_V2_R1_WEIGHTS": "0.2,0.2,0.2,0.2,0.1,0.1",
                "BOTTOM_DIVERGENCE_V2_R2_WEIGHTS": "0.1,0.2,0.2,0.2,0.2,0.1",
                "BACKTEST_BUY_COST_BPS": "1.5",
                "BACKTEST_SELL_COST_BPS": "2.5",
                "BACKTEST_SLIPPAGE_BPS": "3.5",
            },
            clear=True,
        ):
            cfg = Config._load_from_env()

        assert cfg.bottom_divergence_v2_enabled is True
        assert cfg.bottom_divergence_v2_cluster_pct == 0.02
        assert cfg.bottom_divergence_v2_atr_gap_multiplier == 0.7
        assert cfg.bottom_divergence_v2_zone_score_min == 0.6
        assert cfg.bottom_divergence_v2_breakout_buffer_pct == 0.004
        assert cfg.bottom_divergence_v2_sync_window == 4
        assert cfg.bottom_divergence_v2_retention_bars == 25
        assert cfg.bottom_divergence_v2_r1_weights == (
            0.2, 0.2, 0.2, 0.2, 0.1, 0.1,
        )
        assert cfg.bottom_divergence_v2_r2_weights == (
            0.1, 0.2, 0.2, 0.2, 0.2, 0.1,
        )
        assert cfg.backtest_buy_cost_bps == 1.5
        assert cfg.backtest_sell_cost_bps == 2.5
        assert cfg.backtest_slippage_bps == 3.5
        assert cfg._bottom_divergence_v2_parse_errors == ()

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_bottom_divergence_v2_invalid_enabled_typo_is_disabled_error(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ):
        with patch.dict(
            "os.environ",
            {"BOTTOM_DIVERGENCE_V2_ENABLED": " flase "},
            clear=True,
        ):
            cfg = Config._load_from_env()

        assert cfg.bottom_divergence_v2_enabled is False
        assert cfg._bottom_divergence_v2_parse_errors
        assert any(
            issue.severity == "error"
            and issue.field == "BOTTOM_DIVERGENCE_V2_ENABLED"
            for issue in cfg.validate_structured()
        )

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_bottom_divergence_v2_invalid_env_formats_fallback_to_structured_issues(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ):
        invalid_env = {
            "BOTTOM_DIVERGENCE_V2_CLUSTER_PCT": "not-a-number",
            "BOTTOM_DIVERGENCE_V2_SYNC_WINDOW": "3.5",
            "BOTTOM_DIVERGENCE_V2_R1_WEIGHTS": "0.5,0.5",
            "BACKTEST_BUY_COST_BPS": "nan",
        }
        with patch.dict("os.environ", invalid_env, clear=True):
            cfg = Config._load_from_env()

        assert cfg.bottom_divergence_v2_cluster_pct == 0.015
        assert cfg.bottom_divergence_v2_sync_window == 3
        assert cfg.bottom_divergence_v2_r1_weights == (
            0.30, 0.25, 0.15, 0.15, 0.10, 0.05,
        )
        assert cfg.backtest_buy_cost_bps == 0.0
        issues = cfg.validate_structured()
        issue_fields = {
            issue.field for issue in issues if issue.severity == "error"
        }
        assert issue_fields.issuperset(invalid_env)

    @pytest.mark.parametrize(
        ("field_name", "value", "expected_env"),
        [
            ("bottom_divergence_v2_cluster_pct", float("inf"), "BOTTOM_DIVERGENCE_V2_CLUSTER_PCT"),
            ("bottom_divergence_v2_cluster_pct", 0.0, "BOTTOM_DIVERGENCE_V2_CLUSTER_PCT"),
            ("bottom_divergence_v2_atr_gap_multiplier", -0.1, "BOTTOM_DIVERGENCE_V2_ATR_GAP_MULTIPLIER"),
            ("bottom_divergence_v2_zone_score_min", 1.1, "BOTTOM_DIVERGENCE_V2_ZONE_SCORE_MIN"),
            ("bottom_divergence_v2_breakout_buffer_pct", -0.1, "BOTTOM_DIVERGENCE_V2_BREAKOUT_BUFFER_PCT"),
            ("bottom_divergence_v2_sync_window", 3.0, "BOTTOM_DIVERGENCE_V2_SYNC_WINDOW"),
            ("bottom_divergence_v2_sync_window", 0, "BOTTOM_DIVERGENCE_V2_SYNC_WINDOW"),
            ("bottom_divergence_v2_retention_bars", -1, "BOTTOM_DIVERGENCE_V2_RETENTION_BARS"),
            ("bottom_divergence_v2_retention_bars", 0, "BOTTOM_DIVERGENCE_V2_RETENTION_BARS"),
            ("bottom_divergence_v2_r1_weights", (0, 0, 1, 0, 0, 0), "BOTTOM_DIVERGENCE_V2_R1_WEIGHTS"),
            ("bottom_divergence_v2_r2_weights", (0.2,) * 6, "BOTTOM_DIVERGENCE_V2_R2_WEIGHTS"),
            ("backtest_sell_cost_bps", -0.1, "BACKTEST_SELL_COST_BPS"),
            ("backtest_slippage_bps", float("nan"), "BACKTEST_SLIPPAGE_BPS"),
        ],
    )
    def test_bottom_divergence_v2_invalid_values_are_structured_errors(
        self,
        field_name,
        value,
        expected_env,
    ):
        cfg = _make_config(**{field_name: value})

        issues = cfg.validate_structured()

        assert any(
            issue.severity == "error" and issue.field == expected_env
            for issue in issues
        )


# ---------------------------------------------------------------------------
# validate_structured() — notification & search
# ---------------------------------------------------------------------------

class TestValidateStructuredNotification:
    def test_no_notification_is_warning(self):
        cfg = _make_config(wechat_webhook_url=None)
        issues = cfg.validate_structured()
        warn = [i for i in issues if i.severity == "warning"]
        assert any("通知渠道" in i.message for i in warn)

    def test_notification_configured_no_warning(self):
        cfg = _make_config(wechat_webhook_url="https://example.com/wh")
        issues = cfg.validate_structured()
        assert not any(i.severity == "warning" and "通知渠道" in i.message for i in issues)

    def test_feishu_app_chat_counts_as_notification_channel(self):
        cfg = _make_config(
            wechat_webhook_url=None,
            feishu_app_id="cli_test_app_id",
            feishu_app_secret="secret_test_value",
            feishu_chat_id="oc_test_chat_id",
        )
        issues = cfg.validate_structured()
        assert not any(i.severity == "warning" and "通知渠道" in i.message for i in issues)

    def test_feishu_stream_enabled_without_credentials_warns(self):
        cfg = _make_config(
            wechat_webhook_url=None,
            feishu_stream_enabled=True,
            feishu_app_id=None,
            feishu_app_secret=None,
        )
        issues = cfg.validate_structured()
        warn = [i for i in issues if i.field == "FEISHU_STREAM_ENABLED"]
        assert warn
        assert warn[0].severity == "warning"

    def test_feishu_stream_enabled_with_credentials_has_no_stream_warning(self):
        cfg = _make_config(
            wechat_webhook_url=None,
            feishu_stream_enabled=True,
            feishu_app_id="cli_test_app_id",
            feishu_app_secret="secret_test_value",
        )
        issues = cfg.validate_structured()
        assert not any(i.field == "FEISHU_STREAM_ENABLED" for i in issues)

    def test_no_search_engine_is_info(self):
        cfg = _make_config()
        issues = cfg.validate_structured()
        info = [i for i in issues if i.severity == "info"]
        assert any("搜索引擎" in i.message for i in info)

    def test_searxng_configured_no_search_info(self):
        """When searxng_base_urls is configured, no 'unconfigured search engine' info."""
        cfg = _make_config(searxng_base_urls=["https://searx.example.org"])
        issues = cfg.validate_structured()
        info = [i for i in issues if i.severity == "info"]
        assert not any("搜索引擎" in i.message and "未配置" in i.message for i in info)


# ---------------------------------------------------------------------------
# Deprecated field migration hints
# ---------------------------------------------------------------------------

class TestDeprecatedFieldHints:
    def test_openai_vision_model_deprecation_when_env_set(self):
        """When OPENAI_VISION_MODEL is in env, validate_structured reports deprecation hint."""
        cfg = _make_config()
        with patch.dict("os.environ", {"OPENAI_VISION_MODEL": "openai/gpt-4o"}, clear=False):
            issues = cfg.validate_structured()
        deprec = [i for i in issues if i.field == "OPENAI_VISION_MODEL"]
        assert deprec, "Expected deprecation hint when OPENAI_VISION_MODEL is set"
        assert deprec[0].severity == "info"
        assert "VISION_MODEL" in deprec[0].message

    def test_no_deprecation_when_openai_vision_model_not_in_env(self):
        """When OPENAI_VISION_MODEL is not in env, no deprecation hint."""
        import os
        cfg = _make_config()
        real_getenv = os.getenv

        def mock_getenv(key, default=None):
            if key == "OPENAI_VISION_MODEL":
                return None
            return real_getenv(key, default)

        with patch("src.config.os.getenv", side_effect=mock_getenv):
            issues = cfg.validate_structured()
        deprec = [i for i in issues if i.field == "OPENAI_VISION_MODEL"]
        assert not deprec, "Should not report deprecation when OPENAI_VISION_MODEL is unset"


# ---------------------------------------------------------------------------
# Vision key validation
# ---------------------------------------------------------------------------

class TestVisionKeyValidation:
    def test_vision_model_set_no_key_is_warning(self):
        cfg = _make_config(
            vision_model="gemini/gemini-2.0-flash",
            gemini_api_keys=[],
            anthropic_api_keys=[],
            openai_api_keys=[],
            deepseek_api_keys=[],
        )
        issues = cfg.validate_structured()
        warn = [i for i in issues if i.field == "VISION_MODEL"]
        assert warn and warn[0].severity == "warning"

    def test_vision_model_set_with_key_no_warning(self):
        cfg = _make_config(
            vision_model="gemini/gemini-2.0-flash",
            gemini_api_keys=["sk-gemini-testkey-1234"],
        )
        issues = cfg.validate_structured()
        assert not any(
            i.field == "VISION_MODEL" and i.severity == "warning" for i in issues
        )

    def test_vision_model_set_with_short_key_still_warns(self):
        """Short keys (len < 8) are filtered at runtime; validation should warn."""
        cfg = _make_config(
            vision_model="gemini/gemini-2.0-flash",
            gemini_api_keys=["x"],
            anthropic_api_keys=[],
            openai_api_keys=[],
            deepseek_api_keys=[],
        )
        issues = cfg.validate_structured()
        warn = [i for i in issues if i.field == "VISION_MODEL"]
        assert warn and warn[0].severity == "warning"

    def test_primary_provider_key_sufficient_even_if_not_in_priority(self):
        """Primary model's provider key is checked even when absent from VISION_PROVIDER_PRIORITY."""
        cfg = _make_config(
            llm_model_list=[
                {"model_name": "openai/gpt-4o", "litellm_params": {"model": "openai/gpt-4o", "api_key": "sk-test"}},
            ],
            litellm_model="openai/gpt-4o",
            vision_model="openai/gpt-4o",
            vision_provider_priority="gemini,anthropic",  # openai excluded from priority
            openai_api_keys=["sk-openai-validkey-xyz"],
            gemini_api_keys=[],
            anthropic_api_keys=[],
            deepseek_api_keys=[],
        )
        issues = cfg.validate_structured()
        # Should NOT warn: primary model (openai) has a valid key
        assert not any(i.field == "VISION_MODEL" and i.severity == "warning" for i in issues)

    def test_no_vision_model_no_warning(self):
        """When VISION_MODEL is not set, no Vision key warning is raised."""
        cfg = _make_config(vision_model="", gemini_api_keys=[])
        issues = cfg.validate_structured()
        assert not any(i.field == "VISION_MODEL" for i in issues)


# ---------------------------------------------------------------------------
# Env alias compatibility
# ---------------------------------------------------------------------------

class TestEnvAliasCompatibility:
    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_discord_channel_id_legacy_alias_is_still_loaded(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ):
        with patch.dict(
            "os.environ",
            {
                "DISCORD_BOT_TOKEN": "token",
                "DISCORD_CHANNEL_ID": "legacy-channel",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        assert config.discord_bot_token == "token"
        assert config.discord_main_channel_id == "legacy-channel"

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_discord_main_channel_id_takes_precedence_over_legacy_alias(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ):
        with patch.dict(
            "os.environ",
            {
                "DISCORD_BOT_TOKEN": "token",
                "DISCORD_CHANNEL_ID": "legacy-channel",
                "DISCORD_MAIN_CHANNEL_ID": "main-channel",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        assert config.discord_main_channel_id == "main-channel"


# ---------------------------------------------------------------------------
# validate() backward compatibility
# ---------------------------------------------------------------------------

class TestValidateBackwardCompat:
    def test_returns_list_of_str(self):
        cfg = _make_config()
        result = cfg.validate()
        assert isinstance(result, list)
        assert all(isinstance(s, str) for s in result)

    def test_empty_llm_model_list_message_in_validate(self):
        cfg = _make_config(llm_model_list=[])
        messages = cfg.validate()
        assert any("LLM" in m for m in messages)

    def test_messages_match_validate_structured(self):
        """validate() strings must be the message field of each ConfigIssue."""
        cfg = _make_config(llm_model_list=[], stock_list=[])
        structured = cfg.validate_structured()
        plain = cfg.validate()
        assert plain == [i.message for i in structured]
