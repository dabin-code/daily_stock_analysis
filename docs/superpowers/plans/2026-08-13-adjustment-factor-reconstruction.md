# 复权因子本地重建（gate-3）

日期：2026-08-13
状态：v2（经一轮评审 + 补充实测后重写；Task 2/4 实施期间又回写了 5 条，见第 8 节）
前置：`stock_daily` 已晋升为全窗口原始价（962 万行 / 2018-01-02 ~ 2026-08-12）

> v1 的三处设计失误已在本版修正：落点选错、前瞻收益未覆盖、落库会被日常同步擦掉。
> 修改依据记录在第 8 节。

**关于「100% 带 `pre_close`」的更正**（Task 2 实测）：本文原先写全表 100% 带
`pre_close`，不准确。实测 **1014 行 NULL，分布在 81 只标的**上：

- 78 只北交所各 1 行，是各自在本窗口内的**首个交易日**——没有前一日收盘可继承，
  是复权链本来就豁免的锚点情形，不构成断链；
- 另外 3 只是**中段**缺口：`sh000001` 464 行、`sh000905` 459 行、港股 `00435` 13 行。

对股票 universe 结论仍然成立（个股侧只有首行豁免）。但 `sh000001` 是
`screening_market_guard_index` 的默认值，一旦有人把指数接进 `build_factor_snapshot`，
中段缺口会让整段窗口 fail-closed，而且从现象上看不出原因（只会表现为该标的永远
`adjustment_unknown`）。指数目前不走因子快照路径，所以本计划不处理；接指数之前必须
先解决这 3 只的中段缺口。

## 1. 要解决的问题

回测返回 `NO_ELIGIBLE_SAMPLES`，原因不是数据量，而是**没有一根 K 线带可信的复权因子**。

实测（400 只 × 4 个日期）：检测器每个日期找到 227~256 个 v2 候选，但
`actionability_status` 只有两个取值——被拒的 `no_primary_candidate`，其余**全部**
`adjustment_unknown`。2026-07-01 同样如此，说明是当前就存在的结构性阻断。

## 2. 门禁的真实语义（核心约束）

`adj_factor` 在检测器路径上**从不参与数值换算**。唯一的算术使用在
`data_provider/tushare_fetcher.py:449`，且该函数被 `TUSHARE_QFQ_ENABLED` 硬开关短路。
检测器只判：来源在白名单内、数值非空、有限且为正
（`src/indicators/causal_bottom_divergence_detector.py:185-191`）。

因此门禁断言的是：**"喂进来的价格序列是复权一致的"**。

**由此推出不可违反的约束**：只把因子写进列、再把来源加进白名单，是把门禁骗过去，
让检测器在有除权跳空的原始价上算阻力位——从"诚实拒绝"退化为"静默算错"。

**本计划的核心是读取时施加复权，不是填一列数。**

## 3. 重建原理与验证

要求复权序列复现真实收益率 `adj_close(t)/adj_close(t-1) = close(t)/pre_close(t)`，
推出后复权累乘因子：

```
f(t) = f(t-1) * close(t-1) / pre_close(t)      f(首行) = 1
```

### 3.1 事件规模（全表实测）

33,013 个事件覆盖 5320 / 5790 只。5 月 7496、6 月 12652、7 月 6749，三个月占 81%，
与 A 股分红季吻合。比值 1.0~1.10 占 86.4%，1.5~3.0（送转）1145 个。

### 3.2 对外部参考的交叉验证

**第一组：数据完整的大盘股**（10 只 × 约 2089 个交易日，对照新浪前复权）

平均中位偏差 **0.0095%**，两万余次比对最大 **0.1145%**，量级即参考源取整噪声。

**第二组：存储缺口最严重的股票**（专门检验"缺口是否伪造复权事件"）

