# 重放引擎参数化：阶段 1 接口设计笔记

**状态**：设计笔记，非实施计划。2026-08-12。

阶段 1 的目标是把重放引擎从「只能跑 v1/v2 底背离」改造成「能跑任意策略集」。
本文只记录**已用代码与测试证实的事实**和由此推出的接口边界，不含实施步骤。

前置约束（来自 `2026-08-11-signal-research-backtest-design.md` 11.-1）：
阶段 1 **可以设计接口，但不得跑出任何用于下结论的历史候选**。
阶段 2 及之后依赖数据基础设施 J，继续暂缓。

---

## 1. 算力可行性：已证实

这是决定项目能否继续的前提。实测 500 股单日因子 14.822 秒，
串行外推全期约 82.4 小时；若每条 leg 都重算一遍因子，十余条 leg 即上千小时，
项目在算力上直接不可行。

**结论：同一进程内，多条 leg 已经共享同一次因子计算，这是既有设计的自觉选择。**

证据链（`src/backtest/services/bottom_divergence_v2_performance.py`）：

| 机制 | 位置 | 作用 |
| --- | --- | --- |
| `_config_hash(config, include_grid=False)` | `:383-396` | base 因子缓存键**剔除** `bottom_divergence_v2_enabled` 与三个网格参数 |
| 冻结证据键用 `base_hash` 而非 `parameter_hash` | `:512-517` | 检测器证据每 (日期, 代码) 只算一次，跨全部网格点复用 |
| 单个 cache 实例注入共享依赖 | `bottom_divergence_v2_replay.py:928-935` | v1 leg 与全部 v2 网格 leg 走同一个 cache |

每条 leg 真正重算的只有最廉价的一层：按参数阈值的 `parameter_evaluations`。
`cache.stats` 把三层分别计数（`base_snapshot_builds` / `frozen_evidence_builds` /
`parameter_evaluations`），可直接断言。

已有测试覆盖「同策略、不同网格参数」；
`tests/test_bottom_divergence_v2_performance.py::test_v1_and_v2_legs_share_one_base_factor_pass`
补上了「跨策略」（v1 关闭 v2 vs v2 开启）这一条，并经变异测试确认承重：
把 `bottom_divergence_v2_enabled` 加回缓存键，该用例立刻变红。

### 1.1 但跨进程不成立

`ValidationFactorCache.__init__` 在 `cache_directory is None` 时建临时目录
（`:154-164`），`close()` 即删除。构造函数**接受** `cache_directory` 参数，
但 `from_database(...)` 与 `from_groups(...)` 都没有透传（`:191-275`）。

因此两次独立进程调用无法复用因子。既有的跨进程复用在另一个层次：
`CanonicalCheckpointStore` 按 `parameter_hash` 持久化**重放输出样本**
（`bottom_divergence_v2_checkpoint.py:91-193`），`--resume` 据此跳过整条 leg，
但复用的是结果而非因子。

**接口影响**：若阶段 1 要支持分批/断点跑全窗口，需要把 `cache_directory`
透传出来。这是小改动，但必须同时定义缓存目录的失效条件——
`data_version` 变化时旧目录必须作废，否则会用旧数据算出新结论。

### 1.2 透传已完成，但它没有解除算力阻断（2026-08-13 实测补记）

`cache_directory` 已透传到 `from_database` / `from_groups` 与 CLI `--cache-dir`，
并补齐了 base 快照键的算法版本、冻结分区文件名的 `data_version` 口径。
15 只股池 × 32 个交易日（2026-06-01 ~ 2026-07-15）跑两次，第二次三层计数
全部为 0、报告 canonical JSON **逐字节相同**——复用确实生效且不改变结果。

**但耗时几乎没变**：264.3s → 244.4s（同一进程内背靠背测量，约 −7.5%）；
另一对独立 CLI 调用是 259.2s → 256.5s，落在噪声里。原因由探针定位：

