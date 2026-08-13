# -*- coding: utf-8 -*-
"""上市/退市生命周期同步脚本的 CLI 测试。

这个脚本的运行成本是硬约束：tushare stock_basic 在本账号免费额度下实测被限到
5 次/天，一次完整同步要 3 次。所以「--dry-run 一次调用都不发」不是锦上添花的
特性，而是操作员唯一能免费确认计划的手段——本文件把它当回归防线来测。
"""

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import sync_listing_lifecycle


class _RecordingService:
    """记录服务层被怎么调用；构造本身也记一笔。

    dry-run 用例断言的是这份记录为空。用记录而不是 MagicMock：断言失败时能直接
    看到「谁在 dry-run 里被调了」，而不是一句 assert_not_called。
    """

    def __init__(self, stats=None, error=None):
        self.calls = []
        self._stats = stats or {"total": 0, "written": 0, "delisted": 0}
        self._error = error

    def sync_from_tushare(self, list_statuses=None, pause_seconds=None, **kwargs):
        self.calls.append(
            {
                "method": "sync_from_tushare",
                "list_statuses": list_statuses,
                "pause_seconds": pause_seconds,
            }
        )
        if self._error is not None:
            raise self._error
        return self._stats

    def sync_from_baostock(self):
        self.calls.append({"method": "sync_from_baostock"})
        if self._error is not None:
            raise self._error
        return self._stats