| 代码 | 事件处最大间隔 | 比对行数 | 中位偏差 | 最大偏差 |
| --- | --- | --- | --- | --- |
| 600423 | 293 天 | 1889 | 0.0000% | 0.1508% |
| 002252 | 288 天 | 1880 | 0.0386% | 0.1633% |
| 600733 | 238 天 | 1909 | 0.0000% | 0.0000% |
| 600745 | 231 天 | 1919 | 0.0207% | 0.0520% |
| 002819 | 113 天 | 1986 | 0.0362% | 0.0717% |
| 601555 | 11 天 | 2067 | 0.1187% | 0.3487% |

结论：**长间隔不影响重建**。原因是这类间隔基本是停牌，停牌期间不存在 bar，
交易所在复牌日给出的 `pre_close` 仍是正确的复权参考价。因此**不需要**基于
交易日历连续性的额外门禁。

## 4. 设计决策

### 4.1 不落库，读取时按窗口现算

v1 计划把 f(t) 写进 `stock_daily.adj_factor`。这条路有三个问题：

- 日常同步的 `INSERT OR REPLACE` 列清单不含 `adj_factor`
  （`src/services/market_data_sync_service.py:360-369` 的注释已明写会被清成 NULL），
  回填结果会被逐步擦掉，需要额外的维护任务；
- `src/storage.py:109-128` 已把该列语义钉为「fetch 时点的 qfq/raw 比值，配 `adj_anchor_date`」，
  写入后复权累乘因子等于一列两义；
- 962 万行 UPDATE 的写入成本与回滚复杂度。

**改为读取时现算。** f(t) 完全由 `pre_close` 与 `close` 决定，而 `pre_close` 恰好
在同步列清单里被保住，且有 `scripts/validate_staging_before_promotion.py` 守着。

**为什么窗口内现算与全历史现算等价**：施加时用的是 `f(t)/f(D)`，只依赖 t 与 D 之间的
事件，全部落在窗口内。窗口起点之前的任何误差在相除时整体约掉。这同时意味着
稀疏历史带来的风险敞口被自动限制在窗口内。

### 4.2 按窗口末日 D 归一

回放到 D 时，窗口内 bar 的 OHLC 乘以 `f(t)/f(D)`。

- **时点安全**：`f(t)`（t ≤ D）只依赖 D 之前的数据。
- **为什么归一而不是直接用 f(t)**（v1 给的理由「系统内有绝对价格量」经核查在 v2 路径上
  不成立——涨跌停走 `pct_chg` 百分比、止损是 `min(a,b)*0.97` 比例、ATR 门槛是乘数。
  真正的理由是下面两条）：
  1. `entry_close` 取自因子快照、前瞻 bar 取自原始表，只有 `f(D)/f(D) == 1` 才能保证
     入场价等于当日真实可成交价、且与前瞻同尺度；
  2. 累乘误差在 `f(t)/f(D)` 中整体约掉（见 4.1）。

### 4.3 成交量同步处理

1145 个送转事件会让除权日 `volume` 台阶式跳变，而 `volume_ratio` 是「最新量 / 前 5 日均量」
（`src/services/factor_service.py:196-198`），送转当日被虚增 2~3 倍，直接进
`_compute_liquidity_score` 与 `risk_flags`。

`volume` 除以同一个 `f(t)/f(D)`。`amount` **不处理**——价×量守恒，本就不受复权影响。

### 4.4 异常判定按方向，不按幅度

全表 52 个越界事件的画像：

- **ratio < 1 共 36 个，全部是北交所 920xxx**，主板一个都没有。`pre_close` 高于前一日收盘
  对分红送转模型是方向性错误，实为新三板期转让方式变更/转板股本重组。
- **ratio > 3.0 共 16 个，其中 14 个是北交所**，幅度至 9.7173（约 10:1 拆股）；另两个是
  主板的 002594（3.0358，10 送 8 转 12 派 39.74，算术已核对）与 600733（3.5010）。
