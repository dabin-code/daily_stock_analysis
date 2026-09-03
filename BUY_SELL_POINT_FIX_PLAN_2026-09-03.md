# 买卖点识别修复方案（2026-09-03）

> 背景：复核买卖点识别，发现入场点准确，但止损/止盈/仓位存在偏差。
> 本方案针对四项问题给出改法、SOP 映射与决策点。

---

## P0① 止盈加结构触发（远期强阻力 + 顶背离）

**问题**：`bottom_divergence_breakout_detector.py:772-785` 的 `exit_plan` 用**固定百分比**（+10% 减半、+20% 再减 1/4），而 SOP 5.2 要求**结构触发**——「股价触碰日线级远期强阻力位，或 MACD 红柱出现高位顶背离迹象」。

**现有可复用能力**：
- 顶背离：`DivergenceDetector.detect_bearish`（`divergence_detector.py:183`）用 **histogram 红柱**的 swing highs 识别"价格 higher-high + 红柱 lower-high"，正好对应 SOP 的"红柱高位顶背离"。`factor_service.py:778` 已算 `macd_bear_divergence`（布尔），但未接止盈。
- 远期强阻力：`bottom_divergence_breakout_detector.py:599` 已有 `ctx_high`（A 点之前窗口的最高价），即"A 点之前的下跌起点高点"。

**改法**：
1. 在 `bottom_divergence_breakout_detector.detect` 里调用 `DivergenceDetector.detect_bearish(df)`，得顶背离标志 `top_divergence`。
2. `exit_plan` 改造：
   - 新增 `"take_profit_resistance": ctx_high`（远期强阻力止盈目标）。
   - 新增 `"top_divergence": top_divergence`（红柱高位顶背离触发标志）。
   - `take_profit_targets` 首目标从固定 `+10%` 改为 `ctx_high`（若 `ctx_high > entry_price`，否则回退 +10% 兜底）；`top_divergence=True` 时追加"顶背离减仓"触发。

**SOP 映射**：5.2「触碰远期强阻力位 / MACD 红柱高位顶背离 → 分批止盈」。

**验证**：构造顶背离样本（价格新高 + 红柱下降），断言 exit_plan 的 `top_divergence=True` 且首目标 = ctx_high 而非固定 +10%。

---

## P0② 统一止损口径

**问题**：三处止损口径不一致——
- 123：`low_123_trendline_detector.py:595` `stop = p3_val`（P3 本身，无缓冲）。
- 底背离：`bottom_divergence_breakout_detector.py:750` `stop = min(a,b) * 0.97`（新低点下方 3%，偏松）。
- SOP 5.1：**P3（或 P1）下方一个价位**。

**改法**：统一为"结构低点下方统一缓冲"，定义模块级常量 `_STRUCTURAL_STOP_BUFFER_PCT`：
- 123：`stop = round(p3_val * (1 - _STRUCTURAL_STOP_BUFFER_PCT), 4)`。
- 底背离：`stop = round(min(a_price, b_price) * (1 - _STRUCTURAL_STOP_BUFFER_PCT), 4)`。

**决策点（需拍板）**：
| 选项 | buffer | 说明 |
|---|---|---|
| 严格 SOP | 0（一个价位，约 0.01 元） | 最贴近 SOP，但易被噪音扫损 |
| 推荐 | 0.005（0.5%） | 兼顾 SOP 精神与抗噪音，比 3% 紧得多 |

**SOP 映射**：5.1「初始结构止损：P3（或 P1）下方一个价位」。

**验证**：单测断言 123 与底背离的止损均为「结构低点 × (1-buffer)」，口径一致。

---

## P1③ MA10 移动止损（缺口 3）

**问题**：`low_123_trendline_detector.py:681-705` 的 trailing 用 swing low「自然止损法」，SOP 5.1 要求**以 MA10 为保护线**（60min 破 MA10 减 1/2 已改提示、日线破 MA10 清仓）。

**改法**（复用缺口 3 方案）：
1. `factor_snapshot` 已有 `ma10` 值。
2. `trade_plan_builder.py` 的 `TradePlan` 增 `trailing_stop_ma10` 字段 = 当日 MA10，`exit_rules` 追加「收盘跌破 MA10（{ma10:.2f}）清仓」。
3. 「60min 破 MA10 减 1/2」保持**提示文本**（无分钟线），不进 `exit_rules`。

**SOP 映射**：5.1「移动趋势保护：以 MA10 为保护线，日线跌破 MA10 清仓」。

**验证**：`tests/golden_cases/test_candidate_decision_golden.py` 的 TradePlan 断言同步；确认 `exit_rules` 含"跌破 MA10 清仓"。

---

## P1④ 三级买点改递减

**问题**：`bottom_divergence_breakout_detector.py:885-928` 三级买点仓位 `1/5 → 1/3 → 1/3`（**递增**），SOP 4.2 要求**递减**（20% → 15% → 10%）。

**改法**：`_build_buy_points` 的 `position_ratio` 改为递减：
- 买点1 趋势线突破：`20%`（1/5 仓，试错）。
- 买点2 阻力线突破：`15%`。
- 买点3 回踩支撑：`10%`。

**SOP 映射**：4.2「金字塔加仓，遵循递减原则（20% -> 15% -> 10%）」。

**验证**：单测断言三级买点仓位 20% > 15% > 10%（递减）。

---

## 分批与优先级

| 批次 | 项 | 改动面 | 依赖 |
|---|---|---|---|
| P0 | ① 止盈结构触发 | `bottom_divergence_breakout_detector.py` | `DivergenceDetector` 复用 |
| P0 | ② 止损口径统一 | `low_123_trendline_detector.py` + `bottom_divergence_breakout_detector.py` | buffer 决策 |
| P1 | ③ MA10 移动止损 | `trade_plan_builder.py` | 无 |
| P1 | ④ 三级买点递减 | `bottom_divergence_breakout_detector.py` | 无 |

## 待拍板

1. **止损 buffer**：严格 SOP（0，一个价位）还是推荐 0.5% 缓冲？
2. **止盈首目标**：用 `ctx_high`（A 点之前最高价）还是接入 `resistance_zone_detector` 的 R1 上沿（更精确但接口更重）？
