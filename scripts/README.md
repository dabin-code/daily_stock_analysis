# scripts 使用指南

本目录下集中存放仓库的「运维 / 一次性 / 构建」脚本，与 `main.py`、`server.py` 这种长期入口区分开。
本文件按用途分组列出每个脚本的作用、本地运行命令、docker 运行命令，便于日常运维时直接对照执行。

> 文档遵循 `AGENTS.md` 第 6 节交付要求：命令以仓库实际可执行内容为准；当脚本签名、依赖或挂载情况发生变化时，请顺手更新本文。

---

## 0. 通用前置说明

### 0.1 命名约定

| 前缀 | 含义 |
| --- | --- |
| `_kline_*` | K 线治理用的私有运维脚本，临时排查/批量修复使用，**未来可能被收编进 service**。可入库但不进 Docker 镜像。 |
| `migrate_*` | 一次性 DB schema 迁移脚本，幂等可重复执行。 |
| `build-*` / `run-*` | 前端 / 桌面端构建与开发启动脚本。 |
| 其他 | 业务运维脚本（审计、回填、批量审批等）。 |

### 0.2 docker 容器中没有 `scripts/` 目录

`docker/Dockerfile` 只 `COPY` 了 `*.py / api / data_provider / bot / patch / src / strategies`，**没有 COPY scripts/**。
所以容器内运行任何 `scripts/xxx.py` 都需要走以下两种方式之一：

**方式 A：临时 `docker cp` 后 `docker exec`（推荐用于偶发运维）**

```powershell
docker cp .\scripts\<脚本名>.py stock-analyzer:/app/<脚本名>.py
docker exec stock-analyzer python /app/<脚本名>.py <参数>
docker exec stock-analyzer rm /app/<脚本名>.py   # 可选清理
```

**方式 B：在 `docker/docker-compose.yml` 的 `stock-analyzer` 服务下追加只读挂载（推荐用于长期高频使用）**

```yaml
volumes:
  - ../scripts:/app/scripts:ro
```

挂上之后所有命令都可以直接：

```powershell
docker exec stock-analyzer python scripts/<脚本名>.py <参数>
```

下文 docker 运行命令默认使用「方式 A」给出（更稳，零改动）。挂载方式 B 时把命令里的 `python /app/<脚本名>.py` 替换成 `python scripts/<脚本名>.py` 即可。

### 0.3 数据库路径

- 本地运行：默认 `./data/stock_analysis.db`，可通过环境变量 `DATABASE_PATH` 覆盖。
- 容器内运行：镜像 `ENV DATABASE_PATH=/app/data/stock_analysis.db`，宿主机 `./data` 通过 `docker-compose.yml` 挂载到 `/app/data`，**和宿主机看到的是同一个 SQLite 文件**。
- ⚠️ 注意：写库类脚本运行时若 stock-analyzer 容器同时在写库（如 schedule 触发的 sync / governance 正在跑），会触发 SQLite 写锁竞争。**优先选择只读脚本同步执行，写库脚本请确认容器无并发写任务（或临时 `docker compose stop stock-analyzer`）**。

### 0.4 本地 Python 环境要求

- Python 3.11（与镜像基础版本一致）
- `pip install -r requirements.txt` 后，再额外按需安装 `pyinstaller` / `flake8` / `pytest`
- 建议 `python -m venv .venv` 后再装依赖，避免污染全局环境

---

## 1. K 线数据治理（核心运维）

### 1.1 `audit_kline_completeness.py` — 单日 K 线治理 / 审计

**作用**：对指定 `trade_date` 跑「sync → audit → repair → skip_policy → reaudit」完整链路，等价于定时任务 `kline_governance` 的内部逻辑。补单日数据、修复 audit not_passed 的首选入口。

支持两种模式：

- `--repair`：跑完整 daily governance（写库，会拉外网）
- 不加 `--repair`：仅 audit + 把 open gap 提升为 pending_retry（不补数据）

**本地运行**

```powershell
# dry-run 看看会做什么
python scripts/audit_kline_completeness.py --repair --trade-date 2026-05-21 --market cn --dry-run

# 仅审计（不补数据）
python scripts/audit_kline_completeness.py --trade-date 2026-05-21 --market cn

# 单日完整治理（含外网回补）
python scripts/audit_kline_completeness.py --repair --trade-date 2026-05-21 --market cn
```

**docker 运行**

```powershell
docker cp .\scripts\audit_kline_completeness.py stock-analyzer:/app/audit_kline_completeness.py
docker exec stock-analyzer python /app/audit_kline_completeness.py --repair --trade-date 2026-05-21 --market cn
```

**返回码**：`0` 表示 `run_result=succeeded && pass_status=passed`，其他返回 `1`。

---

### 1.2 `_kline_window_sync.py` — 批量补最近 N 个交易日

**作用**：扫描 `stock_daily` 上最近 N 个交易日，对覆盖率 `<100%` 的日期挨个调 `MarketDataSyncService.sync_trade_date`。适合补「连续多日缺失」的场景，比 `_kline_targeted_repair.py` 更粗粒度但更快。

**本地运行**

```powershell
# dry-run 查看会处理多少天
python scripts/_kline_window_sync.py --days 100 --dry-run

# 跑最近 100 个交易日（默认 force=False，跳过已入库）
python scripts/_kline_window_sync.py --days 100

# 强制覆盖每一天（适合修复脏数据）
python scripts/_kline_window_sync.py --days 30 --force
```

**docker 运行**

```powershell
docker cp .\scripts\_kline_window_sync.py stock-analyzer:/app/_kline_window_sync.py
docker exec stock-analyzer python /app/_kline_window_sync.py --days 100
```

---

### 1.3 `_kline_targeted_repair.py` — 精准补 pending_retry 缺口

**作用**：扫描 `kline_audit_gaps` 中 `status=pending_retry` 且 `scope=symbol_range_gap` 的记录，按 `trade_date` 聚合需要回补的代码列表，逐日调 `sync_trade_date(stock_codes=[...], force=True)`。带 Tushare 限流 sleep（默认 1.5s/日，对应 ~40 次/分钟），避开 50 次/分钟硬上限。

**本地运行**

```powershell
# dry-run 查看每个日期需要补多少只
python scripts/_kline_targeted_repair.py --dry-run

# 实际跑（每日间隔 1.5s）
python scripts/_kline_targeted_repair.py

# 调整限流间隔（如果改了 Tushare 套餐）
python scripts/_kline_targeted_repair.py --rate-sleep 0.8
```

**docker 运行**

```powershell
docker cp .\scripts\_kline_targeted_repair.py stock-analyzer:/app/_kline_targeted_repair.py
docker exec stock-analyzer python /app/_kline_targeted_repair.py
```

---

### 1.4 `_kline_master_data_treatment.py` — 一次性主数据治理（3 个 Phase）

**作用**：

- **Phase A**：用 `stock_daily.MIN(date)` 回填 `instrument_master.list_date` 中的 NULL 行（解决「列表里有但不知何时上市」造成的虚假缺口）
- **Phase B**：把长期无新数据的 `cn-active` 股票标 `delisted`（阈值默认 30 个交易日）
- **Phase C**：sweep `kline_audit_gaps`：已经实际入库的 `pending_retry` 标 `healthy`

默认 dry-run，加 `--apply` 才写库。

**本地运行**

```powershell
# 全量 dry-run（推荐先跑这条，看影响范围）
python scripts/_kline_master_data_treatment.py

# 全量 apply
python scripts/_kline_master_data_treatment.py --apply

# 只跑 Phase A + C，跳过 delisted 标记
python scripts/_kline_master_data_treatment.py --phases ac --apply

# 调整退市判定阈值
python scripts/_kline_master_data_treatment.py --apply --delist-threshold-trading-days 60
```

**docker 运行**

```powershell
docker cp .\scripts\_kline_master_data_treatment.py stock-analyzer:/app/_kline_master_data_treatment.py
docker exec stock-analyzer python /app/_kline_master_data_treatment.py --apply
```

---

### 1.5 `_kline_bulk_approve_skip.py` — 批量将 pending_retry 转 approved_skip

**作用**：把所有 `status=pending_retry` 的 gap 整体审批为 `approved_skip`，写 `kline_skip_registry` + `kline_audit_gaps` + 审计事件。默认跳过 `market_day_gap`（全市场缺失需人工审视）。

**适用场景**：bulk 哨兵 + 6 数据源全部确认无源数据后的清理。

**本地运行**

```powershell
# dry-run
python scripts/_kline_bulk_approve_skip.py

# 实际写库
python scripts/_kline_bulk_approve_skip.py --apply

# 自定义 reason / approved_by / notes
python scripts/_kline_bulk_approve_skip.py --apply --reason-type stale_listing --approved-by ops_2026q2 --notes "确认 2026Q2 退市过渡期"

# 同时处理 market_day_gap（谨慎！）
python scripts/_kline_bulk_approve_skip.py --apply --include-market-day-gap
```

**docker 运行**

```powershell
docker cp .\scripts\_kline_bulk_approve_skip.py stock-analyzer:/app/_kline_bulk_approve_skip.py
docker exec stock-analyzer python /app/_kline_bulk_approve_skip.py --apply
```

---

### 1.6 `approve_kline_skip.py` — 单条 / 带过滤的 candidate_skip → approved_skip 审批

**作用**：从 `kline_skip_registry` 列出 `status=candidate_skip` 的待审项，支持按 code / trade_date / 区间过滤，写库前需要明确指定 `--approved-by` 和 `--reason-type`。

**本地运行**

```powershell
# 列出所有 candidate
python scripts/approve_kline_skip.py --list

# 列出指定代码的 candidate
python scripts/approve_kline_skip.py --list --code 600519

# dry-run 审批
python scripts/approve_kline_skip.py --code 600519 --trade-date 2026-05-19 \
    --approved-by ops --reason-type halted --notes "停牌确认" --dry-run

# 实际审批
python scripts/approve_kline_skip.py --code 600519 --trade-date 2026-05-19 \
    --approved-by ops --reason-type halted --notes "停牌确认"
```

**docker 运行**

```powershell
docker cp .\scripts\approve_kline_skip.py stock-analyzer:/app/approve_kline_skip.py
docker exec stock-analyzer python /app/approve_kline_skip.py --list
```

---

## 2. K 线诊断（只读，不写库）

### 2.1 `_kline_data_health_check.py` — 一站式体检

**作用**：DB 基本信息、`instrument_master` 主数据画像、`stock_daily` 覆盖度、最近 100 天每日覆盖率、缺口 Top 30 股、`kline_audit_runs` 最近 5 次、`kline_audit_gaps` 状态分布。**只读连接**（`mode=ro`），可在容器活跃时安全运行。

**本地运行**

```powershell
python scripts/_kline_data_health_check.py

# 自定义 DB 路径
$env:DATABASE_PATH = ".\data\stock_analysis.db"; python scripts/_kline_data_health_check.py
```

**docker 运行**

```powershell
docker cp .\scripts\_kline_data_health_check.py stock-analyzer:/app/_kline_data_health_check.py
docker exec stock-analyzer python /app/_kline_data_health_check.py
```

---

### 2.2 `_kline_pending_inspect.py` — pending_retry 缺口分类

**作用**：扫描所有 `status=pending_retry` 的 gap，按「已退市 / 区间整段无数据 / 部分缺 / 全部已入库 / 无窗口」分桶，定位真实欠账规模。

**本地运行**

```powershell
python scripts/_kline_pending_inspect.py
```

**docker 运行**

```powershell
docker cp .\scripts\_kline_pending_inspect.py stock-analyzer:/app/_kline_pending_inspect.py
docker exec stock-analyzer python /app/_kline_pending_inspect.py
```

---

### 2.3 `_kline_root_cause_probe.py` — 缺口根因排查

**作用**：

- A. `instrument_master.list_date` 填充质量分布
- B. 特定灾难日（脚本里写死 `2026-03-23`，可按需改源）实际入库情况
- C. Top 30 缺口股「首入库日 vs list_date」对比
- D. 扣除上市前 / 退市后空缺后的真实欠账规模

**本地运行**

```powershell
python scripts/_kline_root_cause_probe.py
```

**docker 运行**

```powershell
docker cp .\scripts\_kline_root_cause_probe.py stock-analyzer:/app/_kline_root_cause_probe.py
docker exec stock-analyzer python /app/_kline_root_cause_probe.py
```

---

### 2.4 `_kline_market_day_probe.py` — 交易日 / 入库情况探测

**作用**：对指定窗口（脚本里写死 `2026-03-25 ~ 2026-04-15`）：

- A. 每日 `stock_daily` 入库行数
- B. 通过 `exchange_calendars` 判定 XSHG 是否交易日
- C. 抽取一只样本代码（`000959`）在窗口内的实际入库情况

> 窗口和样本代码硬编码在脚本里，需要查不同时段时改源。

**本地运行**

```powershell
python scripts/_kline_market_day_probe.py
```

**docker 运行**

```powershell
docker cp .\scripts\_kline_market_day_probe.py stock-analyzer:/app/_kline_market_day_probe.py
docker exec stock-analyzer python /app/_kline_market_day_probe.py
```

---

### 2.5 `_kline_schema_inspect.py` — audit 表结构速查

**作用**：打印 `kline_audit_runs` 和 `kline_audit_gaps` 的列名。一次性最小化排错工具。

**本地运行**

```powershell
python scripts/_kline_schema_inspect.py
```

**docker 运行**

```powershell
docker cp .\scripts\_kline_schema_inspect.py stock-analyzer:/app/_kline_schema_inspect.py
docker exec stock-analyzer python /app/_kline_schema_inspect.py
```

---

## 3. 数据回填

### 3.1 `fast_backfill.py` — Tushare 批量历史回填

**作用**：用 `tushare.daily(trade_date=...)` 一次拿全市场某日数据（~5000 行/次），比逐只调用 5000+ 次快 25 倍。也支持 `--to-date` 直接调 `FastBackfillService.backfill_to_trade_date()` 回填到指定日。

**本地运行**

```powershell
# dry-run
python scripts/fast_backfill.py --dry-run

# 回填最近 250 个交易日（默认）
python scripts/fast_backfill.py

# 回填最近 30 天
python scripts/fast_backfill.py --days 30

# 回填到指定日（走 FastBackfillService，含治理）
python scripts/fast_backfill.py --to-date 2026-05-21 --market cn
```

**docker 运行**

```powershell
docker cp .\scripts\fast_backfill.py stock-analyzer:/app/fast_backfill.py
docker exec stock-analyzer python /app/fast_backfill.py --to-date 2026-05-21
```

**注意**：本脚本自带每分钟 45 次的客户端限速，跑 250 天大约耗时 15 分钟。

---

### 3.2 `data_reset_and_backfill.py` — 重置 + 大规模回填

**作用**：可选地清空 `stock_daily`，然后回填「自选股 + 主要指数」（350 天）+ 「筛选宇宙」（分批）。**有备份机制**：清空前自动 `cp` 一份 `stock_analysis.db.backup_<ts>`。

**本地运行**

```powershell
# 仅验证当前数据完整性
python scripts/data_reset_and_backfill.py --verify

# 只回填自选股 + 指数（不清空）
python scripts/data_reset_and_backfill.py --backfill-watchlist

# 只回填宇宙（不清空）
python scripts/data_reset_and_backfill.py --backfill-universe --batch-size 50

# 重置 + 自选股 + 宇宙（一键全量）
python scripts/data_reset_and_backfill.py --reset --backfill-watchlist --backfill-universe

# 限制宇宙回填数量（测试用）
python scripts/data_reset_and_backfill.py --backfill-universe --max-stocks 100
```

**docker 运行**

```powershell
docker cp .\scripts\data_reset_and_backfill.py stock-analyzer:/app/data_reset_and_backfill.py
docker exec stock-analyzer python /app/data_reset_and_backfill.py --verify
```

⚠️ **`--reset` 不可恢复**（除自动备份外），生产前务必确认。

---

### 3.3 `backfill_instrument_boards.py` — 回填股票板块归属

**作用**：调 `BoardSyncService` 把指定股票（或 `cn-active` 全量）的板块归属写入本地 DB。来源默认 `efinance`。

**本地运行**

```powershell
# 全量回填 cn-active 非 ST
python scripts/backfill_instrument_boards.py

# 限定 100 只
python scripts/backfill_instrument_boards.py --limit 100

# 仅指定代码
python scripts/backfill_instrument_boards.py --codes 600519,000001,002594

# dry-run
python scripts/backfill_instrument_boards.py --dry-run

# 限流 sleep
python scripts/backfill_instrument_boards.py --sleep-seconds 0.5
```

**docker 运行**

```powershell
docker cp .\scripts\backfill_instrument_boards.py stock-analyzer:/app/backfill_instrument_boards.py
docker exec stock-analyzer python /app/backfill_instrument_boards.py --codes 600519,000001
```

---

## 4. DB 一次性迁移

所有 `migrate_*` 脚本都遵循同样的约定：

- **幂等**：重复执行安全。
- **默认写库**，但都支持 `--dry-run` 只打印计划。
- 大部分迁移也在 `src/storage.py` 的 `DatabaseManager.__init__` 启动时内联执行；这些脚本是「DB 不在容器内 / 想在服务启动前确定性应用」的离线路径。
- 默认 DB 路径 `data/stock_analysis.db`，可用 `--db` 覆盖。

### 4.1 `migrate_stock_daily_adj_factor.py`

**作用**：`stock_daily` 加 `adj_factor` / `adj_anchor_date` / `adj_factor_source` 列，并把历史行 `adj_factor` 回填为 `1.0`，`source='legacy_assume_one'`。

```powershell
# 本地
python scripts/migrate_stock_daily_adj_factor.py
python scripts/migrate_stock_daily_adj_factor.py --dry-run
python scripts/migrate_stock_daily_adj_factor.py --db .\data\stock_analysis.db

# docker
docker cp .\scripts\migrate_stock_daily_adj_factor.py stock-analyzer:/app/migrate_stock_daily_adj_factor.py
docker exec stock-analyzer python /app/migrate_stock_daily_adj_factor.py --db /app/data/stock_analysis.db
```

### 4.2 `migrate_five_layer_columns.py`

**作用**：`screening_candidates` 加 9 个五层决策列（`trade_stage`、`setup_type`、`risk_level` 等）。

```powershell
# 本地
python scripts/migrate_five_layer_columns.py

# docker
docker cp .\scripts\migrate_five_layer_columns.py stock-analyzer:/app/migrate_five_layer_columns.py
docker exec stock-analyzer python /app/migrate_five_layer_columns.py
```

### 4.3 `migrate_screening_candidate_updated_at.py`

**作用**：`screening_candidates` 加 `updated_at` 列 + 索引，回填值为 `created_at`。

```powershell
# 本地
python scripts/migrate_screening_candidate_updated_at.py
python scripts/migrate_screening_candidate_updated_at.py --dry-run

# docker
docker cp .\scripts\migrate_screening_candidate_updated_at.py stock-analyzer:/app/migrate_screening_candidate_updated_at.py
docker exec stock-analyzer python /app/migrate_screening_candidate_updated_at.py --db /app/data/stock_analysis.db
```

### 4.4 `migrate_evaluation_suppression_reason.py`

**作用**：`five_layer_backtest_evaluations` 加 `suppression_reason` 列，按旧规则回填历史行。

```powershell
# 本地
python scripts/migrate_evaluation_suppression_reason.py
python scripts/migrate_evaluation_suppression_reason.py --dry-run

# docker
docker cp .\scripts\migrate_evaluation_suppression_reason.py stock-analyzer:/app/migrate_evaluation_suppression_reason.py
docker exec stock-analyzer python /app/migrate_evaluation_suppression_reason.py --db /app/data/stock_analysis.db
```

### 4.5 `fix_none_setup_type.sql` — 历史脏数据清理 SQL

**作用**：把 `screening_candidates`、`five_layer_backtest_evaluations` 里值为字符串 `'none'` 的 setup_type 列清成 NULL，并删除回测汇总里基于此的脏行。

```powershell
# 本地（需要本机有 sqlite3 CLI；注意 PowerShell 5 默认编码非 UTF-8，建议用 cmd 或加 -Encoding UTF8）
sqlite3 .\data\stock_analysis.db ".read scripts/fix_none_setup_type.sql"

# docker：先把 SQL 拷进去，再用 python 执行（容器没自带 sqlite3 CLI）
docker cp .\scripts\fix_none_setup_type.sql stock-analyzer:/tmp/fix_none_setup_type.sql
docker exec stock-analyzer python -c "import sqlite3; sql=open('/tmp/fix_none_setup_type.sql').read(); c=sqlite3.connect('/app/data/stock_analysis.db'); c.executescript(sql); c.commit(); c.close()"
```

### 4.6 `backup_production_db.py` — 生产库整库备份

**作用**：用 sqlite3 的 `backup()` API 给整库做一致性快照（不是文件拷贝，容器并发写时也安全），产物写到 `--out` 目录下的 `<库名>_<时间戳>.db`。**只读源库，幂等**。用于历史回补这类数小时长时作业前的兜底快照，属于粗粒度回滚手段，不承担表级回滚职责。

`--db` 不传时取配置里的 `database_path`（即 `DATABASE_PATH` 约定），`--out` 默认 `./data/backups`。

```powershell
# 本地：备份默认库到默认目录
python scripts/backup_production_db.py

# 本地：指定库和输出目录
python scripts/backup_production_db.py --db .\data\stock_analysis.db --out .\data\backups

# docker
docker cp .\scripts\backup_production_db.py stock-analyzer:/app/backup_production_db.py
docker exec stock-analyzer python /app/backup_production_db.py --db /app/data/stock_analysis.db --out /app/data/backups
```

> 备份产物和源库在同一磁盘上时不抗硬件故障，重要节点请再往仓库外冷备一份。

### 4.7 `migrate_stock_daily_pre_close.py`

**作用**：`stock_daily` 加 `pre_close`（前收盘价）/ `adj_convention`（该行价格的复权口径：`raw` / `qfq` / `unknown`）两列 + `adj_convention` 索引。`pre_close` 是免费重建复权因子的唯一依据（`ratio = pre_close(t) / close(prev_observation)`）；`adj_convention` 用于标注逐行口径，因为各数据源口径不一致（Tushare 不复权、Efinance 疑似前复权），混存会让复权重建在数据源边界上出错。

**不回填**（有意为之，不要「顺手」补上）：`pre_close` 没有可辩护的默认值，写任何值都是在编造数据，且它直接喂给复权因子重建；存量行的 `adj_convention` 也无法事后判定。存量行保持 NULL，由后续重新抓取的阶段填充。

> 生产库上建索引会重写库文件并使其变大（实测 297 万行、1.23 GiB 的库副本上耗时数秒、文件增长约 26 MB，含 WAL 归并）。先在副本上跑一遍再决定何时对生产库执行。

```powershell
# 本地
python scripts/migrate_stock_daily_pre_close.py
python scripts/migrate_stock_daily_pre_close.py --dry-run
python scripts/migrate_stock_daily_pre_close.py --db .\data\stock_analysis.db

# docker
docker cp .\scripts\migrate_stock_daily_pre_close.py stock-analyzer:/app/migrate_stock_daily_pre_close.py
docker exec stock-analyzer python /app/migrate_stock_daily_pre_close.py --db /app/data/stock_analysis.db
```

---

## 5. 历史回测 / 抽样

### 5.1 `run_historical_random_screening.py` — 随机抽历史交易日批量选股

**作用**：从 `[start_date, end_date]` 区间 `stock_daily` 实际存在的交易日里随机抽样 N 天，逐日调 `ScreeningTaskService` 跑历史选股，写入 `screening_runs`（`trigger_type='historical_random_sample'`）。

**本地运行**

```powershell
# 默认：2024-04-19 ~ 2026-05-11 之间随机 100 天
python scripts/run_historical_random_screening.py

# 缩小范围 + 限定样本数
python scripts/run_historical_random_screening.py --start-date 2026-01-01 --end-date 2026-05-01 --sample-days 20

# 自定义 candidate_limit / ai_top_k
python scripts/run_historical_random_screening.py --sample-days 50 --candidate-limit 20 --ai-top-k 5

# 固定随机种子（可复现）
python scripts/run_historical_random_screening.py --sample-days 50 --seed 42

# dry-run（不写库，只看会选哪些日期）
python scripts/run_historical_random_screening.py --sample-days 10 --dry-run

# 强制覆盖（删除同日期同配置的旧 run 后重跑）
python scripts/run_historical_random_screening.py --sample-days 10 --force

# 任一日失败就退出（默认不退出）
python scripts/run_historical_random_screening.py --sample-days 50 --fail-fast
```

**docker 运行**

```powershell
docker cp .\scripts\run_historical_random_screening.py stock-analyzer:/app/run_historical_random_screening.py
docker exec stock-analyzer python /app/run_historical_random_screening.py --sample-days 50 --seed 42
```

⚠️ 这个脚本会跑完整选股链路（含 AI 二审，若 `--ai-top-k > 0`），耗时和 token 消耗都不低。

---

## 6. CI / 治理校验

### 6.1 `ci_gate.sh` — 后端 CI gate

**作用**：CI 用的 backend-gate，含 `py_compile` / `flake8` 关键级别检查 / `test.sh code` / `test.sh yfinance` / `pytest -m "not network"`。本地等价于「上传前体检」。

**本地运行**（需要 git bash / WSL / macOS / Linux）

```bash
./scripts/ci_gate.sh
```

PowerShell 用户可分步执行：

```powershell
python -m py_compile main.py src/config.py src/auth.py src/analyzer.py src/notification.py
python -m py_compile src/storage.py src/scheduler.py src/search_service.py
python -m py_compile src/market_analyzer.py src/stock_analyzer.py
Get-ChildItem data_provider\*.py | ForEach-Object { python -m py_compile $_.FullName }
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
bash .\test.sh code        # 需要 git bash / WSL，PowerShell 原生跑不了 .sh
bash .\test.sh yfinance
python -m pytest -m "not network"
```

**docker 运行**：不建议，CI gate 走宿主机环境更接近 GitHub Actions。

---

### 6.2 `check_ai_assets.py` — AI 协作资产一致性校验

**作用**：校验 `AGENTS.md` 是真源、`CLAUDE.md` 是软链、`.github/copilot-instructions.md` 包含必要片段、`.github/instructions/*` 完整、`.claude/skills/` 必备文件存在、`.gitignore` 含必要规则、`.claude/` 下未追踪规则外资产。

CI 工作流 `ai-governance` 走的就是这条。**修改任何 AI 协作治理资产后必须本地跑一次**。

**本地运行**

```powershell
python scripts/check_ai_assets.py
```

**docker 运行**：不建议，校验对象都是仓库根目录的文件，宿主机直接跑即可。

---

## 7. 桌面端 / 前端构建

### 7.1 Windows 构建

```powershell
# 一键全量（Web 构建 + PyInstaller 后端打包 + Electron 桌面端打包）
.\scripts\build-all.ps1

# 仅后端（Web 静态 + PyInstaller）
.\scripts\build-backend.ps1

# 仅桌面端（需先跑 build-backend.ps1）
.\scripts\build-desktop.ps1

# 桌面端开发模式（不打包，热重载）
.\scripts\run-desktop.ps1
```

**前置依赖**：

- Node.js 20+、npm
- Python 3.11 + `pip install -r requirements.txt`
- Windows 开发者模式（`build-desktop.ps1` 要求，否则 electron-builder 无法解压含 symlink 的缓存）；CI 或临时跳过可设 `$env:DSA_SKIP_DEVMODE_CHECK = 'true'`
- 自定义 Python 解释器：`$env:PYTHON_BIN = 'C:\path\to\python.exe'`

### 7.2 macOS 构建

```bash
# 一键全量
bash scripts/build-all-macos.sh

# 仅后端
bash scripts/build-backend-macos.sh

# 仅桌面端
bash scripts/build-desktop-macos.sh

# 指定 mac 架构（默认随宿主机）
DSA_MAC_ARCH=arm64 bash scripts/build-desktop-macos.sh
DSA_MAC_ARCH=x64   bash scripts/build-desktop-macos.sh
```

**docker 运行**：构建脚本本身依赖宿主机的 Node / Python / Electron 工具链，不在容器内运行。

---

## 8. 常见运维场景速查

| 场景 | 推荐顺序 |
| --- | --- |
| 「最近一次 passed audit 退回到很早的日期，选股跑了旧数据」 | `_kline_data_health_check.py` 看 audit 分布 → `audit_kline_completeness.py --repair` 按缺失日逐天补 |
| 「stock_daily 大段缺失（容器掉过/重启过）」 | `_kline_window_sync.py --days 30` 一次性扫近 30 天 → 再跑 `audit_kline_completeness.py --repair` 单日治理 |
| 「pending_retry 越攒越多」 | `_kline_pending_inspect.py` 分类 → `_kline_root_cause_probe.py` 找根因 → `_kline_master_data_treatment.py --apply` 主数据治理 + `_kline_bulk_approve_skip.py --apply` 批量审批 |
| 「全新部署 / 数据库空白」 | `data_reset_and_backfill.py --reset --backfill-watchlist --backfill-universe` → `fast_backfill.py --to-date <today>` |
| 「DB schema 落后于代码」 | 按需跑对应的 `migrate_*` 脚本（启动时也会内联跑一次，幂等） |
| 「评估选股策略历史表现」 | `run_historical_random_screening.py --sample-days 50 --seed 42` |
| 「上线前体检」 | `python scripts/check_ai_assets.py` + `./scripts/ci_gate.sh` |

---

## 9. 如何新增脚本

1. 严格区分「**业务运维脚本（公开命名）**」与「**临时排查脚本（`_` 前缀）**」。
2. 所有 Python 脚本须支持 `--help`，并优先支持 `--dry-run`。
3. 写库类脚本默认 `--dry-run`，需明确加 `--apply` 才生效。
4. 默认 DB 路径用 `os.environ.get("DATABASE_PATH", "./data/stock_analysis.db")`，复用主程序的路径约定。
5. 顶部 docstring 写明：脚本意图、使用场景、回归风险、是否幂等。
6. 加完脚本后，**同步更新本文件对应分组**，否则下次自己也找不到。
7. 如果脚本需要在容器内长期可用，**优先考虑收编到 `src/services/` 作为服务方法 + CLI 子命令**，而不是依赖 `docker cp`。