- **反证**：当前被接受的上沿是 920870 恰好 3.0000、920152 的 2.7586，同为北交所大比例拆股。
  既然接受 3.00 就没有原则拒绝 3.06。

因此：

- **下界 `ratio < 0.99` 判异常**（方向性依据；实测所有 < 1 的案例都 ≤ 0.9347，间隔充足）
- **上界 `ratio > 20` 判异常**——这是**数据损坏警戒线，不是业务规则**（实测最大合法值
  9.7173，留约 2 倍余量）

异常处理：**按异常点切段，只作废异常点之前的前缀**，而不是整只作废。依据同 4.1——
异常点之后的窗口不受其影响。被作废段不施加复权且不打可信来源，fail-closed。

### 4.5 遇到不可信数据一律 fail-closed

窗口内出现缺 `pre_close`（非首行）、落在被作废段、或比值越界时，整只标 unknown。
**绝不 ffill、绝不按 1.0 补**——那会得到部分复权的序列，比拒绝更糟。

**也不能保留取数时的来源标记**（Task 2 补）。「不 ffill、不按 1.0 补」只挡住了造假
因子，没挡住「什么都不做」：那样窗口会原样保留取数带来的 `adj_factor_source`，而
`tushare_native` 本来就在信任白名单里，于是一段没有施加复权的原始价顶着可信标记穿过
门禁——正是第 2 节要消灭的「静默算错」。因此 fail-closed 必须**主动改写**为
`adj_factor = NaN` + `adj_factor_source = pre_close_chain_anomalous`，
实现见 `adjustment_chain.mark_unadjustable`。

**样本级判据**（Task 2 补，原计划只定义到「整只标 unknown」）。前瞻 / 前置窗口没有传递
通道——`ValidationSample` 没有 adjustment 字段，这两种 bar 也不经过检测器，
`adjustment_unknown` 这条路在这里根本走不通。实施时选的是：**解不出整窗可信链就返回空
窗口**（`_adjusted_forward_bars` / `_adjusted_prior_bars` 返回 `[]`），样本因此没有
`close_5d/10d/20d`、没有 MAE/MFE，在下游按「未成熟样本」处理，而不是带着一个错的收益
进统计。判据写死为「窗口内每一行的来源都是 `pre_close_chain`」，即 `_all_trusted`。

这里是**整窗**作废而非切段，两侧理由不同：前瞻侧断链会作废包含锚行在内的整个前缀，锚点
因子成了 NaN，剩下的段再没有任何可信的换算关系能接回 D；前置侧则是因为 `apply_adjustment`
对被作废段按 1.0 原样保留，混着算标准差会得到一个两种尺度拼出来的数，比没有更糟。

## 5. 任务拆解

### Task 1：共享复权模块（核心）

新增 `src/services/adjustment_chain.py`：

- `compute_window_factors(df) -> Series`：按 4.1 算 `f(t)/f(D)`，D 为窗口末行
- `apply_adjustment(df) -> df`：按 4.2/4.3 施加到 OHLC 与 volume，并在内存中写入
  `adj_factor = f(t)/f(D)`、`adj_factor_source='pre_close_chain'`
- 异常段按 4.4 切段并标 `pre_close_chain_anomalous`（不施加、不可信）
- 按 4.5 fail-closed

测试要点（**不变式必须写对**）：正确的不变式是
`close_adj(t)/close_adj(t-1) == close(t)/pre_close(t)`，
等价于 `f(t)*pre_close(t) == f(t-1)*close(t-1)`。
v1 写的 `close(t-1) == pre_close(t)` 在数学上不成立（复权后前者带因子、后者是原始价），
按那个写测试要么恒红要么被改成空断言。

其余：无事件时因子恒 1；窗口末日因子恒为 1；送转后 volume 同步缩放；
异常段切段正确；缺 `pre_close` 触发 fail-closed。

### Task 2：接入三条读取路径

