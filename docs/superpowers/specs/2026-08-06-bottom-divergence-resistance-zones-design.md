# 底背离水平阻力区与分层入场设计

**日期：** 2026-08-06  
**状态：** 已按最终实现同步（v2 默认关闭，等待样本外发布门禁）

## 实现事实摘要

- 最终实现入口为 `src/indicators/causal_bottom_divergence_detector.py`，
  阻力区计算在 `src/indicators/resistance_zone_detector.py`，独立策略封装为
  `src/strategies/bottom_divergence_layered_entry.py`，YAML 名称为
  `bottom_divergence_layered_entry_v2`，setup type 为
  `bottom_divergence_layered_entry`。
- 生产因子通过 `FactorService._compute_bottom_divergence_v2_factors` 追加
  `bottom_divergence_v2_*` 字段；`POST /api/v1/screening/runs` 仍是筛选入口，
  现有 v1 API 和字段保持不变。
- 001337 fixture 为
  `tests/fixtures/001337_bottom_divergence_20251201_20260805.csv`，从
  2025-12-01 开始，确保 MACD/ATR 预热和逐日前缀回放不依赖窗口外数据。
- golden 回放冻结 R1=`37.46–39.20`、R2=`40.61–42.90`：
  2026-07-22 为 early，2026-07-23 为 R1，2026-08-05 为 R2 historical。
- 只有 `adj_factor_source` 属于受信任来源
  `tushare_native` 或 `akshare_qfq_div_raw` 且复权因子完整有效时才允许执行。
  provenance 为 unknown 时 early/R1 的
  `bottom_divergence_v2_candidate` 仍可为 true，以进入候选观察池并保留证据；
  历史 R2 会降级为 `major_unverified` 且 candidate 为 false。所有 unknown
  provenance 阶段都不得形成交易执行或可执行 AI 建议。
- v1 行为不变；v2 由 `BOTTOM_DIVERGENCE_V2_ENABLED=true` 显式启用且默认关闭。

## 一、背景

当前 `BottomDivergenceBreakoutDetector` 将 A/B 两个价格低点之间最高的
`swing high` 作为水平阻力 H，并要求收盘价突破 H 才确认水平突破。

该规则适合严格确认，但会把单日长上影的极端最高价当成唯一阻力阈值。001337
的 A/B 结构中，2026-07-06 最高价 42.90、收盘价 39.08；现有算法以 42.90
作为水平阻力，直到 2026-08-05 收盘 43.27 才确认。与此同时，2026-07-22
已经出现底背离后的反转，2026-07-23 已站上 39 元附近的近端阻力。

从投资角度看，三个时点代表不同成熟度：

- 底背离后的早期反转：允许小仓试错。
- 近端供给区突破：结构得到初步确认，可以加仓。
- 主要套牢区突破：趋势得到完整确认，可以补足目标仓位。

因此，优化目标不是把 42.90 简单改低，而是把“单线、单时点、全或无”的判断
改为“阻力区、分阶段、分仓位”的决策体系。

## 二、目标

1. 使用仅在信号时点可见的数据构建水平阻力，避免未来数据和重绘。
2. 降低单根长上影对阻力阈值的支配作用。
3. 同时输出近端阻力区和主要阻力区，服务分层入场。
4. 将底背离早期反转与水平突破明确拆分，避免语义混淆。
5. 保持现有严格“双突破确认”能力，并提供向后兼容字段。
6. 通过全样本回测评估收益、回撤和假突破，而不是按单只股票调整阈值。

## 三、非目标

- 不重新定义底背离六种形态和 A/B 价格关系。
- 不重写下降趋势线几何算法；但本方案进入严格双突破前，必须将趋势线数据窗口限制
  为 B 点及以前并冻结，消除 `B+10` 导致的重绘。
- 不承诺固定参数是最终最优值；默认值必须经滚动回测和样本外验证。
- 不把 2026-07-22 强行定义为所有股票的标准买点。
- 不以盘中最高价突破代替日线收盘确认。

## 四、方案比较

### 方案 A：最高影线单阻力

继续使用 A/B 之间最高 `swing high` 的最高价。

优点：

- 规则简单。
- 对主要套牢区突破要求严格。
- 假突破相对较少。

缺点：

- 长上影可能使确认大幅延后。
- 无法表达不同成熟度的入场机会。
- 对单个异常价格点过度敏感。

