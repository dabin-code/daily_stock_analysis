# SOP 对齐修复方案（2026-09-03）

> 背景：复核《短线操盘实战交易系统 SOP》与当前仓库代码后，收敛出 5 项真实缺口。
> 本方案逐项给出改法、阈值与 SOP 参数映射，并贯穿一条全局约束——**项目没有 60 分钟线，所有 60 分钟线相关动作一律"提示为主"，不进入量化筛选 / 止损 / 仓位逻辑。**

---

## 0. 60 分钟线现状与处理原则（全局约束）

**现状确认：**

- 五层选股主链路 `src/services/factor_service.py` 完全基于**日线**构建因子，从不拉取或计算 60 分钟线。
- `ma100_60min_combined` 策略的选股门控全部使用日线因子（`above_ma100` / `ma100_bars_since_breakout` / 突破前背景 / 距离 MA100），60 分钟线只出现在 `_compute_ma100_60min_combined_factors` 的 `hit_reasons` 文字提示里（`factor_service.py:1672-1675`："建议关注次日60分钟线，突破60分钟MA20或站稳MA100时买入"）。**该策略本身已是"提示为主"。**
- 数据源层 `get_intraday_data`（akshare / pytdx）与 `src/indicators/multi_timeframe_analyzer.py`、`src/strategies/entry_strategies.py` 的 `EntryStrategyDEnhanced` 属于旧策略入口，不在五层主链路内。

**处理原则：**

1. 任何依赖 60 分钟线的量化规则（如 SOP 5.1 的"60 分钟跌破 MA10 减 1/2"）一律**只产出文字提示**，标注"无分钟线数据，仅供参考"，不写成 `filters` / 硬门控 / 止损计算。
2. 可量化的部分（如"日线跌破 MA10 清仓"）才进入代码逻辑。
3. 不在本轮为选股主链路新增分钟线抓取能力（YAGNI，属于独立数据源任务）。

---

## 1. 五项缺口修复方案

### 缺口 1：换手率假值（P0，致命）

**文件**：`src/services/factor_service.py:222`

**现状**：
```python
turnover_rate = round(min(volume_ratio * 2.0, 100.0), 4)   # 量比×2 的伪造代理值
```
`turnover_rate` 是 `volume_ratio × 2` 的伪造值。根因不在数据源、也不在"只能估算"，而在**数据管道截断**：akshare `stock_zh_a_hist` / efinance 日线接口**本就返回"换手率"列**（`akshare_fetcher.py:983`、`efinance_fetcher.py:539`），但 `data_provider/base.py:35` 的 `STANDARD_COLUMNS` 只有 8 列不含换手率，`column_mapping` 未映射、`keep_cols` 按标准列裁剪 → 换手率列被丢弃。同时 `instrument_master` 表（`storage.py:719-749`）无 `circ_mv`/`float_share`，故 `factor_service.py:254` 的 `info.get("circ_mv")` 恒 None，**`amount/circ_mv` 估算走不通**。

**改法（每日收盘后同步真实值，优先 tushare `daily_basic`，不估算）**：
1. `tushare_fetcher.py`：新增 `get_daily_basic(trade_date)`，复用 `_call_api_with_rate_limit` 调 `daily_basic(trade_date=...)`，一次调用覆盖全市场当日换手率/流通股本/流通市值。
2. `storage.py`：`StockDaily` 表加 `turnover_rate` / `float_share` / `circ_mv`（可选 `total_share` / `total_mv`）列（迁移，参考现有 `_migrate_sqlite_stock_daily_pre_close_fields`）。
3. `market_data_sync_service.py` `_try_bulk_sync`：在 `daily()` 之后追加 `daily_basic` 拉取并落库。注意 `INSERT OR REPLACE` 会清空清单外列，须把新列加进 `daily` 的 INSERT 清单，或 `daily_basic` 在 `daily` 之后用 UPDATE 补写，避免被清成 NULL。
4. `factor_service.py:222`：改读 `latest.get("turnover_rate")` / `latest.get("circ_mv")` 真实值，缺失置 `None`，删 `volume_ratio*2` 伪造。
- 单位换算：`float_share`/`total_share` 万股→股（×10000）、`circ_mv`/`total_mv` 万元→元（×10000）、`turnover_rate` 直接存百分比。
- 降级：tushare 无 token / 限频时，降级 akshare/efinance（日线自带换手率、spot 自带流通市值），缺失置 `None`，不伪造。

