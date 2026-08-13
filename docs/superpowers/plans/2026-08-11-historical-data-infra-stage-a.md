# 历史数据基础设施 A0/A/B/G1/C 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 拆除 Tushare 前复权引信，建立 staging 写入通道、交易日历与上市退市日期，并把 2018 年以来的日线（含 `pre_close`）回补到 staging，为后续复权体系提供原料。

**Architecture:** 全部改动只新增表/列与新增写入通道，**不修改生产表 `stock_daily` 的任何既有行**。回补数据落 `stock_daily_staging`，生产表的提升（C2）需要独立闸门，不在本计划范围。唯一触及生产写入路径的改动是**追加** `pre_close` / `adj_convention` 两列的写入，属纯增量，不改变既有列的值。

**Tech Stack:** Python 3.12、SQLAlchemy ORM（`src/storage.py`，无 Alembic）、SQLite、pytest（marker 定义在 `setup.cfg`）、Tushare / baostock / akshare。

**Spec:** `docs/superpowers/specs/2026-08-11-historical-data-infrastructure-design.md`（v8）

---

## 范围与边界

本计划**只覆盖** spec 8.0 判定为「可开工」的五个阶段：

| 阶段 | 内容 | 任务 |
| --- | --- | --- |
| A0 | 拆除 qfq 引信 | Task 1 |
| A | 备份、维护窗口与 WAL、新增列、staging 建表、写入路径落 `pre_close` | Task 2–5 |
| B | 交易日历建表与抓取 | Task 6–7 |
| G1 | 上市/退市日期 | Task 8–9 |
| C | 全窗口回补至 staging | Task 10–12 |

**明确不做**（spec 8.0 判定暂缓）：D 口径校验、K0 闸门、C2 原子提升、E/E2/F 复权体系、G2/H 积分窗口、J/K/L 因子切换。

**为什么 A0 必须最先做**：`data_provider/tushare_fetcher.py:424` 在普通股票取数路径上**无条件**调用 `_apply_qfq_adjustment`，当前只因免费账号 `adj_factor` 限频而回退成不复权（`:474-476` 熔断）。一旦购买积分，该函数立即生效，生产写入语义会从「不复权」静默翻转为「前复权」，且没有任何开关能拦。

---

## 关键既有事实（实施前必读）

实施者需要知道的仓库现状，全部已核实：

1. **无 Alembic**。新表 = 在 `src/storage.py` 加一个继承 `Base` 的 ORM 类，`DatabaseManager.__init__` 第 1244 行 `Base.metadata.create_all(self._engine)` 自动建表。**新增列**则需要三件套：改 ORM 类 + 在 `_apply_inline_migrations`（`src/storage.py:1290` 起）加 `_migrate_sqlite_*` 方法 + 写一个 `scripts/migrate_*.py` 幂等脚本。
2. **布尔开关约定**：用 `src/config.py:55` 的 `parse_env_bool(os.getenv('X'), default=False)`，在 `Config` dataclass 加字段（参考 `kline_governance_enabled`，`src/config.py:700`）并在 `from_env` 解析（参考 `:1405`）。
3. **`instrument_master` 有 `list_date`（`src/storage.py:662`）但没有 `delist_date`**，退市状态靠 `listing_status`。
4. **交易日历已有 `src/core/trading_calendar.py`**，基于第三方库 `exchange_calendars`，**fail-open**（库不可用即视为开市），**不落任何本地表**。
5. **三条写入路径都不写 `pre_close`**：`market_data_sync_service.py:347-365`（缺 `updated_at`，用 SQLAlchemy）、`fast_backfill_service.py:181-201`（用 sqlite3）、`scripts/fast_backfill.py:137-155`（用 sqlite3）。
6. **Windows 下删临时 DB 必须先 `DatabaseManager.reset_instance()`**（内部 `engine.dispose()`），否则文件被占用。参考 `tests/test_market_data_sync_service.py:79-84`。
7. **`AGENTS.md` 硬规则**：新增配置项必须同步 `.env.example`；涉及 CLI/部署行为变化必须同步 `README.md` 与 `docs/CHANGELOG.md`；未经确认不执行 `git commit` 以外的 git 写操作（本计划每个任务末尾的 commit 属已授权范围，push/tag 不在内）。

---

## 文件结构

**新建：**

| 文件 | 职责 |
| --- | --- |
| `scripts/migrate_stock_daily_pre_close.py` | 给 `stock_daily` 加 `pre_close` / `adj_convention` 的幂等迁移 |
| `scripts/migrate_instrument_delist_date.py` | 给 `instrument_master` 加 `delist_date` 的幂等迁移 |
| `src/services/trading_calendar_service.py` | 交易日历抓取、落库、fail-closed 查询 |
| `src/services/listing_lifecycle_service.py` | 上市/退市日期抓取与回填（含已退市股票） |
| `scripts/backup_production_db.py` | 阶段 A 的整库备份 |
| `tests/test_tushare_qfq_switch.py` | A0 开关的正反测试 |
| `tests/test_trading_calendar_service.py` | 日历服务测试 |
| `tests/test_listing_lifecycle_service.py` | 上市退市服务测试 |
| `tests/test_staging_write_path.py` | staging 写入与 `pre_close` 落库测试 |

**修改：**

| 文件 | 改动 |
| --- | --- |
| `src/config.py` | 新增 `tushare_qfq_enabled`、`backfill_rate_limit_per_min`、`data_convention_version` |
| `data_provider/tushare_fetcher.py:438` | `_apply_qfq_adjustment` 顶部加硬开关短路 |
| `src/storage.py` | 新增 `StockDailyStaging`、`TradingCalendar` 两个 ORM 类；`StockDaily` 加 2 列；`InstrumentMaster` 加 1 列；内联迁移 |
| `src/services/market_data_sync_service.py:347-365` | INSERT 追加 `pre_close`、`adj_convention` |
| `src/services/fast_backfill_service.py` | 写入目标切 staging、批量写入、限速参数化、回补入口、完成判定加口径版本位 |
| `scripts/fast_backfill.py:137-155` | INSERT 追加 `pre_close`、`adj_convention` |
| `.env.example` | 新增三个配置项 |
| `docs/CHANGELOG.md` | 记录 A0 与写入路径变更 |

---

## Task 1: A0 — 拆除 Tushare qfq 引信

**Files:**

- Modify: `src/config.py:700`（dataclass 字段）、`src/config.py:1405`（from_env 解析）
- Modify: `data_provider/tushare_fetcher.py:438-470`
- Modify: `.env.example`
- Modify: `docs/CHANGELOG.md`
- Test: `tests/test_tushare_qfq_switch.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_tushare_qfq_switch.py`：

```python
# -*- coding: utf-8 -*-
"""TUSHARE_QFQ_ENABLED 硬开关测试。

引信语义：默认关闭时 _apply_qfq_adjustment 必须原样返回，且绝不调用 adj_factor。
这条断言比"返回值相同"更重要——买入积分后 adj_factor 会变得可用，
若仍被调用，生产写入语义就会静默翻转。
"""
import os
import unittest
from unittest.mock import MagicMock

import pandas as pd

from src.config import Config


def _make_fetcher():
    from data_provider.tushare_fetcher import TushareFetcher

    fetcher = TushareFetcher.__new__(TushareFetcher)
    fetcher._api = MagicMock()
    fetcher._adj_factor_cooldown_until = 0.0
    fetcher._check_rate_limit = lambda: None
    return fetcher


def _sample_df():
    return pd.DataFrame([
        {"trade_date": "20260810", "open": 10.0, "high": 11.0,
         "low": 9.5, "close": 10.5, "pre_close": 10.0, "pct_chg": 5.0},
    ])


class TushareQfqSwitchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("TUSHARE_QFQ_ENABLED", None)
        Config.reset_instance()

    def tearDown(self) -> None:
        os.environ.pop("TUSHARE_QFQ_ENABLED", None)
        Config.reset_instance()

    def test_disabled_by_default_returns_df_untouched(self) -> None:
        fetcher = _make_fetcher()
        df = _sample_df()

        result = fetcher._apply_qfq_adjustment(df, "000001.SZ", "20260101", "20260810")

        pd.testing.assert_frame_equal(result, df)

    def test_disabled_by_default_never_calls_adj_factor(self) -> None:
        """反例测试：这是引信的核心。adj_factor 一旦被调用，语义就已翻转。"""
        fetcher = _make_fetcher()

        fetcher._apply_qfq_adjustment(_sample_df(), "000001.SZ", "20260101", "20260810")

        fetcher._api.adj_factor.assert_not_called()

    def test_explicitly_enabled_calls_adj_factor(self) -> None:
        os.environ["TUSHARE_QFQ_ENABLED"] = "true"
        Config.reset_instance()
        fetcher = _make_fetcher()
        fetcher._api.adj_factor.return_value = pd.DataFrame([
            {"trade_date": "20260810", "adj_factor": 1.0},
        ])

        fetcher._apply_qfq_adjustment(_sample_df(), "000001.SZ", "20260101", "20260810")

        fetcher._api.adj_factor.assert_called_once()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_tushare_qfq_switch.py -v
```

预期：`test_disabled_by_default_never_calls_adj_factor` FAIL，因为当前无开关，函数会直接调用 `adj_factor`。

- [ ] **Step 3: 加配置字段**

`src/config.py`，在第 700 行 `kline_governance_enabled: bool = False` 之后插入：

```python
    # === 复权口径开关 ===
    # Tushare daily() 返回不复权价。历史上此处会自动转前复权，但该行为与
    # 「stock_daily 是不复权权威源」的设计冲突，且购买积分后会静默生效。
    # 默认关闭，待新复权体系（stock_daily_adj）就绪后本开关连同函数一并移除。
    tushare_qfq_enabled: bool = False
```

在第 1405 行 `kline_governance_enabled=...` 之后插入：

```python
            tushare_qfq_enabled=parse_env_bool(os.getenv('TUSHARE_QFQ_ENABLED'), default=False),
```

- [ ] **Step 4: 在引信处短路**

`data_provider/tushare_fetcher.py`，在 `_apply_qfq_adjustment` 的 docstring 之后、第 469 行 `if df is None or df.empty` **之前**插入：

```python
        if not getattr(get_config(), "tushare_qfq_enabled", False):
            return df
```

`get_config` 已在该文件第 36 行导入，无需新增 import。

同时更新该函数 docstring，在「设计要点」末尾追加一条：