### 方案 B：收盘价或实体高点单阻力

将 H 的最高价替换为 `max(open, close)` 或收盘价。

优点：

- 更接近市场实际接受的成交价格。
- 一般比最高影线更早确认。

缺点：

- 仍然是单点阈值。
- 无法区分近端阻力和主要套牢区。
- 横盘市场容易增加假突破。

### 方案 C：多触点阻力区与分层入场

对 A/B 区间内的摆动高点、K 线实体上沿和成交量信息进行聚类，形成近端阻力区
R1 和主要阻力区 R2，并把早期反转作为独立信号。

该方案能同时保留早期机会和严格确认，推荐采用。

## 五、核心投资语义

### 1. 早期反转

早期反转不是水平阻力突破。它表示：

- B 点低位结构已经得到最小确认。
- 底背离形态有效。
- 当日出现价格反转和量价改善。

该阶段用于小仓试错，不应把仓位提升到完整确认水平。

### 2. 近端阻力区 R1

R1 表示当前价格上方最近、且有重复市场记忆的供给区。它用于确认底背离后的第一段
反弹已经摆脱近期压制。

### 3. 主要阻力区 R2

R2 表示 A/B 结构中的主要套牢和抛压区域。突破 R2 才表示完整水平突破，继续与
下降趋势线突破共同构成严格双突破。

## 六、数据边界与冻结规则

### 1. 统一时点边界

执行路径明确拆分为：

- `legacy_v1`：继续调用现有 `BottomDivergenceBreakoutDetector`，保持历史行为。
- `causal_v2`：新增独立入口，使用统一 `as_of_index`、冻结证据和阻力区。

不得通过修改共享默认参数让 v1 隐式进入 v2。可复用的数学纯函数必须由两条路径
显式传参调用。

`causal_v2` 入口新增必填语义参数 `as_of_index`；未显式传入时只能取
`len(df)-1`。入口首先执行 `visible_df = df.iloc[:as_of_index+1]`，后续 v2 的
A/B、MACD、趋势线、阻力区和突破判断只能消费 `visible_df`。

端到端约束：

- A/B 与 MACD 低点匹配窗口不得越过 `as_of_index`。
- 阻力区和下降趋势线的结构输入不得越过 `B.idx`。
- B 后 K 线只能改变触发状态，不得改变 A/B 版本内的线和区域。
- 对任意时点 T，修改 T 之后全部数据，T 的检测结果必须逐字段不变。

### 2. B 点生命周期

B 点分为：

- `provisional`：至少出现 1 根右侧 K 线，允许产生早期反转试仓信号。
- `confirmed`：已经出现 `swing_order` 根右侧 K 线且 B 仍是窗口最低点。
- `invalidated`：确认窗口内出现更低低点，旧候选失效，但历史信号不回写。

R1/R2 在 B 首次进入 `provisional` 时使用 `df.iloc[:B.idx+1]` 冻结。若 B
最终失效，区域版本状态改为 `invalidated`；不得拿新低点重画旧版本。

B 首次进入 `provisional` 的时点固定为 `B.idx+1`。v2 在该时点冻结：

- A/B 价格低点；
- A/B 对应的 DIF/DEA 低点索引和值；
- `pattern_code` 和价格/MACD 关系；
- R1/R2 和下降趋势线结构。

从 `B+1` 到 `B+swing_order` 只更新 B 生命周期，不重新匹配 MACD、不改变
`candidate_version` 和 `zone_version`。

### 3. 无状态确定性冻结

第一阶段不新增专用事件表。阻力区由纯函数根据不可变前缀确定性重算：

```text
candidate_version_payload = {
    "algorithm_version": "causal-bottom-divergence-v2",
    "pattern_code": pattern_code,
    "price_relation": price_relation,
    "macd_relation": macd_relation,
    "a_trade_date": A.trade_date,
    "b_trade_date": B.trade_date,
    "price_low_a": frozen_price_low_a,
    "price_low_b": frozen_price_low_b,
    "macd_low_a": frozen_macd_low_a,
    "macd_low_b": frozen_macd_low_b,
}
candidate_version = sha256(canonical_json(candidate_version_payload))

zone_version_payload = {
    "algorithm_version": "resistance-zone-v2",
    "candidate_version": candidate_version,
    "freeze_trade_date": B.trade_date,
    "normalized_ohlcv": normalized_ohlcv[A.idx:B.idx+1],
    "parameter_snapshot": parameter_snapshot,
    "data_source": data_source,
    "adj_factor_source": adj_factor_source,
}
zone_version = sha256(canonical_json(zone_version_payload))
```

