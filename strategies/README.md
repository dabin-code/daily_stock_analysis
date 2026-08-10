# 交易策略目录 / Trading Strategies

本目录存放 **自然语言策略文件**（YAML 格式）。系统启动时自动加载此目录下所有 `.yaml` 文件。

## 如何编写自定义策略

只需创建一个 `.yaml` 文件，用中文（或任意语言）描述你的交易策略即可，**无需编写任何代码**。

### 最简模板

```yaml
name: my_strategy          # 唯一标识（英文，下划线连接）
display_name: 我的策略      # 显示名称（中文）
description: 简短描述策略用途

instructions: |
  你的策略描述...
  用自然语言写出判断标准、入场条件、出场条件等。
  可以引用工具名称（如 get_daily_history、analyze_trend）来指导 AI 使用哪些数据。
```

### 完整模板（含量化筛选规则）

```yaml
name: my_strategy
display_name: 我的策略
description: 简短描述策略适用的市场场景

# 策略分类：trend（趋势）、pattern（形态）、reversal（反转）、framework（框架）
category: trend

# 关联的核心交易理念编号（1-7），可选
core_rules: [1, 2]

# 策略需要使用的工具列表，可选
required_tools:
  - get_daily_history
  - analyze_trend

# 量化筛选规则（可选）——用于全市场筛选
screening:
  filters:
    - field: breakout_ratio      # 因子字段名
      op: ">="                   # 操作符: >=, <=, >, <, ==, !=
      value: 0.995               # 阈值
    - field: ma5
      op: ">="
      value_ref: ma10            # 跨字段比较（ma5 >= ma10）
  scoring:
    - field: breakout_ratio      # 因子字段名
      weight: 40                 # 权重（0-100）
      bonus_above: 1.0           # 超过此值给额外加分
      bonus_multiplier: 1000     # 加分倍率
    - field: volume_ratio
      weight: 30
      cap: 5.0                   # 封顶值（防止极端值主导）
      invert: false              # true 时低值得高分（如缩量策略）

# 策略详细说明（自然语言，支持 Markdown 格式）
instructions: |
  **我的策略名称**

  判断标准：
  1. 条件一...
  2. 条件二...
```

### 可用因子字段