**SOP 映射**：2.4「龙头属性：高换手」、2.1「放量突破」。修好后 `turnover_rate` 才可用于"高换手优先级"过滤。

**验证**：取 1~2 只股票，比对落库 `turnover_rate` 与东财/同花顺公开换手率一致；美股路径无换手率不报错。

---

### 缺口 2：entry_core 三策略无成交量硬门槛（P0，致命）

**文件**：`strategies/ma100_low123_combined.yaml`、`strategies/bottom_divergence_double_breakout.yaml`、`strategies/ma100_60min_combined.yaml`

**现状**：三个 entry_core 策略的 `filters` 里只有信号布尔字段，`volume_ratio` 仅进 `scoring`（10%~20% 权重），是软加分而非硬授权。

**改法（分两类）：**

- **`bottom_divergence_double_breakout.yaml` + `ma100_60min_combined.yaml`（突破类）**：在 `filters` 直接加
  ```yaml
  - field: volume_ratio
    op: ">="
    value: 1.5
  ```
  SOP「放量突破」硬授权；量比 ≥1.5 为入门，≥2.0 已在 scoring 内自然放大（cap 5.0）。

- **`ma100_low123_combined.yaml`（埋伏/突破分叉）**：P3-P2 埋伏区天然缩量、突破 P2 才放量，无法用单一 filter 表达。改为在 `factor_service.py` 的 `_compute_ma100_low123_combined_factors` 新增字段：
  ```python
  ma100_low123_volume_confirmed = (
      state != "breakout_ready"           # watching 埋伏区不强制量
      or volume_ratio >= 1.5              # 刚突破 P2 才要求放量
  )
  ```
  并在 YAML `filters` 加：
  ```yaml
  - field: ma100_low123_volume_confirmed
    op: "=="
    value: true
  ```

**SOP 映射**：2.1「股价放量突破日线 MA20」、2.3「P2 突破入场」、2.2「双突破授权」的量价配合。

**验证**：`tests/test_factor_service_ma100.py`、`tests/test_five_layer_pipeline.py` 需同步；新增一条"无量突破被拒"用例。

---

### 缺口 3：MA10 移动止损量化（P2，60min 部分降为提示）

**文件**：`src/services/trade_plan_builder.py`（+ 可选 factor 字段）

**现状**：`_TAKE_PROFIT_TEMPLATES` 里「沿MA10移动止盈;跌破MA10减仓」只是文字模板；SOP 5.1 的"60min 破 MA10 减 1/2、日线破 MA10 清仓"无量化，且 123/底背离的 trailing 用的是 swing-low 而非 MA10。

**改法：**

1. **量化"日线跌破 MA10 清仓"**（有日线数据，可量化）：
   - 在 `TradePlan` 增字段 `trailing_stop_ma10` = `factor_snapshot["ma10"]`（快照已有 `ma10`）。
   - `exit_rules` 追加「收盘跌破 MA10（{ma10:.2f}）清仓」。
   - 对 `TREND_BREAKOUT` / `LOW123_BREAKOUT` / `BOTTOM_DIVERGENCE_BREAKOUT` 等 swing setup 生效。
2. **"60min 破 MA10 减 1/2"降为提示**：
   - `take_profit_plan` 末尾追加固定文案：「【提示·无分钟线】若你有 60 分钟线，跌破 60 分钟 MA10 可先减半仓，仅供参考」。
   - 不写入 `exit_rules`（`exit_rules` 是结构化离场条件，只放可量化的日线规则）。
3. **保留现有 swing-low 自然止损**（`low_123_trendline_detector.py` 的 `trailing_stop`）作为结构止损，MA10 作为趋势保护叠加，二者并列而非替换。

**SOP 映射**：5.1「移动趋势保护：以 MA10 为保护线」，其中日线部分量化、60 分钟部分提示化。

**验证**：`tests/golden_cases/test_candidate_decision_golden.py` 的 TradePlan 断言同步；确认 `exit_rules` 不含 60min 硬条件。

---

### 缺口 4：金字塔递减加仓 + 持仓分散（P3）