索引和 `as_of_index` 不进入哈希，避免补历史数据或后续触发状态导致版本变化。
生命周期、突破事件、当前主候选排名只随输出保存，不进入版本哈希。同一候选参数变化
时 `candidate_version` 不变、`zone_version` 变化。

`frozen_price_low_a/b` 和 `frozen_macd_low_a/b` 对象都必须包含
`trade_date`；日期既作为对象字段，也作为 payload 一级身份字段，避免不同日期但
数值相同的候选发生版本碰撞。

哈希输入使用 UTF-8 canonical JSON：键排序、日期统一 `YYYY-MM-DD`、数值四舍五入
到 6 位小数、NaN 转为 `null`。每个聚类另生成
`zone_id=sha256(zone_version + sorted(touch_trade_dates))`，并列排序使用
`zone_id`，不使用候选级 `zone_version`。

历史保留方式：

- 每日筛选通过 `factor_snapshot_json` 保存当日候选版本。
- 回测按目标日切片后重算同一版本。
- 新 A/B 候选生成新 `candidate_version`，不得覆盖旧筛选日的快照。
- v2 检测结果返回 `candidate_records` 列表和单独的
  `primary_candidate_version`，不再只返回当前最佳候选。
- `invalidated` 候选从 `invalidated_at_bar_index` 起在当前结果中保留 20 根
  K 线；更早历史由每日筛选快照查询。
- 如后续需要跨日查询所有未入选观察结构，再单独设计事件表；不在第一阶段引入。

无状态重建 `candidate_records` 的确定流程：

1. 在 lookback 内扫描满足“低于左侧 `swing_order` 根，且不高于下一根”的历史
   B 点，作为曾经进入 provisional 的 B。
2. 对每个 B 使用截止 `B+1` 的前缀重放 A/B 与 MACD 判定，重建冻结证据和版本。
3. 用当前 `as_of_index` 仅更新生命周期、突破事件和 actionability。
4. 删除失效已超过 20 根的记录，其余按下述固定排序返回。

active 候选排序键：

```text
(
  lifecycle_priority: confirmed=2, provisional=1,
  stage_priority: major_actionable=4, near_cleared=3, early=2, forming=1,
  signal_strength 降序,
  B.trade_date 降序,
  candidate_version 升序
)
```

`invalidated` 候选排在所有 active 候选之后，按
`(invalidated_at_bar_index降序, candidate_version升序)` 排列。只有 active
候选可成为 primary；没有 active 候选时 `primary_candidate_version=None`，所有
顶层兼容字段返回空模板。存在 primary 时，顶层 v2 字段始终投影自该 record，
`FactorService` 不自行二次排序。

### 4. 数据口径

数据使用统一复权 OHLCV。`zone_version` 同时保存 `data_source`、
`adj_factor_source` 和参数快照；无法确认复权口径时输出质量警告。

## 七、阻力候选点提取

在 `[A, B]` 范围内提取以下候选：

### 1. 候选 K 线

- 使用对 `[A, B]` 完整可见窗口运行的 `find_swing_highs`，不得使用 B 后数据确认
  B 前摆动高点。
- 除摆动高点外，纳入满足
  `upper_wick_ratio >= 0.35` 且 `(high-close)/ATR14 >= 0.5`
  的拒绝 K 线，用于识别多个非主摆动高点构成的近端压制。
- 保存 `high`、`body_top=max(open, close)`、日期和 bar 索引。
- 同一根 K 线只产生一个候选对象，不重复计数。
- 缺少 `open` 时使用 `close` 作为 `body_top`，并记录 `missing_open` 降级原因。

### 2. 长上影信息

计算：

```text
upper_wick_ratio = (high - body_top) / max(high - low, epsilon)
```

默认 `upper_wick_ratio >= 0.5` 视为长上影。当满足时：

- `high` 作为阻力区上沿证据。
- `body_top` 作为市场接受价格证据。
- 聚类锚点为
  `anchor_price=body_top+min(high-body_top, 0.75*ATR14_at_B)`，避免极端影线
  直接决定聚类位置；非长上影使用 `anchor_price=high`。
