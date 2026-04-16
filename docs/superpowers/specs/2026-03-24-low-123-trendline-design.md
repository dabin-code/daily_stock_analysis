# 低位123结合趋势线确认设计文档

**日期：** 2026-03-24

**状态：** 已在对话中确认方案，待进入实施计划阶段

## 一、背景

当前仓库中已经有以下相关能力：

- `src/indicators/pattern_detector.py`：基础版 `123 底部形态` 检测
- `src/indicators/trendline_detector.py`：基础版趋势线拟合与突破检测
- `src/strategies/entry_strategies.py`：策略 B 的入场逻辑
- `src/services/factor_service.py`：筛选因子计算

现有实现能够分别识别：

- `P1 / P2 / P3` 这组三点结构
- 趋势线突破

但它还不能稳定识别“**低位123 + 下降趋势线/均线确认**”这一完整交易结构，因此会带来两个实际问题：

1. 普通震荡中的局部三点结构容易被误识别为低位123。
2. 即使识别到了 123 结构，也没有和趋势线、均线确认形成同一套买点逻辑，导致入场点不够稳定。

## 二、目标

构建一个统一检测器，用于识别“**低位123 + 下降趋势线/均线确认**”这一完整买入结构，使系统能够：

- 更准确地区分真正的低位123反转与普通震荡噪声
- 只有在 `位置2` 与下降压力线被有效突破时才确认入场
- 输出可复用的结构信息，供选股、报告、画图、图表标注使用

## 三、非目标

本次迭代不做以下事情：

- 不重写底层 swing high / swing low 工具
- 不整体重构 Strategy A 的趋势线突破逻辑
- 不在本轮引入完整的评分排序系统
- 不在无必要的情况下改动无关模块的用户可见行为

## 四、总体设计

本次方案采用“**保留底层能力，新增一层联合检测器**”的方式推进：

- 继续复用 `find_swing_lows` / `find_swing_highs`
- 继续复用现有趋势线拟合思路与工具函数
- 新增一个专门负责“低位123 + 趋势线/均线确认”的联合检测器
- 让 Strategy B 和筛选因子层直接消费联合检测结果，而不是分别拼接多个松散信号

建议新增模块：

- `src/indicators/low_123_trendline_detector.py`

建议改造接入点：

- `src/strategies/entry_strategies.py`
- `src/services/factor_service.py`
- `tests/` 下对应单测文件

## 五、关键逻辑

这一节是后续实现必须遵守的核心判定规则。

### 1. 必须先确认前置下降趋势

低位123不是单纯的三点几何结构，它必须建立在“前面确实经历过一段下跌”的基础上。

建议在接受任何 `P1` 候选前，先检查以下条件中的若干项：

- 最近一段 swing high 呈下降
- 最近一段 swing low 呈下降或弱势
- 局部下降压力线斜率为负
- `MA10 <= MA20`，或者 `MA20` 在结构形成前走平/向下

只有当前置下跌成立时，才允许继续识别低位123。

如果该条件不成立，应明确返回拒绝原因，例如：

- `rejection_reason = "no_prior_downtrend"`

### 2. 不再找到第一组三点就直接返回

当前 `detect_123_bottom` 的主要问题之一，是找到一组局部满足条件的 `P1 / P2 / P3` 后就直接返回，导致误判概率偏高。

新的联合检测器应当：

- 先生成多个 `P1 / P2 / P3` 候选组合
- 对每个候选都检查时间顺序：
  - `P1.idx < P2.idx < P3.idx`
- 检查价格结构：
  - `P2.price > P1.price`
  - `P3.price > P1.price`
- 排除反弹过弱的 `P1 -> P2`
- 排除回撤过深或过浅、缺乏结构意义的 `P2 -> P3`

这样可以避免把“碰巧符合局部条件的拐点组合”误判为有效低位123。

### 3. 必须单独定义“低位”

低位123不等于任意123。

需要单独增加“低位”判定，用来过滤掉：

- 高位123结构
- 中段整理结构
- 横盘中的局部噪声低点

建议的低位判断包括：

- `P1` 位于近端观察窗口的低位区间
- `P1` 接近近期最低区域
- `P1` 明显低于 `MA20`，必要时也低于 `MA60`
- 结构形成前的整体背景不是上升趋势中的中继整理