| 观测 | 数值 |
| --- | --- |
| 整趟 CLI 耗时中落在 `build_factor_snapshot` 内部的比例 | 冷 250.0s/264.3s，热 230.1s/244.4s |
| `build_factor_snapshot` 调用次数 | 482（≈14 个参数哈希 × 32 天） |
| 冻结分区文件大小 | 中位 1.76 MB，单文件 `evaluated` 1458 条 |
| 分区文件 gzip load + dump 往返 | 0.62s |
| 15 只股票取窗 + 复权（每次调用） | 0.022s |
| 5 天 × 15 只的冷因子计算 | 4.48s（热 0.52s） |

**结论：热点不是因子计算，是 `_switch_frozen_partition` 的 gzip 往返。**
重放按「每条 leg 遍历全部日期」组织，14 条 leg 让每个日期分区被反复
换入换出约 14 次，0.62s × 约 450 次 ≈ 整趟 230s 的全部。因子计算本身
32 天只有约 29s，缓存把它省掉了，所以省下的量级就是那 20s。

**因此下一个算力任务不是继续做缓存，而是消除分区抖动**，候选方向：
把重放循环改成「日期在外、leg 在内」，或给分区加一个内存 LRU 而不是
每次切换都落盘。前者与「四处接缝」中的 `build_isolated_config` 相邻，
后者独立可做。两者都需要先确认 `_evaluated` 的内存占用上界。

### 1.3 分区抖动已消除，但上文的两个量级都测错了（2026-08-13 补记）

分区抖动已解决，代价与结论见下。落地方式既不是改循环顺序也不是内存 LRU，
而是**把 `evaluated` 按 `parameter_hash` 分片** + **不写回未改动的分区**。
同一口径（15 只 × 32 天 × `--workers 4`、成本 2.5/12.5/5.0 bps）实测：

| | 改前冷 | 改后冷 | 改前热 | 改后热 |
| --- | --- | --- | --- | --- |
| 整趟耗时 | 216.6s | **121.2s** | 206.8s | **30.6s** |
| 分区 I/O | 96.06s | 5.49s | 173.44s | 2.56s |
| load 次数 / 耗时 | 425 / 23.75s | 425 / 0.53s | 450 / 44.88s | 900 / 2.56s |
| dump 次数 / 耗时 | 449 / 72.31s | 474 / 4.96s | 449 / 128.57s | **0 / 0s** |

四份报告的 canonical JSON sha256 完全相同。

上文 §1.2 有两处量级错误，都会误导下一步选型：

1. **「0.62s × 450 ≈ 整趟全部」不成立。** 拆开量，冷 0.056s(load) +
 0.161s(dump)、热 0.100s + 0.286s。分区 I/O 占**冷 44.3% / 热 83.9%**，
 不是 100%。热的那趟才接近原判断，冷的那趟一半以上是真在算因子。
2. **「32 天 × 1.76 MB = 56 MB，内存 LRU 没问题」差了 27 倍。** 1.76 MB
 是 gzip 后的体积；单个分区解包后 9.84 MB，**常驻 RSS 实测 48.5 MB**
 （逐个载入 25 个分区，RSS 线性增长，每个 48.4–49.2 MB）。32 天要
 1.55 GB，全窗口 2089 天要 101 GB。而这还是 15 只股池——放大到 500 只
 会再乘 30 倍。**内存 LRU 这条路在目标规模上是死的**，据此放弃。

分片可行的原因是实测出的构成：一个分区里 `evaluated` 占 9.54 MB(96.9%)、
`frozen` + `lookup` 只有 0.30 MB；而 `evaluated` 的 1638 条按 18 个
`parameter_hash` 均分（各 91 条），**任何一条 leg 只读写其中 1/18**。
合住时每次换页搬 9.84 MB，分片后只搬 0.30 + 0.53 MB，且内存占用是 O(1)，
不随日期数或股池规模增长——正是 LRU 给不了的那个性质。

「不写回未改动的分区」单独值 62%：热运行三层计数全为 0，意味着 449 次
写回全都在把刚读进来的字节原样写出去。