- 该 K 线不能凭最高价单独形成 R1。
- 只有当它的 `body_top` 与至少另一个交易日形成有效聚类，或满足第八节单触点
  低置信度 R2 条件时，其 `high` 才可扩展 R2 上沿；否则只保留为 legacy H 证据。

### 3. 成交量和拒绝强度

候选点记录：

- 当日成交量相对前 5 日均量，记为 `volume_ratio_5`。
- 从最高价回落至收盘价的比例。
- 后续 1～3 根 K 线的最大回落幅度，但仅用于 B 点之前已经完整发生的候选。

这些信息用于识别高成交量抛压和明显价格拒绝。

## 八、自适应聚类

候选点按价格聚类，不使用固定绝对价差。

ATR14 使用真实波幅 `TR=max(high-low, abs(high-prev_close),
abs(low-prev_close))` 的 14 日简单移动平均。默认相邻连接容差：

```text
gap_tolerance(price) = max(price * 1.5%, ATR14_at_B * 0.5)
```

确定性聚类步骤：

1. 按 `(anchor_price, trade_date)` 升序排列触点。
2. 相邻两个 `anchor_price` 的差不超过两者较大 `gap_tolerance` 时连接。
3. 对连接关系求连通分量，每个分量形成一个聚类。
4. 同一交易日最多贡献一个触点。

聚类有效性：

- 普通阻力区至少包含两个不同交易日的触点。
- 单触点永远不能成为 R1。
- R2 原则上也要求两个触点；仅当 `volume_ratio_5 >= 2.0` 且
  `(high-close)/ATR14 >= 1.0` 时允许单触点 R2，并标记
  `confidence="low"`。
- 同一天的 `high` 和 `body_top` 属于同一触点，不能虚增触点数量。

阻力区边界：

- 触点权重 `w = min(max(volume_ratio_5, 0.5), 3.0)`；成交量缺失时 `w=1`。
- `zone_lower`：`body_top` 的加权 25 分位数。
- `zone_center`：`body_top` 的加权中位数。
- `zone_upper_body`：`body_top` 的加权 75 分位数。
- `zone_upper`：有效触点 `high` 的加权 90 分位数，但不得高于
  `zone_center + 2*ATR14_at_B`。
- 没有 ATR 时，`zone_upper` 上限回退为 `zone_center*1.05`。

区间重叠比例定义为：

```text
overlap_ratio = intersection_width / min(zone_width_1, zone_width_2)
```

`overlap_ratio >= 0.6` 时合并，重新按上述公式计算边界。

加权分位数采用确定性定义：按价格升序，取累计权重首次达到
`q*total_weight` 的价格；不做插值。

## 九、阻力区评分

每个分量先计算以下 0～1 特征：

```text
touch_score     = min(distinct_touch_days / 4, 1)
recency_score   = exp(-bars_from_latest_touch_to_B / 20)
volume_score    = min(weighted_mean(volume_ratio_5) / 3, 1)
rejection_score = min(weighted_mean((high-close)/ATR14), 1)
tightness_score = 1 - min((zone_upper_body-zone_lower)/(2*ATR14), 1)
distance_score  = 1 - min((zone_lower-B.close)/(B.close*0.25), 1)
height_score    = min((zone_center-B.low)/(max_high_A_B-B.low), 1)
```

缺失 ATR 时，涉及 ATR 的特征使用百分比等价式；缺失成交量时删除
`volume_score` 权重并对其余权重归一化。

R1 分数：

```text
0.30*touch + 0.25*recency + 0.15*volume
+ 0.15*rejection + 0.10*tightness + 0.05*distance
```

R2 分数：

```text
0.35*touch + 0.15*recency + 0.15*volume
+ 0.15*rejection + 0.10*tightness + 0.10*height
```

默认有效阈值为 `0.45`。所有权重和阈值配置化并进入版本哈希。

## 十、R1 与 R2 选择

### R1：近端阻力区

R1 候选集只包含多触点有效区，不包含单触点低置信度 R2。从
`zone_lower > B.close` 且 R1 分数不低于 `0.45` 的候选中选择。排序键固定为：

```text
(zone_lower 距 B.close 的距离升序, R1分数降序, 最新触点日期降序, zone_id升序)
```

