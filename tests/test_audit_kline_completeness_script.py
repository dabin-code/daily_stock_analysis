import io
from contextlib import redirect_stdout
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scripts import audit_kline_completeness


def test_audit_kline_completeness_script_supports_dry_run_and_repair_modes():
    config = SimpleNamespace(kline_audit_lookback_days=30)
    governance_service = MagicMock()
    governance_service.resolve_target_trade_date.return_value = date(2026, 4, 17)
    governance_service.run_daily_governance.return_value = {
        "trade_date": date(2026, 4, 17),
        "run_result": "succeeded",
        "pass_status": "passed",
    }

    with patch.object(audit_kline_completeness, "get_config", return_value=config), patch.object(
        audit_kline_completeness.DatabaseManager,
        "get_instance",
        return_value=MagicMock(),
    ), patch.object(
        audit_kline_completeness,
        "KlineGovernanceScheduleService",
        return_value=governance_service,
    ), patch.object(audit_kline_completeness, "KlineAuditService") as audit_service_cls:
        dry_run_stdout = io.StringIO()
        with redirect_stdout(dry_run_stdout):
            exit_code = audit_kline_completeness.main(["--trade-date", "2026-04-17", "--dry-run"])

        assert exit_code == 0
        assert "mode=audit" in dry_run_stdout.getvalue()
        assert "trade_date=2026-04-17" in dry_run_stdout.getvalue()
        governance_service.run_daily_governance.assert_not_called()
        audit_service_cls.return_value.audit_trade_date.assert_not_called()

        repair_stdout = io.StringIO()
        with redirect_stdout(repair_stdout):
            exit_code = audit_kline_completeness.main(["--trade-date", "2026-04-17", "--repair"])

        assert exit_code == 0
        governance_service.run_daily_governance.assert_called_once_with(
            trade_date=date(2026, 4, 17),
            market="cn",
        )
        assert "mode=repair" in repair_stdout.getvalue()
        assert "pass_status=passed" in repair_stdout.getvalue()


def test_audit_kline_completeness_script_can_target_explicit_trade_date():
    config = SimpleNamespace(kline_audit_lookback_days=30)
    governance_service = MagicMock()
    audit_service = MagicMock()
    audit_service.audit_trade_date.return_value = {
        "trade_date": date(2026, 4, 17),
        "run_result": "succeeded",
        "pass_status": "passed",
    }

    with patch.object(audit_kline_completeness, "get_config", return_value=config), patch.object(
        audit_kline_completeness.DatabaseManager,
        "get_instance",
        return_value=MagicMock(),
    ), patch.object(
        audit_kline_completeness,
        "KlineGovernanceScheduleService",
        return_value=governance_service,
    ), patch.object(
        audit_kline_completeness,
        "KlineAuditService",
        return_value=audit_service,
    ):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = audit_kline_completeness.main(["--trade-date", "2026-04-17"])

    assert exit_code == 0
    governance_service.resolve_target_trade_date.assert_not_called()
    audit_service.audit_trade_date.assert_called_once_with(
        market="cn",
        trade_date=date(2026, 4, 17),
        window_start=date(2026, 3, 19),
        window_end=date(2026, 4, 17),
        run_type="manual_audit",
        trigger_type="manual",
    )
    assert "mode=audit" in stdout.getvalue()
    assert "trade_date=2026-04-17" in stdout.getvalue()


def test_audit_kline_completeness_script_returns_non_zero_when_governance_not_passed():
    config = SimpleNamespace(kline_audit_lookback_days=30)
    governance_service = MagicMock()
    governance_service.run_daily_governance.return_value = {
        "trade_date": date(2026, 4, 17),
        "run_result": "degraded",
        "pass_status": "not_passed",
    }

    with patch.object(audit_kline_completeness, "get_config", return_value=config), patch.object(
        audit_kline_completeness.DatabaseManager,
        "get_instance",
        return_value=MagicMock(),
    ), patch.object(
        audit_kline_completeness,
        "KlineGovernanceScheduleService",
        return_value=governance_service,
    ):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = audit_kline_completeness.main(["--trade-date", "2026-04-17", "--repair"])

    assert exit_code == 1
    assert "pass_status=not_passed" in stdout.getvalue()