```text
        - 本函数默认由 ``TUSHARE_QFQ_ENABLED`` 关闭。它与「stock_daily 存不复权价」
          的设计冲突：开启后生产写入语义会变成前复权。仅供临时排查使用。
```

- [ ] **Step 5: 运行测试确认通过**

```bash
python -m pytest tests/test_tushare_qfq_switch.py -v
```

预期：3 passed。

- [ ] **Step 5b: 修好被开关打挂的 4 条既有测试**

`tests/test_tushare_fetcher_followups.py` 有四条直接调用 `_apply_qfq_adjustment` 的用例
（`:98`、`:127`、`:140`、`:152`）。开关默认关闭后它们必然失败——
`test_apply_qfq_adjustment_scales_prices_and_keeps_volume` 断言价格被缩放，
`test_apply_qfq_adjustment_trips_cooldown_on_rate_limit` 断言熔断被触发且
`adj_factor.call_count == 1`，而短路后这两件事都不会发生。

先确认：

```bash
python -m pytest tests/test_tushare_fetcher_followups.py -v -k qfq
```

预期：2 failed（缩放与熔断两条），2 passed（两条 fallback 用例因为返回原 df 而恰好仍通过，
但它们此时验证的已经不是原本的语义）。

处置：给这四条用例的 setUp 显式开启开关，让它们继续验证**函数本身**的正确性：

```python
    def setUp(self) -> None:
        # 这组用例验证 _apply_qfq_adjustment 函数体的正确性，
        # 因此显式打开开关。生产默认关闭由 test_tushare_qfq_switch.py 把关。
        os.environ["TUSHARE_QFQ_ENABLED"] = "true"
        Config.reset_instance()

    def tearDown(self) -> None:
        os.environ.pop("TUSHARE_QFQ_ENABLED", None)
        Config.reset_instance()
```

**不要**把这四条改成断言"返回原 df"——那样一来函数体就没有任何测试覆盖，
而阶段 L 之前它仍须保持可用。

- [ ] **Step 6: 同步 `.env.example`**

在 `.env.example` 的 Tushare 配置区块追加：

```bash
# 是否让 Tushare 日线自动转前复权（true/false，默认 false）
# 警告：开启会把 stock_daily 的价格口径从「不复权」翻转为「前复权」，
# 与历史复权体系（stock_daily_adj）冲突。仅供临时排查，不要在生产开启。
TUSHARE_QFQ_ENABLED=false
```

- [ ] **Step 7: 记录 CHANGELOG**

`docs/CHANGELOG.md` 顶部新增条目，说明：新增 `TUSHARE_QFQ_ENABLED` 硬开关（默认 false），行为变化是**购买 Tushare 积分后不再自动前复权**，回滚方式是设为 `true`。

- [ ] **Step 8: 回归与提交**

```bash
python -m py_compile src/config.py data_provider/tushare_fetcher.py
python -m pytest tests/test_tushare_qfq_switch.py tests/test_tushare_fetcher_followups.py -m "not network" -q
```

```bash
git add src/config.py data_provider/tushare_fetcher.py .env.example docs/CHANGELOG.md tests/test_tushare_qfq_switch.py
git commit -m "feat: add TUSHARE_QFQ_ENABLED hard switch, default off

Tushare daily() qfq conversion fired unconditionally and would silently
flip production price convention from unadjusted to front-adjusted once
adj_factor became available with paid credits."
```

---

## Task 2: A — 备份、维护窗口与 SQLite 并发保护

**Files:**

- Create: `scripts/backup_production_db.py`
- Modify: `src/storage.py:1221-1227`（连接事件里启用 WAL）
- Modify: `src/config.py`、`.env.example`（维护窗口开关）
- Test: 手工执行验证 + `tests/test_storage_wal.py`（新建）

**为什么并发保护属于本任务而不是可选项**：spec §8 的阶段 A 明确包含「每日任务暂停」，
§9.4 要求启用 WAL、复核 `busy_timeout`、设维护模式开关。而 Task 12 Step 3 是一个
**数小时、约 2080 次事务**的作业，直接压在活跃的 `data/stock_analysis.db` 上——
正是 §9.4 警告的「与每日任务相撞即 `database is locked`」。

仓库现状：`src/storage.py:1226` 只设了 `busy_timeout=30000`，**全仓没有任何
`journal_mode` 设置**，即仍是默认的 rollback journal，写锁会阻塞所有读。

- [ ] **Step 1: 写备份脚本**

```python
# -*- coding: utf-8 -*-
"""生产库整库备份。阶段 A 前置，用于灾难恢复。

注意：这是粗粒度回滚手段。表级回滚由 C2 的 stock_daily_pre_backfill 快照提供，
本脚本不承担常规回滚职责。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


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
```

- [ ] **Step 2: 执行并校验**

```bash
python scripts/backup_production_db.py
```

预期：打印备份路径与体积。校验备份可读：

```bash
python -c "import sqlite3,glob; p=sorted(glob.glob('data/backups/*.db'))[-1]; c=sqlite3.connect(p); print(p, c.execute('SELECT COUNT(*) FROM stock_daily').fetchone()); c.close()"
```

预期：行数与生产库一致。

- [ ] **Step 3: 启用 WAL**

`src/storage.py:1221-1227` 已有 connect 事件监听（设 `foreign_keys` 与 `busy_timeout`）。
在同一处追加：

```python
                # WAL 让读写不互相阻塞。阶段 C 的回补是数小时、约 2080 次事务的
                # 长作业，默认的 rollback journal 会让写锁阻塞全部读，
                # 与每日任务相撞即 database is locked。
                cursor.execute("PRAGMA journal_mode=WAL")
```

写一条测试确认它真的生效（`PRAGMA journal_mode` 是持久化设置，但要确认代码路径没被跳过）：

```python
def test_sqlite_connection_uses_wal(tmp_path):
    # setUp/tearDown 同 Task 3 的 SchemaTestCase
    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"
```

- [ ] **Step 4: 加维护窗口开关**

`src/config.py` dataclass 追加：

```python
    # 维护窗口。开启时每日同步与 K 线治理任务跳过执行，
    # 供阶段 C 的长时回补独占数据库。
    data_maintenance_mode: bool = False
```

`from_env` 追加 `data_maintenance_mode=parse_env_bool(os.getenv('DATA_MAINTENANCE_MODE'), default=False),`，
`.env.example` 同步。

在每日同步与 K 线治理的任务入口处加短路，**并打 `logger.warning`**——
静默跳过会让人以为任务正常跑过了：

```python
        if getattr(get_config(), "data_maintenance_mode", False):
            logger.warning("DATA_MAINTENANCE_MODE 已开启，跳过本次<任务名>")
            return <空结果>
```

**进入与退出条件写进 CHANGELOG**：进入 = 阶段 C 开工前；退出 = Task 12 Step 4 的
实证数字产出后。忘记退出会让生产数据静默停更，因此退出条件必须落在文档里而不是记忆里。

- [ ] **Step 5: 提交**

```bash
git add scripts/backup_production_db.py src/storage.py src/config.py .env.example tests/
git commit -m "chore: add backup script, enable WAL and add maintenance mode switch

Stage C runs a multi-hour backfill against the live database; the default
rollback journal would block every reader for its duration."
```

---

## Task 3: A — `stock_daily` 新增 `pre_close` 与 `adj_convention`

**Files:**

- Modify: `src/storage.py:73-133`（`StockDaily` ORM）、`src/storage.py:1300-1309`（内联迁移登记）
- Create: `scripts/migrate_stock_daily_pre_close.py`
- Test: `tests/test_staging_write_path.py`（新建，本任务只加迁移相关用例）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_staging_write_path.py`：

```python
# -*- coding: utf-8 -*-
"""stock_daily 新增列与 staging 表的结构测试。"""
import os
import sqlite3
import tempfile
import unittest

from src.config import Config
from src.storage import DatabaseManager


def _columns(db_path: str, table: str) -> set:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


class SchemaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "schema.db")
        os.environ["DATABASE_PATH"] = self._db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        DatabaseManager.get_instance()

    def tearDown(self) -> None:
        # Windows 下必须先 dispose engine 才能删除临时文件
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def test_stock_daily_has_pre_close_and_adj_convention(self) -> None:
        cols = _columns(self._db_path, "stock_daily")
        self.assertIn("pre_close", cols)
        self.assertIn("adj_convention", cols)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

```bash
python -m pytest tests/test_staging_write_path.py -v
```

预期：FAIL，`'pre_close' not found in ...`。

- [ ] **Step 3: 改 ORM**

`src/storage.py`，在 `StockDaily` 类的 `adj_factor_source`（第 123 行）之后插入：

```python
    # 前收盘价。Tushare daily() 原生提供，是免费重建复权因子的唯一依据
    # （ratio = pre_close(t) / close(prev_observation)）。历史上三条写入路径
    # 全部丢弃了该字段，导致增量段无法自持。
    pre_close = Column(Float, nullable=True)
    # 该行价格的复权口径：raw / qfq / unknown（枚举取值见 spec 7.1）。
    # 阶段 E 只消费 raw 行，写错值等于让整批数据在复权体系里失效。
    # 实测发现不同数据源口径不一致（Tushare 不复权、Efinance 疑似前复权），
    # 混存会让复权重建在数据源边界上出错，因此必须逐行标注。
    adj_convention = Column(String(16), nullable=True, index=True)
```

- [ ] **Step 4: 加内联迁移**

在 `src/storage.py` 的 `_apply_inline_migrations` 调用链（第 1300-1309 行）注册一个新方法。

**签名必须与相邻方法一致：无参，内部自开连接**（照 `:1425` 的
`_migrate_sqlite_stock_daily_adj_factor_fields`），不要接受 `conn` 参数——
`_apply_inline_migrations` 是无参调用它们的：

```python
    def _migrate_sqlite_stock_daily_pre_close_fields(self) -> None:
        """向后兼容：给已有 stock_daily 补 pre_close / adj_convention 两列。"""
        with self._engine.begin() as conn:
            existing = {
                row[1] for row in conn.exec_driver_sql("PRAGMA table_info(stock_daily)")
            }
            if "pre_close" not in existing:
                conn.exec_driver_sql("ALTER TABLE stock_daily ADD COLUMN pre_close FLOAT")
            if "adj_convention" not in existing:
                conn.exec_driver_sql(
                    "ALTER TABLE stock_daily ADD COLUMN adj_convention VARCHAR(16)"
                )
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_stock_daily_adj_convention "
                    "ON stock_daily(adj_convention)"
                )
```

