# -*- coding: utf-8 -*-
"""供 test_production_db_guard.py 显式调用的探针，故意去摸生产库。

文件名不以 test_ 开头，因此不会被常规收集捞进来——只有把路径显式传给
pytest 时才会执行。它存在的唯一目的，是让「护栏是否真的会拦下写生产库
的用例」这件事可以被验证，而不是靠人肉相信。
"""
import sqlite3
from pathlib import Path

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