**假设「gzip 压缩占大头」成立但已不是主要矛盾。** 真实分区上压缩占 dump
总耗时的 83%（0.223s / 0.268s），级别 9→6 让耗时腰斩而体积只涨 0.7%
（1.757→1.769 MB），已采纳；级别 1 更快（0.057s）但体积涨 17.6%，在分片
之后 dump 已不是瓶颈，不值得。解压耗时与级别无关（0.018–0.020s）。

**「日期在外 / leg 在内」不需要做了。** 它的收益已被分片拿到，而它的代价
（改 `replay()` 签名、破坏按 leg 的断点续跑语义）没有发生。断点仍按
`parameter_hash` 存整个 batch，语义未动。

**下一个瓶颈已不在缓存层。** 对改后的热运行做 cProfile（总 59.2s，含约 2x
剖析开销），按累计耗时归因：

| 位置 | 累计 | 说明 |
| --- | --- | --- |
| `_window` | 23.4s | 其中 `apply_read_adjustment` **16.6s(28%)**，7299 次调用 |
| `five_layer_pipeline.run` | 18.3s | 其中 `theme_mapping_registry._load` **4.63s**，482 次全量重载 |
| `sector_heat_engine.compute_all_sectors` | 6.1s | 32 次 |
| `_candidate_versions` → `decode_payload` | 3.57s | 13500 次 JSON 解码 |
| `_switch_frozen_partition` | 2.79s | 已降到噪声量级 |

**取窗 + 复权是新的头号热点，而且结构与因子层完全同构**：它是
(code, trade_date) 的纯函数，却在 18 条 leg 上各重算一遍——只是它坐落在
因子缓存的**上游**，缓存管不到它。`theme_mapping_registry._load` 则是纯
浪费：每次 pipeline 构造都重载一遍注册表，482 次。这两项是下一轮的候选。

---

## 2. 硬编码在 v1/v2 上的四处接缝

改造面集中在这四处，其余层次已经足够中立：

| # | 位置 | 现状 | 性质 |
| --- | --- | --- | --- |
| 1 | `bottom_divergence_v2_replay.py:834-849` | `screener_factory` 里 `path = V1_STRATEGY_PATH if version == "v1" else V2_STRATEGY_PATH` | 二选一分支，需换成策略集解析 |
| 2 | `bottom_divergence_v2_replay.py:670-685` | `build_isolated_config` 只覆盖三个 v2 参数 | 网格维度写死 |
| 3 | `bottom_divergence_v2_replay.py:103-175`、`:255-309` | 事件与阶段抽取读 `bottom_divergence_v2_*` 字段 | 输出语义写死 |
| 4 | `bottom_divergence_v2_performance.py:44-57` | 因子回调固定调 v2 的冻结与计算函数 | 计算钩子写死 |

**已经中立、可直接复用的层次**：缓存键设计、检查点、数据哈希，
以及策略集的真正接缝 `ScreenerService(strategy_names=[...])`
（`src/services/screener_service.py:53-70, 122-135`）——它按名字从
`SkillManager` 注册的 YAML skill 里取规则，本就支持任意策略集。

`FiveLayerPipeline` 不接收 `strategy_names`，而是接收已构造好的
`screener_service` 与 `skill_manager`（`five_layer_pipeline.py:150-161`），
即策略集在 `ScreenerService` 构造期绑定。这个形状对参数化是有利的。

---

## 3. 最大的未决风险：base 因子对任意策略是否中立

`_config_hash(include_grid=False)` 剔除的是**写死的四个 v2 字段名**。
一个新策略若引入自己的配置字段（如 `my_strategy_threshold`），
该字段会进入 base 缓存键，于是每个策略各算一遍 base 因子——
第 1 节的算力结论随即失效，且**不会有任何报错**，只是慢十倍。

因此阶段 1 的核心设计问题不是「怎么跑多策略」，而是：