> 实施前先打开 `src/storage.py:1425` 核对相邻方法的实际写法，以现状为准。

- [ ] **Step 5: 写幂等迁移脚本**

`scripts/migrate_stock_daily_pre_close.py`，照抄 `scripts/migrate_stock_daily_adj_factor.py`
的结构（常量在 42-51 行，主体在 86-107 行），把常量改为：

```python
TABLE = "stock_daily"
NEW_COLUMNS = [
    ("pre_close", "FLOAT"),
    ("adj_convention", "VARCHAR(16)"),
]
INDEX_NAME = "ix_stock_daily_adj_convention"
INDEX_COLUMN = "adj_convention"
```

**不要照抄该脚本 127-140 行的 legacy 回填段**（它把存量行的 `adj_factor` 填成 1.0）。
`pre_close` 没有可推定的默认值，填任何值都是造假；存量行保持 NULL，
由阶段 C 的重取来补。

- [ ] **Step 6: 补一条真正走迁移路径的测试**

Step 1 的测试只验证 `create_all` 在全新库上建出了列，**没有覆盖迁移本身**——
而迁移才是真正会在存量库上跑的那条路径。补一条：先用不含新列的裸
`CREATE TABLE` 建库，再触发 `DatabaseManager` 初始化，断言两列已被加上。

```python
    def test_inline_migration_adds_columns_to_legacy_schema(self) -> None:
        """存量库走的是迁移路径，不是 create_all，必须单独覆盖。"""
        # 先建不含 pre_close / adj_convention 的旧结构，再让 DatabaseManager 初始化
```

- [ ] **Step 7: 运行确认通过**

```bash
python -m pytest tests/test_staging_write_path.py -v
python scripts/migrate_stock_daily_pre_close.py --db ./data/stock_analysis.db
python scripts/migrate_stock_daily_pre_close.py --db ./data/stock_analysis.db
```

预期：测试 PASS；迁移脚本第一次打印新增两列，第二次全部打印 `[skip] ... already exists`（幂等）。

- [ ] **Step 8: 提交**

```bash
git add src/storage.py scripts/migrate_stock_daily_pre_close.py tests/test_staging_write_path.py
git commit -m "feat: add pre_close and adj_convention columns to stock_daily

pre_close is the only free basis for reconstructing adjustment factors;
all three write paths previously discarded it. adj_convention records the
per-row price convention because sources disagree."
```

---

## Task 4: A — 新增 `stock_daily_staging` 表

**Files:**

- Modify: `src/storage.py`（新增 ORM 类）
- Test: `tests/test_staging_write_path.py`（追加用例）

- [ ] **Step 1: 追加失败测试**

在 `tests/test_staging_write_path.py` 的 `SchemaTestCase` 中追加：

```python
    def test_staging_table_exists_and_mirrors_stock_daily(self) -> None:
        staging = _columns(self._db_path, "stock_daily_staging")
        production = _columns(self._db_path, "stock_daily")

        self.assertTrue(staging, "stock_daily_staging table missing")
        # staging 必须是生产表的超集：提升时逐列对应，缺列会让提升语句写不出来
        missing = production - staging
        self.assertEqual(missing, set(), f"staging missing production columns: {missing}")
        # staging 独有的批次追踪列
        self.assertIn("batch_id", staging)
        self.assertIn("convention_version", staging)
```

- [ ] **Step 2: 运行确认失败**

```bash
python -m pytest tests/test_staging_write_path.py::SchemaTestCase::test_staging_table_exists_and_mirrors_stock_daily -v
```

预期：FAIL，staging 表不存在。

- [ ] **Step 3: 新增 ORM 类**

`src/storage.py`，紧接 `StockDaily` 类之后插入：

```python
class StockDailyStaging(Base):
    """日线回补暂存区。

    阶段 C 的回补只写这里，生产表 stock_daily 由阶段 C2 经闸门原子提升。
    这样做的原因：回补要覆写 2024-2026 的存量，而这批存量正是判断
    数据源口径差异的证据本身，直接 INSERT OR REPLACE 会把证据抹掉。
    """
    __tablename__ = 'stock_daily_staging'
    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)   # 股
    amount = Column(Float)   # 元
    pct_chg = Column(Float)
    ma5 = Column(Float)
    ma10 = Column(Float)
    ma20 = Column(Float)
    volume_ratio = Column(Float)
    data_source = Column(String(50))
    adj_factor = Column(Float, nullable=True)
    adj_anchor_date = Column(Date, nullable=True)
    adj_factor_source = Column(String(32), nullable=True, index=True)
    pre_close = Column(Float, nullable=True)
    adj_convention = Column(String(16), nullable=True, index=True)
    # 回补批次，用于断点续跑与按批回滚
    batch_id = Column(String(64), nullable=True, index=True)
    # 写入时的口径版本，供 _is_date_complete 判定「该日是否已按当前口径写过」
    convention_version = Column(String(32), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    __table_args__ = (
        UniqueConstraint('code', 'date', name='uix_staging_code_date'),
        Index('ix_staging_code_date', 'code', 'date'),
        Index('ix_staging_date_version', 'date', 'convention_version'),
    )
```

- [ ] **Step 4: 运行确认通过**

```bash
python -m pytest tests/test_staging_write_path.py -v
```

预期：全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/storage.py tests/test_staging_write_path.py
git commit -m "feat: add stock_daily_staging table for pre-promotion backfill"
```

---

## Task 5: A — 三条写入路径落 `pre_close` 与 `adj_convention`

**Files:**

- Modify: `src/services/market_data_sync_service.py:347-365`
- Modify: `src/services/fast_backfill_service.py:181-201`
- Modify: `scripts/fast_backfill.py:137-155`
- Test: `tests/test_market_data_sync_service.py`、`tests/test_fast_backfill_service.py`（均已存在，追加用例）

**为什么必须三条都改**：spec 9.1 明确，日常增量同步路径优先级最高——它决定积分到期后系统能否长期自持。只改回补路径，增量段的 `pre_close` 永远为 NULL。

> **两条 Task 3 审查暴露的前置事实，动手前必须先处理：**
>
> **一、`INSERT OR REPLACE` 会把已填好的新列清成 NULL。** 三条路径
> （`fast_backfill_service.py:183-186`、`market_data_sync_service.py:362-366`、
> `scripts/fast_backfill.py:138`）都用**显式列清单**且当前不含新列，而
> `INSERT OR REPLACE` 的语义是**删行重插**，不是字段级更新。也就是说：只要
> 同一 `(code, date)` 被重跑一次，未列出的列就会被重置为 NULL。
> `adj_factor` 今天已经在被这样清掉。
>
> 后果很直接：如果只让「重新抓取阶段」去填 `pre_close`，而三条写入路径没有同步
> 纳入新列，那么下一次日常同步覆盖到同一天，就会把刚填好的值抹掉——阶段自己
> 抹掉自己的成果，且不报错。所以本任务的三处改动**必须包含把新列写进列清单**，
> 或改成真正的字段级 upsert（`ON CONFLICT ... DO UPDATE`）。
>
> **二、`adj_convention` 的取值按路径决定，不能一刀切。** 实施前已核对代码：
>
> - **`_try_bulk_sync` 硬编码 `"raw"` 是对的。** 它在 `:325` 直接调
>   `api.daily(trade_date=...)`，**不经过** `tushare_fetcher.py`，因此
>   `_apply_qfq_adjustment` 根本不在这条路上，`TUSHARE_QFQ_ENABLED` 开不开都
>   不影响它。这条路永远是不复权价。
> - **逐只降级路径不能硬编码。** 它经过数据提供方（efinance 疑似前复权，
>   Tushare 在开关打开时是 qfq），口径确实随路径变化，写死任何值都可能是谎报。
>
> **三、计划原先漏了第四条写入路径。** `sync_trade_date` 在 bulk 之外还有逐只
> 降级，走的是 `self.db.save_daily_data(...)`（`market_data_sync_service.py:211`
> → `storage.py:4017`），是一条独立的 ORM upsert 路径。它不在原先列的三条里，
> 但它同样属于「日常增量同步」——而本任务的立论正是增量段必须能自持。漏掉它，
> 任何走降级的股票 `pre_close` 就是 NULL，增量段照样残缺。
>
> 好消息是这条路**没有抹字段问题**：它是先查后改的字段级 upsert，不是
> `INSERT OR REPLACE`，所以只需补上赋值即可。

- [ ] **Step 1: 追加失败测试**

在 `tests/test_market_data_sync_service.py` 的 bulk sync 测试类中追加：

```python
    def test_bulk_sync_persists_pre_close_and_convention(self) -> None:
        """pre_close 是免费重建复权因子的唯一依据，必须落库。"""
        self._run_bulk_sync_with_fixture()

        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT pre_close, adj_convention FROM stock_daily "
                "WHERE code = ? AND date = ?",
                ("000001", "2026-05-08"),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row)
        self.assertAlmostEqual(row[0], 12.0, places=4)
        self.assertEqual(row[1], "raw")
```

> 实施注意：`_run_bulk_sync_with_fixture` 与 fixture 中的 `pre_close` 值需按该文件既有的 fixture 结构补齐；照抄相邻用例的构造方式，并在 fixture 里加 `"pre_close": 12.0`。

在 `tests/test_fast_backfill_service.py` 追加同构用例（断言 `stock_daily_staging` 而非 `stock_daily`，见 Task 10）。

- [ ] **Step 2: 运行确认失败**

```bash
python -m pytest tests/test_market_data_sync_service.py -v -k pre_close
```

预期：FAIL，`no such column: pre_close` 或返回 None。

- [ ] **Step 3: 改日常同步路径**

`src/services/market_data_sync_service.py:347-365`，把 INSERT 改为：

```python
                        conn.execute(
                            text(
                                "INSERT OR REPLACE INTO stock_daily "
                                "(code, date, open, high, low, close, volume, amount, pct_chg, "
                                "pre_close, adj_convention, data_source, created_at) "
                                "VALUES (:code, :date, :open, :high, :low, :close, :volume, :amount, :pct_chg, "
                                ":pre_close, :adj_convention, :data_source, :created_at)"
                            ),
                            {
                                "code": code, "date": date_str,
                                "open": row.get("open"), "high": row.get("high"),
                                "low": row.get("low"), "close": row.get("close"),
                                "volume": lots_to_shares(row.get("vol")),
                                "amount": thousand_yuan_to_yuan(row.get("amount")),
                                "pct_chg": row.get("pct_chg"),
                                "pre_close": row.get("pre_close"),
                                "adj_convention": "raw",
                                "data_source": "TushareFetcher(bulk)",
                                "created_at": now_str,
                            },
                        )
