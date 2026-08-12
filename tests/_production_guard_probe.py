# -*- coding: utf-8 -*-
"""供 test_production_db_guard.py 显式调用的探针，故意去摸生产库。

文件名不以 test_ 开头，因此不会被常规收集捞进来——只有把路径显式传给
pytest 时才会执行。它存在的唯一目的，是让「护栏是否真的会拦下写生产库
的用例」这件事可以被验证，而不是靠人肉相信。
"""
import os
from pathlib import Path

_PRODUCTION_DB = Path(__file__).resolve().parent.parent / "data" / "stock_analysis.db"


def test_probe_touches_production_database():
    if _PRODUCTION_DB.exists():
        os.utime(_PRODUCTION_DB, None)


def test_probe_leaves_production_alone():
    assert 1 + 1 == 2
