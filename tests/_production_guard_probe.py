# -*- coding: utf-8 -*-
"""供 test_production_db_guard.py 显式调用的探针，故意去摸生产库。

文件名不以 test_ 开头，因此不会被常规收集捞进来——只有把路径显式传给
pytest 时才会执行。它存在的唯一目的，是让「护栏是否真的会拦下写生产库
的用例」这件事可以被验证，而不是靠人肉相信。
"""
import os
import sqlite3
from pathlib import Path

import pytest

_PRODUCTION_DB = Path(__file__).resolve().parent.parent / "data" / "stock_analysis.db"


def test_probe_touches_production_database():
    """以只读方式连一下就够：护栏拦的是「打开」，不是「写坏」。

    探针不能真的写生产库——否则每跑一次护栏测试都在动那个 1.3 GB 的文件。
    """
    sqlite3.connect(f"file:{_PRODUCTION_DB.as_posix()}?mode=ro", uri=True).close()


def test_probe_opens_production_via_sqlalchemy():
    """ORM 路径同样要被拦下。

    测试回落到生产库靠的是 DatabaseManager，走的是 SQLAlchemy 的 pysqlite
    方言——它拿的是 sqlite3.dbapi2.connect。只包 sqlite3.connect 时这条
    探针会漏网，而它恰恰是真实事故的路径。
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{_PRODUCTION_DB}")
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    finally:
        engine.dispose()


def test_probe_leaves_production_alone():
    assert 1 + 1 == 2


@pytest.mark.real_database
def test_probe_marked_real_database_still_cannot_touch_production():
    """标记不再是通行证。

    2026-08-13 第二次写坏生产库之后，real_database 拿到的是私有副本，
    所以护栏可以做成零例外。这条探针钉住的就是「例外已经没了」。
    """
    sqlite3.connect(f"file:{_PRODUCTION_DB.as_posix()}?mode=ro", uri=True).close()


@pytest.mark.real_database
def test_probe_marked_real_database_is_pointed_at_a_replica():
    """反例：标记该给的是一份真有数据的副本，不是空库，也不是生产库。"""
    configured = Path(os.environ["DATABASE_PATH"]).resolve()
    assert configured != _PRODUCTION_DB.resolve()
    assert configured.name.startswith(".pytest-replica-")
    assert configured.exists()

    connection = sqlite3.connect(str(configured))
    try:
        tables = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert tables > 0, "副本里没有任何表，说明拷贝的是空库"