```

`adj_convention` 硬编码为 `"raw"` 在**这条路**上是正确的：`_try_bulk_sync` 直接调
`api.daily()`，不经过 `tushare_fetcher._apply_qfq_adjustment`，与
`TUSHARE_QFQ_ENABLED` 无关，恒为不复权价。（其他路径见任务开头第二条。）

- [ ] **Step 3b: 改逐只降级路径（`save_daily_data`）**

`src/storage.py:4017` 的 `save_daily_data` 是逐只降级的落库口，需要补 `pre_close`
与 `adj_convention` 两个字段的赋值。

**照抄该函数已有的 `adj_factor` 处理形状**（`:4071-4077`）：fetcher 给了就用，
没给就落一个显式的「未提供」标记，而不是猜。对应到新列：

```python
                    pre_close = row.get('pre_close')
                    if pre_close is not None and pd.isna(pre_close):
                        pre_close = None
                    # 口径必须由取数路径显式声明。这条路的数据源可能是 efinance
                    # （疑似前复权）或 Tushare（开关打开时为 qfq），按数据源名字
                    # 猜测等于谎报，因此未声明一律落 unknown。
                    adj_convention = row.get('adj_convention') or 'unknown'
```

> **落 `unknown` 是有意为之，不是偷懒。** spec 6.2 的判定表规定「任一行
> `adj_convention ≠ raw` 即 `unverifiable`，不计算」——也就是说降级路径写入的行
> 会让所在批次被判为不可验证。这正是想要的结果：我们确实不知道 efinance 的口径，
> 让它显式地不可用，好过让它冒充 `raw` 混进复权重建。

- [ ] **Step 3c: 追加降级路径的测试**

在 `tests/` 中已覆盖 `save_daily_data` 的用例附近追加：fetcher 未提供
`adj_convention` 时落 `unknown`；提供时按提供值落库；`pre_close` 缺失时为 NULL
而非 0.0（0.0 会被后续 `ratio = pre_close / close` 当成有效值算出 0）。

> **枚举值不能自创。** spec 7.1（`:768`）定稿的取值是 `raw` / `qfq` / `unknown`，
> 而 6.2 的判定表（`:425`）规定「任一行 `adj_convention ≠ raw` 即 `unverifiable`，不计算」。
> 写成 `unadjusted` 之类的同义词，会让本计划产出的全部数据在阶段 E 被整体判废。

- [ ] **Step 4: 改回补脚本**

`scripts/fast_backfill.py:137-155`，同样在列清单中追加 `pre_close, adj_convention`，
值分别取 `row.get("pre_close")` 与 `"raw"`。

> ~~`fast_backfill_service.py` 的改动并入 Task 10~~ **此推迟已撤销。**
> 审查实测发现该路径今天就有两个**活的**生产入口——
> `POST /api/v1/screening/backfill-to-date`（`api/v1/endpoints/screening.py:119`）
> 与数据健康页的 `backfill_to_date` 操作（`data_health_task_service.py:197`）——
> 都是人工触发、非调度，但按一次按钮就会把整段回补范围内的两列抹成 NULL。
> 「避免改两遍」换不来留一个可点击的擦除入口，因此已在本任务中先行补上两列止血。
> **Task 10 只需再做写入目标切换到 staging，不必重复补列。**

- [ ] **Step 5: 运行确认通过**

```bash
python -m pytest tests/test_market_data_sync_service.py -v
python -m py_compile scripts/fast_backfill.py src/services/market_data_sync_service.py
```

预期：PASS。

- [ ] **Step 6: 提交**

```bash
git add src/services/market_data_sync_service.py scripts/fast_backfill.py tests/test_market_data_sync_service.py
git commit -m "feat: persist pre_close and adj_convention on daily sync path

Without pre_close the incremental segment has no free way to reconstruct
adjustment factors once Tushare credits expire."
```

### Task 5 完成后遗留的缺口（实施中实测发现，必须在阶段 E 之前闭合）

**逐只降级路径实际上一行可用的 `pre_close` 都产不出来。**

根因是取数契约，不是落库代码：`data_provider/base.py:35` 的

```python
STANDARD_COLUMNS = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']
```

不含 `pre_close`，而 `_normalize_data` 只保留 `['code'] + STANDARD_COLUMNS`。也就是说
即便某个 fetcher 拿到了 `pre_close`，它也会在标准化环节被丢掉，`save_daily_data`
永远收不到。所以该路径落的必然是 `pre_close = NULL` + `adj_convention = 'unknown'`。

**为什么不能当成小事**：bulk 路径正常时没问题，但 bulk 失败或缺口少于阈值时
系统会降级到逐只，那些日期的 `pre_close` 就是空的。而复权重建是
`ratio = pre_close(t) / close(prev_observation)` 的**链式**推导——中间断一天，
链条就断在那里。本任务的立论是「增量段必须能自持」，留着这个洞就不成立。

闭合方式（属于取数契约变更，跨 5 个 provider，因此单独排期而不是塞进本任务）：

1. 把 `pre_close` 加进 `STANDARD_COLUMNS`，并确认各 fetcher 的原生字段映射。
2. 让 fetcher 显式声明自己的复权口径，而不是由落库端猜——这样
   `adj_convention` 才可能取到 `raw` 之外的真实值。
3. 注意 `_normalize_data` 是共享的，改动会同时影响所有 provider 的返回结构，
   要检查下游是否有按列数或列顺序取值的地方。

**另一处已知空缺**：目前没有任何地方校验 `adj_convention` 的取值属于
`raw` / `qfq` / `unknown`。fetcher 若写入 `unadjusted` 之类的同义词会被原样落库，
并在阶段 E 被静默判为不可验证。约束应加在阶段 E 定义枚举的地方，不是写入路径。

---

## Task 6: B — 交易日历建表

**Files:**

- Modify: `src/storage.py`（新增 `TradingCalendar` ORM 类）
- Test: `tests/test_trading_calendar_service.py`（新建）

**为什么需要本地表**：既有 `src/core/trading_calendar.py` 基于 `exchange_calendars`，库不可用时 **fail-open 视为开市**（`:79-80`）。回补与缺口归因必须 fail-closed——把「不知道是不是交易日」当成交易日，会把非交易日误判为数据缺口。

> 本任务新建 `tests/test_trading_calendar_service.py`。它的 `setUp` / `tearDown`
> 是 Task 8、Task 9 共用的脚手架模板：临时库 + `DatabaseManager.reset_instance()`，
> tearDown 里**先 reset 再删目录**（Windows 下不 dispose engine 就删不掉文件）。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_trading_calendar_service.py`：

```python
# -*- coding: utf-8 -*-
"""交易日历表与 fail-closed 查询测试。"""
import os
import sqlite3
import tempfile
import unittest
from datetime import date

from src.config import Config
from src.storage import DatabaseManager


class TradingCalendarSchemaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "cal.db")
        os.environ["DATABASE_PATH"] = self._db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        DatabaseManager.get_instance()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def test_trading_calendar_table_exists(self) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(trading_calendar)")}
        finally:
            conn.close()
        self.assertEqual(
            cols & {"market", "trade_date", "is_open", "source"},
            {"market", "trade_date", "is_open", "source"},
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

```bash
python -m pytest tests/test_trading_calendar_service.py -v
```

预期：FAIL，集合为空。

- [ ] **Step 3: 新增 ORM 类**

`src/storage.py`，在 `InstrumentMaster` 附近插入：

```python
class TradingCalendar(Base):
    """交易日历权威表。

    既有 src/core/trading_calendar.py 依赖第三方库且 fail-open，
    不能用于回补与缺口归因——把未知日期当作开市会制造假缺口。
    本表落库后，日历查询改为 fail-closed：查不到即报错，不猜。
    """
    __tablename__ = "trading_calendar"
    id = Column(Integer, primary_key=True, autoincrement=True)
    market = Column(String(16), nullable=False, default="cn", index=True)
    trade_date = Column(Date, nullable=False, index=True)
    is_open = Column(Boolean, nullable=False, default=True)
    source = Column(String(32), nullable=False, default="akshare")
    # 与 exchange_calendars 对照的结果：match / mismatch / unchecked
    cross_check = Column(String(16), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    __table_args__ = (
        UniqueConstraint('market', 'trade_date', name='uix_calendar_market_date'),
        Index('ix_calendar_market_date', 'market', 'trade_date'),
    )
```

- [ ] **Step 4: 运行确认通过并提交**

```bash
python -m pytest tests/test_trading_calendar_service.py -v
```

```bash
git add src/storage.py tests/test_trading_calendar_service.py
git commit -m "feat: add trading_calendar table as fail-closed calendar source"
```

---

## Task 7: B — 交易日历抓取与 fail-closed 查询

**Files:**

- Create: `src/services/trading_calendar_service.py`
- Test: `tests/test_trading_calendar_service.py`（追加）

- [ ] **Step 1: 追加失败测试**

```python
class TradingCalendarServiceTestCase(unittest.TestCase):
    """fail-closed 语义测试。

    这三条用例定义了本服务与既有 src/core/trading_calendar.py 的根本差异：
    未覆盖的日期必须报错，而不是猜一个答案。
    """

    def setUp(self) -> None:
        """建临时库并建表。

        **必须把 db_path 显式传给服务**：服务缺省会从 config 取
        database_path，那是生产库；无参构造会让这些用例写脏生产数据。
        """
        self._tmp = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmp, "cal.db")
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "CREATE TABLE trading_calendar ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT NOT NULL, "
                "trade_date TEXT NOT NULL, is_open INTEGER NOT NULL, "
                "source TEXT, cross_check TEXT, "
                "created_at TIMESTAMP, updated_at TIMESTAMP, "
                "UNIQUE(market, trade_date))"
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_is_trading_day_reads_from_table(self) -> None:
        from src.services.trading_calendar_service import TradingCalendarService

        svc = TradingCalendarService(db_path=self._db_path)
        svc.upsert_days("cn", [(date(2026, 8, 10), True), (date(2026, 8, 9), False)])

        self.assertTrue(svc.is_trading_day(date(2026, 8, 10)))
        self.assertFalse(svc.is_trading_day(date(2026, 8, 9)))

    def test_uncovered_date_raises_instead_of_guessing(self) -> None:
        from src.services.trading_calendar_service import (
            CalendarNotCoveredError,
            TradingCalendarService,
        )

        svc = TradingCalendarService(db_path=self._db_path)
        svc.upsert_days("cn", [(date(2026, 8, 10), True)])

        with self.assertRaises(CalendarNotCoveredError):
            svc.is_trading_day(date(2019, 3, 1))

    def test_get_trading_days_range_is_sorted_and_open_only(self) -> None:
        from src.services.trading_calendar_service import TradingCalendarService

        svc = TradingCalendarService(db_path=self._db_path)
        svc.upsert_days("cn", [
            (date(2026, 8, 10), True),
            (date(2026, 8, 8), False),
            (date(2026, 8, 7), True),
        ])

        days = svc.get_trading_days(date(2026, 8, 7), date(2026, 8, 10))

        self.assertEqual(days, [date(2026, 8, 7), date(2026, 8, 10)])
