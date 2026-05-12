<div align="center">

# 📈 股票智能分析系统

[![GitHub stars](https://img.shields.io/github/stars/ZhuLinsen/daily_stock_analysis?style=social)](https://github.com/ZhuLinsen/daily_stock_analysis/stargazers)
[![CI](https://github.com/ZhuLinsen/daily_stock_analysis/actions/workflows/ci.yml/badge.svg)](https://github.com/ZhuLinsen/daily_stock_analysis/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-Ready-2088FF?logo=github-actions&logoColor=white)](https://github.com/features/actions)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/)

<p>
  <a href="https://trendshift.io/repositories/18527" target="_blank"><img src="https://trendshift.io/api/badge/repositories/18527" alt="ZhuLinsen%2Fdaily_stock_analysis | Trendshift" style="width: 250px; height: 55px;" width="250" height="55"/></a>
  <a href="https://hellogithub.com/repository/ZhuLinsen/daily_stock_analysis" target="_blank"><img src="https://api.hellogithub.com/v1/widgets/recommend.svg?rid=6daa16e405ce46ed97b4a57706aeb29f&claim_uid=pfiJMqhR9uvDGlT&theme=neutral" alt="Featured｜HelloGitHub" style="width: 250px; height: 54px;" width="250" height="54" /></a>
</p>

## Board Cache Warmup

## Five-Layer Backtest Updates

The five-layer backtest workflow now exposes a richer optimization and review path across backend APIs and the Web UI:

- Web backtest results are organized into four layers: system health, strategy comparison, judgment validation, and per-stock drilldown.
- The Web backtest entry now defaults to research mode anchored on the latest screening run, while date-range replay remains available as an explicit fallback mode.
- The Web header now separates `运行上下文` and `研究上下文`, so users can directly see the current screening/backtest anchor together with raw/evaluated/aggregatable/entry/observation/suppressed sample baselines.
- Run-level ranking-effectiveness output is now promoted into the main research narrative, clearly marked as whole-run evidence rather than current-strategy-only evidence, and shows which dimensions are effective or still inconclusive together with high/low tier sample counts and return gaps.
- Backtest recommendations are now surfaced in the research canvas as `优先动作 / 继续观察 / 仅展示`, helping users distinguish immediately actionable setup changes from hypothesis-level follow-up and display-only low-confidence groups.
- The research workbench now enters an explicit degraded-research state when a strategy slice becomes observation-only or loses key attribution/timing semantics, warning that the current view is suitable for observation study rather than buy-point conclusions.
- Group summaries now include `profit_factor`, `avg_holding_days`, `max_consecutive_losses`, `plan_execution_rate`, `stage_accuracy_rate`, and `system_grade`.
- Run and summary payloads now expose structured sample-baseline metadata so clients can distinguish raw samples, aggregatable samples, and suppressed samples with reasons.
- Screening-run backtests now recover the minimum analyzable `probe_entry` from five-layer snapshot fields when persisted `trade_stage` is too conservative, while `ma100_low123` samples with `confirmed_missing_breakout_bar_index` still remain in observation mode.
- Evaluation payloads now expose replay and diagnosis fields such as `signal_type`, `evaluation_mode`, `snapshot_source`, `replayed`, `factor_snapshot_json`, and `trade_plan_json`.
- A dedicated API endpoint is available at `/api/v1/five-layer-backtest/runs/{backtest_run_id}/ranking-effectiveness` for ranking-comparison analysis.

The formal trading decision flow starts at `L1-L5`. The older screening
service should be treated as an engineering preflight / strategy-matching
entry, not a standalone `L0`.
The screening runtime now always uses the unified five-layer main path, so
there is no environment toggle for enabling or disabling that pipeline.
The screening AI second-pass is now a dedicated structured review layer:

- it consumes `CandidateDecision` inputs instead of the legacy single-stock analyzer prompt
- it returns fixed JSON fields such as `trade_stage / entry_maturity / setup_type / risk_level`
- it cannot override upstream environment/theme hard constraints
- invalid JSON, timeout, or non-normalizable output fail closed back to rule results

To reduce the latency of OpenClaw hot-theme screening, the project now persists stock-to-board relationships locally:

- screening reads board memberships from the local database first
- newly discovered instruments sync board memberships during universe sync
- only missing symbols fall back to remote `get_belong_boards` lookups
- successful fallback results are written back for reuse in later runs

Optional prewarm commands:

```bash
python scripts/backfill_instrument_boards.py --dry-run
python scripts/backfill_instrument_boards.py --dry-run --stale-only
python scripts/backfill_instrument_boards.py --limit 200
python scripts/backfill_instrument_boards.py --stale-only
python scripts/backfill_instrument_boards.py --codes 600519,300750
```

`--stale-only` only processes symbols that do not yet have local board memberships, which is the recommended mode for continuing an interrupted backfill or patching historical gaps.

Optional scheduled prewarm after the A-share close:

- `BOARD_SYNC_SCHEDULE_ENABLED=true`
- `BOARD_SYNC_SCHEDULE_TIME=15:05`
- `BOARD_SYNC_RUN_IMMEDIATELY=false`

The scheduled prewarm is now best treated as an optional fallback. The primary low-cost path is incremental board sync when `instrument_master` gains new stocks.

## K线完整性治理

项目现在支持独立的 K 线完整性治理链路，用于在日线同步后自动校验缺口并执行补偿：

- 日常治理任务默认建议在 A 股收盘后 `17:00` 执行，顺序固定为 `sync -> audit -> repair -> re-audit`
- 下游只应消费 `pass_status=passed` 的交易日；`not_passed` 表示该交易日仍存在非豁免缺口，筛选链路会 fail-close
- 手工 `修复缺口` 只会处理最新 `pass_status=passed` 交易日及以前的缺口，避免在当日未收盘或目标日未通过审计时把全市场临时缺口逐只修复
- 无法稳定拉取的股票缺口会先进入 `candidate_skip`，只有经过人工确认后才会升级为 `approved_skip`
- 深度审计任务是独立 job，用于长窗口复核历史缺口与 `candidate_skip` 恢复情况，不会替代日常治理
- 智能选股页提供 `回填至该日` 操作，可按所选交易日快速批量回填本地全市场日线数据，并在完成后触发目标日治理审计
- Web 新增“数据健康”控制台，可查看本地股票池口径、K 线覆盖起止日期、日期覆盖率、审计通过日、K 线缺口和后台治理任务，并通过 `/api/v1/data-health/*` 提交回填、重试、修复、重新审计等操作

推荐环境变量：

- `KLINE_GOVERNANCE_ENABLED=true`
- `KLINE_GOVERNANCE_SCHEDULE_TIME=17:00`
- `KLINE_GOVERNANCE_RUN_IMMEDIATELY=false`
- `KLINE_DEEP_AUDIT_SCHEDULE_ENABLED=false`
- `KLINE_DEEP_AUDIT_SCHEDULE_TIME=17:00`

手工治理命令：

```bash
python scripts/audit_kline_completeness.py --trade-date 2026-04-17
python scripts/audit_kline_completeness.py --trade-date 2026-04-17 --repair
python scripts/audit_kline_completeness.py --trade-date 2026-04-17 --dry-run
python scripts/fast_backfill.py --to-date 2026-04-17
python scripts/approve_kline_skip.py --market cn --trade-date 2026-04-17 --approved-by ops-user --reason-type manual_review
python scripts/approve_kline_skip.py --market cn --code 000001 --from-date 2026-04-10 --to-date 2026-04-17 --approved-by ops-user --reason-type manual_review
```

其中：

- `audit_kline_completeness.py` 用于手工触发单次审计或完整补偿
- `fast_backfill.py --to-date` 用于按交易日批量回填本地全市场日线数据到目标日期
- `approve_kline_skip.py` 用于把人工确认无法拉取的 `candidate_skip` 升级为 `approved_skip`
- 一旦 `approved_skip` 对应缺口在后续治理中连续恢复成功，系统会自动把它回收为健康状态
- `/api/v1/data-health/summary|coverage|gaps|operations|tasks` 用于 Web 控制台读取本地数据健康状态并轮询后台治理任务

> 🤖 基于 AI 大模型的 A股/港股/美股自选股智能分析系统，每日自动分析并推送「决策仪表盘」到企业微信/飞书/Telegram/Discord/邮箱

[**功能特性**](#-功能特性) · [**快速开始**](#-快速开始) · [**推送效果**](#-推送效果) · [**完整指南**](docs/full-guide.md) · [**常见问题**](docs/FAQ.md) · [**更新日志**](docs/CHANGELOG.md) · [**选股模块差距分析**](docs/dsa-screening-module-gap-analysis-2026-04-10.md) · [**选股五层 Debug 手册**](docs/screening-five-layer-debug-guide.md)

简体中文 | [English](docs/README_EN.md) | [繁體中文](docs/README_CHT.md)

</div>

## 💖 赞助商 (Sponsors)
<div align="center">
  <a href="https://serpapi.com/baidu-search-api?utm_source=github_daily_stock_analysis" target="_blank">
    <img src="./sources/serpapi_banner_zh.png" alt="轻松抓取搜索引擎上的实时金融新闻数据 - SerpApi" height="160">
  </a>
</div>
<br>


## ✨ 功能特性

| 模块 | 功能 | 说明 |
|------|------|------|
| AI | 决策仪表盘 | 一句话核心结论 + 精确买卖点位 + 操作检查清单 |
| 分析 | 多维度分析 | 技术面（盘中实时 MA/多头排列）+ 筹码分布 + 舆情情报 + 实时行情 |
| 市场 | 全球市场 | 支持 A股、港股、美股及美股指数（SPX、DJI、IXIC 等） |
| 基本面 | 结构化聚合 | 新增 `fundamental_context`（valuation/growth/earnings/institution/capital_flow/dragon_tiger/boards，其中 `earnings.data` 新增 `financial_report` 与 `dividend`，`boards` 表示板块涨跌榜），主链路 fail-open 降级 |
| 策略 | 市场策略系统 | 内置 A股「三段式复盘策略」与美股「Regime Strategy」，输出进攻/均衡/防守或 risk-on/neutral/risk-off 计划，并附“仅供参考，不构成投资建议”提示 |
| 复盘 | 大盘复盘 | 每日市场概览、板块涨跌；支持 cn(A股)/us(美股)/both(两者) 切换 |
| 智能导入 | 多源导入 | 支持图片、CSV/Excel 文件、剪贴板粘贴；Vision LLM 提取代码+名称；置信度分层确认；名称→代码解析（本地+拼音+AkShare） |
| 历史记录 | 批量管理 | 支持多选、全选及批量删除历史分析记录，优化管理效率与 UI/UX 体验 |
| 回测 | AI 回测验证 | 自动评估历史分析准确率，方向胜率、止盈止损命中率 |
| **Agent 问股** | **策略对话** | **多轮策略问答，支持均线金叉/缠论/波浪等 11 种内置策略，Web/Bot/API 全链路** |
| 推送 | 多渠道通知 | 企业微信、飞书、Telegram、Discord、钉钉、邮件、Pushover |
| 自动化 | 定时运行 | GitHub Actions 定时执行，无需服务器 |

### 智能选股页

- 选股策略默认不选中，需要手动勾选后开始筛选
- 候选上限默认 5，AI 二次分析默认前 2 个候选
- 运行状态面板会自动回填最近一次任务；候选结果现在统一围绕 `trade_stage + setup_type + trade_plan` 展示，命中策略退回审计证据层
- 首筛阶段不再按 `avg_amount` 做硬过滤，低成交额股票会继续进入策略匹配，再由后续题材/候选池/交易阶段链路收敛质量
- OpenClaw 热点题材触发的 `extreme_strength_combo` 现在会固定走策略引擎，按请求 `trade_date` 执行，并结合个股所属板块做热点题材硬门槛匹配
- L2 本地热点板块识别已从“绝对综合分 + 固定阈值”切换为“市场相对排名驱动”：`hot/warm` 由 `board_strength_score / board_strength_rank / percentile` 决定，`stage` 与 `quality_flags` 仅负责生命周期解释和主线优先级排序
- `ma100_low123_combined` 只把最新 K 线位于 P3-P2 之间，或最新 K 线刚突破 P2 的低位 123 结构视为最佳入场区；突破后已走远的候选会被剔除，刚突破 P2 会通过 `ma100_low123_entry_timing_score` 获得额外加分
- 候选详情中的 `phase_results` 已统一为正式五阶段键，并新增 `phase_explanations` 供前端直接展示阶段解释；OpenClaw `options.candidate_limit/ai_top_k` 也会在接口层做准确校验
- OpenClaw `extreme_strength_combo` 候选详情现会把原始规则表达式转成中文展示，并将命中的技术形态单独收敛到独立板块；因子快照中的对象/数组值也会展开为可读文本，避免出现 `[object Object]`
- `extreme_strength_combo` 已明确收敛为“热点题材股票池 / 排序器”角色：候选详情现拆分出 `题材池分 / 龙头分 / 入场信号分 / 时机惩罚` 四桶，并展示 `阶段标签`（`池子层 / 仅观察 / 突破当日 / 回踩确认 / 已走远·勿追`）与 `主信号类型`（`低位结构入场 / 动量突破 / 动量追涨`）；阶段 3 优先展示 `primary_signal`，让结构型右侧买点不再被事件型强势覆盖；当阶段为 `已走远·勿追` 时 L3 候选池会把 `leader_pool` 降级为 `focus_list`，`phase4_entry_readiness` 也会硬闸关闭，避免扎堆追高
- 候选详情 `入选原因` 改由阶段标签主导（如 `突破当日 · 开盘半小时内涨停` / `仅观察 · 刚突破MA100` / `已走远·勿追`），旧描述词退化为可选上下文后缀；阶段 5 止损参考现会按 `primary_signal` 引用真实子信号的止损依据（`123结构低点 / 底背离临界区 / 缺口下沿 / 涨停前关键位`），并通过新增的 `stop_loss_basis` 字段透明标注来源，`已走远·勿追` 阶段仓位建议强制为 `不建议入场`
- L2 题材地位 / 候选详情抽屉新增 `去重净分` 行：显式展示 `leader_score` 与其它桶之间可观测的重复加权估算值（`leader_double_count`）以及去除该部分后的净总分（`extreme_strength_score_deduplicated`）。`extreme_strength_score` 本身保持不变，所有 `>=50 / >=60 / >=80` 阈值契约不受影响，仅作为审计/解释视图存在
- 筛选结果内部与落库统一为 `CandidateDecision`，补齐 `setup_freshness`、`theme_duration/trade_theme_stage`、`trade_plan.execution_note` 等字段，API 与通知模板同步消费这套结构
- `/api/v1/screening/strategies` 现在会返回 `system_role / strategy_family / applicable_market / applicable_theme / setup_type` 元数据，便于前端和审计面板按策略角色解释结果
- 筛选任务响应现在会把 `local_theme_pipeline / external_theme_pipeline / fused_theme_pipeline` 作为一等字段返回；运行状态面板也会直接展示融合后的热点题材摘要，不再需要从 `config_snapshot` 手工解析
- 运行详情页新增完整的题材管道详情面板，默认展示 `fused_theme_pipeline` 融合结果，并可折叠查看 `local_theme_pipeline` 与 `external_theme_pipeline` 的逐题材明细、原始题材名、来源与规范化信息
- 如果选择的日期不是交易日，系统会自动回退到最近一个交易日继续选股，并在结果中保留提示
- 如果选择的是当日交易日，需等待北京时间 15:00 后才允许开始选股，避免使用未收盘的日线数据
- 如果上一次选股任务因进程中断而长期停留在非终态，系统会在后续查询和重试时自动回收该幽灵任务并恢复为可重跑状态
- 历史任务列表支持逐条删除，可直接移除卡住或无用的选股任务及其候选结果

> 历史报告详情会优先展示 AI 返回的原始「狙击点位」文本，避免区间价、条件说明等复杂内容在历史回看时被压缩成单个数字。

> Web 管理认证支持运行时开关；如果系统中已保留管理员密码，重新开启认证时必须提供当前密码，避免在认证关闭窗口内直接获取新的管理员会话。
> 多进程/多 worker 部署时，认证开关仅在当前进程即时生效；需重启或滚动重启全部 worker 以统一状态。

> 持仓管理补充说明：卖出录入现在会在写入前校验可用持仓，超售会直接拒绝；如果历史里误录了交易 / 资金流水 / 公司行为，可在 Web `/portfolio` 页的事件列表中直接删除后恢复快照。

### 技术栈与数据来源

| 类型 | 支持 |
|------|------|
| AI 模型 | [AIHubMix](https://aihubmix.com/?aff=CfMq)、Gemini、OpenAI 兼容、DeepSeek、通义千问、Claude 等（统一通过 [LiteLLM](https://github.com/BerriAI/litellm) 调用，支持多 Key 负载均衡）|
| 行情数据 | AkShare、Tushare、Pytdx、Baostock、YFinance |
| 新闻搜索 | Tavily、SerpAPI、Bocha、Brave、MiniMax |
| 社交舆情 | [Stock Sentiment API](https://api.adanos.org/docs)（Reddit / X / Polymarket，仅美股，可选） |

> 注：美股历史数据与实时行情统一使用 YFinance，确保复权一致性

### 内置交易纪律

| 规则 | 说明 |
|------|------|
| 严禁追高 | 乖离率超阈值（默认 5%，可配置）自动提示风险；强势趋势股自动放宽 |
| 趋势交易 | MA5 > MA10 > MA20 多头排列 |
| 精确点位 | 买入价、止损价、目标价 |
| 检查清单 | 每项条件以「满足 / 注意 / 不满足」标记 |
| 新闻时效 | 可配置新闻最大时效（默认 3 天），避免使用过时信息 |

## 🚀 快速开始

### 方式一：GitHub Actions（推荐）

> 5 分钟完成部署，零成本，无需服务器。


#### 1. Fork 本仓库

点击右上角 `Fork` 按钮（顺便点个 Star⭐ 支持一下）

#### 2. 配置 Secrets

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

**AI 模型配置（至少配置一个）**

> 详细配置说明见 [LLM 配置指南](docs/LLM_CONFIG_GUIDE.md)（三层配置、渠道模式、YAML高级配置、Vision、Agent、排错），GitHub Actions用户也可以实现YAML高级配置。进阶用户可配置 `LITELLM_MODEL`、`LITELLM_FALLBACK_MODELS` 或 `LLM_CHANNELS` 多渠道模式。

> 现在推荐把多模型配置统一写成 `LLM_CHANNELS + LLM_<NAME>_PROTOCOL/BASE_URL/API_KEY/MODELS/ENABLED`。Web 设置页和 `.env` 使用同一套字段，便于相互切换。

> 💡 **推荐 [AIHubMix](https://aihubmix.com/?aff=CfMq)**：一个 Key 即可使用 Gemini、GPT、Claude、DeepSeek 等全球主流模型，无需科学上网，含免费模型（glm-5、gpt-4o-free 等），付费模型高稳定性无限并发。本项目可享 **10% 充值优惠**。

| Secret 名称 | 说明 | 必填 |
|------------|------|:----:|
| `AIHUBMIX_KEY` | [AIHubMix](https://aihubmix.com/?aff=CfMq) API Key，一 Key 切换使用全系模型，免费模型可用 | 可选 |
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/) 获取免费 Key（需科学上网） | 可选 |
| `ANTHROPIC_API_KEY` | [Anthropic Claude](https://console.anthropic.com/) API Key | 可选 |
| `ANTHROPIC_MODEL` | Claude 模型（如 `claude-3-5-sonnet-20241022`） | 可选 |
| `OPENAI_API_KEY` | OpenAI 兼容 API Key（支持 DeepSeek、通义千问等） | 可选 |
| `OPENAI_BASE_URL` | OpenAI 兼容 API 地址（如 `https://api.deepseek.com/v1`） | 可选 |
| `OPENAI_MODEL` | 模型名称（如 `gemini-3.1-pro-preview`、`gemini-3-flash-preview`、`gpt-5.2`） | 可选 |
| `OPENAI_VISION_MODEL` | 图片识别专用模型（部分第三方模型不支持图像；不填则用 `OPENAI_MODEL`） | 可选 |

> 注：AI 优先级 Gemini > Anthropic > OpenAI（含 AIHubmix），至少配置一个。`AIHUBMIX_KEY` 无需配置 `OPENAI_BASE_URL`，系统自动适配。图片识别需 Vision 能力模型。DeepSeek 思考模式（deepseek-reasoner、deepseek-r1、qwq、deepseek-chat）按模型名自动识别，无需额外配置。

<details>
<summary><b>通知渠道配置</b>（点击展开，至少配置一个）</summary>


| Secret 名称 | 说明 | 必填 |
|------------|------|:----:|
| `WECHAT_WEBHOOK_URL` | 企业微信 Webhook URL | 可选 |
| `FEISHU_WEBHOOK_URL` | 飞书 Webhook URL | 可选 |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token（@BotFather 获取） | 可选 |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID | 可选 |
| `TELEGRAM_MESSAGE_THREAD_ID` | Telegram Topic ID (用于发送到子话题) | 可选 |
| `DISCORD_WEBHOOK_URL` | Discord Webhook URL | 可选 |
| `DISCORD_BOT_TOKEN` | Discord Bot Token（与 Webhook 二选一） | 可选 |
| `DISCORD_MAIN_CHANNEL_ID` | Discord Channel ID（使用 Bot 时需要） | 可选 |
| `EMAIL_SENDER` | 发件人邮箱（如 `xxx@qq.com`） | 可选 |
| `EMAIL_PASSWORD` | 邮箱授权码（非登录密码） | 可选 |
| `EMAIL_RECEIVERS` | 收件人邮箱（多个用逗号分隔，留空则发给自己） | 可选 |
| `EMAIL_SENDER_NAME` | 邮件发件人显示名称（默认：daily_stock_analysis股票分析助手，支持中文并自动编码邮件头） | 可选 |
| `STOCK_GROUP_N` / `EMAIL_GROUP_N` | 股票分组发往不同邮箱（如 `STOCK_GROUP_1=600519,300750` `EMAIL_GROUP_1=user1@example.com`） | 可选 |
| `PUSHPLUS_TOKEN` | PushPlus Token（[获取地址](https://www.pushplus.plus)，国内推送服务） | 可选 |
| `PUSHPLUS_TOPIC` | PushPlus 群组编码（一对多推送，配置后消息推送给群组所有订阅用户） | 可选 |
| `SERVERCHAN3_SENDKEY` | Server酱³ Sendkey（[获取地址](https://sc3.ft07.com/)，手机APP推送服务） | 可选 |
| `CUSTOM_WEBHOOK_URLS` | 自定义 Webhook（支持钉钉等，多个用逗号分隔） | 可选 |
| `CUSTOM_WEBHOOK_BEARER_TOKEN` | 自定义 Webhook 的 Bearer Token（用于需要认证的 Webhook） | 可选 |
| `WEBHOOK_VERIFY_SSL` | Webhook HTTPS 证书校验（默认 true）。设为 false 可支持自签名证书。警告：关闭有严重安全风险，仅限可信内网 | 可选 |
| `SCHEDULE_RUN_IMMEDIATELY` | 定时模式启动时是否立即执行一次分析 | 可选 |
| `RUN_IMMEDIATELY` | 非定时模式启动时是否立即执行一次分析 | 可选 |
| `SINGLE_STOCK_NOTIFY` | 单股推送模式：设为 `true` 则每分析完一只股票立即推送 | 可选 |
| `REPORT_TYPE` | 报告类型：`simple`(精简)、`full`(完整)、`brief`(3-5句概括)，Docker环境推荐设为 `full` | 可选 |
| `REPORT_SUMMARY_ONLY` | 仅分析结果摘要：设为 `true` 时只推送汇总，不含个股详情 | 可选 |
| `REPORT_TEMPLATES_DIR` | Jinja2 模板目录（相对项目根，默认 `templates`） | 可选 |
| `REPORT_RENDERER_ENABLED` | 启用 Jinja2 模板渲染（默认 `false`，保证零回归） | 可选 |
| `REPORT_INTEGRITY_ENABLED` | 启用报告完整性校验，缺失必填字段时重试或占位补全（默认 `true`） | 可选 |
| `REPORT_INTEGRITY_RETRY` | 完整性校验重试次数（默认 `1`，`0` 表示仅占位不重试） | 可选 |
| `REPORT_HISTORY_COMPARE_N` | 历史信号对比条数，`0` 关闭（默认），`>0` 启用 | 可选 |
| `ANALYSIS_DELAY` | 个股分析和大盘分析之间的延迟（秒），避免API限流，如 `10` | 可选 |
| `MAX_WORKERS` | 异步分析任务队列并发线程数（默认 `3`）；保存后队列空闲时自动应用，繁忙时延后生效 | 可选 |
| `MERGE_EMAIL_NOTIFICATION` | 个股与大盘复盘合并推送（默认 false），减少邮件数量 | 可选 |
| `MARKDOWN_TO_IMAGE_CHANNELS` | 将 Markdown 转为图片发送的渠道（逗号分隔）：`telegram,wechat,custom,email` | 可选 |
| `MARKDOWN_TO_IMAGE_MAX_CHARS` | 超过此长度不转图片，避免超大图片（默认 `15000`） | 可选 |
| `MD2IMG_ENGINE` | 转图引擎：`wkhtmltoimage`（默认）或 `markdown-to-file`（emoji 更好） | 可选 |

> 至少配置一个渠道，配置多个则同时推送。图片发送与引擎安装细节请参考 [完整指南](docs/full-guide.md)

</details>

**其他配置**

| Secret 名称 | 说明 | 必填 |
|------------|------|:----:|
| `STOCK_LIST` | 自选股代码，如 `600519,hk00700,AAPL,TSLA` | ✅ |
| `TAVILY_API_KEYS` | [Tavily](https://tavily.com/) 搜索 API（新闻搜索） | 推荐 |
| `MINIMAX_API_KEYS` | [MiniMax](https://platform.minimaxi.com/) Coding Plan Web Search（结构化搜索结果） | 可选 |
| `SERPAPI_API_KEYS` | [SerpAPI](https://serpapi.com/baidu-search-api?utm_source=github_daily_stock_analysis) 全渠道搜索 | 可选 |
| `BOCHA_API_KEYS` | [博查搜索](https://open.bocha.cn/) Web Search API（中文搜索优化，支持AI摘要，多个key用逗号分隔） | 可选 |
| `BRAVE_API_KEYS` | [Brave Search](https://brave.com/search/api/) API（隐私优先，美股优化，多个key用逗号分隔） | 可选 |
| `SEARXNG_BASE_URLS` | SearXNG 自建实例（无配额兜底，需在 settings.yml 启用 format: json） | 可选 |
| `SOCIAL_SENTIMENT_API_KEY` | [Stock Sentiment API](https://api.adanos.org/docs)（Reddit/X/Polymarket 社交舆情，仅美股） | 可选 |
| `SOCIAL_SENTIMENT_API_URL` | 自定义社交舆情 API 地址（默认 `https://api.adanos.org`） | 可选 |
| `TUSHARE_TOKEN` | [Tushare Pro](https://tushare.pro/weborder/#/login?reg=834638 ) Token | 可选 |
| `PREFETCH_REALTIME_QUOTES` | 实时行情预取开关：设为 `false` 可禁用全市场预取（默认 `true`） | 可选 |
| `WECHAT_MSG_TYPE` | 企微消息类型，默认 markdown，支持配置 text 类型，发送纯 markdown 文本 | 可选 |
| `NEWS_STRATEGY_PROFILE` | 新闻策略窗口档位：`ultra_short`(1天) / `short`(3天) / `medium`(7天) / `long`(30天)，默认 `short` | 可选 |
| `NEWS_MAX_AGE_DAYS` | 新闻最大时效上限（天），默认 3；实际窗口 `effective_days = min(profile_days, NEWS_MAX_AGE_DAYS)`，例如 `ultra_short(1)` + `7` 仍为 `1` 天 | 可选 |
| `BIAS_THRESHOLD` | 乖离率阈值（%），默认 5.0，超过提示不追高；强势趋势股自动放宽 | 可选 |
| `AGENT_MODE` | 开启 Agent 策略问股模式（`true`/`false`，默认 false） | 可选 |
| `AGENT_SKILLS` | 激活的策略（逗号分隔），`all` 启用全部 11 个；不配置时默认 4 个，详见 `.env.example` | 可选 |
| `AGENT_MAX_STEPS` | Agent 最大推理步数（默认 10） | 可选 |
| `AGENT_STRATEGY_DIR` | 自定义策略目录（默认内置 `strategies/`） | 可选 |
| `TRADING_DAY_CHECK_ENABLED` | 交易日检查（默认 `true`）：非交易日跳过执行；设为 `false` 或使用 `--force-run` 强制执行 | 可选 |
| `ENABLE_CHIP_DISTRIBUTION` | 启用筹码分布（Actions 默认 false；需筹码数据时在 Variables 中设为 true，接口可能不稳定） | 可选 |
| `ENABLE_FUNDAMENTAL_PIPELINE` | 基本面聚合总开关；关闭时保持主流程不变 | 可选 |
| `FUNDAMENTAL_STAGE_TIMEOUT_SECONDS` | 基本面阶段总预算（秒） | 可选 |
| `FUNDAMENTAL_FETCH_TIMEOUT_SECONDS` | 单能力源调用超时（秒） | 可选 |
| `FUNDAMENTAL_RETRY_MAX` | 基本面能力重试次数（包含首次） | 可选 |
| `FUNDAMENTAL_CACHE_TTL_SECONDS` | 基本面缓存 TTL（秒） | 可选 |
| `FUNDAMENTAL_CACHE_MAX_ENTRIES` | 基本面缓存最大条目数（避免长时间运行内存增长） | 可选 |

> 基本面超时语义（P0）：
> - 当前采用 `best-effort` 软超时（fail-open），超时会立即降级并继续主流程；
> - 不承诺严格硬中断第三方调用线程，因此 `P95 <= 1.5s` 是阶段目标而非硬 SLA；
> - 若业务需要硬 SLA，可在后续阶段升级为“子进程隔离 + kill”的硬超时方案。
> - 字段契约：
>   - `fundamental_context.boards.data` = `sector_rankings`（板块涨跌榜，结构 `{top, bottom}`）；
>   - `fundamental_context.earnings.data.financial_report` = 财报摘要（报告期、营收、归母净利润、经营现金流、ROE）；
>   - `fundamental_context.earnings.data.dividend` = 分红指标（仅现金分红税前口径，含 `events`、`ttm_cash_dividend_per_share`、`ttm_dividend_yield_pct`）；
>   - `get_stock_info.belong_boards` = 个股所属板块列表；
>   - `get_stock_info.boards` 为兼容别名，值与 `belong_boards` 相同（未来仅在大版本考虑移除）；
>   - `get_stock_info.sector_rankings` 与 `fundamental_context.boards.data` 保持一致。
> - 板块涨跌榜采用固定回退顺序：`AkShare(EM->Sina) -> Tushare -> efinance`。

#### 3. 启用 Actions

`Actions` 标签 → `I understand my workflows, go ahead and enable them`

#### 4. 手动测试

`Actions` → `每日股票分析` → `Run workflow` → `Run workflow`

#### 完成

默认每个**工作日 18:00（北京时间）**自动执行，也可手动触发。默认非交易日（含 A/H/US 节假日）不执行。

> 💡 **关于跳过交易日检查的两种机制：**
> | 机制 | 配置方式 | 生效范围 | 适用场景 |
> |------|----------|----------|----------|
> | `TRADING_DAY_CHECK_ENABLED=false` | 环境变量/Secrets | 全局、长期有效 | 测试环境、长期关闭检查 |
> | `force_run` (UI 勾选) | Actions 手动触发时选择 | 单次运行 | 临时在非交易日执行一次 |
>
> - **环境变量方式**：在 `.env` 或 GitHub Secrets 中设置，影响所有运行方式（定时触发、手动触发、本地运行）
> - **UI 勾选方式**：仅在 GitHub Actions 手动触发时可见，不影响定时任务，适合临时需求

### 方式二：本地运行 / Docker 部署

```bash
# 克隆项目
git clone https://github.com/ZhuLinsen/daily_stock_analysis.git && cd daily_stock_analysis

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env && vim .env

# 运行分析
python main.py
```

如果你不用 Web，推荐直接在 `.env` 里按条写渠道：

```env
LLM_CHANNELS=primary
LLM_PRIMARY_PROTOCOL=openai
LLM_PRIMARY_BASE_URL=https://api.deepseek.com/v1
LLM_PRIMARY_API_KEY=sk-xxxxxxxx
LLM_PRIMARY_MODELS=deepseek-chat
LITELLM_MODEL=openai/deepseek-chat
```

保存后也可以在 Web 设置页继续编辑同一组字段；不会要求额外配置文件。

如果同时启用了 `LITELLM_CONFIG`，YAML 仍然是运行时主模型 / fallback / Vision 的唯一来源；渠道编辑器只保存渠道条目，不会覆盖 YAML 的运行时选择。

> Docker 部署、定时任务配置请参考 [完整指南](docs/full-guide.md)
> 桌面客户端打包请参考 [桌面端打包说明](docs/desktop-package.md)

## 📱 推送效果

### 决策仪表盘
```
🎯 2026-02-08 决策仪表盘
共分析3只股票 | 🟢买入:0 🟡观望:2 🔴卖出:1

📊 分析结果摘要
⚪ 中钨高新(000657): 观望 | 评分 65 | 看多
⚪ 永鼎股份(600105): 观望 | 评分 48 | 震荡
🟡 新莱应材(300260): 卖出 | 评分 35 | 看空

⚪ 中钨高新 (000657)
📰 重要信息速览
💭 舆情情绪: 市场关注其AI属性与业绩高增长，情绪偏积极，但需消化短期获利盘和主力流出压力。
📊 业绩预期: 基于舆情信息，公司2025年前三季度业绩同比大幅增长，基本面强劲，为股价提供支撑。

🚨 风险警报:

风险点1：2月5日主力资金大幅净卖出3.63亿元，需警惕短期抛压。
风险点2：筹码集中度高达35.15%，表明筹码分散，拉升阻力可能较大。
风险点3：舆情中提及公司历史违规记录及重组相关风险提示，需保持关注。
✨ 利好催化:

利好1：公司被市场定位为AI服务器HDI核心供应商，受益于AI产业发展。
利好2：2025年前三季度扣非净利润同比暴涨407.52%，业绩表现强劲。
📢 最新动态: 【最新消息】舆情显示公司是AI PCB微钻领域龙头，深度绑定全球头部PCB/载板厂。2月5日主力资金净卖出3.63亿元，需关注后续资金流向。

---
生成时间: 18:00
```

### 大盘复盘
```
🎯 2026-01-10 大盘复盘

📊 主要指数
- 上证指数: 3250.12 (🟢+0.85%)
- 深证成指: 10521.36 (🟢+1.02%)
- 创业板指: 2156.78 (🟢+1.35%)

📈 市场概况
上涨: 3920 | 下跌: 1349 | 涨停: 155 | 跌停: 3

🔥 板块表现
领涨: 互联网服务、文化传媒、小金属
领跌: 保险、航空机场、光伏设备
```
## ⚙️ 配置说明

> 📖 完整环境变量、定时任务配置请参考 [完整配置指南](docs/full-guide.md)
> 邮件通知当前基于 SMTP 授权码/基础认证；若 Outlook / Exchange 账号或租户强制 OAuth2，当前版本暂不支持。


## 🖥️ Web 界面

![img.png](sources/fastapi_server.png)

包含完整的配置管理、任务监控和手动分析功能。

**可选密码保护**：在 `.env` 中设置 `ADMIN_AUTH_ENABLED=true` 可启用 Web 登录，首次访问在网页设置初始密码，保护 Settings 中的 API 密钥等敏感配置。系统设置现支持运行时开启或关闭认证；关闭认证不会删除已保存密码，后续可直接重新启用。认证开启时，`POST /api/v1/auth/logout` 也需要有效会话；如果会话已经过期，前端会直接回到登录页。详见 [完整指南](docs/full-guide.md)。

### 智能导入

在 **设置 → 基础设置** 中找到「智能导入」区块，支持三种方式添加自选股：

1. **图片**：拖拽或选择自选股截图（如 APP 持仓页、行情列表），Vision AI 自动识别代码+名称，并给出置信度
2. **文件**：上传 CSV 或 Excel (.xlsx)，自动解析代码/名称列
3. **粘贴**：从 Excel 或表格复制后粘贴，点击「解析」即可

**预览与合并**：高置信度默认勾选，中/低置信度需手动勾选；支持按代码去重、清空、全选；仅合并已勾选且解析成功的项。

**配置与限制**：
- 图片需配置 Vision API（`GEMINI_API_KEY`、`ANTHROPIC_API_KEY` 或 `OPENAI_API_KEY` 至少一个）
- 图片：JPG/PNG/WebP/GIF，≤5MB；文件：≤2MB；粘贴文本：≤100KB

**API**：`POST /api/v1/stocks/extract-from-image`（图片）、`POST /api/v1/stocks/parse-import`（文件/粘贴）。详见 [完整指南](docs/full-guide.md)。

**LLM 用量查询**：`GET /api/v1/usage/summary?period=today|month|all`，返回按调用类型和模型分组的 token 消耗汇总（`total_calls`、`total_tokens`、`by_call_type`、`by_model`）。

**分析 API 说明**：`POST /api/v1/analysis/analyze` 在 `async_mode=false` 时仅支持单只股票；批量 `stock_codes` 需要 `async_mode=true`。异步 `202` 响应对单股返回 `task_id`，对批量返回 `accepted` / `duplicates` 汇总结构；空白股票代码会在服务端过滤，若过滤后为空则返回 `400`。未知 `/api` 路径（含 `/api` 本身）返回 JSON `404`，不再回退到前端页面。详见 [API 规范](docs/architecture/api_spec.json)。

### 历史报告详情

在首页历史记录中选择一条分析记录后，点击「详细报告」按钮可在右侧抽屉中查看与推送通知格式一致的完整 Markdown 分析报告，包含舆情情报、核心结论、数据透视、作战计划等完整内容。

### 🤖 Agent 策略问股

在 `.env` 中设置 `AGENT_MODE=true` 后启动服务，访问 `/chat` 页面即可开始多轮策略问答。

- **选择策略**：均线金叉、缠论、波浪理论、多头趋势等 11 种内置策略
- **自然语言提问**：如「用缠论分析 600519」，Agent 自动调用实时行情、K线、技术指标、新闻等工具
- **流式进度反馈**：实时展示 AI 思考路径（行情获取 → 技术分析 → 新闻搜索 → 生成结论）
- **多轮对话**：支持追问上下文，会话历史持久化保存
- **导出与发送**：可将会话导出为 .md 文件，或发送到已配置的通知渠道
- **后台执行**：切换页面不中断分析，完成时 Dock 问股图标显示角标
- **Bot 命令**：`/ask` 策略分析（支持多股对比）、`/chat` 自由对话
- **自定义策略**：在 `strategies/` 目录下新建 YAML 文件即可添加策略，无需写代码
- **多 Agent 架构**（实验性）：设置 `AGENT_ARCH=multi` 启用 Technical → Intel → Risk → Strategy → Decision 多 Agent 级联编排，通过 `AGENT_ORCHESTRATOR_MODE` 控制深度（quick/standard/full/strategy）。超时或中间阶段 JSON 解析失败时，系统会优先保留已完成阶段结果并降级生成最小可用仪表盘，避免整份报告直接退回默认占位。详见 [完整配置指南](docs/full-guide.md)

> **注意**：配置了任意 AI API Key 后，Agent 对话功能自动可用，无需手动设置 `AGENT_MODE=true`。如需显式关闭可设置 `AGENT_MODE=false`。每次对话会产生 LLM API 调用费用。若你手动修改了 `.env` 中的模型主备配置（如 `LITELLM_MODEL` / `LITELLM_FALLBACK_MODELS` / `LLM_CHANNELS`），需要重启服务或触发配置重载后，新进程才会按新模型生效。

### 启动方式

1. **启动服务**（默认会自动编译前端）
   ```bash
   python main.py --webui       # 启动 Web 界面 + 执行定时分析
   python main.py --webui-only  # 仅启动 Web 界面
   ```
   启动时会在 `apps/dsa-web` 自动执行 `npm install && npm run build`。
   如需关闭自动构建，设置 `WEBUI_AUTO_BUILD=false`，并改为手动执行：
   ```bash
   cd ./apps/dsa-web
   npm install && npm run build
   cd ../..
   ```

访问 `http://127.0.0.1:8000` 即可使用。

> 在云服务器上部署后，不知道浏览器该输入什么地址？请看 [云服务器 Web 界面访问指南](docs/deploy-webui-cloud.md)。

> 也可以使用 `python main.py --serve` (等效命令)

## 🗺️ Roadmap

查看已支持的功能和未来规划：[更新日志](docs/CHANGELOG.md)

> 有建议？欢迎 [提交 Issue](https://github.com/ZhuLinsen/daily_stock_analysis/issues)

> ⚠️ **UI 调整提示**：项目当前正在持续进行 Web UI 调整与升级，部分页面在过渡阶段可能仍存在样式、交互或兼容性问题。欢迎通过 [Issue](https://github.com/ZhuLinsen/daily_stock_analysis/issues) 反馈问题，或直接提交 [Pull Request](https://github.com/ZhuLinsen/daily_stock_analysis/pulls) 一起完善。
---

## ☕ 支持项目

如果本项目对你有帮助，欢迎支持项目的持续维护与迭代，感谢支持 🙏  
赞赏可备注联系方式，祝股市长虹

| 支付宝 (Alipay) | 微信支付 (WeChat) | Ko-fi |
| :---: | :---: | :---: |
| <img src="./sources/alipay.jpg" width="200" alt="Alipay"> | <img src="./sources/wechatpay.jpg" width="200" alt="WeChat Pay"> | <a href="https://ko-fi.com/mumu157" target="_blank"><img src="./sources/ko-fi.png" width="200" alt="Ko-fi"></a> |

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

详见 [贡献指南](docs/CONTRIBUTING.md)

### 本地门禁（建议先跑）

```bash
pip install -r requirements.txt
pip install flake8 pytest
./scripts/ci_gate.sh
```

如修改前端（`apps/dsa-web`）：

```bash
cd apps/dsa-web
npm ci
npm run lint
npm run build
```

## 📄 License
[MIT License](LICENSE) © 2026 ZhuLinsen

如果你在项目中使用或基于本项目进行二次开发，
非常欢迎在 README 或文档中注明来源并附上本仓库链接。
这将有助于项目的持续维护和社区发展。

## 📬 联系与合作
- GitHub Issues：[提交 Issue](https://github.com/ZhuLinsen/daily_stock_analysis/issues)
- 合作邮箱：zhuls345@gmail.com

## ⭐ Star History
**如果觉得有用，请给个 ⭐ Star 支持一下！**

<a href="https://star-history.com/#ZhuLinsen/daily_stock_analysis&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=ZhuLinsen/daily_stock_analysis&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=ZhuLinsen/daily_stock_analysis&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=ZhuLinsen/daily_stock_analysis&type=Date" />
 </picture>
</a>

## ⚠️ 免责声明

本项目仅供学习和研究使用，不构成任何投资建议。股市有风险，投资需谨慎。作者不对使用本项目产生的任何损失负责。

---