**文件**：`src/services/trade_plan_builder.py`（金字塔）；选股汇总/报告层（持仓分散）

**现状**：`_ADD_RULE_TEMPLATES` 与 `_ADD_ON_POSITION` 只有"最多加仓 1 次"，无递减比例；全仓无"持仓分散 3-5 只 / 跨题材"约束。

**改法：**

1. **金字塔递减**：把加仓描述从"最多加仓 1 次"改为递减比例，`add_rule` 文案体现 `20% → 15% → 10%`；`_ADD_ON_POSITION` 语义改为"首次加仓档位"，后续按递减比例递推。SOP 4.2「20% -> 15% -> 10%」。
2. **持仓分散（提示为主）**：这是账户级纪律，单股 `TradePlan` 不承载。落地在选股汇总结语（候选落地层 `screening_notification_service.py` 或报告渲染层）：输出「本次选股为单票信号，账户层面建议持仓 3-5 只、分布在互不相关题材板块」。不写成单股硬门控。

**SOP 映射**：4.2「金字塔加仓」「持仓分散度 3-5 只、不相关题材」。

**验证**：TradePlan 的 `add_rule` 文案断言；选股汇总结语含分散提示。

---

### 缺口 5：三重失败硬停机 + 个股 MA100 斜率向上（P1）

**文件**：`src/core/market_guard.py`、`src/services/market_environment_engine.py`（三重失败）；`src/services/factor_service.py` `_compute_ma100_factors`（斜率）

**现状**：`market_guard.py` 只有「指数价 > MA100」二元判断；`market_environment_engine.py` 已叠加 MA20 斜率与赚钱效应，但无 SOP 1.2 的"跌破 MA100 后连续 3 次上冲不破 → 硬停机"。个股 MA100 策略只有 `close > MA100`，无"MA100 斜率向上"。

**改法：**

1. **三重失败硬停机**：
   - `market_guard.py` 新增 `triple_failure: bool` 字段：指数收盘跌破 MA100 后，统计"盘中最高价触及/接近 MA100（容差 ±1%）但收盘未站回"的连续次数，连续 3 次 → `triple_failure=True`（参考 2018 年 2-5 月上证/深证）。
   - `market_environment_engine.py` 在 `_regime_below_ma100` 分支叠加：`triple_failure=True` 时**无条件 `STAND_ASIDE`**（即使赚钱效应尚可，覆盖 L121-135 现有分支）。
2. **个股 MA100 斜率向上**：
   - `_compute_ma100_factors`（`factor_service.py:580-634`）新增 `ma100_slope_up`：近 5 日 MA100 递增（`ma100_series[-1] > ma100_series[-5]`），或对近 5 日 MA100 做线性拟合斜率 > 0。
   - 在 `ma100_60min_combined` / `ma100_low123_combined` 的门控里加入 `ma100_slope_up == true`（SOP 2.1「日线 MA100 斜率向上且股价站稳」）。

**SOP 映射**：1.2「三重失败准则 / 硬停机」、2.1「MA100 斜率向上」。

**验证**：用 2018 年指数历史回放三重失败计数；`tests/test_market_strategy.py` / `tests/test_factor_service_ma100.py` 增补斜率门控用例。

---

## 1.6 类似问题（数据管道字段缺失审计）

缺口 1 的根因是"数据源有字段、标准化落库时丢弃"。按同一根因排查，发现**市值/换手率三元组**整体缺失，且已造成下游打分系统性退化：

| 字段 | 数据源是否返回 | 本地库现状 | 下游影响 |
|---|---|---|---|
| `turnover_rate` 换手率 | ✅ akshare/efinance 日线、spot 均返回 | `stock_daily` 无列，`factor_service` 用 `volume_ratio*2` 伪造 | 高换手优先级失真 |
| `circ_mv` 流通市值 | ✅ akshare/efinance spot 返回 | `instrument_master` 无列，`info.get("circ_mv")` 恒 None | `leader_score` 小市值分(0-20)恒 0；`extreme_strength_scorer` 小市值分恒 0；`leader_stock_selector` 选"流通市值最小"龙头退化 |
| `float_share` 流通股本 | ❌ 各源均未落库 | 无 | 无法用 `volume/float_share` 精确算换手率 |