### 4. 趋势线必须围绕当前结构绘制，而不是全窗口随意回归

现有 `trendline_detector` 会用整段窗口内的 swing high 拟合趋势线，这种方法在数学上可以成立，但不一定代表“当前这组低位123正在面对的下降压力线”。

新的联合检测器应优先用“与当前结构相关”的高点来拟合下降压力线，重点关注：

- `P1` 前最近一段的下降高点
- `P1 -> P2` 反弹过程中的关键高点
- 属于同一轮结构的触碰点

趋势线质量要求：

- 优先使用 3 个及以上有效接触点
- 最低允许 2 个点成线
- 斜率必须为负，才能算下降压力线
- 需要记录接触点，便于后续画图和调试

### 5. 确认信号必须是联合确认

有效买点不能只靠 `close > P2`。

建议最终确认同时要求：

- 收盘突破 `位置2`
- 收盘突破下降压力线
- 可选但推荐：站上 `MA10` 或 `MA20`

并且，`突破位置2` 与 `突破趋势线` 必须尽量同步，例如：

- 同一根 K 线完成
- 或前后 `1-3` 根 K 线内完成

如果 `位置2` 很早就突破，趋势线却很久之后才突破，则不应视为同一笔低位123买点。

### 6. 直接输出交易与画图所需信息

联合检测器输出不能只包含 `True / False`，必须直接提供结构化结果，供策略、筛选、图表和报告复用。

建议输出结构如下：

```python
{
    "found": bool,
    "state": "rejected" | "structure_only" | "confirmed",
    "rejection_reason": str | None,
    "is_low_level": bool,
    "point1": {"idx": int, "price": float} | None,
    "point2": {"idx": int, "price": float} | None,
    "point3": {"idx": int, "price": float} | None,
    "downtrend_line": {
        "found": bool,
        "slope": float,
        "intercept": float,
        "touch_points": list[dict],
        "touch_count": int,
        "breakout_bar_index": int | None,
        "projected_value_at_breakout": float | None,
        "breakout_confirmed": bool,
    } | None,
    "breakout_point2_confirmed": bool,
    "breakout_trendline_confirmed": bool,
    "breakout_p2_bar_index": int | None,
    "ma_confirmation": bool,
    "entry_price": float | None,
    "stop_loss_price": float | None,
    "signal_strength": float,
}
```

交易语义约定：

- `confirmed`：P2突破确认，可执行买点（趋势线突破为加分项）
- 入场价：确认突破 `位置2` 时的入场价
- 止损价：`位置3`
- `structure_only`：结构已形成，但P2尚未被突破

## 六、文件级设计

### 新增文件

- `src/indicators/low_123_trendline_detector.py`
  - 负责候选结构生成、低位过滤、趋势线拟合、联合确认与统一输出

### 需要修改的现有文件

- `src/strategies/entry_strategies.py`
  - Strategy B 改为消费联合检测器结果
  - 策略触发条件：联合检测器 state = confirmed（P2突破确认，趋势线为加分项）

- `src/services/factor_service.py`
  - 为筛选系统增加联合结构因子
  - 尽量保留现有字段，降低兼容性风险
  - 增加新字段给后续选股和可视化使用

### 明确保留的现有文件

- `src/indicators/pattern_detector.py`
  - 暂时继续作为底层形态原语使用，不强制合并进新模块

- `src/indicators/trendline_detector.py`
  - 保持 Strategy A 行为稳定
  - 如有合适的底层函数可复用，但不让这个模块直接承担新联合逻辑

## 七、建议新增的策略/筛选输出

建议在筛选和策略结果中新增以下字段：

- `pattern_123_low_trendline`
- `pattern_123_state`
- `pattern_123_entry_price`
- `pattern_123_stop_loss`
- `pattern_123_signal_strength`
- `pattern_123_rejection_reason`

对 Strategy B 的建议行为：

- 只有当联合检测器返回 `state = confirmed` 时，`triggered = True`
- `reason` 中应体现：
  - 是否突破位置2
  - 是否突破趋势线
  - 是否有均线确认
- 结果中应暴露：
  - `entry_price`
  - `stop_loss_price`

## 八、测试关注点

这一节是后续开发中最重要的测试检查表。

### 1. 误判抑制

必须重点验证以下误判不会发生：

- 纯震荡行情被识别成低位123
- 中位整理结构被识别成低位123
- 高位123结构被误当作底部买入结构

