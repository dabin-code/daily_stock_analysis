# -*- coding: utf-8 -*-
"""生产库整库备份，用于灾难恢复。

注意：这是粗粒度回滚手段，用于长时数据作业前的兜底快照，
不承担常规的表级回滚职责。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _discard_partial(temp_target: Path) -> None:
    """清掉半成品临时库及其 WAL 边车文件，不留下任何可能被误当成备份的残骸。"""
    for path in (
        temp_target,
        temp_target.with_name(temp_target.name + "-wal"),
        temp_target.with_name(temp_target.name + "-shm"),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # 清理失败不能盖住原始异常：临时文件带 .tmp 后缀，不会被误认为备份。
            pass


def backup(db_path: Path, out_dir: Path) -> Path:
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = out_dir / f"{db_path.stem}_{stamp}.db"
    # 备份写到已有库上会先清空它的表：宁可报错，也不能静默覆盖。
    if target.exists():
        raise FileExistsError(f"backup target already exists: {target}")

    # 默认 --out 与源库同卷，整库上 GB，写到一半磁盘满是最先遇到的失败。
    # 先算清楚放不放得下，放不下就在动手写之前失败。
    required_bytes = db_path.stat().st_size
    free_bytes = shutil.disk_usage(out_dir).free
    if free_bytes < required_bytes:
        raise OSError(
            f"insufficient free space at {out_dir}: "
            f"need {required_bytes} bytes, only {free_bytes} bytes available"
        )

    # 先写临时文件，等目标连接干净关闭后才改名：中途失败绝不会留下一个名字合规、
    # 时间戳正确、integrity_check 也说 ok，但实际读不出表的“备份”。
    temp_target = target.with_suffix(".db.tmp")
    _discard_partial(temp_target)

    # 用 sqlite3 的 backup API 而不是文件拷贝：即使有并发写也能拿到一致快照。
    source = sqlite3.connect(str(db_path))
    try:
        dest = sqlite3.connect(str(temp_target))
        try:
            source.backup(dest)
        finally:
            dest.close()
        os.replace(temp_target, target)
    except BaseException:
        _discard_partial(temp_target)
        raise
    finally:
        source.close()
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup production sqlite database")
    parser.add_argument("--db", default=None, help="database path (default: from config)")
    parser.add_argument("--out", default="./data/backups", help="output directory")
    args = parser.parse_args()

    if args.db:
        db_path = Path(args.db)
    else:
        from src.config import get_config
        db_path = Path(getattr(get_config(), "database_path", "./data/stock_analysis.db"))

    target = backup(db_path, Path(args.out))
    size_mb = target.stat().st_size / 1024 / 1024
    print(f"[ok] backup written: {target} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