若最近区域证据不足，继续选择下一个有效区域。没有合格区域时，R1 为空，不使用
单日噪音补位。

### R2：主要阻力区

R2 候选集包含多触点有效区及满足第八节条件的单触点低置信度区。

- R1 存在时：R2 排除 R1，并要求
  `candidate.zone_lower > R1.zone_upper`。
- R1 不存在时：直接从全部 R2 候选中选择，不访问 R1 字段。

R2 按以下固定排序选择：

```text
(R2分数降序, zone_center降序, 触点数降序, 最新触点日期降序, zone_id升序)
```

如果 R1 存在但没有区位于 R1 上方，则设置
`R1=<该区>, R2=None, zone_count=1`，分层策略只有早期反转和单区确认两级。
如果 R1 为空但 R2 存在，则设置 `R1=None, R2=<该区>, zone_count=1`，阶段为
“early→R2”；如果两者都为空，`zone_count=0`。

如果结构最高价来自单根长上影：

- R2 下沿按实体接受价的加权 25 分位数确定。
- 只有满足第八节聚类和 ATR 上限时，R2 上沿才可包含长上影高点。
- 输出为区间，不压缩为单一最高价。

R1 与 R2 高度重叠时合并为一个主要阻力区，避免制造伪分层。

## 十一、触发规则与仓位分层

### 阶段 0：早期反转试仓

必要条件：

- `pattern_code` 属于
  `{price_down_macd_up, price_down_macd_flat, price_flat_macd_up}`。
- B 点状态至少为 `provisional`。
- 当日收盘高于前一日最高价。

反转强度定义：

```text
close_position_score = clip((close-low)/max(high-low, epsilon), 0, 1)
body_ratio           = abs(close-open)/max(high-low, epsilon)
body_score           = clip(body_ratio/0.5, 0, 1)
volume_score         = clip(volume_ratio_5/2.0, 0, 1)
return_score         = clip(max(pct_chg, 0)/6%, 0, 1)
early_strength       = 0.30*close_position_score
                     + 0.25*body_score
                     + 0.25*volume_score
                     + 0.20*return_score
```

`early_strength >= 0.65` 才触发。成交量缺失时删除其权重并重新归一化；`open`
缺失时删除 `body_score` 权重并重新归一化。

默认仓位建议：目标仓位的 20%。

### 阶段 1：突破 R1

R1 输出四个递进事件；它们的索引可以同时保留：

- `near_zone_entered`：收盘首次达到或高于 `R1.zone_lower`。
- `near_zone_accepted`：首次满足
  `close[t-1] >= zone_lower and close[t] >= zone_lower`。
- `near_zone_crossed`：收盘首次高于 `R1.zone_upper*(1+buffer_pct)`。
- `near_zone_cleared_confirmed`：`near_zone_crossed` 当日
  `volume_ratio_5 >= 1.2` 时与 crossed 同日；否则仅在下一交易日
  `close >= zone_upper` 时确认。

其中：

```text
buffer_pct = max(0.3%, ATR14_at_breakout*0.1/close)
```

分层策略只有 `near_zone_cleared_confirmed` 才计为 R1 突破并驱动加仓。无量越线
后次日未站稳时保留 `crossed_bar_index`，但
`cleared_confirmed_bar_index=None`。

默认累计仓位建议：目标仓位的 50%。

### 阶段 2：突破 R2

R2 历史突破事件一旦成立不再回写为 false。事件条件：

- `close > R2.zone_upper*(1+buffer_pct)`。
- 与已经按 B 点冻结的下降趋势线突破满足
  `abs(r2_breakout_bar-trendline_breakout_bar) <= sync_window`。
- `sync_window` 默认 3 根，并进入 `parameter_snapshot` 和 `zone_version`。

事件输出 `major_zone_breakout.confirmed=true` 和首次确认 bar；后续破位或走远只
影响当前状态，不改写历史事件。

当前可加仓 `major_zone_actionable_entry` 条件：

- `major_zone_breakout.confirmed=true`。
- B 后最低价不低于
  `min(A.price, B.price)*(1-bottom_divergence_break_tolerance)`。
- `confirmation_days <= 3` 且
  `extended_pct=(latest_close-breakout_close)/breakout_close*100`
  满足 `0 <= extended_pct <= 10%`。