### 2. 结构形成与买点确认必须分层

要明确区分：

- `structure_only`：结构存在，但不能买
- `confirmed`：结构成立且入场确认完成

必须覆盖以下场景：

- `P1 / P2 / P3` 成立，但未突破趋势线，不应触发买点
- 只突破 `P2`，没突破趋势线，不应触发买点
- 趋势线突破发生太晚，应降级为弱信号或拒绝

### 3. 突破同步性

必须验证：

- `突破位置2` 与 `突破趋势线` 在设定窗口内同步时才确认
- 如果二者时间差过大，不应算同一笔低位123买点

### 4. 趋势线相关性

必须验证：

- 用于拟合的趋势线来自当前结构相关高点，而不是过早、无关的历史高点
- 只有两个质量很差的点时，不能和多触碰趋势线等价处理

### 5. 交易输出正确性

必须验证：

- 已确认信号能返回 `entry_price`
- `stop_loss_price` 正确映射到 `P3`
- 拒绝状态下有清晰的 `rejection_reason`

## 九、建议新增测试

建议至少新增以下测试用例：

- 纯震荡数据：应拒绝
- 标准低位123，P2未突破：应返回 `structure_only`
- 标准低位123，突破 `P2`：应返回 `confirmed`（趋势线突破为加分项，不影响状态）
- 高位123：应拒绝为非低位结构
- P2突破但趋势线延迟：应返回 `confirmed`（趋势线非门控条件）
- 趋势线接触点质量差：应降低置信度或拒绝
- 同一数据中存在多个候选结构：应选择“最新且质量更高”的那个，而不是第一个局部三元组

## 十、现有测试的兼容要求

后续实现过程中，至少要确保以下测试继续通过：

- `tests/test_pattern_detector.py`
- `tests/test_trendline_detector.py`
- `tests/test_entry_strategy_ab.py`
- `tests/test_factor_service_phase3.py`

建议新增测试文件：

- `tests/test_low_123_trendline_detector.py`

并按需要补充：

- `tests/test_entry_strategy_ab.py`
- `tests/test_factor_service_phase3.py`

## 十一、主要风险

### 风险 1：阈值太严导致漏检

如果前置下跌、低位、趋势线同步确认条件过严，可能会漏掉真实可用结构。

缓解建议：

- 返回中间诊断字段
- 返回明确拒绝原因
- 使用“干净样本 + 边界样本”双重测试

### 风险 2：阈值太松导致误判仍然多

如果结构过滤和确认条件过松，依然会把震荡结构当成低位123。

缓解建议：

- 强制加入前置下跌确认
- 强制加入低位过滤
- 强制加入 `位置2 + 趋势线` 的联合确认

### 风险 3：兼容性漂移

如果 Strategy B 或因子输出改动过大，可能影响现有筛选行为。

缓解建议：

- 能保留的旧字段尽量保留
- 优先新增字段，而不是一次性替换所有输出
- 在过渡期保留原始 detector 作为底层能力

## 十二、实施前仍需在计划阶段明确的参数

这些内容可以在实施计划中细化并由测试驱动确定：

- 低位判定使用的观察窗口
- `P1 -> P2` 最小反弹幅度阈值
- `P2 -> P3` 合理回撤比例阈值
- `突破位置2` 与 `突破趋势线` 的同步时间窗口
- 均线确认是“必须条件”还是“辅助加分”
- 拒绝态是否统一表示为：
  - `found = False`
  - 或 `found = True + state = rejected`

## 十三、验收标准

当满足以下条件时，可认为本次工作完成：

- 系统能稳定识别低位123并结合趋势线上下文完成确认
- Strategy B 改为消费联合确认结果，而不是仅靠三点结构
- 筛选因子层能够暴露联合信号
- 测试覆盖误判、漏确认、滞后确认、标准确认等关键场景
- 检测结果包含后续画图所需的关键点与趋势线参数，无需上层再次推断

## 十四、实施建议

建议按以下顺序增量推进：

1. 先新增联合检测器
2. 用聚焦单测验证其结构识别和确认逻辑
3. 接入 Strategy B
4. 接入 FactorService
5. 回归现有测试，确认未误伤已有 detector 与策略行为

这样可以最大程度保持当前模块边界稳定，并降低回归风险。
