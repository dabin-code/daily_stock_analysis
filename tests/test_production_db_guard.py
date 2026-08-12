# -*- coding: utf-8 -*-
"""确认「测试不得访问生产库」这道护栏本身是活的。

护栏静默失效的代价已经付过一次：2026-08-12 生产库被并发测试进程写坏，
文件头被别的数据页覆盖，1.3 GB 的库直接打不开，只能回滚到前一天的备份。
没有这个用例，conftest 里的拦截逻辑被误删或被条件短路后，谁都不会发现。
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_PROBE = Path(__file__).resolve().parent / "_production_guard_probe.py"


def _run_probe(test_name: str) -> subprocess.CompletedProcess:
    """在子进程里跑探针，让它经过真实的 tests/conftest.py。

    必须是独立进程：护栏记录的是「本进程打开过生产库」，在当前进程里
    直接连一下，触发的会是本用例自己。
    """
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{_PROBE}::{test_name}",
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:warnings",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_guard_fails_a_test_that_opens_the_production_database():
    """连一下生产库的用例必须变红，且失败信息要点明原因。"""
    result = _run_probe("test_probe_touches_production_database")

    assert result.returncode != 0, (
        "连生产库的用例竟然通过了，护栏已失效:\n" + result.stdout
    )
    assert "打开了生产库" in result.stdout, result.stdout


def test_guard_fails_a_test_that_opens_production_through_sqlalchemy():
    """ORM 路径必须同样被拦下，这是测试回落到生产库的真实走法。"""
    result = _run_probe("test_probe_opens_production_via_sqlalchemy")

    assert result.returncode != 0, (
        "经 SQLAlchemy 连生产库的用例竟然通过了，护栏漏了 ORM 路径:\n" + result.stdout
    )
    assert "打开了生产库" in result.stdout, result.stdout


def test_guard_leaves_ordinary_tests_alone():
    """反例：不碰生产库的用例不能被误伤。"""
    result = _run_probe("test_probe_leaves_production_alone")

    assert result.returncode == 0, result.stdout