- 最新收盘不低于 `R2.zone_upper`。

任一条件不满足时分别标记 `structure_broken`、`stale_confirmation` 或
`extended_not_entry`，`major_zone_actionable_entry=false`，不得驱动满仓。

默认累计仓位建议：目标仓位的 100%。

仓位比例是策略建议，不应在检测器中硬编码为交易指令。

## 十二、001337 预期解释

最终 golden fixture 从 2025-12-01 开始，按逐日前缀回放：

- A=2026-07-01，B=2026-07-21。
- 2026-07-22：收盘高于前一日最高价，`close_position=0.5306`、
  `body_ratio=0.4980`、`volume_ratio_5=1.9014`、涨幅 5.5668%，按默认公式
  `early_strength=0.8314`，触发底背离后早期反转并进入小仓试错阶段。
- R1 必须只使用 B 点及以前数据；基于 07-02、07-08、07-10 等重复压制，
  最终冻结为 `37.46–39.20`；不得把
  2026-07-23 的 40.26 反向纳入区域。
- 2026-07-23：收盘 40.26，高于 R1 上沿及缓冲，且
  `volume_ratio_5=1.3745`，预期 `crossed_bar_index` 与
  `cleared_confirmed_bar_index` 均为当日，进入加仓阶段。
- R2 最终冻结为 `40.61–42.90` 的主要供给区，而不是仅有 42.90 一条线。
- 2026-08-05：收盘 43.27，形成 R2 historical 突破事件；只有复权 provenance
  受信任时才可进入严格确认执行阶段，unknown provenance 时为
  `major_unverified`。

R1/R2 数值与触发日期已由 golden fixture 固定。该个股只用于可解释性验收，
不作为参数拟合或发布判断的唯一目标。

## 十三、输出结构

检测器建议新增：

```python
{
    "early_reversal": {
        "triggered": bool,
        "trigger_bar_index": int | None,
        "b_lifecycle": "provisional | confirmed | invalidated",
        "strength": float,
        "reasons": list[str],
    },
    "resistance_near_zone": {
        "zone_id": str,
        "lower": float,
        "upper": float,
        "score": float,
        "touch_points": list[dict],
        "frozen_at_bar_index": int,
        "confidence": "high | medium | low",
    } | None,
    "resistance_major_zone": {
        "zone_id": str,
        "lower": float,
        "upper": float,
        "score": float,
        "touch_points": list[dict],
        "frozen_at_bar_index": int,
        "confidence": "high | medium | low",
    } | None,
    "near_zone_events": {
        "entered_bar_index": int | None,
        "accepted_bar_index": int | None,
        "crossed_bar_index": int | None,
        "cleared_confirmed_bar_index": int | None,
    },
    "major_zone_breakout": {
        "confirmed": bool,
        "bar_index": int | None,
    },
    "major_zone_actionable_entry": bool,
    "major_zone_actionability_status": str,
    "candidate_version": str,
    "resistance_zone_version": str,
    "zone_count": int,
    "parameter_snapshot": dict,
    "degradation_reasons": list[str],
    "candidate_records": list[dict],
    "primary_candidate_version": str | None,
}
```

## 十四、兼容性

采用并行 v2 发布，不静默改变 v1：

- `horizontal_resistance` 继续表示旧算法最高 H，不映射到 R2。
- `horizontal_breakout_confirmed` 和 `bottom_divergence_double_breakout`
  继续保持 v1 行为。
- 旧 `buy_points` 继续保持“趋势线→H→回踩”，新增
  `layered_buy_points_v2` 表示“早期反转→R1→R2”。
- 新增策略 `bottom_divergence_layered_entry_v2`，在回测通过前不替换
  `bottom_divergence_double_breakout`。

联动迁移矩阵：

- `FactorService`：追加 v2 因子、版本和降级原因。
- K 线构建链路：向 v2 计算器显式传入 `data_source`、`adj_factor_source`；
  元数据缺失时不得伪装为已确认复权。
- `EntryStrategyE`：保持 v1；新增独立 v2 策略封装。
- `bottom_divergence_double_breakout.yaml`：保持不变；新增 v2 YAML。
- `SetupFreshnessAssessor`、`EntryMaturityAssessor`、`TradePlanBuilder`：
  识别 early/R1/R2 三阶段。