```

- [ ] **Step 2: 运行确认失败**

```bash
python -m pytest tests/test_trading_calendar_service.py -v
```

预期：`ModuleNotFoundError: No module named 'src.services.trading_calendar_service'`。

- [ ] **Step 3: 实现服务**

`src/services/trading_calendar_service.py`：

```python
# -*- coding: utf-8 -*-
"""交易日历服务：落库、fail-closed 查询、与 exchange_calendars 对照。"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


class CalendarNotCoveredError(RuntimeError):
    """查询的日期不在已落库的日历覆盖范围内。

    刻意设计成异常而非返回默认值：回补与缺口归因场景下，
    「猜一个答案」会静默制造假数据或假缺口。
    """


class TradingCalendarService:
    """统一走裸 sqlite3，不碰 DatabaseManager 单例。

    这样做是为了和 FastBackfillService 保持同一个库文件：后者用
    sqlite3.connect(self.db_path)，而 DatabaseManager 是单例
    （storage.py:1189-1204），传 URL 构造并不会切库，两边会各读各的。
    统一成一种连接方式也免去了 SQLAlchemy 与 sqlite3 两套占位符语法。
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None:
            from src.config import get_config
            db_path = getattr(get_config(), "database_path", "./data/stock_analysis.db")
        self._db_path = db_path

    @contextmanager
    def _connect(self):
        """连接获取的唯一入口，所有读写方法都必须走这里。"""
        conn = sqlite3.connect(self._db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── 写入 ────────────────────────────────────────────────────────────
    def upsert_days(
        self,
        market: str,
        days: Sequence[Tuple[date, bool]],
        source: str = "akshare",
    ) -> int:
        if not days:
            return 0
        payload = [
            (market, trade_date.isoformat(), 1 if is_open else 0, source)
            for trade_date, is_open in days
        ]
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO trading_calendar "
                "(market, trade_date, is_open, source) VALUES (?, ?, ?, ?)",
                payload,
            )
        return len(payload)

    # ── 查询（fail-closed）──────────────────────────────────────────────
    def is_trading_day(self, check_date: date, market: str = "cn") -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT is_open FROM trading_calendar "
                "WHERE market = ? AND trade_date = ?",
                (market, check_date.isoformat()),
            ).fetchone()
        if row is None:
            raise CalendarNotCoveredError(
                f"trading_calendar has no row for {market} {check_date.isoformat()}; "
                f"run TradingCalendarService.sync() first"
            )
        return bool(row[0])

    def get_trading_days(
        self,
        date_from: date,
        date_to: date,
        market: str = "cn",
    ) -> List[date]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT trade_date FROM trading_calendar "
                "WHERE market = ? AND is_open = 1 "
                "AND trade_date >= ? AND trade_date <= ? "
                "ORDER BY trade_date",
                (market, date_from.isoformat(), date_to.isoformat()),
            ).fetchall()
        return [date.fromisoformat(str(r[0])) for r in rows]

    # ── 抓取 ────────────────────────────────────────────────────────────
    def sync(self, date_from: date, date_to: date, market: str = "cn") -> Dict[str, int]:
        """akshare 主源抓取，exchange_calendars 对照。

        对照不一致时**不自动裁决**，只记录 cross_check='mismatch' 并告警——
        日历是所有缺口归因的基准，静默取其一会让后续全部结论建立在猜测上。
        """
        days = self._fetch_from_akshare(date_from, date_to)
        written = self.upsert_days(market, days, source="akshare")
        mismatches = self._cross_check(market, days)
        if mismatches:
            logger.warning(
                "trading_calendar cross-check mismatch on %d dates: %s",
                len(mismatches),
                [d.isoformat() for d in mismatches[:10]],
            )
        return {"written": written, "mismatch": len(mismatches)}

    def _fetch_from_akshare(self, date_from: date, date_to: date) -> List[Tuple[date, bool]]:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        open_days = {
            d for d in (
                _as_date(v) for v in df["trade_date"].tolist()
            )
            if d is not None and date_from <= d <= date_to
        }
        result: List[Tuple[date, bool]] = []
        cursor = date_from
        from datetime import timedelta
        while cursor <= date_to:
            result.append((cursor, cursor in open_days))
            cursor += timedelta(days=1)
        return result

    def _cross_check(self, market: str, days: Iterable[Tuple[date, bool]]) -> List[date]:
        try:
            from src.core.trading_calendar import is_market_open
        except Exception:  # noqa: BLE001
            return []

        mismatches: List[date] = []
        with self._connect() as conn:
            for trade_date, is_open in days:
                try:
                    reference = is_market_open(market, trade_date)
                except Exception:  # noqa: BLE001
                    continue
                verdict = "match" if bool(reference) == bool(is_open) else "mismatch"
                if verdict == "mismatch":
                    mismatches.append(trade_date)
                conn.execute(
                    "UPDATE trading_calendar SET cross_check = ? "
                    "WHERE market = ? AND trade_date = ?",
                    (verdict, market, trade_date.isoformat()),
                )
        return mismatches


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 4: 运行确认通过**

```bash
python -m pytest tests/test_trading_calendar_service.py -v
```

预期：全部 PASS。

- [ ] **Step 5: 抓取 2018-01-01 起的日历**

```bash
python -c "from datetime import date; from src.services.trading_calendar_service import TradingCalendarService; print(TradingCalendarService().sync(date(2018,1,1), date(2026,12,31)))"
```

预期：`{'written': ~3280, 'mismatch': 0}`。**mismatch 非 0 必须先查清再继续**，日历是后续所有缺口归因的基准。

校验交易日总数：

```bash
python -c "import sqlite3; c=sqlite3.connect('data/stock_analysis.db'); print(c.execute(\"SELECT COUNT(*) FROM trading_calendar WHERE is_open=1 AND trade_date>='2018-01-01' AND trade_date<='2026-08-11'\").fetchone()); c.close()"
```

预期：约 2080 个交易日（与 spec 的全窗口估算一致，偏差超过 ±30 需查因）。

- [ ] **Step 6: 提交**

```bash
git add src/services/trading_calendar_service.py tests/test_trading_calendar_service.py
git commit -m "feat: add fail-closed trading calendar service with akshare source"
```

---

## Task 8: G1 — `instrument_master` 新增 `delist_date`

**Files:**

- Modify: `src/storage.py:662`（`InstrumentMaster` ORM）、内联迁移
- Create: `scripts/migrate_instrument_delist_date.py`
- Test: `tests/test_listing_lifecycle_service.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
    def test_instrument_master_has_delist_date(self) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(instrument_master)")}
        finally:
            conn.close()
        self.assertIn("delist_date", cols)
```

- [ ] **Step 2: 运行确认失败**

```bash
python -m pytest tests/test_listing_lifecycle_service.py -v
```

- [ ] **Step 3: 改 ORM 并加迁移**

`src/storage.py`，在 `InstrumentMaster.list_date`（第 662 行）之后插入：

```python
    # 退市日期。缺失该字段时无法区分「停牌」「退市」「抓取失败」三类缺口，
    # 也无法消除幸存者偏差——已退市股票会整体缺席历史回测。
    delist_date = Column(Date, nullable=True, index=True)
```

按 Task 3 Step 4 的方式加 `_migrate_sqlite_instrument_delist_date`，并写 `scripts/migrate_instrument_delist_date.py`（照抄 `scripts/migrate_stock_daily_adj_factor.py` 结构）。

- [ ] **Step 4: 运行确认通过并提交**

```bash
python -m pytest tests/test_listing_lifecycle_service.py -v
python scripts/migrate_instrument_delist_date.py --db ./data/stock_analysis.db
python scripts/migrate_instrument_delist_date.py --db ./data/stock_analysis.db
```

预期：测试 PASS；迁移脚本第二次全 skip。

```bash
git add src/storage.py scripts/migrate_instrument_delist_date.py tests/test_listing_lifecycle_service.py
git commit -m "feat: add delist_date to instrument_master"
```

---

## Task 9: G1 — 上市/退市日期抓取与回填

**Files:**

- Create: `src/services/listing_lifecycle_service.py`
- Test: `tests/test_listing_lifecycle_service.py`（追加）

**关键点**：必须包含**已退市股票**。只同步在市股票会保留幸存者偏差——2018 年上市、2021 年退市的股票在历史回测里完全缺席，而它们恰恰是亏损样本的主要来源。baostock 的 `query_stock_basic` 不传 `code` 时返回全部证券（含已退市），字段含 `ipoDate` / `outDate` / `status`。

- [ ] **Step 1: 追加失败测试**

**测试脚手架同 Task 6**：`setUp` 建临时库并 `DatabaseManager.reset_instance()`，
`tearDown` 先 `reset_instance()` 再 `cleanup()`。`ListingLifecycleService()` 默认连
`DatabaseManager` 单例，不隔离会直接写生产库。

