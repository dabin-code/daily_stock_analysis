import os
import sqlite3
import tempfile
import urllib.parse
from pathlib import Path

import pytest

from src.storage import DatabaseManager
from tests._production_replica import PRODUCTION_DB as _PRODUCTION_DB
from tests._production_replica import production_replica_path

_LEAKS: list[str] = []

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
        "real_database: 该用例需要真实生产数据，会被导向生产库的私有可写副本"
        "（见 production_replica_path），而不是生产库本身",
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

    replica = production_replica_path()
    if replica is None:
        pytest.skip("生产库不存在，real_database 用例无数据可用")

    previous = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(replica)
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
    """任何用例打开生产库都就地失败，没有例外。

    这个库被测试进程写坏过两次：2026-08-12 是文件头被覆盖，1.3 GB 直接
    打不开；2026-08-13 是 stock_daily 的 b-tree 指向了文件尾之后的页号，
    整表不可读，连 DROP TABLE 都做不了，只能从 staging 重建。两次都是
    在跑完门禁之后才发现的，所以这里宁可当场判红。

    曾经给 real_database 留过口子，让它直连生产库。第二次事故后取消：
    那些用例现在拿到的是 production_replica_path() 建的私有副本，
    于是这道门可以做成零例外——不必再判断「这次打开是好人还是坏人」。

    判据是「本进程有没有打开过这个文件」，不是文件 mtime。mtime 是进程
    无关的：并行跑时一个 worker 连上生产库（`PRAGMA journal_mode=WAL`
    会写文件头），另外七个 worker 会把这次改动记到自己正在跑的用例头上，
    一次全量能凭空多出五十个红。

    只读打开同样算违规——它意味着这个用例绕过了会话临时库的重定向，
    离写坏只差一次代码改动。

    残留盲区：不经 sqlite3 直接改文件的用例（比如 shutil 覆盖）拦不住。
    两次事故走的都是 sqlite 连接，这里按事故的路径设防。
    """
    _PRODUCTION_OPENS.clear()
    yield
    if _PRODUCTION_OPENS:
        opened = ", ".join(sorted(set(_PRODUCTION_OPENS)))
        _PRODUCTION_OPENS.clear()
        pytest.fail(
            f"{request.node.nodeid} 打开了生产库 {_PRODUCTION_DB}（{opened}）。"
            "测试必须走会话临时库；确需真实数据的模块请加 "
            "@pytest.mark.real_database，它会拿到生产库的私有副本。"
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