> 如何让「哪些配置字段属于策略私有」由策略定义本身声明，
> 而不是由缓存实现里的一个字面量元组决定。

候选方向（未决，需要在正式设计里比较）：

- 策略声明自己的参数命名空间，缓存按命名空间前缀剔除。
- 缓存键改为**白名单**（只纳入已知影响 base 因子的字段），而非黑名单剔除。
  白名单更安全：新增字段默认不影响 base 键，漏加只会导致过度复用——
  但过度复用会算错，比算慢更危险，所以白名单必须配一条「字段全覆盖」测试。

另一个待验证项（探查时未能定论）：`FactorService.build_factor_snapshot_from_groups`
是否对第三种策略也产出足够的中立因子超集。跨策略共享 base 的前提是它算的
是通用因子而非 v2 专用因子，这一点在依赖之前必须实测。

### 3.1 结论（2026-08-12 补记，本节上文有两处已被证伪）

上述两个问题都已取证定论，实施记录见
`docs/superpowers/plans/2026-08-12-base-factor-cache-key-trustworthiness.md`。

**订正一：base 不是「通用因子层」。** `_compute_extended_factors`
（`factor_service.py:460-549`）在 base pass 里跑的是整套检测器——MA100、跳空涨停、
MACD 背离、趋势线、123 形态、v1 底背离、缩量回踩、MA100+low123、MA100+60min。
它是**策略族输出的超集**。含义是：复用已有检测器输出的新策略免费搭车，
需要新检测器的新策略不在覆盖范围内，得自己进这一层。

**订正二：设计岔路定为白名单，而非策略参数命名空间。** base 路径真正读取的
配置字段只有 8 个（三个 `screening_*`、三个 `low123_*`、两个
`bottom_divergence_*`），不是 232 个，白名单在规模上完全可维护。
命名空间方案被否决的原因是仓库里不存在其基础设施：YAML 策略定义
（`src/agent/skills/base.py:62-117`）只能引用因子快照的已有列名并写字面量阈值，
这些阈值由筛选引擎消费，从不流入 `Config`、也从不参与因子计算。

白名单的失败模式（漏登记 → 过度复用 → 算错）比黑名单（漏加 → 过度失效 → 算慢）
危险，因此配了一条枚举全部 232 个字段的变异测试兜底
（`tests/test_base_factor_cache_key_whitelist.py`）。

**新增事实：不能简单收窄缓存键。** `base_hash` 这个局部变量不只给 base 快照用，
它同时是 v2 冻结证据与已评估因子两层的 `config_hash`（`build_factor_snapshot`
里共 4 个使用点，1 个属 base 层、3 个属证据层）。证据层的输出受
`bottom_divergence_v2_sync_window` / `_retention_bars` / `_breakout_buffer_pct`
及 R1/R2 权重决定，而 `_parameter_hash` 只覆盖三个网格字段——收窄证据键会让
缓存把一套参数的证据当成另一套的结果返回。落地方案是给 base 快照**新增**
一个白名单键，证据层原样不动。

**顺带修掉一个潜伏缺陷**：两个检测器包装函数原先从全局单例读阈值，而缓存键
哈希的是传入的 config，两者可以脱钩并静默返回用另一套参数算出的因子。已改为
显式传参。经核实该缺陷从未发作——重放链路的 config 恒源自单例且只覆盖三个 v2
网格参数——但阶段 1 跑多策略正是激活它的场景。

---

## 4. 与五层回测的关系

`src/backtest/` 下的五层回测是**另一个子系统**，与 v2 重放 CLI 不共用入口：
它读已落库的 `screening_candidates` 而不是重跑筛选
（`src/backtest/services/candidate_selector.py:52-107`）。

两者的交汇点在阶段 2：历史候选生成之后，五层回测消费候选。
本阶段不动它，只需注意其 `data_version` 目前由 `adj_factor_source` 分布组成
（`backtest_service.py:217-274`），与重放侧的内容哈希口径不同，
阶段 2 统一 `run_data_manifest` 时需要合并这两套口径。