| 字段 | 说明 | 策略示例 |
|------|------|----------|
| `close` | 最新收盘价 | - |
| `ma5` / `ma10` / `ma20` / `ma60` | 移动均线 | 均线金叉、缩量回踩 |
| `volume_ratio` | 量比（当日/5日均量） | 放量突破、底部放量 |
| `breakout_ratio` | 突破比（收盘/N日高点） | 放量突破 |
| `trend_score` | 趋势评分（0-100） | 所有趋势类策略 |
| `liquidity_score` | 流动性评分（0-100） | 通用 |
| `pct_chg` | 当日涨跌幅（%） | 底部放量 |
| `pct_chg_5d` | 5日涨跌幅（%） | - |
| `pct_chg_20d` | 20日涨跌幅（%） | 底部放量 |
| `ma5_distance_pct` | 与MA5距离百分比 | 缩量回踩 |
| `bottom_divergence_double_breakout` | 底背离双突破确认（bool） | 底背离双突破 |
| `bottom_divergence_state` | 底背离状态（rejected/divergence_only/structure_ready/confirmed/late_or_weak） | 底背离双突破 |
| `bottom_divergence_signal_strength` | 底背离信号强度（0-1） | 底背离双突破 |
| `bottom_divergence_pattern_code` | 底背离形态编码（六种形态） | 底背离双突破 |
| `bottom_divergence_v2_candidate` | v2 是否进入候选/观察池；unknown provenance 下 early/R1 仍可为 true，但不代表可交易 | 底背离分层入场 v2 |
| `bottom_divergence_v2_stage` | v2 阶段：early/near_cleared/major_actionable/major_unverified 等 | 底背离分层入场 v2 |
| `bottom_divergence_v2_pattern_code` / `bottom_divergence_v2_pattern_label` | 冻结的底背离形态编码与标签 | 底背离分层入场 v2 |
| `bottom_divergence_v2_early_reversal` / `bottom_divergence_v2_early_strength` | early 事件与强度 | 底背离分层入场 v2 |
| `bottom_divergence_v2_near_zone_lower` / `bottom_divergence_v2_near_zone_upper` / `bottom_divergence_v2_near_zone_score` | 冻结 R1 区间与评分 | 底背离分层入场 v2 |
| `bottom_divergence_v2_near_entered` / `bottom_divergence_v2_near_accepted` / `bottom_divergence_v2_near_crossed` / `bottom_divergence_v2_near_cleared` | R1 递进事件 | 底背离分层入场 v2 |
| `bottom_divergence_v2_major_zone_lower` / `bottom_divergence_v2_major_zone_upper` / `bottom_divergence_v2_major_zone_score` | 冻结 R2 区间与评分 | 底背离分层入场 v2 |
| `bottom_divergence_v2_major_breakout` / `bottom_divergence_v2_major_actionable_entry` | R2 历史突破与当前可执行性（两个独立事实） | 底背离分层入场 v2 |
| `bottom_divergence_v2_actionability_status` | 当前执行门禁原因，如 actionable/adjustment_unknown | 底背离分层入场 v2 |
| `bottom_divergence_v2_confirmation_days` / `bottom_divergence_v2_extended_pct` / `bottom_divergence_v2_extended_pct_raw` | R2 确认时效、展示用舍入延伸幅度与原始延伸幅度 | 底背离分层入场 v2 |
| `bottom_divergence_v2_stop_loss_price` | 当前阶段冻结止损价 | 底背离分层入场 v2 |
| `bottom_divergence_v2_candidate_version` / `bottom_divergence_v2_zone_version` | 候选与阻力区版本 | 底背离分层入场 v2 |
| `bottom_divergence_v2_candidate_records` / `bottom_divergence_v2_layered_buy_points` | 版本化候选记录与 early/R1/R2 买点证据 | 底背离分层入场 v2 |
| `bottom_divergence_v2_as_of_index` | 本次 point-in-time 因子快照的可见截止索引 | 底背离分层入场 v2 |
| `bottom_divergence_v2_early_event_index` / `bottom_divergence_v2_near_event_index` / `bottom_divergence_v2_major_event_index` | 各阶段历史事件索引 | 底背离分层入场 v2 |
| `bottom_divergence_v2_active_event_index` / `bottom_divergence_v2_event_days` | 当前可执行阶段事件索引与距今天数 | 底背离分层入场 v2 |
| `bottom_divergence_v2_degradation_reasons` / `bottom_divergence_v2_hit_reasons` | 降级与命中解释 | 底背离分层入场 v2 |
| `amplitude` | 振幅（%） | - |
| `candle_pattern` | K线形态标识 | 一阳夹三阴 |
| `avg_amount` | 5日均成交额 | 策略级流动性约束/评分，不再作为全局首筛硬过滤 |
| `days_since_listed` | 上市天数 | 新股过滤 |
| `is_st` | 是否ST | ST过滤 |

### 核心交易理念参考

| 编号 | 理念 |
|------|------|
| 1 | 严进策略：乖离率 < 5% 才考虑入场 |
| 2 | 趋势交易：MA5 > MA10 > MA20 多头排列 |
| 3 | 效率优先：量能确认趋势有效性 |
| 4 | 买点偏好：优先回踩均线支撑 |
| 5 | 风险排查：利空新闻一票否决 |
| 6 | 量价配合：成交量验证价格运动 |
| 7 | 强势趋势股放宽：龙头股可适当放宽标准 |

## 底背离分层入场 v2

`bottom_divergence_layered_entry_v2.yaml` 是显式 opt-in 策略，不替代 legacy v1
`bottom_divergence_double_breakout`，也不在默认启用列表中。运行时还需设置：

```env
BOTTOM_DIVERGENCE_V2_ENABLED=true
```

策略按 `early`（20%）、`near_cleared`/R1（50%）和
`major_actionable`/R2（100%）分层。R1/R2 与下降趋势线在 B+1 使用当时可见数据冻结，
后续不重画。只有复权 provenance 为受信任的 `tushare_native` 或
`akshare_qfq_div_raw` 才允许执行；unknown provenance 下 early/R1 仅观察，
且 `bottom_divergence_v2_candidate` 仍可为 true 以进入观察池；历史 R2 为
`major_unverified` 且 candidate 为 false。所有 unknown provenance 场景均禁止
交易执行，生产 AI review 只能解释证据，不能生成或升级可执行建议。

## 自定义策略目录

除了本目录（内置策略），你还可以通过环境变量指定额外的自定义策略目录：

```env
AGENT_STRATEGY_DIR=./my_strategies
```

系统会同时加载内置策略和自定义策略。如果名称冲突，自定义策略覆盖内置策略。