- 生产 AI review 路径为
  `CandidateAnalysisService → ScreeningAiReviewService → ScreeningAiReviewGuard`：
  prompt builder 提供区域、触点、版本、阶段与降级证据，guard 保持环境、
  provenance、时效和结构硬门禁，AI 不能把观察态升级为可执行态。
- Web 类型与展示：用区间带展示 R1/R2，明确 v1/v2。
- 回测：按策略版本分组，禁止混合统计。

v2 YAML 必须把 early、R1 或 R2 任一有效阶段纳入候选池，使每日
`screening_candidates.factor_snapshot_json` 能保存观察版本；否则“无状态重算”
无法提供生产历史证据。

v2 样本外达标后再单独决定是否弃用 v1，不在本设计中自动替换。

## 十五、异常与降级

- 候选触点不足：R1/R2 返回 `None`，事件索引全部为 `None`。
- OHLC 预热不足 15 根或 ATR 为 NaN：聚类容差回退到 1.5%，区域上沿上限回退到
  `zone_center*1.05`，记录 `atr_unavailable`。
- 缺少 `open`：`body_top=close`，记录 `missing_open`。
- 成交量缺失：取消量能评分权重并重新归一化，不直接拒绝。
- 缺失或非有限 OHLC：跳过该 bar；有效 bar 不足时拒绝候选。
- 复权 provenance 不明确：记录 `adjustment_unknown`；early/R1 只保留观察证据，
  但 `bottom_divergence_v2_candidate` 仍可为 true 以进入观察池。历史 R2 阶段投影为
  `major_unverified` 并将 candidate 设为 false；所有阶段均强制
  `bottom_divergence_v2_major_actionable_entry=false`，交易执行与 AI 可执行建议
  fail-closed。只有
  `tushare_native` / `akshare_qfq_div_raw` 受信任来源可执行。
- R1/R2 重叠：合并为一个主要阻力区。
- 最新收盘已远离 R2：标记为 `extended_not_entry`，不得因为刚计算出区域而追高。
- v2 追高阈值固定使用 R2 突破收盘价：`confirmation_days>3` 为
  `stale_confirmation`，或 `extended_pct>10%` 为 `extended_not_entry`。

## 十六、测试

### 1. 单元测试

- 多个接近高点能聚类为一个阻力区。
- 聚类、加权分位数、评分和并列排序使用精确数值 golden tests。
- 单根长上影不能独自决定 R1。
- 长上影可作为 R2 上沿，但实体价格决定下沿。
- 同一天的最高价和实体上沿只计一个触点。
- R1 选择近端有效区，R2 选择主要供给区。
- R1/R2 重叠时正确合并。
- 缺少 ATR、成交量和复权信息时安全降级。
- `provisional→confirmed/invalidated` 生命周期正确。
- `candidate_version` 和 `zone_version` 对相同输入稳定，对参数或前缀变化敏感。
- `candidate_records` 能在主候选替换后保留 20 根内的失效版本。
- primary 排序和顶层字段投影使用确定性 golden tests。
- R2 历史突破事件在走远或后续破位后保持不变，但
  `major_zone_actionable_entry` 正确变为 false。

### 2. 时点一致性测试

对同一 A/B 结构分别输入：

- 截止 B 确认日的数据；
- B 后 5 根数据；
- B 后 20 根数据。

三次输出的 R1/R2 边界、触点和版本必须一致，仅生命周期和突破状态允许变化。
其中 B+1、B+`swing_order`、B+20 三个截面的 `candidate_version`、
`zone_version` 和冻结 MACD 证据必须完全一致。

增加两类不变性测试：

- 后置扰动：随机修改 `as_of_index` 之后全部 OHLCV，目标时点完整输出逐字段不变。
- 全链路前缀：逐日调用 FactorService、YAML 筛选、EntryStrategy、交易计划和 AI
  evidence，后续日期不得改写较早日期的结构和突破日。

下降趋势线必须同时通过上述测试；否则 v2 严格双突破不得上线。

### 3. 001337 回归

最低验收：

- 2026-07-22 出现早期反转信号。
- 2026-07-22 不得伪报 R2 完整突破。
- 2026-07-23 `crossed_bar_index` 与 `cleared_confirmed_bar_index`
  均为当日；R1 区间不得使用 07-23 数据生成。