**必须三条都接，漏一条就是两套口径并存。**

1. **回测因子路径**：`ValidationFactorCache._window`
   （`src/backtest/services/bottom_divergence_v2_performance.py:557-571`）。
   **不能放在 `from_database`**——它一次性拉整段区间（`:307-320`），结构上不知道单个
   回放日 D，在那里归一只能除以晚于 D 的 `f(ordered_dates[-1])`，等于把 D 之后的分红
   信息带进 D，直接违反时点安全。
2. **前瞻收益路径**：`_build_validation_sample`
   （`src/backtest/services/bottom_divergence_v2_replay.py:346-404`）内部，对
   `get_forward_bars` / `get_prior_bars` 的结果施加。
   **不要改 `src/repositories/stock_repo.py`**——那是共享仓储层，五层流水线也在用。
   遗漏这条的后果：前瞻窗口内一次除权就被计成亏损，送转事件造成 33%~66% 假亏损，
   而回测照常产出样本，即第 2 节警告的失败模式。

   **更正（Task 2 实测）**：原文写「forward / prior 用同一个锚 D」，**prior 侧做不到**。
   prior 窗口止于 `signal_date - 1`，要把它接回 D 需要 `pre_close(D)`，而因子快照只给
   `close(D)`——链条差的正是这一步。实现因此**刻意偏离**：prior 窗口按**自身末行**归一。
   两种锚只差一个常数 `f(prior 末行)/f(D)`，成立前提是**该窗口的两个消费方都对常数免疫**：
   `compute_pre_signal_features` 的波动率是收益率的标准差（常数在相除时约掉），流动性用
   `amount`（本就不受复权影响）。**这个前提不是普遍成立的**——一旦有人给 prior 窗口加上
   绝对价格量的消费方（例如用 prior 收盘价与 `entry_close` 比大小），这条偏离立刻变成错，
   届时必须补 `pre_close(D)` 的传递通道而不是继续沿用。forward 侧**没有**这个问题：缺的
   那一环 `ratio(D+1) = close(D)/pre_close(D+1)` 只需要 `close(D)`，而它就是 `entry_close`
   （因子快照已按 D 归一、`g(D) == 1`），所以实现把 D 拼成窗口首行再解链，锚就是 D 本身，
   尺度与 `entry_close` 严格对齐。
3. **生产因子路径**：`src/services/factor_service.py` 的 `build_factor_snapshot`
   （D = `trade_date`）。同时在 `build_factor_snapshot_from_groups` 入口加显式断言/标记，
   堵住第三个调用方——回测正是经由它而非 `build_factor_snapshot`
   （`bottom_divergence_v2_performance.py:629-637`、`:134-155`）。

`build_factor_snapshot` 的取数列表（`factor_service.py:126-139`）目前不含 `pre_close`，
需补列。

开关 `ADJ_APPLY_ON_READ`（默认 `true`）**必须做成 `Config` 的 dataclass 字段**并同步登记进
`BASE_FACTOR_CONFIG_FIELDS`（`bottom_divergence_v2_performance.py:44-64`）。该白名单漏登记
会导致过度复用——切换开关后读回上一次的 base 快照，是算错不是算慢。做成裸 `os.getenv`
则 `tests/test_base_factor_cache_key_whitelist.py` 也探不到（它只遍历 `fields(Config)`）。
同步更新 `.env.example`。

**同时补 `data_version` 的内容哈希**（Task 2 补，原计划漏了）。隔离数据集的内容哈希
`_HASH_BAR_FIELDS`（`bottom_divergence_v2_dataset.py:18-36`）不含 `pre_close`，于是两份
只有 `pre_close` 不同的数据集会算出同一个 `data_version`，冻结证据与 base 快照缓存会跨
数据集复用。这与 `BASE_FACTOR_CONFIG_FIELDS` 漏登记是**同一类错误**（白名单漏项 → 过度
复用 → 算错），而且**正是本 Task 让它从死条款变成活漏洞**：改动之前 `pre_close` 不影响
任何输出，漏哈希没有后果；改动之后它决定回测看到的每一个价格与成交量。已在 Task 2 一并
修复。

