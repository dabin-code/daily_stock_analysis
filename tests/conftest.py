import os
import sqlite3
import tempfile
import urllib.parse
from pathlib import Path

import pytest

from src.storage import DatabaseManager

_LEAKS: list[str] = []

_PRODUCTION_DB = Path(__file__).resolve().parent.parent / "data" / "stock_analysis.db"

_PRODUCTION_OPENS: list[str] = []


def _resolve_sqlite_target(database) -> "Path | None":
    """还原 sqlite3.connect 的第一个参数指向的文件，内存库返回 None。"""
    if not isinstance(database, (str, os.PathLike)):
        return None
    text = os.fspath(database)
    if text == ":memory:" or text.startswith("file::memory:"):
        return None
    if text.startswith("file:"):
        text = urllib.parse.unquote(text[len("file:") :].split("?", 1)[0])
    if not text:
        return None
    try:
        return Path(text).resolve()
    except (OSError, ValueError):
        return None


def _install_production_open_tracker() -> None:
    """记录本进程里每一次打开生产库的动作。

    必须在收集用例之前装好，否则导入期就建好引擎的模块会漏掉。
    SQLAlchemy 的 pysqlite 方言拿的是 sqlite3.dbapi2.connect，
    只包 sqlite3.connect 会漏掉全部 ORM 路径，两个名字都要换。
    """
    real_connect = sqlite3.dbapi2.connect

    def tracking_connect(database, *args, **kwargs):
        if _resolve_sqlite_target(database) == _PRODUCTION_DB:
            _PRODUCTION_OPENS.append(str(database))
        return real_connect(database, *args, **kwargs)

    sqlite3.dbapi2.connect = tracking_connect
    sqlite3.connect = tracking_connect


_install_production_open_tracker()

_SESSION_DB_DIR: "tempfile.TemporaryDirectory | None" = None
_SESSION_DB_PATH: "str | None" = None

_SESSION_DB_ENV_MARKER = "DSA_PYTEST_SESSION_DB"


def pytest_configure(config):
    """把未显式指定库路径的用例导向会话临时库。

    没有这道重定向时，任何走 DatabaseManager 默认路径的用例都会打开
    data/stock_analysis.db 本身：跑一遍离线测试就能看到生产库的 mtime
    被推进。它同时挡住了并行化——多个 worker 争抢同一个库文件会让
    互不相关的用例随机变红。

    必须放在 pytest_configure 而不是 fixture：Config 是缓存单例，
    等到 fixture 阶段再改环境变量，配置往往已经在导入期被读过了。

    显式设了 DATABASE_PATH 的场景（比如 CI 指定库）保持不动。
    """
    config.addinivalue_line(
        "markers",
        "real_database: 该用例有意读取真实生产库 data/stock_analysis.db，"
        "不受会话级临时库重定向影响",
    )

    global _SESSION_DB_DIR, _SESSION_DB_PATH

    # pytest-xdist 的 worker 是子进程，会继承 master 的环境变量。若在这里
    # 见到 DATABASE_PATH 就直接返回，八个 worker 会共用 master 那一个临时库，
    # 于是 create_all 互相撞车（"table ... already exists"）。用哨兵区分
    # 「我们自己设的会话库」与「调用方显式指定的库」，前者每个 worker 各建一份。
    ours = os.environ.get(_SESSION_DB_ENV_MARKER)
    if os.environ.get("DATABASE_PATH") and not ours:
        return

    worker_id = getattr(config, "workerinput", {}).get("workerid", "master")
    _SESSION_DB_DIR = tempfile.TemporaryDirectory(prefix=f"pytest-dsa-db-{worker_id}-")
    _SESSION_DB_PATH = os.path.join(_SESSION_DB_DIR.name, "test_session.db")
    os.environ["DATABASE_PATH"] = _SESSION_DB_PATH
    os.environ[_SESSION_DB_ENV_MARKER] = "1"

    from src.config import Config

    Config.reset_instance()
    DatabaseManager.reset_instance()


def pytest_unconfigure(config):
    global _SESSION_DB_DIR
    if _SESSION_DB_DIR is not None:
        _SESSION_DB_DIR.cleanup()
        _SESSION_DB_DIR = None


@pytest.fixture(autouse=True)
def _database_path_isolation(request):
    """维持「默认临时库、显式标记才用真实库」这条边界。

    四十多个用例在 tearDown 里 `os.environ.pop("DATABASE_PATH")`，
    把变量还原成「未设置」而不是会话默认值——于是它们之后的每个用例
    都会静默回落到生产库。在这里补回默认值，就不必去改那几十处 pop。
    """
    if _SESSION_DB_PATH is None:
        yield
        return

    if request.node.get_closest_marker("real_database") is None:
        yield
        if os.environ.get("DATABASE_PATH") is None:
            os.environ["DATABASE_PATH"] = _SESSION_DB_PATH
        return

    from src.config import Config

    previous = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(_PRODUCTION_DB)
    Config.reset_instance()
    DatabaseManager.reset_instance()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = previous
        Config.reset_instance()
        DatabaseManager.reset_instance()


@pytest.fixture(autouse=True)
def _fail_on_production_access(request):
    """未标记 real_database 的用例连了生产库，就地失败。

    2026-08-12 这个库被并发测试进程写坏过一次：文件头被别的数据页覆盖，
    1.3 GB 的库直接打不开，只能从前一天的备份回滚。事后才发现远比当场
    拦下昂贵，所以这里不只是记录，而是直接判失败。

    判据是「本进程有没有打开过这个文件」，不是文件 mtime。mtime 是进程
    无关的：并行跑时一个 worker 里标了 real_database 的用例连上生产库
    （`PRAGMA journal_mode=WAL` 会写文件头），另外七个 worker 会把这次
    改动记到自己正在跑的用例头上，一次全量能凭空多出五十个红。

    只读打开同样算违规——它意味着这个用例绕过了会话临时库的重定向，
    离写坏只差一次代码改动。

    残留盲区：不经 sqlite3 直接改文件的用例（比如 shutil 覆盖）拦不住。
    真实事故走的是 sqlite 连接，这里按事故的路径设防。
    """
    if request.node.get_closest_marker("real_database") is not None:
        yield
        return

    _PRODUCTION_OPENS.clear()
    yield
    if _PRODUCTION_OPENS:
        opened = ", ".join(sorted(set(_PRODUCTION_OPENS)))
        _PRODUCTION_OPENS.clear()
        pytest.fail(
            f"{request.node.nodeid} 打开了生产库 {_PRODUCTION_DB}（{opened}）。"
            "测试必须走会话临时库；确有必要访问真实库的模块请显式加 "
            "@pytest.mark.real_database。"
        )


def _current_url():
    instance = getattr(DatabaseManager, "_instance", None)
    engine = getattr(instance, "_engine", None)
    return str(getattr(engine, "url", None)) if engine is not None else None


@pytest.fixture(autouse=True)
def _observe_database_singleton_leak(request):
    """记录哪些用例换掉了全局单例指向的库但没还原。

    单例泄漏的症状是跨用例的隐性失败，排查成本极高——本次定位
    test_e2e_five_layer_local 的两个失败花了四轮二分。
    """
    before = _current_url()
    yield
    after = _current_url()
    if before != after:
        _LEAKS.append(f"{request.node.nodeid}: {before} -> {after}")


def pytest_sessionfinish(session, exitstatus):
    if _LEAKS:
        print(f"\n[singleton-leak] {len(_LEAKS)} test(s) left a different database:")
        for line in _LEAKS:
            print(f"  {line}")
