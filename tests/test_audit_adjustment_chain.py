# -*- coding: utf-8 -*-
"""复权链离线抽查脚本的行为测试。

链路本身的语义（累乘方向、归一、切段、fail-closed、上下界）全部在
``tests/test_adjustment_chain.py`` 里测，这里只测脚本层剩下的三件事：

1. 它真的不写库——v2 不落库，只读是这个脚本的核心契约
2. 它只看 ``adj_convention='raw'`` 的行
3. 断链代码有没有被点名，否则操作员无从知道哪只股票会被切段

全部用临时库。生产库 data/stock_analysis.db 曾被并发测试写坏过一次，conftest.py
里有护栏会让任何打开它的用例直接失败；本文件不依赖那道护栏兜底，始终显式传 --db。
"""

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import audit_adjustment_chain

# 建一张只含脚本读取的列的最小表。用手写 DDL 而不是走 ORM：待验证的是脚本的 SQL，
# 让建表依赖 src.storage 会把无关的模型变更卷进这份测试的失败原因里。
_DDL = """
CREATE TABLE stock_daily (
    id INTEGER PRIMARY KEY,
    code VARCHAR(10) NOT NULL,
    date DATE NOT NULL,
    close FLOAT,
    pre_close FLOAT,
    adj_factor FLOAT,
    adj_anchor_date DATE,
    adj_factor_source VARCHAR(32),
    adj_convention VARCHAR(16),
    CONSTRAINT uix_code_date UNIQUE (code, date)
)
"""


class _Fixture:
    def __init__(self, path: Path) -> None:
        self.path = path
        con = sqlite3.connect(str(path))
        try:
            con.execute(_DDL)
            con.commit()
        finally:
            con.close()

    def seed(self, code, bars, convention="raw"):
        """bars 是 (date, close, pre_close) 序列，日期按传入顺序即为时间序。"""
        con = sqlite3.connect(str(self.path))
        try:
            con.executemany(
                "INSERT INTO stock_daily (code, date, close, pre_close,"
                " adj_convention) VALUES (?, ?, ?, ?, ?)",
                [
                    (code, date, close, pre_close, convention)
                    for date, close, pre_close in bars
                ],
            )
            con.commit()
        finally:
            con.close()

    def snapshot(self):
        con = sqlite3.connect(str(self.path))
        try:
            return con.execute(
                "SELECT code, date, close, pre_close, adj_factor,"
                " adj_factor_source, adj_anchor_date FROM stock_daily"
                " ORDER BY code, date"
            ).fetchall()
        finally:
            con.close()


def _bars(closes, pre_closes, start=2):
    dates = [f"2024-01-{start + i:02d}" for i in range(len(closes))]
    return list(zip(dates, closes, pre_closes))


def _directional_gap_bars():
    """比值 0.98：pre_close 高于前一日收盘，方向上分红送转做不出来。"""
    return _bars([10.0, 9.8, 10.1, 10.2], [9.9, 10.0, 10.0, 10.1])


class AuditAdjustmentChainCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.db_path = Path(self._temp_dir.name) / "adj.db"
        self.fixture = _Fixture(self.db_path)

    def run_cli(self, *extra):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with contextlib.redirect_stderr(stderr):
                exit_code = audit_adjustment_chain.main(
                    ["--db", str(self.db_path), *extra]
                )
        return exit_code, stdout.getvalue() + stderr.getvalue()

    def audit(self, *extra):
        exit_code, output = self.run_cli(*extra)
        self.assertEqual(exit_code, 0, output)
        return output

    # ── 只读契约 ──────────────────────────────────────────────────────
    def test_the_audit_writes_nothing(self) -> None:
        """v2 不落库。抽查跑完，库里必须一个字节都没变。"""
        self.fixture.seed("600001", _bars([10.0, 10.0, 9.2, 9.5],
                                          [9.9, 10.0, 9.0, 9.2]))
        self.fixture.seed("920089", _directional_gap_bars())
        before = self.fixture.snapshot()

        self.audit()

        self.assertEqual(self.fixture.snapshot(), before)

    def test_the_connection_refuses_writes(self) -> None:
        """「不写库」由连接模式兜底，不靠代码里的 if 分支。

        单靠分支守卫时，漏写的 UPDATE 会被 close() 的隐式回滚吞掉，于是「抽查写了库」
        这个 bug 在行为测试里完全看不出来——直到某天有人补了一句 commit。
        """
        self.fixture.seed("600001", _bars([10.0, 11.0], [9.9, 10.0]))
        con = audit_adjustment_chain.connect_readonly(str(self.db_path))
        try:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("UPDATE stock_daily SET adj_factor = 1.0")
                con.commit()
        finally:
            con.close()

    # ── 取数范围 ──────────────────────────────────────────────────────
    def test_only_raw_convention_rows_are_audited(self) -> None:
        """指数与港股的 adj_convention 不是 raw，它们的 pre_close 链没验证过。"""
        self.fixture.seed(
            "sh000001",
            _bars([3000.0, 3010.0], [2990.0, 3000.0]),
            convention=None,
        )

        exit_code, output = self.run_cli()

        self.assertNotEqual(exit_code, 0, "没有 raw 行时应报错而不是静默成功")
        self.assertIn("[error]", output)

    def test_explicit_codes_override_the_full_scan(self) -> None:
        self.fixture.seed("600001", _bars([10.0, 11.0], [9.9, 10.0]))
        self.fixture.seed("600002", _bars([20.0, 21.0], [19.9, 20.0]))

        output = self.audit("--codes", "600001")

        self.assertIn("待抽查代码: 1", output)
        self.assertIn("已解链 1 个代码 / 2 行", output)

    def test_limit_caps_the_number_of_codes(self) -> None:
        self.fixture.seed("600001", _bars([10.0, 11.0], [9.9, 10.0]))
        self.fixture.seed("600002", _bars([20.0, 21.0], [19.9, 20.0]))

        output = self.audit("--limit", "1")

        self.assertIn("待抽查代码: 1", output)

    # ── 画像 ──────────────────────────────────────────────────────────
    def test_broken_codes_are_named_with_their_first_break(self) -> None:
        """断链代码必须点名，否则操作员无从知道哪只股票会被切段。"""
        self.fixture.seed("920089", _directional_gap_bars())

        output = self.audit()

        self.assertIn("920089", output)
        self.assertIn("ratio_below_floor", output)
        self.assertIn("有断链的代码: 1 / 1", output)
        # 0.98 那处断链在第 2 行，因此前两行被作废、后两行照常施加。
        self.assertIn("被切段作废的行: 2 / 4", output)

    def test_a_clean_universe_reports_no_cuts(self) -> None:
        self.fixture.seed(
            "600001", _bars([10.0, 10.0, 9.2, 9.5], [9.9, 10.0, 9.0, 9.2])
        )

        output = self.audit()

        self.assertIn("有断链的代码: 0 / 1", output)
        self.assertNotIn("断链代码清单", output)

    def test_ratio_distribution_separates_events_from_quiet_days(self) -> None:
        """一次 10 送 10：三行观测比值里两行为 1，一行落在送转区间。"""
        self.fixture.seed(
            "600004", _bars([20.0, 20.0, 10.5, 11.0], [19.5, 20.0, 10.0, 10.5])
        )

        output = self.audit()

        self.assertIn("单日复权比值分布（合计 3）", output)
        self.assertIn("总复权倍数分布（按代码）（合计 1）", output)

    def test_missing_database_exits_non_zero(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        missing = Path(self._temp_dir.name) / "nope.db"
        with contextlib.redirect_stdout(stdout):
            with contextlib.redirect_stderr(stderr):
                exit_code = audit_adjustment_chain.main(["--db", str(missing)])
        self.assertNotEqual(exit_code, 0)
        self.assertIn("[error]", stdout.getvalue() + stderr.getvalue())


class DefaultDbPathTestCase(unittest.TestCase):
    def test_env_wins_over_the_built_in_default(self) -> None:
        previous = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = "/tmp/elsewhere.db"
        try:
            self.assertEqual(
                audit_adjustment_chain.default_db_path(), "/tmp/elsewhere.db"
            )
        finally:
            if previous is None:
                os.environ.pop("DATABASE_PATH", None)
            else:
                os.environ["DATABASE_PATH"] = previous

    def test_falls_back_to_the_repository_default(self) -> None:
        previous = os.environ.get("DATABASE_PATH")
        os.environ.pop("DATABASE_PATH", None)
        try:
            self.assertEqual(
                audit_adjustment_chain.default_db_path(),
                os.path.join("data", "stock_analysis.db"),
            )
        finally:
            if previous is not None:
                os.environ["DATABASE_PATH"] = previous


if __name__ == "__main__":
    unittest.main()