### Task 3：离线连续性抽查

产物：N 只 code × 除权日，施加前后 `close_adj(t)/close_adj(t-1)` 与 `close(t)/pre_close(t)`
的偏差分布。

**本 Task 不以「回测产出非空样本」为验收点**——白名单在 Task 4 才加，
`actionability_v2` 只看 source 字符串（`causal_bottom_divergence_events.py:244-248`），
此前必然仍是 `adjustment_unknown`。

### Task 4：加入信任白名单（三处）

- `src/services/factor_service.py` 的 `_bottom_divergence_v2_metadata`
- `src/indicators/causal_bottom_divergence_detector.py` 的 `_TRUSTED_ADJUSTMENT_SOURCES`
- `src/strategies/bottom_divergence_layered_entry.py` 的 `_TRUSTED_ADJUSTMENT_SOURCES`
  （Strategy E v2 实盘封装，`_has_trusted_adjustment` 用它决定是否允许执行）

漏第三处即「回测放行、实盘拒绝」。加入 `pre_close_chain`，不含 `_anomalous`。
`tests/test_adjustment_trust_whitelist.py` 钉三份**逐值相等**并逐处钉行为；
三处各去掉一次 `pre_close_chain` 都能让 2~3 条测试变红。

**实测（15 只 smoke 股池 / 2025-01-02~2025-09-30 / 183 个交易日）**：

| | `actionability_status` 分布（5 个探针日 / 75 行快照） | v2 候选 | 样本 |
| --- | --- | --- | --- |
| 改动前（`ADJ_APPLY_ON_READ=false`） | `adjustment_unknown` 53 / `no_primary_candidate` 22 | 34 | **0** |
| 改动后 | `major_not_confirmed` 34 / `no_primary_candidate` 24 / `confirmation_too_old` 14 / `structure_floor_broken` 3 | 33 | **23**（全部 executable） |

**Task 4 期间发现并修复的一处漏洞**：`factor_service._compute_bottom_divergence_v2_factors`
原先用 `zone_metadata.get("adj_factor_source") or metadata.adj_factor_source` 取
provenance——这是**回落**而不是合取，候选级只要给出非空值，整组级的 unknown 就再也起
不了作用。而候选级的 `zone_metadata` 是检测器冻结到 A/B 前缀那一段
（`visible.iloc[a_idx:b_idx+1]`）的，断链前缀落在 A 之前时它完全看不见；阻力区却是在
**整个可见窗口**上找摆动高点的，那段没复权的原始价照样参与计算。实测：整组 metadata
已经是 `unknown`，最终 `actionability_status` 仍然是 `actionable`。

这个洞在 `pre_close_chain` 进白名单之前是死的（没有任何来源被信任，整组必然 unknown），
**是本 Task 把它变活的**，与 `_HASH_BAR_FIELDS` 漏 `pre_close` 同类。已改成两级取「或」，
方向上只增加 unknown、永远 fail-closed。检测器把 provenance 冻结到 A/B 段是有意设计
（候选版本要稳定），因此不动它，只把整组级的判定加回来。

假亏损抽样核对：000333 信号 2025-05-21，前瞻窗口内 2025-06-12 除权
（`pre_close` 72.01 比前一日收盘 75.49 低 3.48，即 10 派 34.8）。手算 g = 1.048326621，
与流水线输出的 `close_20d = 74.77713789751422`、`future_closes_20d[14] = 75.68918205804749`
逐位一致。20 日收益复权后 −5.11%、原始价 −9.48%，差的 4.37 个百分点全是除权缺口。
更值得注意的是 MAE：原始价会给出 −10.09% 且落在 2025-06-16，复权后是 −6.00% 落在
2025-06-09 ——假亏损不只是缩放数值，还会**改变哪一根 bar 是最差的那根**。

