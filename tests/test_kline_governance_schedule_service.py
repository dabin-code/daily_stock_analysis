import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from src.services.kline_governance_schedule_service import KlineGovernanceScheduleService


class KlineGovernanceScheduleServiceTestCase(unittest.TestCase):
    @staticmethod
    def _build_config(**overrides):
        config = SimpleNamespace(
            kline_audit_lookback_days=30,
            kline_deep_audit_lookback_days=365,
            kline_skip_candidate_failure_threshold=3,
        )
        for key, value in overrides.items():
            setattr(config, key, value)
        return config

    def test_kline_governance_schedule_fails_closed_when_calendar_unavailable(self) -> None:
        service = KlineGovernanceScheduleService(config=self._build_config())

        with patch(
            "src.services.kline_governance_schedule_service._XCALS_AVAILABLE",
            False,
        ):
            with self.assertRaisesRegex(RuntimeError, "calendar"):
                service.resolve_target_trade_date(trade_date=None, market="cn")

    def test_kline_governance_schedule_runs_sync_then_audit_then_repair_then_reaudit(self) -> None:
        sync_service = mock.MagicMock()
        audit_service = mock.MagicMock()
        repair_service = mock.MagicMock()
        skip_policy_service = mock.MagicMock()
        skip_policy_service.apply_auto_skip.return_value = {"approved_count": 0}
        call_order = []

        target_trade_date = date(2026, 4, 17)

        def _run_sync(**kwargs):
            call_order.append("sync")
            return {"target_trade_date": target_trade_date.isoformat()}

        audit_results = iter(
            [
                {"run_id": "audit-run-1", "run_result": "succeeded", "pass_status": "passed"},
                {"run_id": "audit-run-2", "run_result": "succeeded", "pass_status": "passed"},
            ]
        )

        def _run_audit(**kwargs):
            if len([step for step in call_order if step.startswith("audit")]) == 0:
                call_order.append("audit")
            else:
                call_order.append("reaudit")
            return next(audit_results)

        def _run_repair(**kwargs):
            call_order.append("repair")
            return {
                "repaired_gap_count": 1,
                "candidate_skip_gap_count": 0,
                "recovered_gap_count": 0,
            }

        def _run_auto_skip(**kwargs):
            call_order.append("auto_skip")
            return {"approved_count": 0}

        sync_service.sync_trade_date.side_effect = _run_sync
        audit_service.audit_trade_date.side_effect = _run_audit
        repair_service.repair_gaps.side_effect = _run_repair
        skip_policy_service.apply_auto_skip.side_effect = _run_auto_skip
        db_manager = mock.MagicMock()
        db_manager.list_kline_audit_gaps.return_value = []

        service = KlineGovernanceScheduleService(
            config=self._build_config(kline_audit_lookback_days=10),
            market_data_sync_service=sync_service,
            audit_service=audit_service,
            repair_service=repair_service,
            skip_policy_service=skip_policy_service,
            db_manager=db_manager,
        )

        with patch.object(service, "resolve_target_trade_date", return_value=target_trade_date):
            result = service.run_daily_governance()

        self.assertEqual(call_order, ["sync", "audit", "repair", "auto_skip", "reaudit", "repair"])
        sync_service.sync_trade_date.assert_called_once_with(
            trade_date=target_trade_date,
            force=False,
        )
        first_audit_call = audit_service.audit_trade_date.call_args_list[0]
        self.assertEqual(first_audit_call.kwargs["market"], "cn")
        self.assertEqual(first_audit_call.kwargs["trade_date"], target_trade_date)
        self.assertEqual(first_audit_call.kwargs["window_start"], date(2026, 4, 8))
        self.assertEqual(first_audit_call.kwargs["window_end"], target_trade_date)
        first_repair_call, second_repair_call = repair_service.repair_gaps.call_args_list
        assert first_repair_call.kwargs == {
            "market": "cn",
            "governance_run_succeeded": False,
            "governance_run_id": "audit-run-1",
            "included_statuses": {"pending_retry", "candidate_skip"},
        }
        assert second_repair_call.kwargs == {
            "market": "cn",
            "governance_run_succeeded": True,
            "governance_run_id": "audit-run-2",
            "included_statuses": {"approved_skip"},
        }
        self.assertEqual(result["approved_skip_recovery_result"]["repaired_gap_count"], 1)
        self.assertEqual(result["approved_skip_recovery_result"]["recovered_gap_count"], 0)
        self.assertEqual(result["approved_skip_recovery_result"]["candidate_skip_gap_count"], 0)
        self.assertEqual(result["repair_result"]["repaired_gap_count"], 1)
        self.assertEqual(result["repair_result"]["candidate_skip_gap_count"], 0)
        self.assertEqual(result["repair_result"]["recovered_gap_count"], 0)
        skip_policy_service.apply_auto_skip.assert_called_once_with(
            market="cn",
            source_run_id="audit-run-1",
            trade_date=target_trade_date,
            sync_result={"target_trade_date": target_trade_date.isoformat()},
        )
        self.assertEqual(result["run_result"], "succeeded")
        self.assertEqual(result["pass_status"], "passed")
        self.assertEqual(result["trade_date"], target_trade_date)

    def test_run_daily_governance_skips_non_trading_day_without_crashing_scheduler(self) -> None:
        service = KlineGovernanceScheduleService(config=self._build_config())

        with patch.object(
            service,
            "resolve_target_trade_date",
            side_effect=RuntimeError("non-trading day; explicit trade_date is required for K-line governance"),
        ):
            result = service.run_daily_governance()

        self.assertEqual(result["run_result"], "skipped")
        self.assertEqual(result["pass_status"], "skipped")
        self.assertEqual(result["reason"], "non_trading_day")

    def test_run_deep_audit_skips_non_trading_day_without_crashing_scheduler(self) -> None:
        service = KlineGovernanceScheduleService(config=self._build_config())

        with patch.object(
            service,
            "resolve_target_trade_date",
            side_effect=RuntimeError("non-trading day; explicit trade_date is required for K-line governance"),
        ):
            result = service.run_deep_audit()

        self.assertEqual(result["run_result"], "skipped")
        self.assertEqual(result["pass_status"], "skipped")
        self.assertEqual(result["reason"], "non_trading_day")

    def test_kline_governance_schedule_promotes_current_open_gaps_before_repair(self) -> None:
        sync_service = mock.MagicMock()
        sync_service.sync_trade_date.return_value = {"target_trade_date": "2026-04-17"}
        audit_service = mock.MagicMock()
        audit_service.audit_trade_date.side_effect = [
            {"run_id": "audit-run-1", "run_result": "degraded", "pass_status": "not_passed"},
            {"run_id": "audit-run-2", "run_result": "degraded", "pass_status": "not_passed"},
        ]
        repair_service = mock.MagicMock()
        repair_service.repair_gaps.return_value = {
            "repaired_gap_count": 1,
            "candidate_skip_gap_count": 0,
            "recovered_gap_count": 0,
        }
        db_manager = mock.MagicMock()
        db_manager.list_kline_audit_gaps.return_value = [
            SimpleNamespace(
                market="cn",
                gap_scope="market_day_gap",
                code=None,
                trade_date=date(2026, 4, 17),
                missing_date_from=None,
                missing_date_to=None,
                source_run_id="audit-run-1",
                status="open",
            ),
            SimpleNamespace(
                market="cn",
                gap_scope="market_day_gap",
                code=None,
                trade_date=date(2026, 4, 16),
                missing_date_from=None,
                missing_date_to=None,
                source_run_id="older-run",
                status="open",
            ),
        ]

        service = KlineGovernanceScheduleService(
            config=self._build_config(),
            market_data_sync_service=sync_service,
            audit_service=audit_service,
            repair_service=repair_service,
            db_manager=db_manager,
        )

        with patch.object(service, "resolve_target_trade_date", return_value=date(2026, 4, 17)):
            service.run_daily_governance()

        db_manager.upsert_kline_audit_gap.assert_called_once_with(
            market="cn",
            gap_scope="market_day_gap",
            code=None,
            trade_date=date(2026, 4, 17),
            missing_date_from=None,
            missing_date_to=None,
            source_run_id="audit-run-1",
            status="pending_retry",
        )
        self.assertEqual(repair_service.repair_gaps.call_count, 2)
        first_repair_call, second_repair_call = repair_service.repair_gaps.call_args_list
        assert first_repair_call.kwargs == {
            "market": "cn",
            "governance_run_succeeded": False,
            "governance_run_id": "audit-run-1",
            "included_statuses": {"pending_retry", "candidate_skip"},
        }
        assert second_repair_call.kwargs == {
            "market": "cn",
            "governance_run_succeeded": False,
            "governance_run_id": "audit-run-2",
            "included_statuses": {"approved_skip"},
        }

    def test_deep_audit_job_scans_long_window_and_rechecks_candidate_skips(self) -> None:
        audit_service = mock.MagicMock()
        audit_service.audit_trade_date.side_effect = [
            {
                "run_id": "deep-audit-1",
                "run_result": "degraded",
                "pass_status": "not_passed",
            },
            {
                "run_id": "deep-audit-2",
                "run_result": "succeeded",
                "pass_status": "passed",
            },
        ]
        repair_service = mock.MagicMock()
        repair_service._recover_candidate_skip.side_effect = [True, False]
        target_trade_date = date(2026, 4, 17)
        preserved_truth = SimpleNamespace(
            market="cn",
            trade_date=target_trade_date,
            pass_status="passed",
            window_start=date(2026, 3, 19),
            window_end=target_trade_date,
            rule_version="daily-v1",
            source_run_id="daily-run-1",
            passed_at=None,
        )
        db_manager = mock.MagicMock()
        db_manager.get_kline_audit_trade_date.return_value = preserved_truth
        db_manager.list_kline_audit_gaps.return_value = [
            SimpleNamespace(status="candidate_skip", gap_key="gap-1"),
            SimpleNamespace(status="candidate_skip", gap_key="gap-2"),
            SimpleNamespace(status="approved_skip", gap_key="gap-3"),
        ]

        service = KlineGovernanceScheduleService(
            config=self._build_config(kline_deep_audit_lookback_days=365),
            audit_service=audit_service,
            repair_service=repair_service,
            db_manager=db_manager,
        )

        with patch.object(service, "resolve_target_trade_date", return_value=target_trade_date):
            result = service.run_deep_audit()

        self.assertEqual(audit_service.audit_trade_date.call_count, 2)
        first_audit_call = audit_service.audit_trade_date.call_args_list[0]
        self.assertEqual(first_audit_call.kwargs["market"], "cn")
        self.assertEqual(first_audit_call.kwargs["trade_date"], target_trade_date)
        self.assertEqual(first_audit_call.kwargs["window_start"], date(2025, 4, 18))
        self.assertEqual(first_audit_call.kwargs["window_end"], target_trade_date)
        db_manager.list_kline_audit_gaps.assert_called_once_with(market="cn", status="candidate_skip")
        self.assertEqual(repair_service._recover_candidate_skip.call_count, 2)
        self.assertEqual(result["run_result"], "succeeded")
        self.assertEqual(result["pass_status"], "passed")
        self.assertEqual(result["candidate_skip_rechecked"], 2)
        self.assertEqual(result["candidate_skip_recovered_count"], 1)
        db_manager.upsert_kline_audit_trade_date.assert_called_once_with(
            market="cn",
            trade_date=target_trade_date,
            pass_status="passed",
            window_start=date(2026, 3, 19),
            window_end=target_trade_date,
            rule_version="daily-v1",
            source_run_id="daily-run-1",
            passed_at=None,
        )

    def test_deep_audit_fails_closed_when_daily_governance_truth_missing(self) -> None:
        service = KlineGovernanceScheduleService(
            config=self._build_config(),
            audit_service=mock.MagicMock(),
            repair_service=mock.MagicMock(),
            db_manager=mock.MagicMock(get_kline_audit_trade_date=mock.MagicMock(return_value=None)),
        )

        with patch.object(service, "resolve_target_trade_date", return_value=date(2026, 4, 17)):
            with self.assertRaisesRegex(RuntimeError, "daily governance truth is unavailable"):
                service.run_deep_audit()

    def test_run_daily_governance_with_catch_up_runs_each_missing_session_then_target(self) -> None:
        db_manager = mock.MagicMock()
        db_manager.get_latest_passed_kline_audit_trade_date.return_value = SimpleNamespace(
            trade_date=date(2026, 6, 26)
        )
        service = KlineGovernanceScheduleService(
            config=self._build_config(kline_governance_max_catch_up_sessions=30),
            market_data_sync_service=mock.MagicMock(),
            audit_service=mock.MagicMock(),
            repair_service=mock.MagicMock(),
            skip_policy_service=mock.MagicMock(),
            db_manager=db_manager,
        )

        governance_calls = []

        def _run_daily(*, trade_date, market):
            governance_calls.append(trade_date)
            return {"trade_date": trade_date, "run_result": "succeeded", "pass_status": "passed"}

        with patch.object(service, "run_daily_governance", side_effect=_run_daily), patch(
            "src.services.kline_governance_schedule_service.KlineAuditService._is_market_session",
            side_effect=lambda *, market, trade_date: trade_date.weekday() < 5,
        ):
            result = service.run_daily_governance_with_catch_up(
                trade_date=date(2026, 7, 2),
                market="cn",
            )

        # 06-26 通过后，逐日补跑 06-29/06-30/07-01（跳过周末），最后跑目标日 07-02
        self.assertEqual(
            governance_calls,
            [date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1), date(2026, 7, 2)],
        )
        self.assertEqual(
            result["catch_up_dates"],
            ["2026-06-29", "2026-06-30", "2026-07-01"],
        )
        self.assertEqual(len(result["catch_up_results"]), 3)
        self.assertEqual(result["pass_status"], "passed")
        self.assertEqual(result["trade_date"], date(2026, 7, 2))

    def test_run_daily_governance_with_catch_up_respects_session_cap(self) -> None:
        db_manager = mock.MagicMock()
        # 没有任何通过日：起点缺失时靠 cap 限制回溯范围
        db_manager.get_latest_passed_kline_audit_trade_date.return_value = None
        service = KlineGovernanceScheduleService(
            config=self._build_config(kline_governance_max_catch_up_sessions=2),
            market_data_sync_service=mock.MagicMock(),
            audit_service=mock.MagicMock(),
            repair_service=mock.MagicMock(),
            skip_policy_service=mock.MagicMock(),
            db_manager=db_manager,
        )

        governance_calls = []

        def _run_daily(*, trade_date, market):
            governance_calls.append(trade_date)
            return {"trade_date": trade_date, "run_result": "succeeded", "pass_status": "passed"}

        with patch.object(service, "run_daily_governance", side_effect=_run_daily), patch(
            "src.services.kline_governance_schedule_service.KlineAuditService._is_market_session",
            side_effect=lambda *, market, trade_date: trade_date.weekday() < 5,
        ):
            service.run_daily_governance_with_catch_up(
                trade_date=date(2026, 7, 2),
                market="cn",
            )

        # cap=2：最多补跑 2 个交易日（07-01、06-30），再加目标日 07-02
        self.assertEqual(
            governance_calls,
            [date(2026, 6, 30), date(2026, 7, 1), date(2026, 7, 2)],
        )

    def test_kline_governance_schedule_does_not_reuse_previous_session_on_closed_day(self) -> None:
        service = KlineGovernanceScheduleService(config=self._build_config())
        calendar = mock.MagicMock()
        calendar.is_session.return_value = False

        with patch(
            "src.services.kline_governance_schedule_service._XCALS_AVAILABLE",
            True,
        ), patch(
            "src.services.kline_governance_schedule_service.trading_calendar.xcals.get_calendar",
            return_value=calendar,
        ):
            with self.assertRaisesRegex(RuntimeError, "non-trading day"):
                service.resolve_target_trade_date(
                    trade_date=None,
                    market="cn",
                    now=datetime(2026, 4, 18, 18, 0, 0),
                )

        calendar.previous_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
