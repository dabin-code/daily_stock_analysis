# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

> For user-friendly release highlights, see the [GitHub Releases](https://github.com/ZhuLinsen/daily_stock_analysis/releases) page.

## [Unreleased]

### Added

- Added opt-in causal bottom-divergence v2 detection with B+1-frozen MACD/trendline evidence,
  deterministic R1/R2 resistance zones, versioned candidate records, and staged
  `early` / `near_cleared` / `major_actionable` factors. The new strategy
  `bottom_divergence_layered_entry_v2` remains disabled by default through
  `BOTTOM_DIVERGENCE_V2_ENABLED=false`.
- Added point-in-time replay coverage and the 001337 fixture beginning at 2025-12-01.
  Its frozen explainability regression is R1=`37.46–39.20`,
  R2=`40.61–42.90`, with 2026-07-22 early, 2026-07-23 R1, and
  2026-08-05 R2 historical. Frozen evidence and prefix-invariance tests prevent
  future bars from changing earlier zones or event dates.
- Added a reproducible sample-out CLI,
  `scripts/validate_bottom_divergence_v2.py`, with chronological 60/20/20
  selection, canonical JSON reports, version-separated v1/v2 metrics, isolated
  replay storage, and a zero-cost fail-closed release gate.
- Added bounded-memory frozen-evidence replay, Windows spawn-safe stock workers,
  stable progress/ETA output, and atomic parameter checkpoints with strict
  data/config/YAML/algorithm identity checks, content integrity, and atomic
  corruption recovery for resumable sample-out validation.
- Added Web cards and backtest labels for v2 stages/resistance ranges, plus
  production AI-review evidence and hard guards for provenance, stale,
  invalidated, extended, and incomplete execution states.

### Changed

- Preserved legacy v1 detector, factors, strategy, API, and Web rendering
  unchanged while exposing v2 through additive `bottom_divergence_v2_*`
  fields and the existing screening API.
- Adjustment provenance is now an execution boundary for v2: only trusted
  `tushare_native` / `akshare_qfq_div_raw` data is executable. Unknown
  provenance keeps early/R1 as observation-only evidence, projects historical
  R2 as `major_unverified`, and blocks trade-plan execution even when the
  historical breakout event remains visible.
- Five-layer decisions, AI review, Web presentation, and backtest replay now
  preserve the distinction between historical R2 confirmation and current
  actionability; AI cannot override rule-level safety gates.
- Sample-out replay now streams OHLCV once, reuses parameter-independent base
  factors and FiveLayer sector context, evaluates immutable causal/zone evidence
  per parameter hash, and runs only the selected hash for test/future maturation.
- Verification gates are green: the complete backend suite has 0 failures
  across stable shards, expected skips remain intentional, and the subsequently
  added configuration/API cases pass separately. Python compilation, flake8,
  `test.sh`-equivalent checks, full Web lint, and the production Web build pass.
  Direct Bash invocation of the CRLF gate script on Windows remains only a local
  line-ending difference because its equivalent commands pass. Exact collection
  totals remain in delivery evidence rather than this long-lived changelog.
- The sole remaining release blocker is the sample-out CLI's default zero-cost
  safety gate and the absence of a user-approved non-zero cost model and run
  range. Causal bottom-divergence v2 therefore remains disabled by default.

### 成交量/成交额落库单位统一（修复量能因子跨数据源 100 倍断层）

- Fixed 三条写入路径把数据源的原生单位原样写入 `stock_daily`，而该表 `volume` 的口径是股、`amount` 的口径是元。Tushare `daily` 的 `vol` 单位为手、`amount` 单位为千元；efinance/东财的成交量单位为手。受影响的写入方为 `src/services/market_data_sync_service.py` 的 `_try_bulk_sync`（每日行情同步主路径）、`src/services/fast_backfill_service.py`、`scripts/fast_backfill.py` 与 `data_provider/efinance_fetcher.py` 的历史 K 线归一化。
- 影响面：量比等量能因子在跨越数据源边界的窗口内会出现 100 倍级断层，系统会把正常成交误判为极度缩量。生产库实测 296.4 万条可判定记录中有 110.0 万条（37.1%）单位错误，其中 `TushareFetcher(bulk)` 写入的 51.4 万条无一正确。
- Added `data_provider/base.py` 新增 `SHARES_PER_LOT` / `YUAN_PER_THOUSAND_YUAN` 与 `lots_to_shares()` / `thousand_yuan_to_yuan()`，把落库单位口径收敛到单一出处；四条写入路径统一调用，避免约定散落各处再次被遗漏。
- Added `scripts/repair_volume_amount_units.py` 修正存量数据。按 `amount / (volume × close)`（物理含义为成交均价与收盘价之比，单位正确时必然接近 1）的量级分类，`~0.1` 补 `volume×100` 与 `amount×1000`、`~100` 补 `volume×100`。脚本默认 dry-run，幂等，且带越界守卫。
- 修正后全部数据源的该比值中位数回到 1.00 附近（此前 `TushareFetcher(bulk)` 为 0.1000、`EfinanceFetcher` 为 100.76）。落在判定区间之外的 397 条（0.013%）保持原样并输出清单待人工确认——其中部分记录的 `amount` 与 `close` 相差恰好 2 倍，疑为复权价与不复权成交额混存，属另一问题。

### 低位123检测改为段内极值选点（修复下跌中继反弹高点被误判为 P2）

- Fixed `Low123TrendlineDetector` 用"最新摆动点优先"的方式挑选 P1/P2/P3，导致把真实反弹高点之后的**下跌中继反弹高点误选为 P2** 的问题。典型误判：603980 于 2026-07-28 收盘 6.50 站上伪 P2（07-17 反弹高点 6.40）即被报为 `breakout_ready` 最佳买点，而真实结构的 P2（06-24 高点 7.02）远未突破；此前多轮外围补丁（跨度上限、突破时效、破位失效）均未触及该选点根因。
- Changed 候选生成新增三条**段内极值约束**：①P2 必须是 P1→P2 段的最高价（中途存在更高高点则该 P2 只是中继反弹）；②P1→P2 段内最低价不得跌破 P1（否则 P1 不是本轮底部）；③P3 必须是 P2→P3 段的最低价（中途存在更低回撤低点则当前摆动低点不是 P3）。
- Added P1 阶段新低校验：P1 之前 `low_pos_window`（默认 60 根）内存在更低低价时判为伪结构并剔除（新增拒绝原因 `p1_not_lowest_low`），防止下跌中继段自立门户拼出"更新"的伪123抢占选点。
- 修复后 603980 识别为 P1=06-12(5.30) → P2=06-24(7.02) → P3=07-09(5.71)，状态 `watching`（未破真 P2），不再误报买点。无新增配置项，现有 `LOW123_*` 配置继续生效。

### 底背离双突破检测新增结构护栏（修复陈旧 / 已失效结构被误判为最佳买点）

- Fixed `BottomDivergenceBreakoutDetector` 把早已作废或陈旧的底背离结构误判为 `confirmed`（最佳买点）的问题。根因：检测器只在 A 点之前做前置校验，从不检查 B 点之后的走势；双突破搜索窗口固定为 `b_idx+50` 无有效时效约束；候选排序仅按 `state_priority` + 强度，使「陈旧但已 confirmed」的老结构压过更贴近当前走势的新结构。典型误判：001229 于 2026-06-09 除权后（叠加不复权数据断层）旧结构被「复活」为 confirmed。
- Added 结构破位失效护栏：B 之后最低价跌破 `min(A,B)*(1-BOTTOM_DIVERGENCE_BREAK_TOLERANCE)` 时，该候选判为结构失效并剔除（与 `Low123TrendlineDetector` 的 `structure_broken_below_p1` 同源思路）。
- Added 突破时效护栏：水平阻力线 / 下降趋势线的双突破须在 B 之后 `BOTTOM_DIVERGENCE_MAX_BREAKOUT_GAP`（默认 30 根）内完成，超出视为陈旧突破，不计双突破确认，避免「结构成立后长期未突破、很晚才收复阻力」被误报为买点。
- Changed 候选排序引入新鲜度加权（权重 < 1，仅在同状态内决定优先级，不跨状态误升级），同强度 / 同状态下更贴近当前 K 线的结构优先。
- New `BOTTOM_DIVERGENCE_MAX_BREAKOUT_GAP`（默认 30）/ `BOTTOM_DIVERGENCE_BREAK_TOLERANCE`（默认 0）env / config keys，可通过 `.env` 调整；`src/services/factor_service.py` 与 `src/strategies/entry_strategies.py` 的检测调用统一读取该配置。

### Tushare 日线统一前复权（修复除权除息导致的技术形态误判）

- Fixed `TushareFetcher` 返回**不复权**日线价格，导致含除权除息事件的个股技术指标严重失真的问题。根因：`daily()` 接口返回不复权价，除权日会在价格序列中制造巨大「假断层」，污染 MACD / 均线 / 摆动点 / 趋势线 / 背离等所有基于价格的形态识别；且当配置 `TUSHARE_TOKEN` 时 Tushare 为最高优先级数据源，与 `EfinanceFetcher`（`fqt=1`）/`AkshareFetcher`（`adjust="qfq"`）的前复权口径不一致。典型误判：001229 于 2026-06-09 除权（`pct_chg=+3.41%` 但不复权名义价从 37.66「跌」到 27.60），除权后名义价自然回补被 `BottomDivergenceBreakoutDetector` 识别为「底背离双突破 confirmed」；修复后该假断层消失，状态降为 `late_or_weak`，不再命中。
- Added `TushareFetcher._apply_qfq_adjustment`：普通股票 `daily()` 取数后追加一次 `adj_factor()`，按「锚定窗口内最新交易日」的前复权公式 `qfq = raw * adj_factor / adj_factor[latest]` 转换 open/high/low/close/pre_close；volume/amount/pct_chg 保持原值，与主流行情软件及其它数据源口径一致。回测取历史窗口时得到 point-in-time 前复权，避免未来函数。
- adj_factor 接口不可用或返回空（如账号权限不足）时记录告警并**优雅回退**为不复权数据，保证取数主流程不中断；ETF（`fund_daily`）路径不变。
- 针对免费 Tushare 账号 `adj_factor` 接口限频（1 次/小时）场景，新增**熔断冷却**：命中限频 / 无权限类错误后暂停 qfq 尝试 1 小时并统一回退不复权，避免全市场逐股重试造成延迟与日志刷屏（限频账号下等价于不复权，但叠加底背离 / 低位123 的结构护栏仍可拦截除权断层类误判）。
- 注意：该改动使 Tushare 数据源的日线价格口径由不复权切换为前复权，会影响基于 Tushare 的回测基准与报告展示价格；若持久化的 K 线库中混有历史不复权 Tushare 数据，建议评估是否需要重建缓存以统一口径。

### Low-123 detector structural guards (fixes stale / loose structures mis-flagged as best entry)

- Fixed `Low123TrendlineDetector` 把早已作废的低位 123 结构误判为 `breakout_ready`（最佳买点）的问题。根因：检测器只在 P1 之前做前置校验，从不检查 P3 之后的走势，且 P2 突破窗口无时效上限、P1→P2 跨度无约束。典型误判：某结构在 P3 后暴跌至远低于 P1，两个月后一段不相关的反弹重新收复旧 P2 价位，旧结构被错误「复活」为最佳买点。
- Added 三道结构护栏：①破位失效——P3 之后最低价跌破 P1（容差 `LOW123_BREAK_TOLERANCE`）时判为 `rejected`（`structure_broken_below_p1`）；②P1→P2 跨度上限（`LOW123_MAX_P1_P2_BARS`，默认 30 根）剔除相隔过久的松散伪结构；③P2 突破距 P3 的时效上限（`LOW123_MAX_BREAKOUT_GAP`，默认 20 根），超过则视为陈旧突破，结构降级为 `watching`/`structure_only` 而非买点。
- New `LOW123_MAX_P1_P2_BARS` / `LOW123_MAX_BREAKOUT_GAP` / `LOW123_BREAK_TOLERANCE` env / config keys（默认 30 / 20 / 0），可通过 `.env` 调整，无需改代码或重建镜像；`src/services/factor_service.py` 与 `src/strategies/entry_strategies.py` 的检测调用统一读取该配置。

### Data health backfill / gap repair now advances the audit pass date (fixes screening blocked after repair)

- Fixed 数据健康「回填到目标日 / 修复缺口 / 重试失败股票」修完缺口后最新交易日仍 `not_passed`、选股拿不到可用交易日的问题。根因：单日治理的 auto-skip 只能豁免「当日」停牌/无源缺口，一旦某些交易日的日常治理被跳过（例如程序未在那些交易日运行），这些日期的停牌缺口会长期滞留在最新交易日的审计窗口内，使最新交易日始终无法通过审计。
- Added `KlineGovernanceScheduleService.run_daily_governance_with_catch_up`：从最近一次审计通过日起，按交易日顺序逐日补跑 `run_daily_governance`（sync → audit → repair → auto-skip → re-audit），让每个被跳过的交易日各自获得当日证据并被正确豁免，从而把审计通过日推进到目标日。
- `FastBackfillService`（数据健康「回填到目标日」）改为优先走逐日补跑治理；`DataHealthTaskService` 的 `repair_gaps` / `retry_failed` 操作在修复缺口后追加一次逐日补跑治理以推进通过日（补跑失败不影响已完成的修复结果）。
- 新增配置 `KLINE_GOVERNANCE_MAX_CATCH_UP_SESSIONS`（默认 `30`）控制逐日补跑最多向前补齐的交易日数量，避免通过日缺失/相距过远时回溯过多导致大量全市场同步。
- `repair_gaps` / `retry_failed` 后台任务结果结构调整为 `{"repair_result"/"sync_result": ..., "catch_up_result": ...}`。

### Hot-theme screening universe locking

- `POST /api/v1/screening/openclaw-theme-run` now locks the screening universe to the constituents of the boards resolved from the incoming `themes`, instead of scanning the whole market. The passed-in theme/board directly determines the candidate pool.
- Added `ScreeningTaskService._resolve_theme_universe_codes`, which reuses the existing `ThemeNormalizationService` (alias table + board-name recall) to map each theme to `matched_boards`, then resolves board constituents via `batch_get_board_member_codes`.
- In `execute_run`, when a `theme_context` is provided without explicit `stock_codes`, the resolved constituents become the universe; if no board constituents can be resolved, the run fails fast with a clear `ValueError` (surfaced as HTTP `422`) instead of silently falling back to a full-market scan.
- `FiveLayerPipeline.run` gained a `lock_universe` flag: when the universe is theme-locked, L2 hot-board heat shrink is skipped (`l2_filter_mode="theme_universe_locked"`) so all board constituents flow straight into strategy screening. Sector-heat annotation (leaders / theme labels) still runs for reporting only.

### Screening final-score gate

- Added a final-layer minimum score gate in `ScreeningTaskService.execute_run`: candidates whose post L1-L5 rerank `rule_score` (the persisted `final_score`) falls below `SCREENING_MIN_FINAL_SCORE` are dropped before AI 二筛、persistence、通知 / 报告 / API 全链路。默认阈值 `80` 等价于至少达到 `probe_entry` 阶段或同等多维组合，设置为 `0` 关闭过滤
- New `SCREENING_MIN_FINAL_SCORE` env / config key registered in `config_registry`，可通过 `.env` 或 Web 设置页调整，并通过 `Config.validate_structured()` 守卫负值
- `ResolvedScreeningRuntimeConfig` 与 `pipeline_stats` 同步暴露 `min_final_score / below_min_final_score_count / after_min_final_score_count`，便于审计和 UI 展示

### Notifications

- Added Feishu app-based proactive notifications via `FEISHU_APP_ID` + `FEISHU_APP_SECRET` + `FEISHU_CHAT_ID`, so accounts without a custom bot webhook can still receive scheduled reports in a Feishu group
- Screening runs now auto-send the screening recommendation notification after every successful run (`completed` or `completed_with_ai_degraded`) regardless of whether the run was triggered by schedule, CLI, or API
- Fixed manually/API-triggered screening runs being marked `skipped` for notifications before auto-send, which prevented Feishu delivery even after the run completed successfully
- Fixed screening unit tests so they cannot send real Feishu notifications, and made zero-candidate notifications explicitly say that no candidates were produced instead of showing an empty recommendation block
- Long Feishu notifications now use a collapsed interactive-card panel by default, keeping the run summary visible while hiding detailed screening content until the user expands it
- Expanded screening recommendation notifications to render every pushed candidate as a detailed Web-like block with matched rules, buy/sell plan, theme/sector context, layered scores, phase explanations, risk parameters, and AI review details
- Screening auto-push (`ScreeningNotificationService.notify_run`) now generates push content adaptively: by default every candidate keeps its full audit block (matched rules / score breakdown / factor snapshot / audit evidence / AI review), so users see the complete details inside the Feishu collapsible panel; only when the content exceeds the single-message size budget does it gracefully degrade by capping the number of full audit blocks (`audit_top_n` ladder `None → 15 → 10 → 7 → 5 → 3 → 1 → 0`), keeping the result on a single, default-collapsed Feishu card. The complete audit content is still archived to `reports/screening_<run_id>.md`
- Raised the default `FEISHU_MAX_BYTES` from `20000` to `28000` (Feishu's interactive-card hard limit is ~30KB; we reserve ~2KB for card envelope JSON) so screening notifications with ~10–15 candidates fit in a single message instead of being split into multiple uncollapsed chunks
- Lowered the Feishu interactive-card default collapse threshold (`feishu_cards.DEFAULT_COLLAPSE_THRESHOLD_BYTES`) from 4000 to 1500 bytes so that even short screening summaries (or compact fallback) are hidden inside the collapsible panel by default and only shown when the user expands it

### Docker deployment

- Raised the default Docker Compose memory limit from `512M` to `2G` to give full-market screening enough headroom during factor snapshot generation

### Screening schedule notifications

- Added an independent `SCREENING_SCHEDULE_ENABLED` / `SCREENING_SCHEDULE_TIME` / `SCREENING_SCHEDULE_RUN_IMMEDIATELY` schedule for full-market screening, defaulting to a `07:00` trading-day run that reuses the existing screening notification workflow and Feishu webhook delivery

### Extreme strength stock-pool semantics

- `extreme_strength_combo` 策略明确收敛为 **热点题材股票池 / 排序器（`system_role: stock_pool`）**，不再承担精确买点语义。`setup_resolver` 回归测试锁定 `extreme_strength_combo` 永远不会被解析为 `setup_type` 或 `primary_strategy`
- `extreme_strength_scorer` 新增 `calculate_layered_scores` 和 `LayeredExtremeStrengthScores` 数据类，把总分拆为 `theme_pool_score / leadership_score / entry_signal_score / timing_penalty` 四桶；原 `calculate_extreme_strength_score` 继续返回同一个总分，向后兼容所有既有消费方
- 新增 `ExtremeStrengthTimingAssessor` 和 `StageLabel` 枚举（`pool_only / watch_only / breakout_day / retest_entry / extended_do_not_chase`），用 `bars_since_primary_event` 和 `extended_pct` 对"强势但已走远"的候选给出 `timing_penalty`；当 snapshot 包含 `stage_label == extended_do_not_chase` 时，`CandidatePoolClassifier` 会把 `LEADER_POOL` 档位 opt-in 降级为 `FOCUS_LIST`，snapshot 未填该字段时完全保留旧行为
- `CoreSignalIdentifier` 新增 `classify_signals` 统一入口和 `SignalKind` 枚举（`structure_low_entry / momentum_breakout / momentum_chase / none`）；原先"涨停 / 缺口 默认压过 低位123 / 底背离双突破"的硬优先级被移除，`primary_signal` 改由 `SignalKind` 优先级（结构入场 > 动量突破 > 动量追涨）选出；旧 `core_signal / bonus_signals` 字段继续保留以向后兼容
- `hot_theme_factor_enricher` 的 snapshot 现在会写入 `theme_pool_score / leadership_score / entry_signal_score / timing_penalty / extreme_strength_breakdown / stage_label / timing_reasons / timing_bars_since_event / timing_extended_pct / primary_signal / signal_kind / all_signals` 新字段；没有热点匹配或无信号的场景会填入零值/`none` 默认值，保持 schema 稳定
- `entry_reason` 改由 `stage_label` 中文标签（`仅观察 / 突破当日 / 回踩确认 / 已走远·勿追`）主导，旧描述词（`开盘半小时内涨停 / 刚突破MA100`）降级为可选上下文后缀，例如 `"突破当日 · 开盘半小时内涨停"`；非热点分支仍返回 `None` 以保留旧 schema
- `phase4_entry_readiness` 改为以 `stage_label` 为硬闸：`extended_do_not_chase` 阶段即使 `extreme_strength_score >= 60` 也不会被勾选为"入场准备就绪"，避免扎堆追高
- `risk_params` 按 `primary_signal` 引用真实子信号的止损依据：低位 123 → `pattern_123_stop_loss / pattern_123_pullback_support_price`、底背离双突破 → `bottom_divergence_stop_loss`、缺口突破 MA100 → `breakaway_gap_low`、涨停/跳空涨停 → `limitup_key_level_price`（回退 `breakaway_gap_low`）；均未命中才回退到旧 `MA100 × 0.95` 模板。新增 `stop_loss_basis` 字段透明标注止损依据来源（如 `"123结构低点" / "缺口下沿" / "涨停前trendline" / "MA100×0.95"`）；`stage_label == extended_do_not_chase` 时 `position_size` 强制为 `不建议入场`、`take_profit_ratio=0`
- `ExtremeStrengthScorer.calculate_layered_scores` 新增 `leader_double_count` / `deduplicated_total_score` 两个只读字段，用来显式估算 `leader_score` 与其它桶（`small_circ_mv / turnover / breakout_strength / trend_strength` 的 `above_ma100` 下界）之间可观测的重复加权。`total_score` / `calculate_extreme_strength_score` 数值语义保持不变，所有 `>=50 / >=60 / >=80` 阈值契约均不受影响；snapshot 同步新增 `leader_double_count` 与 `extreme_strength_score_deduplicated` 字段，默认值为 0
- Web 候选详情面板新增分层评分、`stage_label` 徽章、`primary_signal + signal_kind` 徽章及 `timing_penalty` 红色提示区；阶段 3 描述优先取 `primary_signal`，回退到 `core_signal`；阶段 5 止损描述会在价格后追加 `（<basis>）` 提示依据来源；L2 题材地位 + 候选详情抽屉均新增 `· 去重净分` 行，以琥珀色展示 `extreme_strength_score_deduplicated` 及扣除的 `leader_double_count`

### K-line completeness governance

- Added K-line governance truth-source tables, audit service, repair service, and skip-registry workflow so daily data completeness is tracked explicitly instead of inferred from loose sync heuristics
- Added controlled auto-approval for small `skip_eligible` symbol-range K-line gaps between daily audit and re-audit, configurable with `KLINE_AUDIT_AUTO_SKIP_*`, while still failing closed for market-wide gaps and blocking/retryable sync errors
- K-line audit runs now record `completed_at` for degraded/not-passed terminal results so health views no longer look stuck after an audit finishes unsuccessfully
- Screening ingest now consumes audit-derived truth: blocking/retryable sync failures are no longer silently downgraded, and non-`passed` trade dates fail closed before factor building
- Added a fast backfill-to-date path for screening: Web users can run `回填至该日`, the API exposes `POST /api/v1/screening/backfill-to-date`, and `scripts/fast_backfill.py --to-date YYYY-MM-DD` reuses the same service logic before target-date governance audit
- Data-health `repair_gaps` now caps repair attempts at the latest `pass_status=passed` K-line audit date, so intraday/current-date gaps are skipped instead of keeping the repair task stuck in `processing`
- Added scheduled K-line governance wiring with opt-in `17:00` post-close execution, independent deep-audit registration, and explicit failure bubbling through the scheduler
- Added manual governance scripts for auditing/repairing completeness and approving `candidate_skip -> approved_skip` transitions
- Added a Web “数据健康” console and `/api/v1/data-health/*` APIs for local stock database health checks, K-line start/end coverage range, coverage/gap drilldown, and background operations for backfill, retry, repair, and re-audit
- `MarketDataSyncService` now uses the Tushare bulk-daily snapshot as a sentinel: codes outside the bulk universe (delisted/suspended/不在 list 的股票) are immediately recorded with `reason='not_in_bulk_universe'` / `reason_class='skip_eligible'` instead of wasted individual fetcher fallbacks, dramatically shortening end-to-end sync time when `instrument_master` lags upstream listing changes. Toggle via `KLINE_SYNC_BULK_SENTINEL_ENABLED` (default `true`)
- Documented `pass_status` semantics, the daily `sync -> audit -> repair -> re-audit` flow, and the manual recovery/approval commands in `README.md` / `.env.example`

### Backtest experience

- Reworked the five-layer backtest page into four layers: system scorecard, strategy comparison, judgment validation, and per-stock drill-down
- The Web backtest entry now defaults to research mode anchored on the latest screening run, while date-range replay remains available as an explicit fallback mode
- The Web header now renders `运行上下文 + 研究上下文` as separate context cards, exposing raw/evaluated/aggregatable/entry/observation/suppressed sample baselines and suppressed reasons directly on the page
- Run-level `RankingEffectiveness` is now surfaced in the research canvas as a first-class conclusion block, explicitly labeled as whole-run evidence and showing effective vs. inconclusive dimensions, sample counts, and high/low tier return gaps instead of a single lightweight hint
- Run recommendations are now mapped into `优先动作 / 继续观察 / 仅展示` buckets in the research canvas, so setup weighting, execution review, and display-only low-confidence groups can be read directly from the page
- The Web research canvas and sample browser now enter a degraded-research state when a strategy slice becomes observation-only or attribution/timing semantics are incomplete, explicitly warning that the current view is only suitable for observation study
- The Web backtest page now explains low-sample and observation-only runs in Chinese, distinguishes observation risk-avoidance metrics from tradable entry returns, and localizes the main sample-baseline, filtering, and sample-browser labels for easier reading
- Structured `TradePlan` now freezes executable entry/stop-loss/take-profit prices from screening snapshots; the five-layer backtest can replay those plans as real round-trip trades and persist `trade_return_pct`, actual entry/exit dates, exit reason, and replay status for API/Web display
- Added new summary metrics including `profit_factor`, `avg_holding_days`, `max_consecutive_losses`, `plan_execution_rate`, `stage_accuracy_rate`, and `system_grade`
- Run and summary responses now expose structured sample-baseline metadata, including raw samples, aggregatable samples, and suppressed-sample reasons, so clients can explain summary compression explicitly
- Screening-run backtests now recover the minimum analyzable `probe_entry` from five-layer snapshot fields when persisted `trade_stage` is overly conservative, while `ma100_low123` samples with `confirmed_missing_breakout_bar_index` still remain observation-only
- Added `GET /api/v1/five-layer-backtest/runs/{backtest_run_id}/ranking-effectiveness` so Web/Desktop clients can display maturity-ranking effectiveness directly
- Evaluation payloads now expose `factor_snapshot_json`, `trade_plan_json`, `signal_type`, `evaluation_mode`, `snapshot_source`, and `replayed` for expanded stock-level detail views
- `ScreeningCandidate` now keeps a stable `created_at` (decision-time anchor) and a refreshed `updated_at` on every rewrite; `save_screening_candidates` preserves the first-seen `created_at` even when the same `(run_id, code)` is re-saved (e.g. AI re-screen, manual rerun)
- Backtest runs now persist a `candidate_staleness` block under `config_json` listing every candidate whose `updated_at` is later than `started_at`, so analysts can spot snapshot drift without re-running the pipeline (added `scripts/migrate_screening_candidate_updated_at.py` for non-SQLite deployments)
- Recommendation engine now derives "increase weight / decrease weight" suggestions from `family_breakdown[entry|observation]` when available, instead of the legacy mixed `win_rate_pct` that averages entry's `forward_return_5d` with observation's `risk_avoided_pct`; recommendation evidence carries `inference_source.family_scope`, `sample_quality.aggregatable_ratio`, and `suppressed_reasons` so reviewers can audit the basis without joining back to evaluations
- `SystemGrader` returns a structured `GradeResult` (grade + raw_grade + reasons + downgrade flag) and forces a one-step grade downgrade when the aggregatable-sample ratio falls below 60 %; overall summary's `metrics_json.system_grade_breakdown` now exposes the per-axis reasons so the headline grade is explainable
- `compute_summaries` now feeds `SystemGrader` from `family_breakdown.entry` (falling back to `family_breakdown.observation`, then mixed) so the headline grade is no longer inflated by observation's always-non-negative `risk_avoided_pct`; `system_grade_breakdown.metric_source` and `metric_inputs` record exactly which sub-population drove the grade for audit
- Recommendation stability gate (`_check_stability`) now reads `time_bucket_stability` / `extreme_sample_ratio` from the same family-correct metrics dict that drives recommendation inference (entry's family_breakdown when available, mixed summary otherwise), closing the asymmetry where inference used family-correct numbers but stability validation used the mixed-pollued ones — `setup_type` rows whose mixed stability fails but whose entry-only stability passes are no longer silently demoted to `observation`
- New `src/backtest/execution/limit_pct_resolver.py` resolves per-board daily price-limit thresholds (主板 ±10 %, 创业板/科创板 ±20 %, 北交所 ±30 %, ST/*ST ±5 %); `ExecutionModelResolver.resolve` now accepts `code` / `is_st` / `market` / `exchange` and applies the correct threshold for each candidate, so 创业板/科创板 high-flyers are no longer silently flagged as `limit_blocked` at +9.9 %, and ST stocks are correctly blocked at +5 %. `FiveLayerBacktestService._evaluate_candidates` prefetches `InstrumentMaster` once per run (single SELECT) and injects `is_st` / `market` / `exchange` into each candidate dict; legacy DBs without `InstrumentMaster` rows fall back to ±10 % so existing behaviour is preserved. `ExecutionResult` now carries `limit_up_pct` / `limit_down_pct` for audit.
- `StockRepository` now exposes `get_forward_bars_with_meta` returning `(bars, ForwardBarsMeta)` with `gap_too_long` / `insufficient_bars` flags so `_process_candidate` can suppress suspended-trading samples (5d window actually spanning >10 calendar days, typical of mid-window halts) and under-resourced samples (data source returned <ceil(window*0.6) bars) before they pollute `forward_return_5d` / `risk_avoided_pct` aggregates. New `suppression_reason`s `gap_too_long` and `insufficient_forward_bars` flow through `sample_baseline` so the front-end histogram can show halt-driven distortions distinctly. `metrics_json.forward_window` records the per-sample window-quality block (`actual_bar_count` / `actual_span_days` / `gap_threshold_days` / `tolerance_factor`) for audit. New env knobs: `BACKTEST_FORWARD_GAP_CHECK_ENABLED` (default `true`, escape hatch) and `BACKTEST_FORWARD_GAP_TOLERANCE_FACTOR` (default `2.0`). The legacy `get_forward_bars` API is preserved as a facade returning only the bar list.
- `StockDaily` gained `adj_factor` / `adj_anchor_date` / `adj_factor_source` columns so each row records the forward-adjustment factor that was applied at fetch time; `save_daily_data` reads these from the DataFrame (defaulting to `adj_factor=1.0` / `adj_factor_source='fetcher_unset'` when the fetcher hasn't supplied them). Inline SQLite migration (`_migrate_sqlite_stock_daily_adj_factor_fields`) auto-adds the columns and back-fills legacy rows as `adj_factor=1.0` / `'legacy_assume_one'`; the same change is also available as the deterministic offline script `scripts/migrate_stock_daily_adj_factor.py`. `FiveLayerBacktestRun.data_version` is now populated at run-completion as `adj1|<source>:<count>,...|total:N` so reruns against drifted qfq prices produce a different fingerprint and analysts can spot reproducibility risk; per-evaluation `metrics_json.forward_window.adj_factor_sources` records the same distribution at sample granularity. `AkshareFetcher._enrich_with_adj_factor` performs an opt-in dual fetch (qfq + raw) and stamps `adj_factor = qfq_close / raw_close` per row; gated by the new env knob `AKSHARE_FETCH_ADJ_FACTOR=true` (default `false` to avoid doubling the API call rate). The backtest layer does not yet *consume* `adj_factor` for price normalisation — that is the planned 3b follow-up; this change only persists the data and the version fingerprint.
- `RankingEffectivenessReport.top_k_hit_rate` was renamed to `leader_pool_win_share` because the legacy name implied a precision@k semantic, while the actual metric is `leader_pool_wins / total_pool_wins` — a *share* of pool-level wins, not a top-k hit rate. `top_k_hit_rate` is preserved as a deprecated alias on the dataclass, the `FiveLayerBacktestGroupSummary` ORM (new `leader_pool_win_share` column, inline SQLite migration backfills from `top_k_hit_rate`), the API responses (`FiveLayerGroupSummaryItem` / `RankingEffectivenessResponse` expose both names with identical values), and the agent tools (`get_five_layer_backtest_run_summary` / `get_five_layer_group_summary` payloads) so legacy dashboards and clients keep working through the deprecation window. `BacktestService.compute_summaries` now writes both columns, and the helper has a docstring warning that the metric is contaminated by family mix when entry vs observation samples are unevenly distributed across pool tiers (planned follow-up: D1 family-correct ranking).
- `RankingEffectivenessCalculator.compute` now anchors all tier comparisons (`candidate_pool_level`, `theme_position`, `entry_maturity`) on the *entry* family by default, reading `avg_return_pct` / `win_rate_pct` / `aggregatable_sample_count` from `metrics_json.family_breakdown[entry]` instead of the mixed-family summary columns. This closes the failure mode where a pool tier dominated by `signal_family='observation'` samples (whose `risk_avoided_pct` is non-negative by construction) would silently inflate its mixed `win_rate_pct` to ~75 % and produce a fake `excess_return_pct` over watchlist; the family-correct view now reports the underlying entry-only win rate (e.g. 40 % vs the inflated 75 %), and `RankingEffectivenessReport` exposes both views via `leader_pool_win_share` / `excess_return_pct` (family-correct) and `leader_pool_win_share_mixed` / `excess_return_pct_mixed` (legacy alias) so analysts can quantify the family-mix bias on a per-run basis. Each `RankingComparisonResult` now carries `metric_source` ∈ `{family_entry, family_observation, mixed_legacy, mixed_fallback}` so reviewers can spot tiers that lacked a `family_breakdown` payload and silently fell back to the mixed columns. `RankingEffectivenessCalculator.compute(summaries, family_scope="observation")` is also accepted for analysts who want to inspect wait-correctness rankings independently. `BacktestService.compute_summaries` persists the family-correct view as the canonical `leader_pool_win_share` / `top_k_hit_rate` columns and stashes the full ranking_effectiveness block (family_scope, mixed-alias values, observed metric_sources) under `overall.metrics_json.ranking_effectiveness` for audit. The `/five-layer-backtest/runs/{id}/ranking-effectiveness` endpoint now exposes `family_scope` plus both views in parallel.
- `RecommendationEngine` now factors a *family-share* signal into every recommendation's confidence: the new `compute_family_share(summary)` helper extracts the (entry / observation) sample mix from `metrics_json.family_breakdown` and the engine compares the dominant family against `family_scope` of the inference. Recommendations whose inferring family dominates the group (≥50 % share) gain a small confidence bonus (+0.05); recommendations anchored on a minority family (<50 %) lose a strict penalty (-0.10), and below the hard threshold (<20 %) lose a heavy penalty (-0.20) so an entry-family inference built on a 90 % observation slice can no longer carry the same confidence as one built on a 90 % entry slice. `mixed`-scope inferences receive the strict penalty regardless of share because the underlying numbers are contaminated. `signal_family` rows are exempt because they are single-family by construction, and groups without `family_breakdown` keep the legacy formula. The full family_share snapshot is persisted both inside `recommendations.evidence_json.sample_quality.family_share` (so the reviewer audit chain shows the discount path) and inside `recommendations.metrics_before_json.family_share` (so dashboards can query the snapshot without re-parsing evidence); `inference_source.dominant_family_match` flags whether the inference was anchored on the dominant population for fast triage. `_family_share_confidence_adjustment` is exposed as a pure function so tests can validate the discount table independently of the engine.

### Screening behavior

- Added `scripts/run_historical_random_screening.py` for historical random screening samples backed by local `stock_daily`: by default it samples 100 trade dates from `2024-04-19` to `2026-05-11`, writes 10 candidates per day through the normal screening pipeline, and fixes `ai_top_k=0` so no AI second-pass analysis is triggered. The script supports `--dry-run`, `--seed`, `--fail-fast`, and a guarded `--force` that only deletes matching `historical_random_sample` runs; matching manual/scheduled runs are skipped to avoid altering existing history.
- 首筛公共过滤已移除 `avg_amount < min_avg_amount` 的硬拒绝逻辑，当前会先放行可用股票进入策略匹配，再交给后续五层链路继续收敛
- L2 本地热点板块已改为排名驱动识别：`hot/warm` 不再由固定阈值切分，而是基于 `board_strength_score / board_strength_rank / percentile` 进行分桶；`stage` 与 `quality_flags` 改为解释性字段，并同步落库到 `daily_sector_heat`
- `ma100_60min_combined` 策略的 MA100 突破语义修正：门控从“最近连续站上 MA100 的天数 ≤ 5”改为“最近一次真实上穿（前一日收盘 ≤ MA100、当日收盘 > MA100）发生在近 5 根 K 线内”，并新增 (1) 突破前背景过滤 `pre_breakout_below_ratio ≥ 0.6` 或 `pre_breakout_consecutive_below_bars ≥ 3`，(2) `|ma100_distance_pct| ≤ 6.0%` 硬距离门控。原 `ma100_breakout_days` 字段以“连续站上”语义保留在 factor 快照中供 `leader_score`、`stock_analyzer` 报告等按既有语义继续复用；新增 `ma100_bars_since_breakout` / `ma100_breakout_bar_index` / `ma100_pre_breakout_below_ratio` / `ma100_pre_breakout_consecutive_below_bars` 精准字段用于新门控
- `ma100_low123_combined` 的低位 123 买点门控改为只接受两类最佳入场区：最新 K 线介于 P3-P2 之间，或最新 K 线刚突破 P2；突破后第 2 根及以后即使仍在旧的 3 根 K 线窗口内也会被标记为 `not_best_entry_zone` 并剔除。新增 `ma100_low123_entry_timing_score` / `ma100_low123_entry_zone` 字段，刚突破 P2 会获得额外入场时机加分，避免已远离 P2 的候选继续作为初始买点推荐
- `bottom_divergence_double_breakout` 保留“双突破已确认”的原语义，新增 `bottom_divergence_actionable_entry` 作为主筛选买点门控：主策略仅接受 `price_down_macd_up` / `price_down_macd_flat` / `price_flat_macd_up` 三类底部反转形态，A/B 低点跨度限制为 10~60 根 K，并输出 `bottom_divergence_entry_zone` / `bottom_divergence_entry_timing_score` / `bottom_divergence_extended_pct` / `bottom_divergence_validation_status`。确认后已远离突破确认价的样本会标记为 `extended_not_entry`，不再作为底背离初始买点推荐

### Screening architecture consolidation

- Screening runtime/storage/API output now converges on a single `CandidateDecision` object instead of mixing flat candidate rows with scattered `trade_plan_json` / AI fields
- The screening run no longer depends on the optional `screening_use_five_layer_pipeline` split; the five-layer pipeline is now the only main path
- Added missing five-layer semantics including `setup_freshness`, `theme_duration`, `trade_theme_stage`, and `trade_plan.execution_note`
- AI secondary review now follows a fixed schema and reads/writes the unified candidate decision object end-to-end
- Screening AI second-pass now uses a dedicated review service with guard enforcement, explicit `rules_only / rules_fallback / rules_plus_ai` source states, and fail-closed fallback on invalid JSON / timeout / normalize failure
- Strategy YAML metadata is now fully populated with `system_role / strategy_family / applicable_market / applicable_theme / setup_type`, and `/api/v1/screening/strategies` exposes those fields
- Notification copy now prioritizes `trade_stage + setup + trade_plan` and moves matched strategies to an audit-evidence role
- Screening run responses now expose `local_theme_pipeline / external_theme_pipeline / fused_theme_pipeline` as first-class fields, so clients no longer need to inspect `config_snapshot` for hot-theme pipeline details
- Local/OpenClaw theme summaries now reuse `ThemeNormalizationService`, emit `normalized_name/raw_name`, and fuse by normalized topic with `raw_names / matched_sources / priority_source`
- Screening run persistence now writes `decision_context` independently from theme-pipeline snapshots, so theme-fusion failures no longer block the original five-layer context snapshot

### Screening architecture wording

- Clarified that the five-layer trading system starts at `L1-L5`; the older screening entry is documented as engineering preflight / strategy matching instead of a formal `L0`
- Removed the obsolete five-layer pipeline env toggle wording; screening now always runs through the unified five-layer main path

### Screening detail view

- OpenClaw `extreme_strength_combo` detail view now translates raw rule expressions into readable Chinese labels
- Added a dedicated technical-hit section for key signals such as `bottomDivergenceHitReasons`
- Factor snapshot object and array fields are now expanded into readable text instead of rendering as `[object Object]`

### Board membership persistence

- Added normalized `board_master` and `instrument_board_membership` tables to persist stock-to-board relationships locally
- Added `BoardRepository` and `BoardSyncService` so board memberships can be bulk-written, bulk-read, and reused by screening
- `FactorService` now resolves board memberships from the local database first and only falls back to remote lookups for missing symbols; successful fallbacks are written back to the database
- Added `python scripts/backfill_instrument_boards.py` for offline board-cache prewarm with `--codes`, `--limit`, and `--dry-run` support
- `python scripts/backfill_instrument_boards.py --stale-only` now performs a real gap-only backfill, so interrupted runs can resume by syncing only symbols that still lack local board memberships
- Added opt-in schedule-mode board-cache prewarm with `BOARD_SYNC_SCHEDULE_ENABLED`, `BOARD_SYNC_SCHEDULE_TIME`, and `BOARD_SYNC_RUN_IMMEDIATELY`; the board sync job can now run after the A-share close alongside the existing scheduled tasks
- `UniverseService.sync_universe()` now treats board sync as an incremental hook: only newly discovered instruments trigger board membership sync, avoiding wasteful full-pool refreshes

### OpenClaw 热点题材选股

- 新增热点主题归一化层 (`ThemeNormalizationService`)，将 OpenClaw 语义化主题（如 `锂电池/锂矿`、`AI Agent`）自动映射为本地标准板块名
- 归一化流水线：复合主题拆分 → 别名字典匹配 → 候选板块召回 → 置信度分类（`high_confidence` / `weak_match` / `unresolved`）
- 新增 `data/theme_aliases.json` 别名词汇资产，支持确定性主题到板块映射
- 新增 `BoardCandidateRecallService`，当别名未命中时从 `board_master` 词汇中通过精确命中、子串匹配、关键词重叠召回候选板块
- `FactorService` 在热点因子富化前自动执行主题归一化，将归一化板块传入 `HotThemeFactorEnricher`
- `HotThemeFactorEnricher` 支持 `normalized_themes` 参数，用归一化板块替代原始主题名做匹配，解决语义正确但措辞不同导致零匹配的问题
- `_build_run_config_snapshot` 现在生成 `normalized_themes` 字段，与原始 `theme_context` 共存于 run snapshot 中用于审计
- `POST /api/v1/screening/openclaw-theme-run` 现在会真正注入 `SkillManager` 并固定走 `extreme_strength_combo` 策略引擎，不再回落到 legacy 选股逻辑
- OpenClaw 请求中的 `trade_date` 现在会按请求日期传入筛选任务，而不是被忽略后回退到默认日期
- `theme_context` 会写入 screening run 的 `config_snapshot`，便于后续结果解释、审计和回放
- 热点题材匹配会结合个股所属板块信息，不再把 `boards=[]` 传入题材富化流程
- 策略引擎新增对嵌套 `any` 过滤组的支持，`extreme_strength_combo.yaml` 中“至少命中一个强势信号”的门槛开始生效
- 热点题材候选的 `phase_results` 已统一为正式五阶段键，并新增 `phase_explanations` 作为可直接消费的阶段解释结构
- `POST /api/v1/screening/openclaw-theme-run` 对 `options.candidate_limit` / `options.ai_top_k` 补齐了准确参数校验，`ai_top_k > candidate_limit` 现在会返回明确的 `422 validation_error`

### 智能选股策略引擎

- 📈 **底背离双突破策略 (Strategy E)** — 基于 DIF/DEA 六形态底背离检测 + 下降趋势线/水平阻力线双突破联合确认
  - 新增 `BottomDivergenceBreakoutDetector` 检测器，支持六种底背离形态（price_down_macd_up、price_down_macd_flat、price_flat_macd_up、price_flat_macd_down、price_up_macd_down、price_up_macd_flat）
  - 新增上下文门控：底部反转型需前置下跌，强势回撤型需前置上涨
  - 新增双突破同步窗口（3 bars）确认机制，区分 confirmed / late_or_weak
  - FactorService 新增 10 个底背离因子（bottom_divergence_*）
  - 新增 `EntryStrategyE` 封装，仅 confirmed 状态触发
  - 新增 `bottom_divergence_double_breakout.yaml` YAML 策略文件
  - TDD 覆盖：detector 15 测试 + FactorService 5 测试 + EntryStrategyE 5 测试

### Web 智能选股页优化

- 选股策略改为默认不选中，候选上限默认 5，AI 分析默认前 2 个
- 运行状态面板会在进入页面后自动回填最近一次任务，并在任务未结束时继续轮询
- 候选结果新增所用策略展示，前端显示为策略中文名
- 运行状态面板新增融合题材摘要展示，直接显示当前任务的 `fused_theme_pipeline` 结果
- 运行详情区域新增完整题材管道面板，支持查看融合后的最终题材集合，并折叠展开本地/外部题材链路的逐项明细
- 选择非交易日时会自动回退到最近一个交易日继续选股，并在任务告警中保留提示
- 选择当日交易日时，前后端都会在北京时间 15:00 前阻止启动选股，避免同步和筛选使用未收盘的日线数据
- 因进程中断遗留的选股幽灵任务会在列表查询、详情轮询和后续重试时自动回收，避免页面长期卡在旧任务上
- 历史任务列表新增单条删除入口，可直接移除卡住任务及其关联候选结果

### 说明

- 暂无。

## [3.8.0] - 2026-03-17

### 发布亮点

- 🎨 **Web 界面完成一轮骨架升级** — 新的 App Shell、侧边导航、主题能力、登录与系统设置流程已经串成统一体验，桌面端加载背景也完成对齐。
- 📈 **分析上下文继续补强** — 美股新增社交舆情情报，A 股补齐财报与分红结构化上下文，Tushare 新接入筹码分布和行业板块涨跌数据。
- 🔒 **运行稳定性与配置兼容性提升** — 退出登录会立即让旧会话失效，定时启动兼容旧配置，运行中的 `MAX_WORKERS` 调整和新闻时效窗口反馈更清晰。
- 💼 **持仓纠错链路更完整** — 超售会被前置拦截，错误交易/资金流水/公司行为可以直接删除回滚，便于修复脏数据。

### 新功能

- 📱 **美股社交舆情情报** — 新增 Reddit / X / Polymarket 社交媒体情绪数据源，为美股分析提供实时社交热度、情绪评分和提及量等补充指标；完全可选，仅在配置 `SOCIAL_SENTIMENT_API_KEY` 后对美股生效。
- 📊 **A 股财报与分红结构化增强**（Issue #710）— `fundamental_context.earnings.data` 新增 `financial_report` 与 `dividend` 字段；分红统一按“仅现金分红、税前口径”计算，并补充 `ttm_cash_dividend_per_share` 与 `ttm_dividend_yield_pct`；分析/历史 API 的 `details` 追加 `financial_report`、`dividend_metrics` 可选字段，保持 fail-open 与向后兼容。
- 🔍 **接入 Tushare 筹码与行业板块接口** — 新增筹码分布、行业板块涨跌数据获取能力，并统一纳入配置化数据源优先级；默认按上海时间区分盘中/盘后交易日取数，优先使用 Tushare 同花顺接口，必要时降级到东财。
- 🧱 **Web UI 基础骨架升级** — 重建共享设计令牌与通用组件，新增 App Shell、Theme Provider、侧边导航，并同步调整 Electron 加载背景，为 Web / Desktop 的统一体验打底。
- 🔐 **登录与系统设置流程重做** — 重构 Login、Settings 与 Auth 管理流程，补上显式的认证 setup-state 处理，并让 Web 端与运行时认证配置 API 行为对齐。
- 🧪 **前端回归与冒烟覆盖补强** — 新增并扩展登录、首页、聊天、移动端 Shell、设置页、回测入口等关键路径的组件测试与 Playwright smoke coverage。

### 变更

- 🧭 **页面接入新 Shell 布局契约** — Home、Chat、Settings、Backtest 已统一接入新的页面容器、抽屉和滚动约定，降低 UI 迁移期间的页面行为不一致。
- 💾 **设置页状态同步更稳** — 优化草稿保留、直接保存同步与冲突处理，减少模块级保存后前后端配置状态不一致的问题。
- 🎭 **登录页视觉基线回归** — 登录页恢复到既有 `006` 分支的视觉基线，同时保留新的认证状态逻辑和统一表单交互模型。
- 🏛️ **AI 协作治理资产加固** — 收敛并加强 `AGENTS.md`、`CLAUDE.md`、Copilot 指令和校验脚本的一致性约束，降低治理资产长期漂移风险。

### 修复

- ⏰ **定时启动立即执行兼容旧配置**（Issue #726）— `SCHEDULE_RUN_IMMEDIATELY` 未设置时会回退读取 `RUN_IMMEDIATELY`，修复升级后旧 `.env` 在定时模式下的兼容性问题；同时澄清 `.env.example` / README 中两个配置项的适用范围，并注明 Outlook / Exchange 强制 OAuth2 暂不支持。
- 🧵 **运行期 `MAX_WORKERS` 配置生效与可解释性增强**（#633）— 修复异步分析队列未按 `MAX_WORKERS` 同步的问题；新增任务队列并发 in-place 同步机制（空闲即时生效、繁忙延后），并在设置保存反馈与运行日志中明确输出 `profile/max/effective`，减少“参数未生效”误解。
- 🔐 **退出登录立即失效现有会话** — `POST /api/v1/auth/logout` 现在会轮换 session secret，避免旧 cookie 在退出后仍可继续访问受保护接口；同浏览器标签页和并发页面会被同步登出。认证开启时，该接口也不再属于匿名白名单，未登录请求会返回 `401`，避免匿名请求触发全局 session 失效。
- 🧮 **Tushare 板块/筹码调用限流与跨日缓存修复** — 新增的 `trade_cal`、行业板块排行、筹码分布链路统一接入 `_check_rate_limit()`；交易日历缓存改为按自然日刷新，避免服务跨天运行后继续沿用旧交易日判断取数日期。
- 💼 **持仓超售拦截与错误流水恢复**（#718）— `POST /api/v1/portfolio/trades` 现在会在写入前校验可卖数量，超售返回 `409 portfolio_oversell`；持仓页新增交易 / 资金流水 / 公司行为删除能力，删除后会同步失效仓位缓存与未来快照，便于从错误流水中直接恢复。
- 📧 **邮件中文发件人名编码**（#708）— 邮件通知现在会对包含中文的 `EMAIL_SENDER_NAME` 自动做 RFC 2047 编码，并在异常路径补充 SMTP 连接清理，修复 GitHub Actions / QQ SMTP 下 `'ascii' codec can't encode characters` 导致的发送失败。
- 🐛 **港股 Agent 实时行情去重与快速路由** — 统一 `HK01810` / `1810.HK` / `01810` 等港股代码归一规则；港股实时行情改为直接走单次 `akshare_hk` 路径，避免按 A 股 source priority 重复触发同一失败接口；Agent 运行期对显式 `retriable=false` 的工具失败增加短路缓存，减少同轮分析中的重复失败调用。
- 📰 **新闻时效硬过滤与策略分窗**（#697）— 新增 `NEWS_STRATEGY_PROFILE`（`ultra_short/short/medium/long`）并与 `NEWS_MAX_AGE_DAYS` 统一计算有效窗口；搜索结果在返回后执行发布时间硬过滤（时间未知剔除、超窗剔除、未来仅容忍 1 天），并在历史 fallback 链路追加相同约束，避免旧闻再次进入“最新动态/风险警报”。

### 文档

- ☁️ **新增云服务器 Web 界面部署与访问教程**（Fixes #686）— 补充从云端部署到外部访问的落地说明，降低远程自托管门槛。
- 🌍 **补齐英文文档索引与协作文档** — 新增英文文档索引、贡献指南、Bot 命令文档，并补充中英双语 issue / PR 模板，方便中英文协作与外部贡献者理解项目入口。
- 🏷️ **本地化 README 补充 Trendshift badge** — 在多语言 README 中同步补上新版能力入口标识，减少中英文说明面不一致。

## [3.7.0] - 2026-03-15

### 新功能

- 💼 **持仓管理 P0 全功能上线**（#677，对应 Issue #627）
  - **核心账本与快照闭环**：新增账户、交易、现金流水、企业行为、持仓缓存、每日快照等核心数据模型与 API 端点；支持 FIFO / AVG 双成本法回放；同日事件顺序固定为 `现金 → 企业行为 → 交易`；持仓快照写入采用原子事务。
  - **券商 CSV 导入**：支持华泰 / 中信 / 招商首批适配，含列名别名兼容；两阶段接口（解析预览 + 确认提交）；`trade_uid` 优先、key-field hash 兜底的幂等去重；前导零股票代码完整保留。
  - **组合风险报告**：集中度风险（Top Positions + A 股板块口径）、历史回撤监控（支持回填缺失快照）、止损接近预警；多币种统一换算 CNY 口径；汲取失败时回退最近成功汇率并标记 stale。
  - **Web 持仓页**（`/portfolio`）：组合总览、持仓明细、集中度饼图、风险摘要、全组合 / 单账户切换；手工录入交易 / 资金流水 / 企业行为；内嵌账户创建入口；CSV 解析 + 提交闭环与券商选择器。
  - **Agent 持仓工具**：新增 `get_portfolio_snapshot` 数据工具，默认紧凑摘要，可选持仓明细与风险数据。
  - **事件查询 API**：新增 `GET /portfolio/trades`、`GET /portfolio/cash-ledger`、`GET /portfolio/corporate-actions`，支持日期过滤与分页。
  - **可扩展 Parser Registry**：应用级共享注册，支持运行时注册新券商；新增 `GET /portfolio/imports/csv/brokers` 发现接口。

- 🎨 **前端设计系统与原子组件库**（#662）
  - 引入渐进式双主题架构（HSL 变量化设计令牌），清理历史 Legacy CSS；重构 Button / Card / Badge / Collapsible / Input / Select 等 20+ 核心组件；新增 `clsx` + `tailwind-merge` 类名合并工具；提升历史记录、LLM 配置等页面可读性。

- ⚡ **分析 API 异步契约与启动优化**（#656）
  - 规范 `POST /api/v1/analysis/analyze` 异步请求的返回契约；优化服务启动辅助逻辑；修复前端报告类型联合定义与后端响应对齐问题。

### 修复

- 🔔 **Discord 环境变量向后兼容**（#659）：运行时新增 `DISCORD_CHANNEL_ID` → `DISCORD_MAIN_CHANNEL_ID` 的 fallback 读取；历史配置用户无需修改即可恢复 Discord Bot 通知；全部相关文档与 `.env.example` 对齐。
- 🔧 **GitHub Actions Node 24 升级**（#665）：将所有 GitHub 官方 actions 升级至 Node 24 兼容版本，消除 CI 日志中的 Node.js 20 deprecation warning（影响 2026-06-02 强制升级窗口）。
- 📅 **持仓页默认日期本地化**：手工录入表单默认日期改用本地时间（`getFullYear/Month/Date`），修复 UTC-N 时区用户在当天晚间出现日期偏移的问题。
- 🔁 **CSV 导入去重逻辑加固**：dedup hash 纳入行序号作为区分因子，确保同字段合法分笔成交不被误折叠；同时在 `trade_uid` 存在时也持久化 hash，防止混合来源重复写入。

### 变更

- `POST /api/v1/portfolio/trades` 在同账户内 `trade_uid` 冲突时返回 `409`。
- 持仓风险响应新增 `sector_concentration` 字段（增量扩展），原有 `concentration` 字段保持不变。
- 分析 API `analyze` 接口异步行为契约文档化；前端报告类型联合更新。

### 测试

- 新增持仓核心服务测试（FIFO / AVG 部分卖出、同日事件顺序、重复 `trade_uid` 返回 409、快照 API 契约）。
- 新增 CSV 导入幂等性、合法分笔成交不误去重、去重边界、风险阈值边界、汇率降级行为测试。
- 新增 Agent `get_portfolio_snapshot` 工具调用测试。
- 新增分析 API 异步契约回归测试。

## [3.6.0] - 2026-03-14

### Added
- 📊 **Web UI Design System** — implemented dual-theme architecture and terminal-inspired atomic UI components
- 📊 **UI Components Refactoring** — integrated `clsx` and `tailwind-merge` for robust class composition across Web UI

- 🗑️ **History batch deletion** — Web UI now supports multi-selection and batch deletion of analysis history; added `POST /api/v1/history/batch-delete` endpoint and `ConfirmDialog` component.
- 🔐 **Auth settings API** — new `POST /api/v1/auth/settings` endpoint to enable or disable Web authentication at runtime and set the initial admin password when needed
- openclaw Skill 集成指南 — 新增 [docs/openclaw-skill-integration.md](openclaw-skill-integration.md)，说明如何通过 openclaw Skill 调用 DSA API
- ⚙️ **LLM channel protocol/test UX** — `.env` and Web settings now share the same channel shape (`LLM_CHANNELS` + `LLM_<NAME>_PROTOCOL/BASE_URL/API_KEY/MODELS/ENABLED`); settings page adds per-channel connection testing, primary/fallback/vision model selection, and protocol-aware model prefixing
- 🤖 **Agent architecture Phase 0+1** — shared protocols (`AgentContext`, `AgentOpinion`, `StageResult`), extracted `run_agent_loop()` runner, `AGENT_ARCH` switch (`single`/`multi`), config registry entries
- 🔍 **Bot NL routing** — two-layer natural-language routing: cheap regex pre-filter (stock codes + finance keywords) → lightweight LLM intent parsing; controlled by `AGENT_NL_ROUTING=true`; supports multi-stock and strategy extraction
- 💬 **`/ask` multi-stock analysis** — comma or `vs` separated codes (max 5), parallel thread execution with 150s timeout (preserves partial results), Markdown comparison summary table at top
- 📋 **`/history` command** — per-user session isolation via `{platform}_{user_id}:{scope}` format (colon delimiter prevents prefix collision); lists both `/chat` and `/ask` sessions; view detail or clear
- 📊 **`/strategies` command** — lists available strategy YAML files grouped by category (趋势/形态/反转/框架) with ✅/⬜ activation status
- 🔧 **Backtest summary tools** — `get_strategy_backtest_summary` and `get_stock_backtest_summary` registered as read-only Agent tools
- ⚙️ **Agent auto-detection** — `is_agent_available()` auto-detects from `LITELLM_MODEL`; explicit `AGENT_MODE=true/false` takes full precedence
- 🏗️ **Multi-Agent orchestrator (Phase 2)** — `AgentOrchestrator` with 4 modes (`quick`/`standard`/`full`/`strategy`); drop-in replacement for `AgentExecutor` via `AGENT_ARCH=multi`; `BaseAgent` ABC with tool subset filtering, cached data injection, and structured `AgentOpinion` output
- 🧩 **Specialised agents (Phase 2-4)** — `TechnicalAgent` (8 tools, trend/MA/MACD/volume/pattern analysis), `IntelAgent` (news & sentiment, risk flag propagation), `DecisionAgent` (synthesis into Decision Dashboard JSON), `RiskAgent` (7 risk categories, two-level severity with soft/hard override)
- 📈 **Strategy system (Phase 3)** — `StrategyAgent` (per-strategy evaluation from YAML skills), `StrategyRouter` (rule-based regime detection → strategy selection), `StrategyAggregator` (weighted consensus with backtest performance factor)
- 🔬 **Deep Research agent (Phase 5)** — `ResearchAgent` with 3-phase approach (decompose → research sub-questions → synthesise report); token budget tracking; new `/research` bot command with aliases (`/深研`, `/deepsearch`)
- 🧠 **Memory & calibration (Phase 6)** — `AgentMemory` with prediction accuracy tracking, confidence calibration (activates after minimum sample threshold), strategy auto-weighting based on historical win rate
- 📊 **Portfolio Agent (Phase 7)** — `PortfolioAgent` for multi-stock portfolio analysis (position sizing, sector concentration, correlation risk, cross-market linkage, rebalance suggestions)
- 🔔 **Event-driven alerts (Phase 7)** — `EventMonitor` with `PriceAlert`, `VolumeAlert`, `SentimentAlert` rules; async checking, callback notifications, serializable persistence
- ⚙️ **New config entries** — `AGENT_ORCHESTRATOR_MODE`, `AGENT_RISK_OVERRIDE`, `AGENT_DEEP_RESEARCH_BUDGET`, `AGENT_MEMORY_ENABLED`, `AGENT_STRATEGY_AUTOWEIGHT`, `AGENT_STRATEGY_ROUTING` — all registered in `config.py` + `config_registry.py` (WebUI-configurable)

### Changed
- 🔐 **Auth password state semantics** — stored password existence is now tracked independently from auth enablement; when auth is disabled, `/api/v1/auth/status` returns `passwordSet=false` while preserving the saved password for future re-enable
- 🔐 **Auth settings re-enable hardening** — re-enabling auth with a stored password now requires `currentPassword`, and failed session creation rolls back the auth toggle to avoid lockout
- ♻️ **AgentExecutor refactored** — `_run_loop` delegates to shared `runner.run_agent_loop()`; removed duplicated serialization/parsing/thinking-label code
- ♻️ **Unified agent switch** — Bot, API, and Pipeline all use `config.is_agent_available()` instead of divergent `config.agent_mode` checks
- 📖 **README.md** — expanded Bot commands section (ask/chat/strategies/history), added NL routing note, updated agent mode description
- 📖 **.env.example** — added `AGENT_ARCH` and `AGENT_NL_ROUTING` configuration documentation
- 🔌 **Analysis API async contract** — `POST /api/v1/analysis/analyze` now documents distinct async `202` payloads for single-stock vs batch requests, and `report_type=full` is treated consistently with the existing full-report behavior

### Fixed
- 🐛 **Analysis API blank-code guardrails** — `POST /api/v1/analysis/analyze` now drops whitespace-only entries before batch enqueue and returns `400` when no valid stock code remains
- 🐛 **Bare `/api` SPA fallback** — unknown API paths now return JSON `404` consistently for both `/api/...` and the exact `/api` path
- 🎮 **Discord channel env compatibility** — runtime now accepts legacy `DISCORD_CHANNEL_ID` as a fallback for `DISCORD_MAIN_CHANNEL_ID`, and the docs/examples now use the same variable name as the actual workflow/config implementation
- 🐛 **Session secret rotation on Windows** — use atomic replace so auth toggles invalidate existing sessions even when `.session_secret` already exists
- 🐛 **Auth toggle atomicity** — persist `ADMIN_AUTH_ENABLED` before rotating session secret; on rotation failure, roll back to the previous auth state
- 🔧 **LLM runtime selection guardrails** — YAML 模式下渠道编辑器不再覆盖 `LITELLM_MODEL` / fallback / Vision；系统配置校验补上全部渠道禁用后的运行时来源检查，并修复 `vertexai/...` 这类协议别名模型被重复加前缀的问题
- 🐛 **Multi-stock `/ask` follow-up regressions** — portfolio overlay now shares the same timeout budget as the per-stock phase and is skipped on timeout instead of blocking the bot reply; `/history` now stores the readable per-stock summary instead of raw dashboard JSON; condensed multi-stock output now renders numeric `sniper_points` values
- 🐛 **Decision dashboard enum compatibility** — multi-agent `DecisionAgent` now keeps `decision_type` within the legacy `buy|hold|sell` contract and normalizes stray `strong_*` outputs before risk override, pipeline conversion, and downstream统计/通知汇总
- 🛟 **Multi-Agent partial-result fallback** — `IntelAgent` now caches parsed intel for downstream reuse, shared JSON parsing tolerates lightly malformed model output, and the orchestrator preserves/synthesizes a minimal dashboard on timeout or mid-pipeline parse failure instead of always collapsing to `50/观望/未知`
- 🐛 **Shared LiteLLM routing restored** — bot NL intent parsing and `ResearchAgent` planning/synthesis now reuse the same LiteLLM adapter / Router / fallback / `api_base` injection path as the main Agent flow, so `LLM_CHANNELS` / `LITELLM_CONFIG` / OpenAI-compatible deployments behave consistently
- 🐛 **Bot chat session backward compatibility** — `/chat` now keeps using the legacy `{platform}_{user_id}` session id when old history already exists, and `/history` can still list / view / clear those pre-migration sessions alongside the new `{platform}_{user_id}:chat` format
- 🐛 **EventMonitor unsupported rule rejection** — config validation/runtime loading now reject or skip alert types the monitor cannot actually evaluate yet, so schedule mode no longer silently accepts permanent no-op rules
- 🐛 **P0 基本面聚合稳定性修复** (#614) — 修复 `get_stock_info` 板块语义回归（新增 `belong_boards` 并保留 `boards` 兼容别名）、引入基本面上下文精简返回以控制 token、为基本面缓存增加最大条目淘汰，并补齐 ETF 总体状态聚合与 NaN 板块字段过滤，保证 fail-open 与最小入侵。
- 🔧 **GitHub Actions 搜索引擎环境变量补充** — 工作流新增 `MINIMAX_API_KEYS`、`BRAVE_API_KEYS`、`SEARXNG_BASE_URLS` 环境变量映射，使 GitHub Actions 用户可配置 MiniMax、Brave、SearXNG 搜索服务（此前 v3.5.0 已添加 provider 实现但缺少工作流配置）
- 🤖 **Multi-Agent runtime consistency** — `AGENT_MAX_STEPS` now propagates to each orchestrated sub-agent; added cooperative `AGENT_ORCHESTRATOR_TIMEOUT_S` budget to stop overlong pipelines before they cascade further
- 🔌 **Multi-Agent feature wiring** — `AGENT_RISK_OVERRIDE` now actively downgrades final dashboards on hard risk findings; `AGENT_MEMORY_ENABLED` now injects recent analysis memory + confidence calibration into specialised agents; multi-stock `/ask` now runs `PortfolioAgent` to add portfolio-level allocation and concentration guidance
- 🔔 **EventMonitor runtime wiring** — schedule mode can now load alert rules from `AGENT_EVENT_ALERT_RULES_JSON`, poll them at `AGENT_EVENT_MONITOR_INTERVAL_MINUTES`, and send triggered alerts through the existing notification service
- 🛠️ **Follow-up stability fixes** — multi-stock `/ask` now falls back to usable text output when dashboard JSON parsing fails; EventMonitor skips semantically invalid rules instead of aborting schedule startup; background alert polling now runs independently of the main scheduled analysis loop
- 🧪 **Multi-Agent regression coverage** — added orchestrator execution tests for `run()`, `chat()`, critical-stage failure, graceful degradation, and timeout handling
- 🧹 **PortfolioAgent cleanup** — `post_process()` now reuses shared JSON parsing and removed stale unused imports
- 🚦 **Bot async dispatch** — `CommandDispatcher` now exposes `dispatch_async()`; NL intent parsing and default command execution are offloaded from the event loop, DingTalk stream awaits async handlers directly, and Feishu stream processing is moved off the SDK callback thread
- 🌐 **Async webhook handler** — new `handle_webhook_async()` function in `bot/handler.py` for use from async contexts (e.g. FastAPI); calls `dispatch_async()` directly without thread bridging
- 🧵 **Feishu stream ThreadPoolExecutor** — replaced unbounded per-message `Thread` spawning with a capped `ThreadPoolExecutor(max_workers=8)` to prevent thread explosion under message bursts
- 🔒 **EventMonitor safety** — `_check_volume()` now safely handles `get_daily_data` returning `None` (no tuple-unpacking crash); `on_trigger` callbacks support both sync and async callables via `asyncio.to_thread`/`await`
- 🧹 **ResearchAgent dedup** — `_filtered_registry()` now delegates to `BaseAgent._filtered_registry()` instead of duplicating the filtering logic
- 🧹 **Bot trailing whitespace cleanup** — removed W291/W293 whitespace issues across `bot/handler.py`, `bot/dispatcher.py`, `bot/commands/base.py`, `bot/platforms/feishu_stream.py`, `bot/platforms/dingtalk_stream.py`
- 🐛 **Dispatcher `_parse_intent_via_llm` safety** — replaced fragile `'raw' in dir()` with `'raw' in locals()` for undefined-variable guard in `JSONDecodeError` handler
- 🐛 **筹码结构 LLM 未填写时兜底补全** (#589) — DeepSeek 等模型未正确填写 `chip_structure` 时，自动用数据源已获取的筹码数据补全，保证各模型展示一致；普通分析与 Agent 模式均生效
- 🐛 **历史报告狙击点位显示原始文本** (#452) — 历史详情页现优先展示 `raw_result.dashboard.battle_plan.sniper_points` 中的原始字符串，避免 `analysis_history` 数值列把区间、说明文字或复杂点位压缩成单个数字；保留原有数值列作为回退
- 🐛 **Session prefix collision** — user ID `123` could see sessions of user `1234` via `startswith`; fixed with colon delimiter in session_id format
- 🐛 **NL pre-filter false positives** — `re.IGNORECASE` caused `[A-Z]{2,5}` to match common English words like "hello"; removed global flag, use inline `(?i:...)` only for English finance keywords
- 🐛 **Dotted ticker in strategy args** — `_get_strategy_args()` didn't recognize `BRK.B` as a stock code, leaving it in strategy text; now accepts `TICKER.CLASS` format
- ⏱️ **efinance 长调用挂起修复** (#660) — 为所有 efinance API 调用引入 `_ef_call_with_timeout()` 包装（默认 30 秒，可通过 `EFINANCE_CALL_TIMEOUT` 配置）；使用 `executor.shutdown(wait=False)` 确保超时后不再阻塞主线程，彻底消除 81 分钟挂起问题
- 🛡️ **类型安全内容完整性检查** (#660) — `check_content_integrity()` 现在将非字符串类型的 `operation_advice` / `analysis_summary` 视为缺失字段，避免下游 `get_emoji()` 因 `dict.strip()` 崩溃
- 📄 **报告保存与通知解耦** (#660) — `_save_local_report()` 不再依赖 `send_notification` 标志触发，`--no-notify` 模式下本地报告照常保存
- 🔄 **operation_advice 字典归一化** (#660) — Pipeline 和 BacktestEngine 现在将 LLM 返回的 `dict` 格式 `operation_advice` 通过 `decision_type`（不区分大小写）映射为标准字符串，防止因模型输出格式变化导致崩溃
- 🛡️ **runner.py usage None 防护** (#660) — `response.usage` 为 `None` 时不再抛出 `AttributeError`，回退为 0 token 计数
- 📋 **orchestrator 静默失败改为日志警告** (#660) — `IntelAgent` / `RiskAgent` 阶段失败现在记录 `WARNING` 而非静默跳过，便于诊断

### Notes
- ⚠️ **Multi-worker auth toggles** — runtime auth updates are process-local; multi-worker deployments must restart/roll workers to keep auth state consistent

## [3.5.0] - 2026-03-12

### Added
- 📊 **Web UI full report drawer** (Fixes #214) — history page adds "Full Report" button to display the complete Markdown analysis report in a side drawer; new `GET /api/v1/history/{record_id}/markdown` endpoint
- 📊 **LLM cost tracking** — all LLM calls (analysis, agent, market review) recorded in `llm_usage` table; new `GET /api/v1/usage/summary?period=today|month|all` endpoint returns aggregated token usage by call type and model
- 🔍 **SearXNG search provider** (Fixes #550) — quota-free self-hosted search fallback; priority: Bocha > Tavily > Brave > SerpAPI > MiniMax > SearXNG
- 🔍 **MiniMax web search provider** — `MiniMaxSearchProvider` with circuit breaker (3 failures → 300s cooldown) and dual time-filtering; configured via `MINIMAX_API_KEYS`
- 🤖 **Agent models discovery API** — `GET /api/v1/agent/models` returns available model deployments (primary/fallback/source/api_base) for Web UI model selector
- 🤖 **Agent chat export & send** (#495) — export conversation to .md file; send to configured notification channels; new `POST /api/v1/agent/chat/send`
- 🤖 **Agent background execution** (#495) — analysis continues when switching pages; badge notification on completion; auto-cancel in-progress stream on session switch
- 📝 **Report Engine P0** — Pydantic schema validation for LLM JSON; Jinja2 templates (markdown/wechat/brief) with legacy fallback; content integrity checks with retry; brief mode (`REPORT_TYPE=brief`); history signal comparison
- 📦 **Smart import** — multi-source import from image/CSV/Excel/clipboard; Vision LLM extracts code+name+confidence; name→code resolver (local map + pinyin + AkShare); confidence-tiered confirmation
- ⚙️ **GitHub Actions LiteLLM config** — workflow supports `LITELLM_CONFIG`/`LITELLM_CONFIG_YAML` for flexible AI provider configuration
- ⚙️ **Config engine refactor & system API** (#602) — unified config registry, validation and API exposure
- 📖 **LLM configuration guide** — new `docs/LLM_CONFIG_GUIDE.md` covering 3-tier config, quick start, Vision/Agent/troubleshooting

### Fixed
- 🐛 **analyze_trend always reports No historical data** (#600) — now fetches from DB/DataFetcher instead of broken `get_analysis_context`
- 🐛 **Chip structure fallback when LLM omits it** (#589) — auto-fills from data source chip data for consistent display across models
- 🐛 **History sniper points show raw text** (#452) — prioritizes original strings over compressed numeric values
- 🐛 **GitHub Actions ENABLE_CHIP_DISTRIBUTION configurable** (#617) — no longer hardcoded, supports vars/secrets override
- 🐛 **`.env` save preserves comments and blank lines** — Web settings no longer destroys `.env` formatting
- 🐛 **Agent model discovery fixes** — legacy mode includes LiteLLM-native providers; source detection aligned with runtime; fallback deployments no longer expanded per-key
- 🐛 **Stooq US stock previous close semantics** — no longer misuses open price as previous close
- 🐛 **Stock name prefetch regression** — prioritizes local `STOCK_NAME_MAP` before remote queries
- 🐛 **AkShare limit-up/down calculation** (#555) — fixed market analysis statistics
- 🐛 **AkShare Tencent source field index & ETF quote mapping** (#579)
- 🐛 **Pytdx stock name cache pagination** (#573) — prevents cache overflow
- 🐛 **PushPlus oversized report chunking** (#489) — auto-segments long content
- 🐛 **Agent chat cancel & switch** (#495) — cancel no longer misreports as failure; fast switch no longer overwrites stream state
- 🐛 **MiniMax search status in `/status` command** (#587)
- 🐛 **config_registry duplicate BOCHA_API_KEYS** — removed duplicate dict entry that silently overwrote config

### Changed
- 🔎 **Fetcher failure observability** — logs record start/success/failure with elapsed time, failover transitions; Efinance/Akshare include upstream endpoint and classified failure categories
- ♻️ **Data source resilience & cleanup** (#602) — fallback chain optimization
- ♻️ **Image extract API response extension** — new `items` field (code/name/confidence); `codes` preserved for backward compatibility
- ♻️ **Import parse error messages** — specific failure reasons for Excel/CSV; improved logging with file type and size

### Docs
- 📖 LLM config guide refactored for clarity (#583)
- 📖 `image-extract-prompt.md` with full prompt documentation
- 📖 AkShare fallback cache TTL documentation
## [3.4.10] - 2026-03-07

### Fixed
- 🐛 **EfinanceFetcher ETF OHLCV data** (#541, #527) — switch `_fetch_etf_data` from `ef.fund.get_quote_history` (NAV-only, no OHLCV, no `beg`/`end` params) to `ef.stock.get_quote_history`; ETFs now return proper open/high/low/close/volume/amount instead of zeros; remove obsolete NAV column mappings from `_normalize_data`
- 🐛 **tiktoken 0.12.0 `Unknown encoding cl100k_base`** (#537) — pin `tiktoken>=0.8.0,<0.12.0` in requirements.txt to avoid plugin-registration regression introduced in 0.12.0
- 🐛 **Web UI API error classification** (#540) — frontend no longer treats every HTTP 400 as the same "server/network" failure; now distinguishes Agent disabled / missing params / model-tool incompatibility / upstream LLM errors / local connection failures
- 🐛 **北交所代码识别失败** (#491, #533) — 8/4/92 开头的 6 位代码现正确识别为北交所；Tushare/Akshare/Yfinance 等数据源支持 .BJ 或 bj 前缀；Baostock/Pytdx 对北交所代码显式切换数据源；避免误判上海 B 股 900xxx
- 🐛 **狙击点位解析错误** (#488, #532) — 理想买入/二次买入等字段在无「元」字时误提取括号内技术指标数字；现先截去第一个括号后内容再提取

### Added
- **Markdown-to-image for dashboard report** (#455, #535) — 个股日报汇总支持 markdown 转图片推送（Telegram、WeChat、Custom、Email），与大盘复盘行为一致
- **markdown-to-file engine** (#455) — `MD2IMG_ENGINE=markdown-to-file` 可选，对 emoji 支持更好，需 `npm i -g markdown-to-file`
- **PREFETCH_REALTIME_QUOTES** (#455) — 设为 `false` 可禁用实时行情预取，避免 efinance/akshare_em 全市场拉取
- **Stock name prefetch** (#455) — 分析前预取股票名称，减少报告中「股票xxxxx」占位符
- 📊 **分析报告模型标记** (#528, #534) — 在分析报告 meta、报告末尾、推送内容中展示 `model_used`（完整 LLM 模型名）；Agent 多轮调用时记录并展示每轮实际使用的模型（支持 fallback 切换）

### Changed
- **Enhanced markdown-to-image failure warning** (#455) — 转图失败时提示具体依赖（wkhtmltopdf 或 m2f）
- **WeChat-only image routing optimization** (#455) — 仅配置企业微信图片时，不再对完整报告做冗余转图，避免误导性失败日志
- **Stock name prefetch lightweight mode** (#455) — 名称预取阶段跳过 realtime quote 查询，减少额外网络开销

## [3.4.9] - 2026-03-06

### Added
- 🧠 **Structured config validation** — `ConfigIssue` dataclass and `validate_structured()` with severity-aware logging; `CONFIG_VALIDATE_MODE=strict` aborts startup on errors
- 🖼️ **Vision model config** — `VISION_MODEL` and `VISION_PROVIDER_PRIORITY` for image stock extraction; provider fallback (Gemini → Anthropic → OpenAI → DeepSeek) when primary fails
- 🚀 **CLI init wizard** — `python -m dsa init` 3-step interactive bootstrap (model → data source → notification), 9 provider presets, incremental merge by default
- 🔧 **Multi-channel LLM support** with visual channel editor (#494)

### Changed
- ♻️ **Vision extraction** — migrated from gemini-3 hardcode to `litellm.completion()` with configurable model and provider fallback; `OPENAI_VISION_MODEL` deprecated in favor of `VISION_MODEL`
- ♻️ **Market analyzer** — uses `Analyzer.generate_text()` for LLM calls; fixes bypass and Anthropic `AttributeError` when using non-Router path
- ♻️ **Config validation refinements** — test_env output format syncs with `validate_structured` (severity-aware ✓/✗/⚠/·); Vision key warning when `VISION_MODEL` set but no provider API key; market_analyzer test covers `generate_market_review` fallback when `generate_text` returns None
- ⚙️ **Auto-tag workflow defaults to NO tag** — only tags when commit message explicitly contains `#patch`, `#minor`, or `#major`
- ♻️ **Formatter and notification refactor** (#516)

### Fixed
- 🐛 **STOCK_LIST not refreshed on scheduled runs** — `.env` or WebUI changes to `STOCK_LIST` now hot-reload before each scheduled analysis (#529)
- 🐛 **WebUI fails to load with MIME type error** — SPA fallback route now resolves correct `Content-Type` for JS/CSS files (#520)
- 🐛 **AstrBot sender docstring misplaced** — `import time` placed before docstring in `_send_astrbot`, causing it to become dead code
- 🐛 **Telegram Markdown link escaping** — `_convert_to_telegram_markdown` escaped `[]()` characters, breaking all Markdown links in reports
- 🐛 **Duplicate `discord_bot_status` field** in Config dataclass — second declaration silently shadowed the first
- 🧹 **Unused imports** — removed `shutil`/`subprocess` from `main.py`
- 🔧 **Config validation and Vision key check** (#525)

### Docs
- 📝 Clarified GitHub Actions non-trading-day manual run controls (`TRADING_DAY_CHECK_ENABLED` + `force_run`) for Issue #461 / PR #466

## [3.4.8] - 2026-03-02

### Fixed
- 🐛 **Desktop exe crashes on startup with `FileNotFoundError`** — PyInstaller build was missing litellm's JSON data files (e.g. `model_prices_and_context_window_backup.json`). Added `--collect-data litellm` to both Windows and macOS build scripts so the files are correctly bundled in the executable.

### CI
- 🔧 Cache Electron binaries on macOS CI runners to prevent intermittent EOF download failures when fetching `electron-vX.Y.Z-darwin-*.zip` from GitHub CDN
- 🔧 Fix macOS DMG `hdiutil Resource busy` error during desktop packaging

### Docs
- 📝 Clarify non-trading-day manual run controls for GitHub Actions (`TRADING_DAY_CHECK_ENABLED` + `force_run`) (#474)

## [3.4.7] - 2026-02-28

### Added
- 🧠 **CN/US Market Strategy Blueprint System** (#395) — market review prompt injects region-specific strategy blueprints with position sizing and risk trigger recommendations

### Fixed
- 🐛 **`TRADING_DAY_CHECK_ENABLED` env var and `--force-run` for GitHub Actions** (#466)
- 🐛 **Agent pipeline preserved resolved stock names** (#464) — placeholder names no longer leak into reports
- 🐛 **Code cleanup** (#462, Fixes #422)
- 🐛 **WebUI auto-build on startup** (#460)
- 🐛 **ARCH_ARGS unbound variable** (#458)
- 🐛 **Time zone inconsistency & right panel flash** (#439)

### Docs
- 📝 Clarify potential ambiguities in code (#343)
- 📝 ENABLE_EASTMONEY_PATCH guidance for Issue #453 (#456)

## [3.4.0] - 2026-02-27

### Added
- 📡 **LiteLLM Direct Integration + Multi API Key Support** (#454, Fixes #421 #428)
  - Removed native SDKs (google-generativeai, google-genai, anthropic); unified through `litellm>=1.80.10`
  - New config: `LITELLM_MODEL`, `LITELLM_FALLBACK_MODELS`, `GEMINI_API_KEYS`, `ANTHROPIC_API_KEYS`, `OPENAI_API_KEYS`
  - Multi-key auto-builds LiteLLM Router (simple-shuffle) with 429 cooldown
  - **Breaking**: `.env` `GEMINI_MODEL` (no prefix) only for fallback; explicit config must include provider prefix

### Changed
- ♻️ **Notification Refactoring** (#435) — extracted 10 sender classes into `src/notification_sender/`

### Fixed
- 🐛 LLM NoneType crash, history API 422, sniper points extraction
- 🐛 Auto-build frontend on WebUI startup — `WEBUI_AUTO_BUILD` env var (default `true`)
- 🐛 Docker explicit project name (#448)
- 🐛 Bocha search SSL retry (#445, #446) — transient errors retry up to 3 times
- 🐛 Gemini google-genai SDK migration (Fixes #440, #444)
- 🐛 Mobile home page scrolling (Fixes #419, #433)
- 🐛 History list scroll reset (#431)
- 🐛 Settings save button false positive (fixes #417, #430)

## [3.3.22] - 2026-02-26

### Added
- 💬 **Chat History Persistence** (Fixes #400, #414) — `/chat` page survives refresh, sidebar session list
- 🎨 Project VI Assets — logo icon set, PSD, vector, banner (#425)
- 🚀 Desktop CI Auto-Release (#426) — Windows + macOS parallel builds

### Fixed
- 🐛 Agent Reasoning 400 & LiteLLM Proxy (fixes #409, #427)
- 🐛 Discord chunked sending (#413) — `DISCORD_MAX_WORDS` config
- 🐛 yfinance shared DataFrame (#412)
- 🐛 sniper_points parsing (#408)
- 🐛 Agent framework category missing (#406)
- 🐛 Date inconsistency & query id (fixes #322, #363)

## [3.3.12] - 2026-02-24

### Added
- 📈 **Intraday Realtime Technical Indicators** (Issue #234, #397) — MA calculated from realtime price, config: `ENABLE_REALTIME_TECHNICAL_INDICATORS`
- 🤖 **Agent Strategy Chat** (#367) — full ReAct pipeline, 11 YAML strategies, SSE streaming, multi-turn chat
- 📢 PushPlus Group Push — `PUSHPLUS_TOPIC` (#402)
- 📅 Trading Day Check (Issue #373, #375) — `TRADING_DAY_CHECK_ENABLED`, `--force-run`

### Fixed
- 🐛 DeepSeek reasoning mode (Issue #379, #386)
- 🐛 Agent news intel persistence (Fixes #396, #405)
- 🐛 Bare except clauses replaced with `except Exception` (#398)
- 🐛 UUID fallback for HTTP non-secure context (fixes #377, #381)
- 🐛 Docker DNS resolution (Fixes #372, #374)
- 🐛 Agent session/strategy bugs — multiple follow-up fixes for #367
- 🐛 yfinance parallel download data filtering

### Changed
- Market review strategy consistency — unified cn/us template
- Agent test assertions updated (`6 -> 11`)


## [3.2.11] - 2026-02-23

### 修复（#patch）
- 🐛 **StockTrendAnalyzer 从未执行** (Issue #357)
  - 根因：`get_analysis_context` 仅返回 2 天数据且无 `raw_data`，pipeline 中 `raw_data in context` 始终为 False
  - 修复：Step 3 直接调用 `get_data_range` 获取 90 日历天（约 60 交易日）历史数据用于趋势分析
  - 改善：趋势分析失败时用 `logger.warning(..., exc_info=True)` 记录完整 traceback

## [3.2.10] - 2026-02-22

### 新增
- ⚙️ 支持 `RUN_IMMEDIATELY` 配置项，设为 `true` 时定时任务触发后立即执行一次分析，无需等待首个定时点

### 修复
- 🐛 修复 Web UI 页面居中问题
- 🐛 修复 Settings 返回 500 错误

## [3.2.9] - 2026-02-22

### 修复
- 🐛 **ETF 分析仅关注指数走势**（Issue #274）
  - 美股/港股 ETF（如 VOO、QQQ）与 A 股 ETF 不再纳入基金公司层面风险（诉讼、声誉等）
  - 搜索维度：ETF/指数专用 risk_check、earnings、industry 查询，避免命中基金管理人新闻
  - AI 提示：指数型标的分析约束，`risk_alerts` 不得出现基金管理人公司经营风险

## [3.2.8] - 2026-02-21

### 修复
- 🐛 **BOT 与 WEB UI 股票代码大小写统一**（Issue #355）
  - BOT `/analyze` 与 WEB UI 触发分析的股票代码统一为大写（如 `aapl` → `AAPL`）
  - 新增 `canonical_stock_code()`，在 BOT、API、Config、CLI、task_queue 入口处规范化
  - 历史记录与任务去重逻辑可正确识别同一股票（大小写不再影响）

## [3.2.7] - 2026-02-20

### 新增
- 🔐 **Web 页面密码验证**（Issue #320, #349）
  - 支持 `ADMIN_AUTH_ENABLED=true` 启用 Web 登录保护
  - 首次访问在网页设置初始密码；支持「系统设置 > 修改密码」和 CLI `python -m src.auth reset_password` 重置

## [3.2.6] - 2026-02-20
### ⚠️ 破坏性变更（Breaking Changes）

- **历史记录 API 变更 (Issue #322)**
  - 路由变更：`GET /api/v1/history/{query_id}` → `GET /api/v1/history/{record_id}`
  - 参数变更：`query_id` (字符串) → `record_id` (整数)
  - 新闻接口变更：`GET /api/v1/history/{query_id}/news` → `GET /api/v1/history/{record_id}/news`
  - 原因：`query_id` 在批量分析时可能重复，无法唯一标识单条历史记录。改用数据库主键 `id` 确保唯一性
  - 影响范围：使用旧版历史详情 API 的所有客户端需同步更新

### 修复
- 修复美股（如 ADBE）技术指标矛盾：akshare 美股复权数据异常，统一美股历史数据源为 YFinance（Issue #311）
- 🐛 **历史记录查询和显示问题 (Issue #322)**
  - 修复历史记录列表查询中日期不一致问题：使用明天作为 endDate，确保包含今天全天的数据
  - 修复服务器 UI 报告选择问题：原因是多条记录共享同一 `query_id`，导致总是显示第一条。现改用 `analysis_history.id` 作为唯一标识
  - 历史详情、新闻接口及前端组件已全面适配 `record_id`
  - 新增后台轮询（每 30s）与页面可见性变更时静默刷新历史列表，确保 CLI 发起的分析完成后前端能及时同步，使用 `silent` 模式避免触发 loading 状态
- 🐛 **美股指数实时行情与日线数据** (Issue #273)
  - 修复 SPX、DJI、IXIC、NDX、VIX、RUT 等美股指数无法获取实时行情的问题
  - 新增 `us_index_mapping` 模块，将用户输入（如 SPX）映射为 Yahoo Finance 符号（如 ^GSPC）
  - 美股指数与美股股票日线数据直接路由至 YfinanceFetcher，避免遍历不支持的数据源
  - 消除重复的美股识别逻辑，统一使用 `is_us_stock_code()` 函数

### 优化
- 🎨 **首页输入栏与 Market Sentiment 布局对齐优化**
  - 股票代码输入框左缘与历史记录 glass-card 框左对齐
  - 分析按钮右缘与 Market Sentiment 外框右对齐
  - Market Sentiment 卡片向下拉伸填满格子，消除与 STRATEGY POINTS 之间的空隙
  - 窄屏时输入栏填满宽度，响应式对齐保持一致

## [3.2.5] - 2026-02-19

### 新增
- 🌍 **大盘复盘可选区域**（Issue #299）
  - 支持 `MARKET_REVIEW_REGION` 环境变量：`cn`（A股）、`us`（美股）、`both`（两者）
  - us 模式使用 SPX/纳斯达克/道指/VIX 等指数；both 模式可同时复盘 A 股与美股
  - 默认 `cn`，保持向后兼容

## [3.2.4] - 2026-02-18

### 修复
- 🐛 **统一美股数据源为 YFinance**（Issue #311）
  - akshare 美股复权数据异常，统一美股历史数据源为 YFinance
  - 修复 ADBE 等美股股票技术指标矛盾问题

## [3.2.3] - 2026-02-18

### 修复
- 🐛 **标普500实时数据缺失**（Issue #273）
  - 修复 SPX、DJI、IXIC、NDX、VIX、RUT 等美股指数无法获取实时行情的问题
  - 新增 `us_index_mapping` 模块，将用户输入（如 SPX）映射为 Yahoo Finance 符号（如 `^GSPC`）
  - 美股指数与美股股票日线数据直接路由至 YfinanceFetcher，避免遍历不支持的数据源

## [3.2.2] - 2026-02-16

### 新增
- 📊 **PE 指标支持**（Issue #296）
  - AI System Prompt 增加 PE 估值关注
- 📰 **新闻时效性筛查**（Issue #296）
  - `NEWS_MAX_AGE_DAYS`：新闻最大时效（天），默认 3，避免使用过时信息
- 📈 **强势趋势股乖离率放宽**（Issue #296）
  - `BIAS_THRESHOLD`：乖离率阈值（%），默认 5.0，可配置
  - 强势趋势股（多头排列且趋势强度 ≥70）自动放宽乖离率到 1.5 倍

## [3.2.1] - 2026-02-16

### 新增
- 🔧 **东财接口补丁可配置开关**
  - 支持 `EFINANCE_PATCH_ENABLED` 环境变量开关东财接口补丁（默认 `true`）
  - 补丁不可用时可降级关闭，避免影响主流程

## [3.2.0] - 2026-02-15

### 新增
- 🔒 **CI 门禁统一（P0）**
  - 新增 `scripts/ci_gate.sh` 作为后端门禁单一入口
  - 主 CI 改为 `backend-gate`、`docker-build`、`web-gate` 三段式
  - CI 触发改为所有 PR，避免 Required Checks 因路径过滤缺失而卡住合并
  - `web-gate` 支持前端路径变更按需触发
  - 新增 `network-smoke` 工作流承载非阻断网络场景回归
- 📦 **发布链路收敛（P0）**
  - `docker-publish` 调整为 tag 主触发，并增加发布前门禁校验
  - 手动发布增加 `release_tag` 输入与 semver/changelog 强校验
  - 发布前新增 Docker smoke（关键模块导入）
- 📝 **PR 模板升级（P0）**
  - 增加背景、范围、验证命令与结果、回滚方案、Issue 关联等必填项
- 🤖 **AI 审查覆盖增强（P0）**
  - `pr-review` 纳入 `.github/workflows/**` 范围
  - 新增 `AI_REVIEW_STRICT` 开关，可选将 AI 审查失败升级为阻断

## [3.1.13] - 2026-02-15

### 新增
- 📊 **仅分析结果摘要**（Issue #262）
  - 支持 `REPORT_SUMMARY_ONLY` 环境变量，设为 `true` 时只推送汇总，不含个股详情
  - 默认 `false`，多股时适合快速浏览

## [3.1.12] - 2026-02-15

### 新增
- 📧 **个股与大盘复盘合并推送**（Issue #190）
  - 支持 `MERGE_EMAIL_NOTIFICATION` 环境变量，设为 `true` 时将个股分析与大盘复盘合并为一次推送
  - 默认 `false`，减少邮件数量、降低被识别为垃圾邮件的风险

## [3.1.11] - 2026-02-15

### 新增
- 🤖 **Anthropic Claude API 支持**（Issue #257）
  - 支持 `ANTHROPIC_API_KEY`、`ANTHROPIC_MODEL`、`ANTHROPIC_TEMPERATURE`、`ANTHROPIC_MAX_TOKENS`
  - AI 分析优先级：Gemini > Anthropic > OpenAI
- 📷 **从图片识别股票代码**（Issue #257）
  - 上传自选股截图，通过 Vision LLM 自动提取股票代码
  - API: `POST /api/v1/stocks/extract-from-image`；支持 JPEG/PNG/WebP/GIF，最大 5MB
  - 支持 `OPENAI_VISION_MODEL` 单独配置图片识别模型
- ⚙️ **通达信数据源手动配置**（Issue #257）
  - 支持 `PYTDX_HOST`、`PYTDX_PORT` 或 `PYTDX_SERVERS` 配置自建通达信服务器

## [3.1.10] - 2026-02-15

### 新增
- ⚙️ **立即运行配置**（Issue #332）
  - 支持 `RUN_IMMEDIATELY` 环境变量，`true` 时定时任务启动后立即执行一次
- 🐛 修复 Docker 构建问题

## [3.1.9] - 2026-02-14

### 新增
- 🔌 **东财接口补丁机制**
  - 新增 `patch/eastmoney_patch.py` 修复 efinance 上游接口变更
  - 不影响其他数据源的正常运行

## [3.1.8] - 2026-02-14

### 新增
- 🔐 **Webhook 证书校验开关**（Issue #265）
  - 支持 `WEBHOOK_VERIFY_SSL` 环境变量，可关闭 HTTPS 证书校验以支持自签名证书
  - 默认保持校验，关闭存在 MITM 风险，仅建议在可信内网使用

## [3.1.7] - 2026-02-14

### 修复
- 🐛 修复包导入错误（package import error）

## [3.1.6] - 2026-02-13

### 修复
- 🐛 修复 `news_intel` 中 `query_id` 不一致问题

## [3.1.5] - 2026-02-13

### 新增
- 📷 **Markdown 转图片通知**（Issue #289）
  - 支持 `MARKDOWN_TO_IMAGE_CHANNELS` 配置，对 Telegram、企业微信、自定义 Webhook（Discord）、邮件发送图片格式报告
  - 邮件为内联附件，增强对不支持 HTML 客户端的兼容性
  - 需安装 `wkhtmltopdf` 和 `imgkit`

## [3.1.4] - 2026-02-12

### 新增
- 📧 **股票分组发往不同邮箱**（Issue #268）
  - 支持 `STOCK_GROUP_N` + `EMAIL_GROUP_N` 配置，不同股票组报告发送到对应邮箱
  - 大盘复盘发往所有配置的邮箱

## [3.1.3] - 2026-02-12

### 修复
- 🐛 修复 Docker 内运行时通过页面修改配置报错 `[Errno 16] Device or resource busy` 的问题

## [3.1.2] - 2026-02-11

### 修复
- 🐛 修复 Docker 一致性问题，解决关键批次处理与通知 Bug

## [3.1.1] - 2026-02-11

### 变更
- ♻️ `API_HOST` → `WEBUI_HOST`：Docker Compose 配置项统一

## [3.1.0] - 2026-02-11

### 新增
- 📊 **ETF 支持增强与代码规范化**
  - 统一各数据源 ETF 代码处理逻辑
  - 新增 `canonical_stock_code()` 统一代码格式，确保数据源路由正确

## [3.0.5] - 2026-02-08

### 修复
- 🐛 修复信号 emoji 与建议不一致的问题（复合建议如"卖出/观望"未正确映射）
- 🐛 修复 `*ST` 股票名在微信/Dashboard 中 markdown 转义问题
- 🐛 修复 `idx.amount` 为 None 时大盘复盘 TypeError
- 🐛 修复分析 API 返回 `report=None` 及 ReportStrategy 类型不一致问题
- 🐛 修复 Tushare 返回类型错误（dict → UnifiedRealtimeQuote）及 API 端点指向

### 新增
- 📊 大盘复盘报告注入结构化数据（涨跌统计、指数表格、板块排名）
- 🔍 搜索结果 TTL 缓存（500 条上限，FIFO 淘汰）
- 🔧 Tushare Token 存在时自动注入实时行情优先级
- 📰 新闻摘要截断长度 50→200 字

### 优化
- ⚡ 补充行情字段请求限制为最多 1 次，减少无效请求

## [3.0.4] - 2026-02-07

### 新增
- 📈 **回测引擎** (PR #269)
  - 新增基于历史分析记录的回测系统，支持收益率、胜率、最大回撤等指标评估
  - WebUI 集成回测结果展示

## [3.0.3] - 2026-02-07

### 修复
- 🐛 修复狙击点位数据解析错误问题 (PR #271)

## [3.0.2] - 2026-02-06

### 新增
- ✉️ 可配置邮件发送者名称 (PR #272)
- 🌐 外国股票支持英文关键词搜索

## [3.0.1] - 2026-02-06

### 修复
- 🐛 修复 ETF 实时行情获取、市场数据回退、企业微信消息分块问题
- 🔧 CI 流程简化

## [3.0.0] - 2026-02-06

### 移除
- 🗑️ **移除旧版 WebUI**
  - 删除基于 `http.server.ThreadingHTTPServer` 的旧版 WebUI（`web/` 包）
  - 旧版 WebUI 的功能已完全被 FastAPI（`api/`）+ React 前端替代
  - `--webui` / `--webui-only` 命令行参数标记为弃用，自动重定向到 `--serve` / `--serve-only`
  - `WEBUI_ENABLED` / `WEBUI_HOST` / `WEBUI_PORT` 环境变量保持兼容，自动转发到 FastAPI 服务
  - `webui.py` 保留为兼容入口，启动时直接调用 FastAPI 后端
  - Docker Compose 中移除 `webui` 服务定义，统一使用 `server` 服务

### 变更
- ♻️ **服务层重构**
  - 将 `web/services.py` 中的异步任务服务迁移至 `src/services/task_service.py`
  - Bot 分析命令（`bot/commands/analyze.py`）改为使用 `src.services.task_service`
  - Docker 环境变量 `WEBUI_HOST`/`WEBUI_PORT` 更名为 `API_HOST`/`API_PORT`（旧名仍兼容）

## [2.3.0] - 2026-02-01

### 新增
- 🇺🇸 **增强美股支持** (Issue #153)
  - 实现基于 Akshare 的美股历史数据获取 (`ak.stock_us_daily()`)
  - 实现基于 Yfinance 的美股实时行情获取（优先策略）
  - 增加对不支持数据源（Tushare/Baostock/Pytdx/Efinance）的美股代码过滤和快速降级

### 修复
- 🐛 修复 AMD 等美股代码被误识别为 A 股的问题 (Issue #153)

## [2.2.5] - 2026-02-01

### 新增
- 🤖 **AstrBot 消息推送** (PR #217)
  - 新增 AstrBot 通知渠道，支持推送到 QQ 和微信
  - 支持 HMAC SHA256 签名验证，确保通信安全
  - 通过 `ASTRBOT_URL` 和 `ASTRBOT_TOKEN` 配置

## [2.2.4] - 2026-02-01

### 新增
- ⚙️ **可配置数据源优先级** (PR #215)
  - 支持通过环境变量（如 `YFINANCE_PRIORITY=0`）动态调整数据源优先级
  - 无需修改代码即可优先使用特定数据源（如 Yahoo Finance）

## [2.2.3] - 2026-01-31

### 修复
- 📦 更新 requirements.txt，增加 `lxml_html_clean` 依赖以解决兼容性问题

## [2.2.2] - 2026-01-31

### 修复
- 🐛 修复代理配置区分大小写问题 (fixes #211)

## [2.2.1] - 2026-01-31

### 修复
- 🐛 **YFinance 兼容性修复** (PR #210, fixes #209)
  - 修复新版 yfinance 返回 MultiIndex 列名导致的数据解析错误

## [2.2.0] - 2026-01-31

### 新增
- 🔄 **多源回退策略增强**
  - 实现了更健壮的数据获取回退机制 (feat: multi-source fallback strategy)
  - 优化了数据源故障时的自动切换逻辑

### 修复
- 🐛 修复 analyzer 运行后无法通过改 .env 文件的 stock_list 内容调整跟踪的股票

## [2.1.14] - 2026-01-31

### 文档
- 📝 更新 README 和优化 auto-tag 规则

## [2.1.13] - 2026-01-31

### 修复
- 🐛 **Tushare 优先级与实时行情** (Fixed #185)
  - 修复 Tushare 数据源优先级设置问题
  - 修复 Tushare 实时行情获取功能

## [2.1.12] - 2026-01-30

### 修复
- 🌐 修复代理配置在某些情况下的区分大小写问题
- 🌐 修复本地环境禁用代理的逻辑

## [2.1.11] - 2026-01-30

### 优化
- 🚀 **飞书消息流优化** (PR #192)
  - 优化飞书 Stream 模式的消息类型处理
  - 修改 Stream 消息模式默认为关闭，防止配置错误运行时报错

## [2.1.10] - 2026-01-30

### 合并
- 📦 合并 PR #154 贡献

## [2.1.9] - 2026-01-30

### 新增
- 💬 **微信文本消息支持** (PR #137)
  - 新增微信推送的纯文本消息类型支持
  - 添加 `WECHAT_MSG_TYPE` 配置项

## [2.1.8] - 2026-01-30

### 修复
- 🐛 修正日志中 API 提供商显示错误 (PR #197)

## [2.1.7] - 2026-01-30

### 修复
- 🌐 禁用本地环境的代理设置，避免网络连接问题

## [2.1.6] - 2026-01-29

### 新增
- 📡 **Pytdx 数据源 (Priority 2)**
  - 新增通达信数据源，免费无需注册
  - 多服务器自动切换
  - 支持实时行情和历史数据
- 🏷️ **多源股票名称解析**
  - DataFetcherManager 新增 `get_stock_name()` 方法
  - 新增 `batch_get_stock_names()` 批量查询
  - 自动在多数据源间回退
  - Tushare 和 Baostock 新增股票名称/列表方法
- 🔍 **增强搜索回退**
  - 新增 `search_stock_price_fallback()` 用于数据源全部失败时
  - 新增搜索维度：市场分析、行业分析
  - 最大搜索次数从 3 增加到 5
  - 改进搜索结果格式（每维度 4 条结果）

### 改进
- 更新搜索查询模板以提高相关性
- 增强 `format_intel_report()` 输出结构

## [2.1.5] - 2026-01-29

### 新增
- 📡 新增 Pytdx 数据源和多源股票名称解析功能

## [2.1.4] - 2026-01-29

### 文档
- 📝 更新赞助商信息

## [2.1.3] - 2026-01-28

### 文档
- 📝 重构 README 布局
- 🌐 新增繁体中文翻译 (README_CHT.md)

### 修复
- 🐛 修复 WebUI 无法输入美股代码问题
  - 输入框逻辑改成所有字母都转换成大写
  - 支持 `.` 的输入（如 `BRK.B`）

## [2.1.2] - 2026-01-27

### 修复
- 🐛 修复个股分析推送失败和报告路径问题 (fixes #166)
- 🐛 修改 CR 错误，确保微信消息最大字节配置生效

## [2.1.1] - 2026-01-26

### 新增
- 🔧 添加 GitHub Actions auto-tag 工作流
- 📡 添加 yfinance 兜底数据源及数据缺失警告

### 修复
- 🐳 修复 docker-compose 路径和文档命令
- 🐳 Dockerfile 补充 copy src 文件夹 (fixes #145)

## [2.1.0] - 2026-01-25

### 新增
- 🇺🇸 **美股分析支持**
  - 支持美股代码直接输入（如 `AAPL`, `TSLA`）
  - 使用 YFinance 作为美股数据源
- 📈 **MACD 和 RSI 技术指标**
  - MACD：趋势确认、金叉死叉信号（零轴上金叉⭐、金叉✅、死叉❌）
  - RSI：超买超卖判断（超卖⭐、强势✅、超买⚠️）
  - 指标信号纳入综合评分系统
- 🎮 **Discord 推送支持** (PR #124, #125, #144)
  - 支持 Discord Webhook 和 Bot API 两种方式
  - 通过 `DISCORD_WEBHOOK_URL` 或 `DISCORD_BOT_TOKEN` + `DISCORD_MAIN_CHANNEL_ID` 配置
- 🤖 **机器人命令交互**
  - 钉钉机器人支持 `/分析 股票代码` 命令触发分析
  - 支持 Stream 长连接模式
- 🌡️ **AI 温度参数可配置** (PR #142)
  - 支持自定义 AI 模型温度参数
- 🐳 **Zeabur 部署支持**
  - 添加 Zeabur 镜像部署工作流
  - 支持 commit hash 和 latest 双标签

### 重构
- 🏗️ **项目结构优化**
  - 核心代码移至 `src/` 目录，根目录更清爽
  - 文档移至 `docs/` 目录
  - Docker 配置移至 `docker/` 目录
  - 修复所有 import 路径，保持向后兼容
- 🔄 **数据源架构升级**
  - 新增数据源熔断机制，单数据源连续失败自动切换
  - 实时行情缓存优化，批量预取减少 API 调用
  - 网络代理智能分流，国内接口自动直连
- 🤖 Discord 机器人重构为平台适配器架构

### 修复
- 🌐 **网络稳定性增强**
  - 自动检测代理配置，对国内行情接口强制直连
  - 修复 EfinanceFetcher 偶发的 `ProtocolError`
  - 增加对底层网络错误的捕获和重试机制
- 📧 **邮件渲染优化**
  - 修复邮件中表格不渲染问题 (#134)
  - 优化邮件排版，更紧凑美观
- 📢 **企业微信推送修复**
  - 修复大盘复盘推送不完整问题
  - 增强消息分割逻辑，支持更多标题格式
  - 增加分批发送间隔，避免限流丢失
- 👷 **CI/CD 修复**
  - 修复 GitHub Actions 中路径引用的错误

## [2.0.0] - 2026-01-24

### 新增
- 🇺🇸 **美股分析支持**
  - 支持美股代码直接输入（如 `AAPL`, `TSLA`）
  - 使用 YFinance 作为美股数据源
- 🤖 **机器人命令交互** (PR #113)
  - 钉钉机器人支持 `/分析 股票代码` 命令触发分析
  - 支持 Stream 长连接模式
  - 支持选择精简报告或完整报告
- 🎮 **Discord 推送支持** (PR #124)
  - 支持 Discord Webhook 推送
  - 添加 Discord 环境变量到工作流

### 修复
- 🐳 修复 WebUI 在 Docker 中绑定 0.0.0.0 (fixed #118)
- 🔔 修复飞书长连接通知问题
- 🐛 修复 `analysis_delay` 未定义错误
- 🔧 启动时 config.py 检测通知渠道，修复已配置自定义渠道情况下仍然提示未配置问题

### 改进
- 🔧 优化 Tushare 优先级判断逻辑，提升封装性
- 🔧 修复 Tushare 优先级提升后仍排在 Efinance 之后的问题
- ⚙️ 配置 TUSHARE_TOKEN 时自动提升 Tushare 数据源优先级
- ⚙️ 实现 4 个用户反馈 issue (#112, #128, #38, #119)

## [1.6.0] - 2026-01-19

### 新增
- 🖥️ WebUI 管理界面及 API 支持（PR #72）
  - 全新 Web 架构：分层设计（Server/Router/Handler/Service）
  - 核心 API：支持 `/analysis` (触发分析), `/tasks` (查询进度), `/health` (健康检查)
  - 交互界面：支持页面直接输入代码并触发分析，实时展示进度
  - 运行模式：新增 `--webui-only` 模式，仅启动 Web 服务
  - 解决了 [#70](https://github.com/ZhuLinsen/daily_stock_analysis/issues/70) 的核心需求（提供触发分析的接口）
- ⚙️ GitHub Actions 配置灵活性增强（[#79](https://github.com/ZhuLinsen/daily_stock_analysis/issues/79)）
  - 支持从 Repository Variables 读取非敏感配置（如 STOCK_LIST, GEMINI_MODEL）
  - 保持对 Secrets 的向下兼容

### 修复
- 🐛 修复企业微信/飞书报告截断问题（[#73](https://github.com/ZhuLinsen/daily_stock_analysis/issues/73)）
  - 移除 notification.py 中不必要的长度硬截断逻辑
  - 依赖底层自动分片机制处理长消息
- 🐛 修复 GitHub Workflow 环境变量缺失（[#80](https://github.com/ZhuLinsen/daily_stock_analysis/issues/80)）
  - 修复 `CUSTOM_WEBHOOK_BEARER_TOKEN` 未正确传递到 Runner 的问题

## [1.5.0] - 2026-01-17

### 新增
- 📲 单股推送模式（[#55](https://github.com/ZhuLinsen/daily_stock_analysis/issues/55)）
  - 每分析完一只股票立即推送，不用等全部分析完
  - 命令行参数：`--single-notify`
  - 环境变量：`SINGLE_STOCK_NOTIFY=true`
- 🔐 自定义 Webhook Bearer Token 认证（[#51](https://github.com/ZhuLinsen/daily_stock_analysis/issues/51)）
  - 支持需要 Token 认证的 Webhook 端点
  - 环境变量：`CUSTOM_WEBHOOK_BEARER_TOKEN`

## [1.4.0] - 2026-01-17

### 新增
- 📱 Pushover 推送支持（PR #26）
  - 支持 iOS/Android 跨平台推送
  - 通过 `PUSHOVER_USER_KEY` 和 `PUSHOVER_API_TOKEN` 配置
- 🔍 博查搜索 API 集成（PR #27）
  - 中文搜索优化，支持 AI 摘要
  - 通过 `BOCHA_API_KEYS` 配置
- 📊 Efinance 数据源支持（PR #59）
  - 新增 efinance 作为数据源选项
- 🇭🇰 港股支持（PR #17）
  - 支持 5 位代码或 HK 前缀（如 `hk00700`、`hk1810`）

### 修复
- 🔧 飞书 Markdown 渲染优化（PR #34）
  - 使用交互卡片和格式化器修复渲染问题
- ♻️ 股票列表热重载（PR #42 修复）
  - 分析前自动重载 `STOCK_LIST` 配置
- 🐛 钉钉 Webhook 20KB 限制处理
  - 长消息自动分块发送，避免被截断
- 🔄 AkShare API 重试机制增强
  - 添加失败缓存，避免重复请求失败接口

### 改进
- 📝 README 精简优化
  - 高级配置移至 `docs/full-guide.md`


## [1.3.0] - 2026-01-12

### 新增
- 🔗 自定义 Webhook 支持
  - 支持任意 POST JSON 的 Webhook 端点
  - 自动识别钉钉、Discord、Slack、Bark 等常见服务格式
  - 支持配置多个 Webhook（逗号分隔）
  - 通过 `CUSTOM_WEBHOOK_URLS` 环境变量配置

### 修复
- 📝 企业微信长消息分批发送
  - 解决自选股过多时内容超过 4096 字符限制导致推送失败的问题
  - 智能按股票分析块分割，每批添加分页标记（如 1/3, 2/3）
  - 批次间隔 1 秒，避免触发频率限制

## [1.2.0] - 2026-01-11

### 新增
- 📢 多渠道推送支持
  - 企业微信 Webhook
  - 飞书 Webhook（新增）
  - 邮件 SMTP（新增）
  - 自动识别渠道类型，配置更简单

### 改进
- 统一使用 `NOTIFICATION_URL` 配置，兼容旧的 `WECHAT_WEBHOOK_URL`
- 邮件支持 Markdown 转 HTML 渲染

## [1.1.0] - 2026-01-11

### 新增
- 🤖 OpenAI 兼容 API 支持
  - 支持 DeepSeek、通义千问、Moonshot、智谱 GLM 等
  - Gemini 和 OpenAI 格式二选一
  - 自动降级重试机制

## [1.0.0] - 2026-01-10

### 新增
- 🎯 AI 决策仪表盘分析
  - 一句话核心结论
  - 精确买入/止损/目标点位
  - 检查清单（✅⚠️❌）
  - 分持仓建议（空仓者 vs 持仓者）
- 📊 大盘复盘功能
  - 主要指数行情
  - 涨跌统计
  - 板块涨跌榜
  - AI 生成复盘报告
- 🔍 多数据源支持
  - AkShare（主数据源，免费）
  - Tushare Pro
  - Baostock
  - YFinance
- 📰 新闻搜索服务
  - Tavily API
  - SerpAPI
- 💬 企业微信机器人推送
- ⏰ 定时任务调度
- 🐳 Docker 部署支持
- 🚀 GitHub Actions 零成本部署

### 技术特性
- Gemini AI 模型（gemini-3-flash-preview）
- 429 限流自动重试 + 模型切换
- 请求间延时防封禁
- 多 API Key 负载均衡
- SQLite 本地数据存储

---

[Unreleased]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.8.0...HEAD
[3.8.0]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.7.0...v3.8.0
[3.7.0]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.6.0...v3.7.0
[3.6.0]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.5.0...v3.6.0
[3.5.0]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.4.10...v3.5.0
[3.4.10]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.4.9...v3.4.10
[3.4.9]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.4.8...v3.4.9
[3.4.8]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.4.7...v3.4.8
[3.4.7]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.4.0...v3.4.7
[3.4.0]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.3.22...v3.4.0
[3.3.22]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.3.12...v3.3.22
[3.3.12]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.2.11...v3.3.12
[3.2.11]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.2.10...v3.2.11
[2.3.0]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.2.5...v2.3.0
[2.2.5]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.2.4...v2.2.5
[2.2.4]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.2.3...v2.2.4
[2.2.3]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.2.2...v2.2.3
[2.2.2]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.2.1...v2.2.2
[2.2.1]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.2.0...v2.2.1
[2.2.0]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.14...v2.2.0
[2.1.14]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.13...v2.1.14
[2.1.13]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.12...v2.1.13
[2.1.12]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.11...v2.1.12
[2.1.11]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.10...v2.1.11
[2.1.10]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.9...v2.1.10
[2.1.9]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.8...v2.1.9
[2.1.8]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.7...v2.1.8
[2.1.7]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.6...v2.1.7
[2.1.6]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.5...v2.1.6
[2.1.5]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.4...v2.1.5
[2.1.4]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.3...v2.1.4
[2.1.3]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.2...v2.1.3
[2.1.2]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.1...v2.1.2
[2.1.1]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.0...v2.1.1
[2.1.0]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v1.6.0...v2.0.0
[1.6.0]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/ZhuLinsen/daily_stock_analysis/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/ZhuLinsen/daily_stock_analysis/releases/tag/v1.0.0
