# 选股回测重构：进度基线与防跑偏边界

**更新于** 2026-08-12。本文是两份实施计划的执行状态快照，用于在上下文被压缩后
快速恢复「做到哪、下一步做什么、什么不能做」，不替代 spec 与 plan 本身。

- 设计：`docs/superpowers/specs/2026-08-11-signal-research-backtest-design.md`（v7）
- 设计：`docs/superpowers/specs/2026-08-11-historical-data-infrastructure-design.md`（v8）
- 计划：`docs/superpowers/plans/2026-08-11-signal-research-backtest-stage-0.md`
- 计划：`docs/superpowers/plans/2026-08-11-historical-data-infra-stage-a.md`

---

## 1. 原始目标（未变）

用户的诉求是**验证选股信号是否真的有效**，而不是造一个更漂亮的回测界面。
原系统至今无法正常回测任何一条选股结果，也无法回测五层选股策略。
因此判定标准始终是：**结论可信**——任何反常结果都必须能归因到策略本身，
而不是实现缺陷或数据缺陷。

由此拆出两条线，顺序不可调换：

1. **数据基础设施**：让历史数据在时点上正确（复权、日历、退市、口径）。
2. **信号研究回测**：在可信数据上做重放、消融与统计推断。

---

## 2. 当前位置

```
数据基础设施  A0 ✅ → A ✅ → B ✅(已同步) → G1 ⚠(代码 ✅ / 数据阻塞) → C ✅(962 万行已入 staging)
              → D(阻塞已解除，未开工) → K0 → C2 → E → F → H → I → J → K → L
信号研究回测  阶段 0 ✅ → 阶段 1(接口设计已出笔记) → 阶段 2+ ⛔ 阻塞于 J
```

**C 已完成，D 的暂缓理由（「需 C 产出后才有对象」）随之解除**，但 D 不在最初
审计批准的开工范围（A0/A/B/G1/C）内，是否开工需要确认。C2 提升属于用户可见
变更，必须先有 K0 的差异证据。

审计定论是**分段 Go、完整确认性回测 No-Go**，该定论至今成立。

---

## 3. 已完成

### 生产 hotfix

| commit | 内容 |
| --- | --- |
| `b6f128b` | 统一全部 `stock_daily` 写入路径的成交量/成交额单位 |

### 数据基础设施 Task 1–11

| commit | 任务 |
| --- | --- |
| `e1b2279` | A0 拆 Tushare 前复权引信（`TUSHARE_QFQ_ENABLED` 默认关） |
| `6b0d53b` `67f3db5` `37f8cb9` `c36b26b` | A 备份脚本、WAL、维护窗口开关及其安全护栏 |
| `67e1710` `823b7d2` | A `stock_daily` 新增 `pre_close` / `adj_convention` |
| `6b890b1` `ea90d39` | A 新增 `stock_daily_staging` 表 |
| `6850623` `99c047c` | A 三条写入路径落 `pre_close`，并校验口径枚举 |
| `8a66950` `d9fb192` | B 交易日历建表与 fail-closed 查询 |
| `43d793d` `f8c378c` | G1 `instrument_master` 新增 `delist_date` 与回填 |
| `68c76f0` `d384931` | C 回补写入目标参数化、限速参数化、口径版本位 |

### 信号研究回测 阶段 0 Task 1–9

| commit | 任务 |
| --- | --- |
| `19e23a8` | 修复 `DatabaseManager` 单例泄漏导致的测试污染 |
| `b4d184b` | L2 板块热度降级时告警而非静默返回空 |
| `f647d9a` | 主题数量上限在主识别路径生效 |
| `641e536` | 黄金用例对齐涨停准入规则 |
| `5a34a69` | **setup 级止损统一解析（核心根因）** |
| `a633e0e` `240c0ea` | 保留 `raw_rule_score` |
| `88a603b` | 回测运行表七个版本字段全部落地 |
| `3412787` `fd1c9ae` | 顶层指标改为 `entry` 单族 |
| `0f27946` `9b69a90` | CHANGELOG 同步与实测效果记录 |

### 测试与运维

| commit | 内容 |
| --- | --- |
| `3ed418e` | 测试隔离护栏：默认重定向到临时库，写生产库直接失败，解锁并行 |

---

## 4. 已固化的护栏（勿回退）

这些是事故与实测换来的，改动时必须先理解成因：

- **测试默认不得触碰生产库。** `tests/conftest.py` 把 `DATABASE_PATH` 重定向到
  会话级临时库，并包住 `sqlite3.connect` 与 `sqlite3.dbapi2.connect`，
  记录本进程打开生产库的动作，未标记的用例一旦打开即 `pytest.fail`。
  确需读真实数据的用例必须显式标 `@pytest.mark.real_database`。
  `tests/test_production_db_guard.py` 保证这条护栏本身有效，覆盖裸 sqlite3
  与 SQLAlchemy 两条路径。
  **成因**：2026-08-12 生产库首页被并发写坏，已从 08-11 18:18 备份恢复。
  多个 pytest 进程与调度进程同时对同一 SQLite 文件跑 `create_all` 与内联迁移。
  **不要退回按文件 mtime 判定**：mtime 是进程无关的，并行下一个 worker 里
  标记用例连生产库（WAL pragma 会写文件头），另外七个 worker 会把这次改动
  记到自己头上，实测一次全量凭空多出 51 个红。
  **两处补丁缺一不可**：SQLAlchemy 的 pysqlite 方言绑的是 `dbapi2.connect`，
  只包 `sqlite3.connect` 会漏掉全部 ORM 路径——而那正是测试回落生产库的走法。