### Task 5：时变成本模型 —— 移出本计划

原以为是改三个标量，实际涉及 8 个文件的签名透传
（`bottom_divergence_v2_cli_service.py` / `_selection` / `_metrics` / `_models` /
`backtest_service.py` 的 `_CONFIG_HASH_FIELDS` / `_dataset.py` 的清单 / `config.py`），
另有两个正确性坑：`_zero_cost_result` 用 `round_trip != 0.0` 判零成本短路
（`bottom_divergence_v2_cli_service.py:70-99`），时变下没有单一 round_trip 需重新定义；
印花税按**卖出日**征收而非信号日，跨越 2023-08-28 的样本要读
`future_trade_dates_20d[N-1]`。

单独立计划，不阻塞本计划。当前先用保守常数（买 2.6bp / 卖 12.6bp / 滑点 5bp）跑通。

## 6. 验收

- [x] 三条读取路径口径一致（`test_production_and_backtest_agree_row_by_row` 逐行比因子；
      前瞻窗口锚在另一端，改钉更强的「跨越 D 的收益率仍成立」）
- [x] 施加后不变式 `f(t)*pre_close(t) == f(t-1)*close(t-1)` 成立
- [x] 窗口末日因子恒为 1（入场价等于真实可成交价）
- [x] 送转事件的 volume 同步缩放
- [x] 回测产出非空样本（23 个），且抽样核对收益不含除权假亏损（见 Task 4）
- [x] `ADJ_APPLY_ON_READ` 已登记进 `BASE_FACTOR_CONFIG_FIELDS`
- [x] `.env.example` / `README.md` / `scripts/README.md` / `docs/CHANGELOG.md` 同步

**未覆盖**：`scripts/validate_bottom_divergence_v2.py` 的完整发布闸门跑不到底，
但**卡点不在复权**。改动前它停在 `NO_ELIGIBLE_SAMPLES`；改动后越过了这道门，转而停在
`fit_tertile_boundaries` 的 `tertile fitting requires training samples`——实测 v1 基线臂
在 15 只 smoke 股池上产出 0 个样本（把区间放宽到 2019-01-02~2025-09-30 也只有 10 个，
且全部落在 2024-09/10 那波行情里、恰好都在 test 段），train 段始终为空。这是股池规模
的性质，与本计划无关，需要一个真实规模的股池才能跑通。

## 7. 回滚

`ADJ_APPLY_ON_READ=false` 即可完全回退——不落库意味着没有需要清理的数据。
全库备份：`data\backups\stock_analysis_20260813_095547.db`。

## 8. v1 → v2 的修改依据

| 项 | v1 | v2 | 依据 |
| --- | --- | --- | --- |
| 落库 | 写 `stock_daily.adj_factor` | 不落库，读取时现算 | 日常同步会擦掉；一列两义；省 962 万行写入 |
| 回测落点 | `from_database` | `ValidationFactorCache._window` | 前者不知道单个 D，归一会引入未来函数 |
| 生产落点 | `build_factor_snapshot` | 同前 + `_from_groups` 入口断言 | 回测经由 `_from_groups`，原方案实盘施加、回测不施加 |
| 前瞻收益 | 未覆盖 | `_build_validation_sample` 内施加 | `get_forward_bars` 是裸查询，除权被计成亏损 |
| 白名单 | 两处 | 三处 | 漏 Strategy E v2 实盘封装 |
| 不变式 | `close(t-1)==pre_close(t)` | `f(t)*pre_close(t)==f(t-1)*close(t-1)` | 原式数学上不成立 |
| 归一理由 | 存在绝对价格量 | entry/前瞻同尺度 + 误差约掉 | 前者经核查在 v2 路径不成立 |
| 异常判定 | `<0.95` 或 `>3.0`，整只作废 | 方向判定，切段作废 | 上界在切真实分布；36 个方向性错误全在北交所 |
| volume | 未提 | 同步缩放 | 送转日 `volume_ratio` 被虚增 2~3 倍 |
| 连续性门禁 | —— | 明确不需要 | 293 天间隔股票实测偏差 0.0000%，停牌不破坏链条 |
| 成本模型 | 本计划 Task 5 | 移出单独立计划 | 涉及 8 文件签名 + 两个正确性坑 |