class SyncListingLifecycleScriptTestCase(unittest.TestCase):
    """全部离线：服务层被替换成 _RecordingService，永远不碰上游也不碰生产库。"""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.db_path = Path(self._temp_dir.name) / "instruments.db"

        # 库路径必须指向临时库：脚本跑完会去读 instrument_master 统计覆盖度，
        # 默认路径会让它读到会话库甚至生产库。
        self._previous_db_path = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(self.db_path)
        self.addCleanup(self._restore_database_path)

    def _restore_database_path(self) -> None:
        if self._previous_db_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self._previous_db_path

    def _seed_instrument_master(self) -> None:
        """建一张最小的 instrument_master，用于验证覆盖度统计。

        只放同步脚本读的三列：待验证的是脚本的取证查询，不是 ORM 的建表。
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "CREATE TABLE instrument_master ("
                "code TEXT PRIMARY KEY, list_date DATE, delist_date DATE)"
            )
            conn.executemany(
                "INSERT INTO instrument_master (code, list_date, delist_date) VALUES (?, ?, ?)",
                [
                    ("600000", "1999-11-10", None),
                    ("000033", "1994-01-03", "2016-05-26"),
                    ("000001", None, None),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def _run_cli(self, *argv, service=None):
        """跑一遍 CLI，返回 (exit_code, 合并后的 stdout+stderr, 被注入的假服务)。

        stderr 一起收：[error] / [hint] 按兄弟脚本的约定走 stderr。
        """
        fake = service if service is not None else _RecordingService()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(
            sync_listing_lifecycle, "ListingLifecycleService", lambda *a, **kw: fake
        ):
            with patch.object(
                sync_listing_lifecycle.sys,
                "argv",
                ["sync_listing_lifecycle.py", *argv],
            ):
                with contextlib.redirect_stdout(stdout):
                    with contextlib.redirect_stderr(stderr):
                        exit_code = sync_listing_lifecycle.main()
        return exit_code, stdout.getvalue() + stderr.getvalue(), fake

    # ── dry-run：零调用 ────────────────────────────────────────────────
    def test_dry_run_makes_no_call_into_the_service(self) -> None:
        """dry-run 必须一次都不进服务层，也不建库。

        这是本文件最吃重的一条：额度是 5 次/天，dry-run 一旦「顺手」落到真实
        调用，操作员每确认一次计划就烧掉 3 次额度。把 dry-run 的 return 去掉，
        这条用例必须红。
        """
        exit_code, output, fake = self._run_cli("--dry-run")

        self.assertEqual(fake.calls, [], f"dry-run 发生了真实调用: {fake.calls}")
        self.assertEqual(exit_code, 0)
        self.assertFalse(
            self.db_path.exists(),
            "dry-run 不该建库：建库说明已经走到了服务层",
        )
        self.assertIn("[dry-run]", output)

    def test_dry_run_reports_api_call_count_for_default_statuses(self) -> None:
        """默认 L,D,P 是 3 次调用，必须原样打出来供操作员核对。"""
        exit_code, output, _ = self._run_cli("--dry-run")

        self.assertEqual(exit_code, 0)
        self.assertIn("3 API call(s)", output)
        self.assertIn("L, D, P", output)

    def test_dry_run_reports_api_call_count_for_custom_statuses(self) -> None:
        """状态数变了，次数必须跟着变——写死 3 会让 --list-statuses 的代价失真。"""
        exit_code, output, fake = self._run_cli("--dry-run", "--list-statuses", "L,D")

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake.calls, [])
        self.assertIn("2 API call(s)", output)
        self.assertNotIn("3 API call(s)", output)

    def test_dry_run_echoes_resolved_database_path(self) -> None:
        """预检要回显真正会被写的库文件，否则确认的是一个看不见目标的计划。"""
        _, output, _ = self._run_cli("--dry-run")

        self.assertIn(f"[info] Target database: {self.db_path}", output)

    # ── 成功路径 ──────────────────────────────────────────────────────
    def test_successful_run_passes_parsed_statuses_and_pause(self) -> None:
        """解析出的状态与 pause_seconds 必须原样透传给服务层。"""
        self._seed_instrument_master()
        service = _RecordingService(stats={"total": 5, "written": 5, "delisted": 2})

        exit_code, output, fake = self._run_cli(
            "--list-statuses", "L,D", "--pause-seconds", "1.5", service=service
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            fake.calls,
            [
                {
                    "method": "sync_from_tushare",
                    "list_statuses": ["L", "D"],
                    "pause_seconds": 1.5,
                }
            ],
        )
        self.assertIn("total=5 written=5 delisted=2", output)

    def test_successful_run_reports_delist_date_coverage_from_the_database(self) -> None:
        """覆盖度必须来自库里的实际行数，而不是服务返回的写入计数。

        证明幸存者偏差可纠正的是「库里此刻存着多少个退市日期」；用 written 冒充
        它，一个 delist_date 全为 NULL 的库也会报告成功。
        """
        self._seed_instrument_master()
        service = _RecordingService(stats={"total": 3, "written": 3, "delisted": 1})

        exit_code, output, _ = self._run_cli(service=service)

        self.assertEqual(exit_code, 0)
        self.assertIn("instrument_master rows with list_date: 2", output)
        self.assertIn("instrument_master rows with delist_date: 1", output)

    def test_unreadable_database_does_not_fail_a_successful_sync(self) -> None:
        """覆盖度读不到只能降级成告警。

        取证查询失败（库还没建、表还没建、库被锁）时返回非零退出码，会让一次
        已经花掉额度、也确实写成功的同步被判成失败，操作员的下一步是重跑——
        而重跑再花一遍额度。
        """
        service = _RecordingService(stats={"total": 3, "written": 3, "delisted": 1})

        exit_code, output, _ = self._run_cli(service=service)

        self.assertEqual(exit_code, 0)
        self.assertIn("[warn]", output)
        self.assertIn("total=3 written=3 delisted=1", output)

    # ── 参数冲突 ──────────────────────────────────────────────────────
    def test_baostock_with_list_statuses_is_rejected(self) -> None:
        """baostock 不分状态；显式传了 --list-statuses 必须报错而不是静默忽略。

        静默忽略会让操作员以为自己限定了抓取范围，而这个脚本的产出正是样本范围。
        """
        exit_code, output, fake = self._run_cli(
            "--source", "baostock", "--list-statuses", "L,D"
        )

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(fake.calls, [], "参数冲突必须在任何调用之前拦下")
        self.assertIn("[error]", output)
        self.assertIn("--list-statuses", output)

    # ── 失败路径 ──────────────────────────────────────────────────────
    def test_runtime_error_exits_non_zero_with_error_line(self) -> None:
        """服务层报错必须转成 [error] + 非零退出码，不甩 traceback。"""
        service = _RecordingService(
            error=RuntimeError("tushare stock_basic fetch failed for list_status=D")
        )

        exit_code, output, _ = self._run_cli(service=service)

        self.assertNotEqual(exit_code, 0)
        self.assertIn("[error]", output)
        self.assertIn("list_status=D", output)

    def test_rate_limited_error_adds_the_quota_hint(self) -> None:
        """限频文案要额外给出「额度可能已耗尽、立刻重跑只会更亏」的提示。

        没有这条提示，操作员的第一反应是立刻重跑，而重跑同样烧额度。
        """
        service = _RecordingService(
            error=RuntimeError(
                "tushare stock_basic failed for list_status=L: "
                "抱歉，您访问接口(stock_basic)频率超限(5次/天)"
            )
        )

        exit_code, output, _ = self._run_cli(service=service)

        self.assertNotEqual(exit_code, 0)
        self.assertIn("[hint]", output)
        self.assertIn("quota", output)

    def test_plain_failure_does_not_claim_a_quota_problem(self) -> None:
        """非限频失败不能挂上额度提示：假归因会让操作员白等一天。"""
        service = _RecordingService(error=ValueError("nothing to fetch"))

        exit_code, output, _ = self._run_cli(service=service)

        self.assertNotEqual(exit_code, 0)
        self.assertNotIn("[hint]", output)


class SyncListingLifecycleHelpersTestCase(unittest.TestCase):
    """模块级辅助函数，不需要库也不需要参数解析。"""

    def test_planned_api_calls_follows_status_count(self) -> None:
        self.assertEqual(
            sync_listing_lifecycle.planned_api_calls("tushare", ["L", "D", "P"]), 3
        )
        self.assertEqual(sync_listing_lifecycle.planned_api_calls("tushare", ["L"]), 1)
        # baostock 一次返回全市场（含已退市），与状态数无关
        self.assertEqual(
            sync_listing_lifecycle.planned_api_calls("baostock", ["L", "D", "P"]), 1
        )

    def test_parse_list_statuses_normalizes_and_drops_blanks(self) -> None:
        self.assertEqual(sync_listing_lifecycle.parse_list_statuses("l, d ,p"), ["L", "D", "P"])
        self.assertEqual(sync_listing_lifecycle.parse_list_statuses("L,,D,"), ["L", "D"])
        self.assertEqual(sync_listing_lifecycle.parse_list_statuses(" , "), [])

    def test_looks_rate_limited_recognizes_the_observed_rejection_text(self) -> None:
        self.assertTrue(
            sync_listing_lifecycle.looks_rate_limited(
                "抱歉，您访问接口(stock_basic)频率超限(5次/天)"
            )
        )
        self.assertTrue(
            sync_listing_lifecycle.looks_rate_limited("抱歉，您每小时最多访问该接口1次")
        )
        self.assertFalse(sync_listing_lifecycle.looks_rate_limited("network unreachable"))

    def test_default_db_path_honours_env(self) -> None:
        previous = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = "/tmp/elsewhere.db"
        try:
            self.assertEqual(
                sync_listing_lifecycle.default_db_path(), Path("/tmp/elsewhere.db")
            )
        finally:
            if previous is None:
                os.environ.pop("DATABASE_PATH", None)
            else:
                os.environ["DATABASE_PATH"] = previous


if __name__ == "__main__":
    unittest.main()