- **门禁跑并行版本**：`pytest -n 8`，约 90 秒；串行约 396 秒。
  `unittest.subTest` 的参数必须是字符串，枚举与 `date` 无法被 xdist 序列化。

- **回补一律先写 staging**，验证通过后由 C2 原子提升。
  验证前 `INSERT OR REPLACE` 覆盖生产表会让阶段 D 失去审计对象。

- **`_apply_qfq_adjustment` 默认关闭**，阶段 L 才随 `ADJ_ENABLED` 一并永久删除。

- **先关调度，再开维护窗口。** 顺序反了会在第一个筛选时点终止整个调度进程。
  `Config` 是缓存单例，进入和退出窗口都需要重启进程。

---

## 5. 阶段 0 的实测效果（2026-08-12）

同一交易日、同一配置（`balanced`、`candidate_limit=30`、AI 关闭），
唯一差别是把旧的止损解析还原回去：

| | 修复前 | 修复后 |
| --- | --- | --- |
| 候选数 | 7 | 20 |
| `trade_stage` | 全部 `focus` | 13 `probe_entry` + 7 `focus` |
| 有交易计划 | 0 | 13 |

旧行为不只是把候选压在 `focus`，而是让整类 setup 消失：
11 只 `trend_pullback` 与 2 只 `bottom_divergence_breakout` 修复前一只都没有，
7 只 `trend_breakout` 两边一致。变化幅度 2.9 倍，未达计划风险表中
「超过一个数量级则退回加运行时开关」的阈值，因此不加开关。

---

## 6. 待办

### 6.1 运维

| # | 事项 | 状态 |
| --- | --- | --- |
| 1 | `trading_calendar` 首次同步 | ✅ 3287 天 / 2184 交易日 / 对照零不一致 |
| 2 | baostock 上市/退市日期回填 | ⛔ **阻塞**：baostock 登录持续返回 10002007（服务端网络错误），六次重试均失败 |
| 3 | 基础设施 Task 12：全窗口回补至 staging | ✅ 962 万行 / 2089 交易日 / 83 分钟 / 零失败日。**不需要积分**，120 档即可 |

第 2 项是 D/E 阶段缺口归因与幸存者偏差校正的前置。若 baostock 长期不可用，
需要评估 akshare 的退市清单接口作为替代信源——但那是新增信源，要按 spec 的
交叉校验要求处理，不能直接顶替。

### 6.2 可开工

- 阶段 1 的**接口设计**：笔记见
  `docs/superpowers/specs/2026-08-12-replay-engine-parameterization-notes.md`。
  「跑两条 leg，第二条不重算因子」这条已验证成立并有承重测试
  （`test_v1_and_v2_legs_share_one_base_factor_pass`）：base 缓存键剔除了
  `bottom_divergence_v2_enabled` 与三个网格参数，v1 与 v2 落在同一 base 分区。
  **算力可行性因此成立**，但仅限同一进程内——`cache_directory` 未透传，
  跨进程无法复用因子。剩余最大风险是新策略引入的配置字段会重新进入 base 键。

### 6.3 完整回测开工前必须闭合的五项

| # | 事项 | 状态 |
| --- | --- | --- |
| 1 | 首日 / 延续持仓 / 市场中位数的逐日收益公式 | 设计已定稿 |
| 2 | 不可变链路 copy → hash → export → `run_data_manifest` | 待实现 |
| 3 | 公司行为建成正式版本化数据产物 | 待实现 |
| 4 | free/paid 模式的机器可执行闸门 | 待实现 |
| 5 | 候选 / L2 相关的 5 个失败测试 | ✅ 阶段 0 已清 |

---

## 7. 明确不做（防跑偏）

- **阶段 2 及之后一律暂缓**，直到数据基础设施 J 完成。
- **阶段 1 不得跑出任何用于下结论的历史候选。** 接口可以设计，重放不能出结论。
- 依赖的是 **J（能力可用）而非 L（生产默认翻转）**：
  回测可在自己的 `config_json` 里置 `ADJ_ENABLED=true`，不必等生产默认翻转。
- 严格历史结论只覆盖 **L2 关闭**的策略及 L1/L3/L4/L5。
  完整 L2 依赖板块快照，历史不可重建，只能靠短期快照与未来前瞻跟踪。
- 不做与当前任务无关的重构、抽象与基础设施迁移。

---

## 8. 已知但未修的问题

按 spec 判定属于后续阶段，此处登记避免遗忘：

- `circ_mv` 在因子快照中非空但恒为 0，真实换手率尚未接入。
- 创业板涨停判定误用主板规则，15% 涨幅被判为涨停（偏差方向不确定：
  过判剔除好候选，欠判留下不可买标的）。
- `trading_calendar` 为空期间，`scripts/_kline_window_sync.py` 仍从 `stock_daily`
  自身反推交易日窗口，因此看不见尚未入库的新交易日。
- `TradingCalendarService.upsert_days` 不写 `created_at` / `updated_at`；
  `_cross_check` 的宽 `except` 会吞掉对照异常。

- **门禁仍可能写生产库**（待决策）。`tests/test_e2e_five_layer_local.py` 标了
  `real_database`，护栏对它放行；`_database_path_isolation` 会把
  `DATABASE_PATH` 指向生产库，于是 `DatabaseManager` 以读写方式打开并写入
  WAL 文件头——实测每跑一次并行门禁，生产库 mtime 都会被推进。
  其中 Case 8 会调 `execute_run`，条件具备时**会在生产库里建出选股 run**
  （2026-08-12 的门禁未建，因该用例走了 skip 分支）。
  可选处置：把 Case 8 移出默认门禁，或让该模块只读打开。