## 8.1 Task 2 / Task 4 实施期间的回写

下面 5 条是计划没有覆盖、由实施者提出并已确认成立的问题，全部已回写进正文。

| 项 | 计划原文 | 更正 | 依据 |
| --- | --- | --- | --- |
| `data_version` 内容哈希 | 未提 | `_HASH_BAR_FIELDS` 补 `pre_close`（见 Task 2） | 与 `BASE_FACTOR_CONFIG_FIELDS` 漏登记同类；**本计划让它从死条款变成活漏洞**：改动前 `pre_close` 不影响任何输出，改动后它决定回测看到的每一个价格 |
| `pre_close` 覆盖率 | 「100% 带 `pre_close`」 | 1014 行 NULL / 81 只：78 只北交所各 1 行是首个交易日（合法），另有 `sh000001` 464 行、`sh000905` 459 行、`00435` 13 行的**中段**缺口（见开头更正） | 对股票 universe 结论不变；但 `sh000001` 是 `screening_market_guard_index`，接指数进因子快照会整段 fail-closed 且看不出原因 |
| prior 窗口的锚 | 「forward / prior 用同一个锚 D」 | prior 侧**做不到**，改为按窗口自身末行归一（见 Task 2 第 2 条） | prior 窗口止于 `signal_date - 1`，接回 D 需要 `pre_close(D)` 而因子快照只给 `close(D)`；两种锚只差常数，该窗口两个消费方对常数免疫（波动率是收益率标准差、流动性用 `amount`），但这是**刻意偏离**，前提写在正文里 |
| 样本级 fail-closed | 只定义到「整只标 unknown」 | 补样本级判据：前瞻 / 前置窗口 `_all_trusted` 为假即返回**空窗口**（见 4.5） | 前瞻窗口没有传递通道，`ValidationSample` 无 adjustment 字段；「没有前瞻窗口」比「有个错的收益」安全 |
| fail-closed 的写法 | 「不 ffill、不按 1.0 补」 | 补「**也不能保留取数时的来源标记**」（见 4.5） | 原文只挡住了造假因子，没挡住「什么都不做」；`tushare_native` 已在白名单里，保留它等于让没复权的价格顶着可信标记进检测器 |
| `adj_convention` 消费方 | 未提 | 读取路径入口对非 `raw`（含**缺列** / NULL / `unknown` / `qfq`）整窗 fail-closed；三条读取路径补该列；`_HASH_BAR_FIELDS` 一并补上 | 三条写入路径维护这一列、零个读取路径消费它。efinance 降级写死 `fqt=1`，覆写一行即「qfq 的 `close` + 残留的 raw `pre_close`」，混着成链算出的因子不越界、不断链、不报错，**只是错**；D 恰好是这样一行时整窗价格水平一起偏。缺列取拒绝而非放行：放行等于让守卫被「忘了 SELECT 一列」整体关掉，而代价（`mark_unadjustable` → `adjustment_unknown`）是退回 gate-3 之前那个安全状态 |
| 候选级 / 整组级 provenance | 未提 | `_compute_bottom_divergence_v2_factors` 的两级判定由**回落**改为**合取**（见 Task 4） | 候选级 `zone_metadata` 冻结到 A/B 前缀，看不见落在 A 之前的断链前缀；而阻力区在整个可见窗口上找摆动高点。同样是本计划让这个洞从死条款变成活漏洞 |
