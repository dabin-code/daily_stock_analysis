"""生产库的私有可写副本。

给标了 `real_database` 的用例用。这些用例不是只读的——`execute_run`
会建选股 run，板块热度用例会落库——所以给它们的必须是可写副本，
而不是只读连接；写入落在副本里，生产库一次都不被打开。

单独成模块而不是放进 conftest：`setUpClass` 是类级的，早于函数级
autouse 夹具执行，那时 `DATABASE_PATH` 还指向会话临时库。用例模块必须
能不依赖夹具时序直接问出这个路径。
"""

import os
import shutil
from pathlib import Path

PRODUCTION_DB = Path(__file__).resolve().parent.parent / "data" / "stock_analysis.db"

REPLICA_PREFIX = ".pytest-replica-"


def production_replica_path() -> "Path | None":
    """返回副本路径，必要时先建出来；生产库不存在则返回 None。

    副本按「生产库的大小 + mtime」命名，因此同一份生产库在多个 xdist
    worker 和多次运行之间只拷一次；生产库一变，token 变，旧副本被清掉。

    这里刻意用普通文件拷贝而不是 SQLite backup API：后者要打开生产库，
    会让「零例外」的护栏自相矛盾，而且在损坏页上必然失败。WAL 模式下
    纯文件拷贝拿到的是最后一次 checkpoint 的状态，对测试足够。
    """
    if not PRODUCTION_DB.exists():
        return None
    stat = PRODUCTION_DB.stat()
    token = f"{stat.st_size}-{stat.st_mtime_ns}"
    target = PRODUCTION_DB.parent / f"{REPLICA_PREFIX}{token}.db"
    if target.exists():
        return target

    for stale in PRODUCTION_DB.parent.glob(f"{REPLICA_PREFIX}*"):
        if stale != target:
            stale.unlink(missing_ok=True)

    staging = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    shutil.copyfile(PRODUCTION_DB, staging)
    os.replace(staging, target)
    return target
