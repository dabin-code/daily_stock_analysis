# -*- coding: utf-8 -*-
"""生产库整库备份，用于灾难恢复。

注意：这是粗粒度回滚手段，用于长时数据作业前的兜底快照，
不承担常规的表级回滚职责。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def backup(db_path: Path, out_dir: Path) -> Path:
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = out_dir / f"{db_path.stem}_{stamp}.db"

    # 用 sqlite3 的 backup API 而不是文件拷贝：即使有并发写也能拿到一致快照。
    source = sqlite3.connect(str(db_path))
    try:
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
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