```python
class ListingLifecycleServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # 与 Task 6 的 TradingCalendarSchemaTestCase.setUp 完全一致
        ...

    def tearDown(self) -> None:
        # 与 Task 6 一致：先 reset_instance() 释放 engine，再删临时目录
        ...

    def test_upsert_writes_list_and_delist_date(self) -> None:
        from src.services.listing_lifecycle_service import ListingLifecycleService

        svc = ListingLifecycleService()
        svc.upsert_lifecycle([
            {"code": "000001", "name": "平安银行", "list_date": date(1991, 4, 3),
             "delist_date": None, "listing_status": "active"},
            {"code": "000033", "name": "新都退", "list_date": date(1994, 1, 3),
             "delist_date": date(2016, 5, 26), "listing_status": "delisted"},
        ])

        rows = svc.get_lifecycle(["000001", "000033"])
        self.assertIsNone(rows["000001"]["delist_date"])
        self.assertEqual(rows["000033"]["delist_date"], date(2016, 5, 26))
        self.assertEqual(rows["000033"]["listing_status"], "delisted")

    def test_delisted_instruments_are_not_dropped_by_universe_sync(self) -> None:
        """反例测试：幸存者偏差的直接防线。

        已退市股票必须留在 instrument_master，否则历史回测样本里
        永远看不到它们，亏损样本被系统性抹掉。
        """
        from src.services.listing_lifecycle_service import ListingLifecycleService

        svc = ListingLifecycleService()
        svc.upsert_lifecycle([
            {"code": "000033", "name": "新都退", "list_date": date(1994, 1, 3),
             "delist_date": date(2016, 5, 26), "listing_status": "delisted"},
        ])

        codes = svc.list_codes_alive_on(date(2015, 1, 1))
        self.assertIn("000033", codes)
        codes_after = svc.list_codes_alive_on(date(2020, 1, 1))
        self.assertNotIn("000033", codes_after)
```

- [ ] **Step 2: 运行确认失败**

```bash
python -m pytest tests/test_listing_lifecycle_service.py -v
```

- [ ] **Step 3: 实现服务**

`src/services/listing_lifecycle_service.py`，核心方法：

- `upsert_lifecycle(rows)` — 写 `instrument_master` 的 `list_date` / `delist_date` / `listing_status`
- `get_lifecycle(codes)` — 批量读回
- `list_codes_alive_on(as_of)` — point-in-time 在市清单，条件是 `list_date <= as_of AND (delist_date IS NULL OR as_of < delist_date)`
- `sync_from_baostock()` — 调 `bs.query_stock_basic()` 全量拉取，映射 `ipoDate`→`list_date`、`outDate`→`delist_date`、`status`（1 上市 / 0 退市）→`listing_status`

`sync_from_baostock` 的骨架：

```python
    def sync_from_baostock(self) -> Dict[str, int]:
        import baostock as bs

        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"baostock login failed: {login.error_msg}")
        try:
            rs = bs.query_stock_basic()
            if rs.error_code != "0":
                raise RuntimeError(f"query_stock_basic failed: {rs.error_msg}")
            rows = []
            while rs.next():
                record = dict(zip(rs.fields, rs.get_row_data()))
                if record.get("type") != "1":   # 只要股票，排除指数与基金
                    continue
                code = _normalize_baostock_code(record.get("code", ""))
                if not code:
                    continue
                rows.append({
                    "code": code,
                    "name": record.get("code_name") or code,
                    "list_date": _as_date(record.get("ipoDate")),
                    "delist_date": _as_date(record.get("outDate")),
                    "listing_status": "active" if record.get("status") == "1" else "delisted",
                })
        finally:
            bs.logout()
        written = self.upsert_lifecycle(rows)
        delisted = sum(1 for r in rows if r["listing_status"] == "delisted")
        return {"written": written, "total": len(rows), "delisted": delisted}
```

`_normalize_baostock_code` 把 `sh.600000` / `sz.000001` 转成仓库统一的
`600000` / `000001` 格式。**不要直接调 `data_provider/base.py` 的 `normalize_stock_code`**——
它在 `:119-125` 明确排除了 `SH.` / `SZ.` 形式，`sh.600000` 会被原样返回，
结果是回填出一批孤儿代码。

**复用 `data_provider/baostock_fetcher.py:344` 已有的前缀剥离逻辑**
（`x.split('.')[1] if '.' in x else x`），剥离后再交给 `normalize_stock_code` 做最终归一。

另外 `upsert_lifecycle` 的行字典要带 `market`（`InstrumentMaster.market` 是
`nullable=False`）。走 ORM 有默认值可以省，走裸 SQL 必须显式给 `"cn"`。

- [ ] **Step 4: 运行确认通过**

```bash
python -m pytest tests/test_listing_lifecycle_service.py -v
```

- [ ] **Step 5: 实际同步并校验**

```bash
python -c "from src.services.listing_lifecycle_service import ListingLifecycleService; print(ListingLifecycleService().sync_from_baostock())"
```

预期：`total` 在 5000–6000 之间，`delisted` **明显大于 0**（A 股累计退市数百家）。`delisted == 0` 说明退市股票没拉到，幸存者偏差仍在，**必须查清再继续**。

校验 `list_date` 的合理性：

```bash
python -c "import sqlite3; c=sqlite3.connect('data/stock_analysis.db'); print('null list_date:', c.execute('SELECT COUNT(*) FROM instrument_master WHERE list_date IS NULL').fetchone()); print('delisted:', c.execute(\"SELECT COUNT(*) FROM instrument_master WHERE delist_date IS NOT NULL\").fetchone()); c.close()"
```

- [ ] **Step 6: 提交**

```bash
git add src/services/listing_lifecycle_service.py tests/test_listing_lifecycle_service.py
git commit -m "feat: backfill list_date and delist_date from baostock

Includes delisted instruments; omitting them leaves survivor bias that
silently removes the loss-heavy tail from any historical study."
```

---

## Task 10: C — 回补写入目标参数化（生产入口保持不变）

**Files:**

- Modify: `src/services/fast_backfill_service.py:165`（`_save_day_data` 签名与实现）
- Test: `tests/test_fast_backfill_service.py`（追加）

**先搞清楚一件事：`_save_day_data` 在生产链路上。** 它的唯一调用方是
`backfill_to_trade_date`（`:91`），而后者是活跃的生产入口：

- `api/v1/endpoints/screening.py:119` 暴露的 `POST /api/v1/screening/backfill-to-date`（Web 端「回填至该日」按钮）
- `src/services/data_health_task_service.py:95,197` 的 `backfill_to_date` 治理操作

**因此不能把 `_save_day_data` 整体改写到 staging。** 那样这两条路径会照常返回
`saved_rows > 0`，但生产 `stock_daily` 一行不写，随后的治理审计仍判缺口——
一个不报错的功能失效，比报错难查得多。

正确改法是**把写入目标做成参数**（spec 13.2 的 `BACKFILL_TARGET_TABLE`）：
既有入口继续写 `stock_daily`，阶段 C 的新入口写 staging。

> **本服务拿不到 `DatabaseManager` 的并发保护（Task 2 代码审查实测发现）。**
> `fast_backfill_service.py` 的三处连接（`:106`、`:134`、`:171`）都是裸
> `sqlite3.connect(self.db_path)`，没传 `timeout`，因此只有 Python sqlite3 的
> **5 秒**默认忙等超时；而 Task 2 给 `DatabaseManager` 配的是 30 秒。
> WAL 是文件级持久设置，这条路径能自动享受到，**busy_timeout 是连接级的，享受不到**。
>
> 这正是阶段 C 最可能撞锁的一条路径：Task 12 的数小时回补全程走这里，
> 而维护窗口只挡住了两个入口，Web 数据健康操作等写入方仍在活动（见 Task 2 的
> CHANGELOG 运维警告）。因此本任务必须把这三处连接统一到一个带
> `timeout=30` 的辅助方法上，不要逐处传参——逐处传参下次加连接时必然又漏。
>
> 同样的问题也适用于 Task 7 的 `TradingCalendarService`（它同样走裸 sqlite3），
> 实施 Task 7 时一并处理。

- [ ] **Step 0: 先补配置与测试脚手架，否则后续步骤会引用到不存在的东西**

两件前置，都很小但漏掉会直接卡住：

1. **配置先行。** Step 3 的 `_build_row` 要读 `self._convention_version`，
   而该配置原本排在 Task 11。把它提到这里：`src/config.py` dataclass 加

```python
    # 落库口径版本。变更单位换算、新增必填列时必须递增，
    # 否则续跑逻辑会把旧口径数据误判为已完成而跳过。
    data_convention_version: str = "v1_unadjusted_shares_yuan"
```

   `from_env` 加 `data_convention_version=os.getenv('DATA_CONVENTION_VERSION', 'v1_unadjusted_shares_yuan'),`，
   `.env.example` 同步，并在 `FastBackfillService.__init__`（`:22-36`）里
   `self._convention_version = get_config().data_convention_version`。

**第二件** —— **扩测试建表 DDL。** `tests/test_fast_backfill_service.py:13-31` 的 `_init_stock_daily`
   用裸 `CREATE TABLE` 建 `stock_daily`，**没有 `pre_close` / `adj_convention` 列**。
   Step 3 给生产分支的 INSERT 加了这两列，不扩 DDL 的话 `:77`、`:142`、`:177`
   三条既有用例会以 `table stock_daily has no column named pre_close` 报错。
   在该 helper 的列清单里补 `pre_close REAL, adj_convention TEXT`，
   并新增 `_init_staging` 建 `stock_daily_staging`。

   > `tests/test_market_data_sync_service.py` 不受影响——它通过 `DatabaseManager`
   > 建表，Task 3 的 ORM 改动已经覆盖。

- [ ] **Step 1: 追加失败测试**

该文件里**只有 `_init_stock_daily`（`:11`）和 `_FakeTushareApi`（`:43`）两个既有辅助**，
没有 `_make_service` / `_query` / `_row_hash` / `_init_staging` / `_tushare_fixture_df`。
现有用例都是**在每个测试里直接构造** `FastBackfillService(db_path=..., tushare_api=...,
governance_service=..., min_full_count=..., sleep=...)`（`:89-95`）。

所以下面的片段里，凡是 `_make_service(db_path)` 都表示「照 `:89-95` 原样构造」，
其余 `_init_staging` / `_query` / `_row_hash` / `_tushare_fixture_df`
**都需要在本步一并写出来**，不是既有可调用物：