**连锁影响（SOP 2.4「龙头属性：小盘次新、高换手」整条依赖此三元组）**：
- `leader_score_calculator.py`：`small_circ_mv`(0-20) + `turnover`(0-20) 两个分量共 40 分，因 `circ_mv=None` + `turnover_rate` 伪造而失真。
- `extreme_strength_scorer.py`：`small_circ_mv` + `turnover` 分量同理退化。
- `leader_stock_selector.py`：`_circ_mv_sort_value` 在 `circ_mv=None` 时选不出"涨停且流通市值最小"的龙头。

**统一修复（并入缺口 1，作为一个 P0 数据补全批次，优先 tushare）**：
- **落库设计**：`turnover_rate` / `float_share` / `circ_mv`（+可选 `total_share`/`total_mv`）都是**日频字段**（每日随价格/股本变化），落 `stock_daily`（每日一行），不放 `instrument_master`（静态主数据）。
- **每日同步**：`market_data_sync_service._try_bulk_sync` 在 Tushare `daily()` 之后追加 `daily_basic(trade_date=...)` 全市场批量拉取，一次调用覆盖全市场（免费账号可跑通，符合"每日收盘后同步"）。
- **因子层**：`factor_service` 删 `volume_ratio*2` 伪造，改读 `latest.get("turnover_rate")` / `latest.get("circ_mv")` 真实值。
- **单位换算**：`float_share`/`total_share` 万股→股、`circ_mv`/`total_mv` 万元→元、`turnover_rate` 百分比直存；`close×float_share` 需与 `circ_mv` 交叉校验（参考 2026-08-11 历史数据设计文档的单位陷阱）。
- **降级**：tushare 无 token / 限频时降级 akshare/efinance（日线自带换手率、spot 自带流通市值），缺失置 `None`。

> 注：缺口 1 与本节合并为**一个 P0 数据补全批次**，改动面 = 数据源（`tushare_fetcher` 加 `daily_basic`）+ 存储（`StockDaily` 加列迁移）+ 每日同步（`market_data_sync_service`）+ 因子层（`factor_service`）。

---

## 2. 分批与优先级

| 批次 | 缺口 | 改动面 | 依赖 |
|---|---|---|---|
| P0（本周） | 缺口 1 换手率假值（含市值/换手率三元组补全） | `base.py` / `akshare_fetcher.py` / `efinance_fetcher.py` / `storage.py` / `factor_service.py` | 无 |
| P0（本周） | 缺口 2 成交量硬门槛 | 3 个 YAML + `factor_service.py` 新增 1 字段 | 缺口 1 修好后的 `turnover_rate` |
| P1 | 缺口 5 三重失败 + MA100 斜率 | `market_guard.py` / `market_environment_engine.py` / `factor_service.py` | 指数历史数据 |
| P2 | 缺口 3 MA10 日线清仓量化 | `trade_plan_builder.py` | 无 |
| P3 | 缺口 4 金字塔 + 分散提示 | `trade_plan_builder.py` + 报告层 | 无 |

## 3. 风险与未覆盖项

- **数据补全的单位口径**：`float_share` 万股→股、`circ_mv` 元、`amount` 元；`close×float_share` 需与 `circ_mv` 交叉校验（参考 2026-08-11 历史数据设计文档的单位陷阱）。tushare `daily_basic` 有限频/权限，需降级路径（失败回退 spot 或置 `None`）。
- **三重失败**的"上冲不破"容差（±1%）需用 2018 案例回放校准，避免误触发硬停机。
- **持仓分散**落在报告提示层而非引擎层，是"提示为主"的刻意取舍（选股系统不管理真实账户持仓）。
- 本轮**不新增**分钟线抓取、不动 `get_intraday_data` / `multi_timeframe_analyzer.py`；这些属于独立数据源任务，避免夹带。

## 4. 回滚方式

- 缺口 1/2/3/5 均为 `factor_service.py`、`market_guard.py`、`market_environment_engine.py`、`trade_plan_builder.py`、`strategies/*.yaml` 的定点改动，逐项 revert 即可；YAML 改动可单文件回滚。
- 阈值以模块级常量落地（参照 `_MA100_60MIN_*` 的既有风格），不新增 `.env` 配置项，无环境变量迁移成本。