- R2 完整突破保持在真正收复主要供给区的时点。
- 2026-08-05 `major_zone_breakout.confirmed=true`；受信任 provenance 时
  `major_zone_actionable_entry=true`，unknown provenance 时保持 historical 事件但
  投影为 `major_unverified` 且不可执行；超过 3 根或跌回区域后仅 actionability 变化。
- 逐日重放不得改写历史突破日期。

再增加至少：

- 10 只长上影但后续失败的负例。
- 10 只多触点阻力突破成功的正例。
- 除权、低流动性、高波动和横盘环境各一组样本。

### 4. 全样本回测

使用现有回测交易成本和滑点配置。按时间顺序划分 60% 训练、20% 验证、20% 样本外
测试，并在训练/验证段滚动寻参；测试段只运行一次。

比较旧算法与新算法各阶段的：

- 信号数量和覆盖率；
- 5/10/20 日收益分布；
- 胜率和盈亏比；
- 最大有利变动与最大不利变动；
- 假突破率；
- 最大回撤；
- 从早期反转到 R1/R2 的转化率；
- 分市场环境、波动率和流动性分组表现。

参数选择使用滚动训练/验证和样本外测试，不得在全样本上一次性寻优。

指标定义：

- 10 日净收益期望值：所有信号按确认日收盘买入、第 10 个交易日收盘卖出的单笔净
  收益算术平均值，扣除仓库现有双边交易成本和滑点。
- 假突破：确认突破后 3 个交易日内出现收盘低于对应 `zone_lower`，且失效前最大
  收盘浮盈不足 3%。
- 最大回撤：按各策略既定仓位规则生成权益曲线后的峰谷回撤。
- 市场环境使用现有 `market_regime`；波动率和流动性分组边界由训练集三分位数固定，
  验证集和测试集不得重新计算边界。

预注册判断标准：

- 主指标：样本外 10 日净收益期望值。
- R2 v2 相对 v1：当 v1 主指标为正时，
  `v2_expectancy >= 0.98*v1_expectancy`；v1 不为正时 v2 不得更低。
  最大回撤不得恶化超过 2 个百分点，
  假突破率不得增加超过 3 个百分点。
- R1：10 日净收益期望值必须为正，且 5 日最大不利变动中位数不得比 v1 入场恶化
  超过 1 个百分点。
- early：单独统计，不以命中数量作为成功；10 日净收益期望值必须为正，且
  `early→R1` 转化率不低于训练集该转化率的 Wilson 95% 置信下界；该下界在查看
  测试集前锁定。
- 任何阶段只提高覆盖率但违反风险非劣约束，均判定失败。
- 每个阶段样本外总信号数至少 100；单个分组至少 30 个信号才进入分组判断。
- 在满足最小样本量的市场环境、波动率和流动性分组中，至少 70% 分组的 10 日净
  收益期望值为正，避免收益仅由单一行情贡献。

## 十七、实施边界

建议拆为四个可独立验证的步骤：

1. 新增独立 `causal_v2` 检测入口及统一 `as_of_index`，冻结 v2 的 MACD 证据
   和趋势线；`legacy_v1` 不改，先通过端到端因果性测试。
2. 新增纯函数阻力区计算器、确定性版本和黄金样例，不接筛选。
3. 接入检测器输出及因子快照，保留旧策略语义。
4. 新增 v2 分层入场策略、Web 展示和回测对比。

第一步只关闭未来窗口和冻结结构，不重写趋势线数学模型；更深入的趋势线算法优化
仍作为独立任务。

## 十八、验收标准

- 阻力区只使用 B 点及以前数据。
- v2 所有组件只使用 `as_of_index` 及以前数据。
- B 后数据不会改写已冻结区间。
- v2 MACD 匹配和下降趋势线不再读取时点之后或 B 之后数据。
- 单根长上影不会独自决定近端阻力。
- 聚类、评分、边界、并列排序和版本公式均有确定性 golden tests。
- 早期反转、R1 突破和 R2 突破具有独立语义和字段。
- v1 字段、`buy_points`、策略和展示保持原行为；v2 独立发布。
- 001337 能解释为 07-22 early、07-23 R1、08-05 R2 historical；只有受信任
  provenance 才能把相应阶段转为执行建议。
- 全样本样本外结果满足预注册收益、回撤和假突破非劣约束。