```python
def _make_service(db_path):
    """照 :89-95 的既有构造方式，仅集中到一处避免重复。"""
    return FastBackfillService(
        db_path=str(db_path),
        tushare_api=_FakeTushareApi(),
        governance_service=SimpleNamespace(
            run_daily_governance=lambda **kw: {
                "run_result": "succeeded", "pass_status": "passed",
                "trade_date": kw["trade_date"],
            }
        ),
        min_full_count=2,
        sleep=lambda _s: None,
    )


def _query(db_path, sql):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _row_hash(db_path, table):
    """整表内容指纹，用于证明生产表一个字节都没被动过。"""
    rows = _query(db_path, f"SELECT * FROM {table} ORDER BY rowid")
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def _init_staging(db_path):
    """建 stock_daily_staging，DDL 与 Task 4 的 ORM 保持一致。"""
    ...


def _tushare_fixture_df():
    """一天的 Tushare daily() 返回，含 pre_close 列。"""
    ...


def test_save_day_data_defaults_to_production_table(tmp_path):
    """既有生产入口的行为必须原样保留。

    backfill_to_trade_date 服务于 Web 的「回填至该日」与数据健康治理，
    改到 staging 会让它们静默失效。
    """
    db_path = tmp_path / "backfill.db"
    _init_stock_daily(db_path)
    _init_staging(db_path)

    svc = _make_service(db_path)
    saved = svc._save_day_data(_tushare_fixture_df())

    assert saved > 0
    assert _query(db_path, "SELECT COUNT(*) FROM stock_daily")[0][0] > 0
    assert _query(db_path, "SELECT COUNT(*) FROM stock_daily_staging")[0][0] == 0


def test_save_day_data_can_target_staging_without_touching_production(tmp_path):
    """反例测试：阶段 C 绝不能碰生产表。

    生产表 2024-2026 的存量正是判断数据源口径差异的证据本身，
    被 INSERT OR REPLACE 覆盖后证据就没了。
    """
    db_path = tmp_path / "backfill.db"
    _init_stock_daily(db_path)
    _init_staging(db_path)
    before = _row_hash(db_path, "stock_daily")

    svc = _make_service(db_path)
    svc._save_day_data(
        _tushare_fixture_df(),
        target_table="stock_daily_staging",
        batch_id="test-batch",
    )

    assert _row_hash(db_path, "stock_daily") == before, "production table was modified"
    rows = _query(
        db_path,
        "SELECT pre_close, adj_convention, batch_id, convention_version "
        "FROM stock_daily_staging",
    )
    assert rows, "staging table is empty"
    assert rows[0][1] == "raw"
    assert rows[0][2] == "test-batch"
```

- [ ] **Step 2: 运行确认失败**

```bash
python -m pytest tests/test_fast_backfill_service.py -v -k save_day_data
```

- [ ] **Step 3: 改签名并参数化写入目标**

`src/services/fast_backfill_service.py:165`，现签名是 `_save_day_data(self, day_df)`。改为：

```python
    def _save_day_data(
        self,
        day_df: pd.DataFrame,
        target_table: str = "stock_daily",
        batch_id: Optional[str] = None,
    ) -> int:
        """把一天的行情写入目标表。

        target_table 默认生产表：backfill_to_trade_date 服务于 Web 回填与
        数据健康治理，改默认值会让这两条路径静默失效。
        阶段 C 的 backfill_range 显式传 stock_daily_staging。
        """
```

批量写入（`executemany` 而非逐行 `execute`）——spec 9.1 指出逐行写补至 2018
约 1160 万行需 40 分钟至 1.6 小时，批量写可降到十分钟内：

```python
        is_staging = target_table == "stock_daily_staging"
        if is_staging:
            columns = (
                "code, date, open, high, low, close, volume, amount, pct_chg, "
                "pre_close, adj_convention, data_source, batch_id, convention_version, "
                "created_at, updated_at"
            )
            placeholders = "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
        else:
            columns = (
                "code, date, open, high, low, close, volume, amount, pct_chg, "
                "pre_close, adj_convention, data_source, created_at, updated_at"
            )
            placeholders = "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"

        payload = [self._build_row(row, is_staging, batch_id, now) for _, row in day_df.iterrows()]
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO {target_table} ({columns}) VALUES ({placeholders})",
                payload,
            )
```

`target_table` 只允许 `stock_daily` 与 `stock_daily_staging` 两个字面量，
在方法开头显式校验后再拼进 SQL——它不是用户输入，但拼接前校验是零成本的。

`_build_row` 中 `pre_close` 取 `row.get("pre_close")`、`adj_convention` 取 `"raw"`
（spec 7.1 定稿枚举，Task 1 已关闭 qfq 转换所以该值成立）、
`convention_version` 取 `self._convention_version`（Step 0 已引入）。

- [ ] **Step 4: 运行确认通过**

```bash
python -m pytest tests/test_fast_backfill_service.py -v
```

预期：全部 PASS，**包括既有断言 `stock_daily` 的用例**（`:77`、`:142`、`:177`）——
它们验证的正是默认写生产表这个行为。前提是 Step 0 已扩过 `_init_stock_daily` 的 DDL；
若它们报 `no column named pre_close`，说明 Step 0 漏做，不是本任务改坏了。

- [ ] **Step 5: 提交**

```bash
git add src/services/fast_backfill_service.py tests/test_fast_backfill_service.py
git commit -m "feat: parameterize backfill write target, add batch write

Default stays stock_daily so the Web backfill endpoint and data health
governance keep working; stage C opts into staging explicitly."
```

---

## Task 11: C — 限速参数化、向后回补入口、完成判定加口径版本位

**Files:**

- Modify: `src/config.py`（新增两项配置）
- Modify: `src/services/fast_backfill_service.py:77-91`（限速）、`:132-138`（`_is_date_complete`）、回补入口
- Modify: `.env.example`
- Test: `tests/test_fast_backfill_service.py`（追加）

**为什么 `_is_date_complete` 必须加版本位**：spec 9.1 实测，现有 561 天中 560 天的股票数超过 4000，而完成判定是 `COUNT(DISTINCT code) >= min_full_count`（默认 3000），**全部会被判为已完成而跳过**。不改造则 `pre_close` 永远为 NULL，整个阶段 C 空转。

- [ ] **Step 1: 追加失败测试**

复用 Task 10 Step 1 写的 `_make_service` / `_init_staging` / `_query`。
`_seed_staging` 与 `_seed_production` 是本步新增的辅助（造 N 只股票的存量行）。

```python
def test_staging_date_with_old_convention_is_not_complete(tmp_path):
    """已有数据但口径版本不匹配时，必须重取而不是跳过。

    这是阶段 C 能否真正拿到 pre_close 的关键：561 天存量的股票数都超过
    阈值，纯计数判定会把它们全部跳过。
    """
    db_path = tmp_path / "backfill.db"
    _init_staging(db_path)
    _seed_staging(db_path, date(2026, 5, 8), codes=4000, convention_version="v0_legacy")

    svc = _make_service(db_path)

    assert svc._is_date_complete(date(2026, 5, 8), target_table="stock_daily_staging") is False


def test_staging_date_with_current_convention_is_complete(tmp_path):
    db_path = tmp_path / "backfill.db"
    _init_staging(db_path)
    _seed_staging(db_path, date(2026, 5, 8), codes=4000,
                  convention_version="v1_unadjusted_shares_yuan")

    svc = _make_service(db_path)

    assert svc._is_date_complete(date(2026, 5, 8), target_table="stock_daily_staging") is True


def test_production_completeness_check_is_unchanged(tmp_path):
    """既有生产入口的完成判定行为必须原样保留。"""
    db_path = tmp_path / "backfill.db"
    _init_stock_daily(db_path)
    _seed_production(db_path, date(2026, 5, 8), codes=4000)

    svc = _make_service(db_path)

    assert svc._is_date_complete(date(2026, 5, 8)) is True
```

- [ ] **Step 2: 运行确认失败**

```bash
python -m pytest tests/test_fast_backfill_service.py -v -k convention
```

- [ ] **Step 3: 加限速配置项**

> `data_convention_version` 已在 Task 10 Step 0 加过，这里只加限速。

`src/config.py` dataclass 追加：

```python
    # 回补限速（次/分钟）。免费档 45，5000 积分档可提到 500。
    backfill_rate_limit_per_min: int = 45
```

`from_env` 追加：

```python
            backfill_rate_limit_per_min=max(1, int(os.getenv('BACKFILL_RATE_LIMIT_PER_MIN', '45'))),
```

`.env.example` 同步，并在 `FastBackfillService.__init__`（`:22-36`）里
`self._rate_limit_per_min = get_config().backfill_rate_limit_per_min`。

- [ ] **Step 4: 改 `_is_date_complete`**

现签名是 `_is_date_complete(self, trade_date: date)`（`:133`），两个既有调用方
（`:49`、`:128`）传的都是 `date` 对象。**保留 `date` 入参**，只加一个目标表参数——
改成 `str` 会依赖 Python 3.12 已废弃的 sqlite3 date 适配器：

```python
    def _is_date_complete(
        self,
        trade_date: date,
        target_table: str = "stock_daily",
    ) -> bool:
        """该日是否已按**当前口径版本**写入。

        对 staging 而言纯行数判定不够：存量数据行数达标但缺 pre_close，
        会让回补整段空转（spec 9.1 实测 561 天中 560 天都会被误判为已完成）。
        生产表没有 convention_version 列，沿用原有的纯计数判定。
        """
        date_str = trade_date.isoformat()
        with sqlite3.connect(self.db_path) as conn:
            if target_table == "stock_daily_staging":
                row = conn.execute(
                    "SELECT COUNT(DISTINCT code) FROM stock_daily_staging "
                    "WHERE date = ? AND convention_version = ?",
                    (date_str, self._convention_version),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT code) FROM stock_daily WHERE date = ?",
                    (date_str,),
                ).fetchone()
        return bool(row and row[0] >= self.min_full_count)
```

`:49` 与 `:128` 两个既有调用点**不需要改**——它们用默认的生产表分支，行为不变。

- [ ] **Step 5: 改限速与新增回补入口**

把 `:77-82` 的 `45` 与 `:92` 的 `sleep(1.3)` 改为从 `self._rate_limit_per_min` 推导：
`sleep_seconds = 60.0 / max(1, self._rate_limit_per_min)`。

新增向后区间回补入口：

```python
    def backfill_range(
        self,
        date_from: date,
        date_to: date,
        batch_id: str,
        target_table: str = "stock_daily_staging",
    ) -> Dict[str, Any]:
        """按交易日历逐日回补到 staging。

        与既有 backfill_to_trade_date 的两点差异：
        1. 起点由调用方指定，而非「最近完整日 + 1」，因此能补 2018-2023
           这段生产表从未覆盖的区间；
        2. 默认写 staging，不碰生产表。
        """
```

交易日列表来自 Task 7 的 `TradingCalendarService.get_trading_days`，**不再**用
`SELECT DISTINCT date FROM stock_daily`（那是用行情反推日历，2018-2023 无数据时得到空集）。

**注意库句柄一致性**：`FastBackfillService` 走裸 `sqlite3.connect(self.db_path)`。
若 `TradingCalendarService` 读的是另一个库文件，测试用 `tmp_path` 时两者会分叉，
而 Task 7 Step 5 已经把**生产**日历灌满 2018-2026——
测试会读到真实日历行，然后「因为错误的原因通过」。

**不能靠 `DatabaseManager(f"sqlite:///{db_path}")` 来注入**：`__new__`（`storage.py:1189-1194`）
是单例，`__init__` 在 `_initialized` 已设时直接返回（`:1203-1204`），
传进去的 URL 会被静默忽略，拿回的还是原来那个实例。

Task 7 已经把 `TradingCalendarService` 定成「统一走裸 sqlite3、构造参数只有
`db_path`、缺省时从 config 取」。**本步不要再改它的签名**，只需在构造时把
`FastBackfillService` 自己的路径传进去：

```python
        calendar = TradingCalendarService(db_path=self.db_path)
        trading_days = calendar.get_trading_days(date_from, date_to, market="cn")
```

这样两个服务读写的是同一个文件，`tmp_path` 场景下也不会误读生产日历。

- [ ] **Step 6: 运行确认通过并提交**

```bash
python -m pytest tests/test_fast_backfill_service.py -v
python -m py_compile src/config.py src/services/fast_backfill_service.py
```

```bash
git add src/config.py src/services/fast_backfill_service.py .env.example tests/test_fast_backfill_service.py
git commit -m "feat: parameterize backfill rate limit, add range entry and convention version gate"
```

---

## Task 12: C — 执行全窗口回补（运维步骤）

**Files:** 无代码改动。这是一次数据作业。

- [ ] **Step 0: 进入维护窗口**

Task 2 Step 4 的开关，回补开工前必须打开：

```bash
$env:DATA_MAINTENANCE_MODE = "true"
```

确认每日同步与 K 线治理确实被跳过（日志里应出现 Task 2 Step 4 的那条 warning）。
**退出条件是 Step 4 的实证数字产出后**，别忘了关。

> **开关只挡住两个入口，不等于数据库安静了。** Task 2 的代码审查实测确认，
> 下列写入方在维护窗口内**照常运行**，必须逐项确认关闭，否则「独占数据库」
> 只是纸面上的：
>
> | 写入方 | 确认方式 |
> | --- | --- |
> | 定时 `screening`（五层流水线，系统最重的写入方） | `SCREENING_SCHEDULE_ENABLED=false` |
> | 定时 `analysis` | `SCHEDULE_ENABLED=false` |
> | 定时 `board_sync` | 对应调度开关置 false |
> | 定时 `kline_deep_audit` | `KLINE_DEEP_AUDIT_SCHEDULE_ENABLED=false` |
> | Web 数据健康页的全部操作（含 `backfill_to_date`） | 回补期间不要操作该页面 |
> | `scripts/_kline_window_sync.py`、`scripts/_kline_targeted_repair.py` | 窗口内不要手工执行 |
>
> **上表的顺序不能反过来：必须先关调度，再开维护开关。** Task 2 的复审实测确认，
> 定时任务失败不会被兜住，而是会终止整个调度进程——`Scheduler._safe_run_named_task`
> （`src/scheduler.py:156`）记完 ERROR 后 `raise`，`run()` 的 `run_pending()` 没有
> try 保护，异常一路到 `main.py:905` 返回退出码 1；`--serve --schedule` 下 API 服务
> 是守护线程，会一起退出。
>
> 而维护窗口恰恰会让定时 `screening` 失败（Task 2 让它抛出点名 `DATA_MAINTENANCE_MODE`
> 的错误，`main.py:464` 再转成 `RuntimeError`）。也就是说：**开着调度器打开维护开关，
> 到第一个筛选时点（默认 07:00）调度进程就会退出。** 回补作业本身不受影响
> （它是独立进程），但你会以为服务挂了。
>
> 另外一点也已在 Task 2 的 CHANGELOG 记录，这里重复因为会直接影响本次作业：
> `Config` 是缓存单例，**进入和退出窗口都需要重启进程**才生效。
> 只改环境变量而不重启，开关等于没动。

- [ ] **Step 1: 小窗口试跑**

先跑一个月验证链路，不要直接开全量：

```bash
python -c "
from datetime import date
from src.services.fast_backfill_service import FastBackfillService
print(FastBackfillService().backfill_range(date(2023,1,1), date(2023,1,31), batch_id='smoke-202301'))
"
```

- [ ] **Step 2: 校验试跑结果**

```bash
python -c "
import sqlite3
c = sqlite3.connect('data/stock_analysis.db')
print('rows:', c.execute(\"SELECT COUNT(*) FROM stock_daily_staging WHERE date LIKE '2023-01%'\").fetchone())
print('null pre_close:', c.execute(\"SELECT COUNT(*) FROM stock_daily_staging WHERE date LIKE '2023-01%' AND pre_close IS NULL\").fetchone())
print('production untouched:', c.execute(\"SELECT COUNT(*) FROM stock_daily WHERE date LIKE '2023-01%'\").fetchone())
c.close()
"
```

三条验收，任一不满足即停止排查：

1. `rows` 约等于 `该月交易日数 × 在市股票数`（2023 年 1 月约 20 天 × 5000 ≈ 10 万）。
2. `null pre_close` **为 0**。非 0 说明 Task 5/10 的落库改造没生效，继续跑全量只会白跑。
3. `production untouched` 为 0（生产表 2023 年本就没有数据），且 `stock_daily` 全表行哈希与回补前一致。

- [ ] **Step 3: 全窗口回补**

确认试跑三条全绿后再开全量。**分年分批**，每批单独 `batch_id`，便于失败时按批重跑：

```bash
python -c "
from datetime import date
from src.services.fast_backfill_service import FastBackfillService
svc = FastBackfillService()
for year in range(2018, 2027):
    result = svc.backfill_range(date(year,1,1), date(year,12,31), batch_id=f'full-{year}')
    print(year, result, flush=True)
"
```

预计耗时：按 45 次/分的免费档限速，全窗口需要数小时。**这一步允许后台长跑**，但必须能断点续跑（Task 11 的口径版本位保证重跑不会跳过）。

- [ ] **Step 4: 产出两个实证数字**

spec 8.0 明确，C 跑完后才有以下两个数字，它们决定 E/F/C2 能否开工：

```bash
python -c "
import sqlite3
c = sqlite3.connect('data/stock_analysis.db')
total = c.execute('SELECT COUNT(*) FROM stock_daily_staging').fetchone()[0]
null_pc = c.execute('SELECT COUNT(*) FROM stock_daily_staging WHERE pre_close IS NULL').fetchone()[0]
by_year = c.execute(\"SELECT substr(date,1,4) y, COUNT(*), COUNT(DISTINCT code) FROM stock_daily_staging GROUP BY y ORDER BY y\").fetchall()
print('total:', total, 'null pre_close:', null_pc)
for r in by_year: print(r)
c.close()
"
```

把结果写入 spec 的实测审计表（1.7b）。**`gap_flat` 占比与配股发生率需要等 E 阶段才能算出**，本阶段只产出行数与 `pre_close` 覆盖率。

- [ ] **Step 5: 退出维护窗口并验证恢复**

```bash
Remove-Item Env:DATA_MAINTENANCE_MODE
```

跑一次日常同步，确认它恢复执行且写入了 `pre_close`：

```bash
python -c "import sqlite3; c=sqlite3.connect('data/stock_analysis.db'); print(c.execute('SELECT COUNT(*) FROM stock_daily WHERE pre_close IS NOT NULL').fetchone()); c.close()"
```

预期：非 0。**这一步不做，生产数据会静默停更。**

- [ ] **Step 6: 记录 CHANGELOG 并提交**

`docs/CHANGELOG.md` 记录：新增 staging 回补通道与交易日历、上市退市日期，
新增 `DATA_MAINTENANCE_MODE` 开关并启用 WAL，生产表既有行未变更。

```bash
git add docs/CHANGELOG.md
git commit -m "docs: record stage A0/A/B/G1/C data infrastructure rollout"
```

---

## 验收（本计划范围内）

对应 spec 第十节验收矩阵中本期可达成的条目：

| # | 条目 | 验证方式 |
| --- | --- | --- |
| 1 | qfq 引信默认关闭且不调 `adj_factor` | `tests/test_tushare_qfq_switch.py` 三条用例（含反例） |
| 2 | 三条写入路径均落 `pre_close` | 各路径单测 + 试跑后 `null pre_close = 0` |
| 3 | staging 未越权写生产表 | Task 10 的行哈希反例测试 + Task 12 Step 2 |
| 4 | 交易日历 fail-closed | `test_uncovered_date_raises_instead_of_guessing` |
| 5 | 日历与 `exchange_calendars` 对照无 mismatch | Task 7 Step 5 |
| 6 | 退市股票已入库 | Task 9 Step 5 的 `delisted > 0` |
| 7 | 口径版本位阻止误跳过 | `test_date_with_old_convention_is_not_complete` |

**未覆盖项**（明确留给后续阶段）：`gap_flat` 占比、配股发生率、复权因子正确性、C2 提升前后的选股差异——这些都依赖 D/E/F，本计划不产出。

---

## 风险与回滚

| 风险 | 影响 | 回滚方式 |
| --- | --- | --- |
| A0 开关误设为 `true` | 生产价格口径翻转 | 设回 `false`，重取受影响日期 |
| 三条写入路径的 INSERT 改动引入列错位 | 日常同步写入错误数据 | 单测覆盖；回滚 commit 后重跑当日同步 |
| baostock 代码格式与仓库不一致 | `instrument_master` 出现孤儿代码 | 按 `batch_id` 删除本次写入行；`normalize_stock_code` 一致性在 Task 9 Step 3 已列为硬要求 |
| 全窗口回补中断 | 数据不完整 | 口径版本位保证断点续跑；按 `batch_id` 重跑单年 |
| 忘记退出维护窗口 | 生产数据静默停更 | Task 12 Step 5 是必做项；退出条件已写入 CHANGELOG 而非仅存记忆 |
| G1 修正 `list_date` 后 K 线审计期望域扩大 | 审计把历史停牌日大量标为缺口 | 影响面被 30 天默认审计窗口限制（spec 9.2）；本期不改审计，观察即可，处置留给阶段 I |
| staging 表体积 | 磁盘占用翻倍（约 1160 万行） | C2 提升后可清空；本阶段需预留磁盘 |

**整体回滚**：本计划所有改动都是新增列、新增表、新增写入通道。撤销方式是回滚对应 commit 并 `DROP TABLE stock_daily_staging` / `trading_calendar`。生产表 `stock_daily` 的既有行**从未被本计划修改**，无需恢复备份。
