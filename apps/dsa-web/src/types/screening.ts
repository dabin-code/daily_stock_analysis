/**
 * 筛选模块类型定义
 * 与后端 API Schema 对齐
 */

// ============ 策略 ============

export interface ScreeningStrategy {
  name: string;
  displayName: string;
  description: string;
  category: 'trend' | 'pattern' | 'reversal' | 'framework';
  hasScreeningRules: boolean;
  systemRole?: string;
  strategyFamily?: string;
  applicableMarket?: string[];
  applicableTheme?: string[];
  setupType?: SetupType | 'none';
}

export interface ScreeningStrategyListResponse {
  strategies: ScreeningStrategy[];
}

// ============ 筛选运行 ============

export type ScreeningMode = 'balanced' | 'aggressive' | 'quality';

export type ScreeningRunStatus =
  | 'pending'
  | 'resolving_universe'
  | 'ingesting'
  | 'factorizing'
  | 'screening'
  | 'ai_enriching'
  | 'completed'
  | 'completed_with_ai_degraded'
  | 'failed';

export interface CreateScreeningRunRequest {
  tradeDate?: string;
  stockCodes?: string[];
  mode?: ScreeningMode;
  candidateLimit?: number;
  aiTopK?: number;
  strategies?: string[];
  rerunFailed?: boolean;
  resumeFrom?: 'ingesting' | 'factorizing';
  market?: string;
}

export interface ScreeningBackfillToDateRequest {
  tradeDate: string;
  market?: string;
}

export interface ScreeningBackfillGovernanceResult {
  tradeDate?: string;
  runResult?: string;
  passStatus?: string;
  reason?: string;
}

export interface ScreeningBackfillToDateResponse {
  status: string;
  targetTradeDate: string;
  fromTradeDate?: string;
  backfilledDates: string[];
  savedRows: number;
  failedDates: string[];
  governanceResult: ScreeningBackfillGovernanceResult;
}

export interface ScreeningRun {
  runId: string;
  mode?: string;
  status: ScreeningRunStatus;
  tradeDate?: string;
  market?: string;
  universeSize: number;
  candidateCount: number;
  aiTopK: number;
  errorSummary?: string;
  failedSymbols: string[];
  warnings: string[];
  syncFailureRatio: number;
  configSnapshot: Record<string, unknown>;
  startedAt?: string;
  completedAt?: string;
  triggerType?: string;
  notificationStatus?: string;
  notificationAttempts: number;
  notificationSentAt?: string;
  notificationError?: string;
  strategyNames?: string[];
  decisionContext?: DecisionContext;
  localThemePipeline?: LocalThemePipelineSnapshot;
  externalThemePipeline?: ExternalThemePipelineSnapshot;
  fusedThemePipeline?: FusedThemePipelineSnapshot;
}

export interface ScreeningRunListResponse {
  total: number;
  items: ScreeningRun[];
}

// ============ 五层决策系统类型 ============

export type MarketRegime = 'aggressive' | 'balanced' | 'defensive' | 'stand_aside';
export type ThemePosition = 'main_theme' | 'secondary_theme' | 'follower_theme' | 'fading_theme' | 'non_theme';
export type TradeStage = 'stand_aside' | 'watch' | 'focus' | 'probe_entry' | 'add_on_strength' | 'reject';
export type EntryMaturity = 'low' | 'medium' | 'high';
export type CandidatePoolLevel = 'leader_pool' | 'focus_list' | 'watchlist';
export type SetupType =
  | 'bottom_divergence_breakout'
  | 'low123_breakout'
  | 'trend_breakout'
  | 'trend_pullback'
  | 'gap_breakout'
  | 'limitup_structure'
  | 'none';

export interface TradePlan {
  initialPosition?: string;
  addRule?: string;
  stopLossRule?: string;
  takeProfitPlan?: string;
  invalidationRule?: string;
  riskLevel?: string;
  holdingExpectation?: string;
  executionNote?: string;
}

export interface AiReviewDecision {
  aiQueryId?: string;
  aiSummary?: string;
  aiOperationAdvice?: string;
  aiTradeStage?: string;
  aiReasoning?: string;
  aiConfidence?: number;
  aiEnvironmentOk?: boolean;
  aiThemeAlignment?: boolean;
  aiEntryQuality?: string;
  stageConflict?: boolean;
  resultSource?: string;
  isFallback?: boolean;
  fallbackReason?: string;
  downgradeReasons?: string[];
  initialPosition?: string;
  stopLossRule?: string;
  takeProfitPlan?: string;
  invalidationRule?: string;
  promptVersion?: string;
  modelName?: string;
  parseStatus?: string;
  retryCount?: number;
}

export interface MarketEnvironmentSnapshot {
  marketRegime?: MarketRegime;
  riskLevel?: string;
  indexName?: string;
  indexPrice?: number;
  indexMa100?: number;
  isSafe?: boolean;
  message?: string;
}

export interface SectorHeatSnapshot {
  boardName: string;
  boardType: string;
  sectorHotScore: number;
  sectorStatus: string;
  sectorStage: string;
  canonicalTheme?: string;
  stockCount: number;
  upCount: number;
  limitUpCount: number;
}

export interface DecisionContext {
  marketEnvironment?: MarketEnvironmentSnapshot;
  sectorHeatResults: SectorHeatSnapshot[];
  hotThemeCount: number;
  warmThemeCount: number;
}

export interface ThemePipelineThemeItem {
  name: string;
  normalizedName?: string;
  rawName?: string;
  rawNames?: string[];
  source?: string;
  prioritySource?: string;
  matchedSources?: string[];
  heatScore?: number;
  confidence?: number;
  sourceBoard?: string;
  sectorStatus?: string;
  sectorStage?: string;
  stockCount?: number;
  upCount?: number;
  limitUpCount?: number;
  catalystSummary?: string;
  keywordCount?: number;
  keywords?: string[];
  normalizationStatus?: string;
  normalizationConfidence?: number;
  normalizationMatchReasons?: string[];
  normalizationMatchedBoards?: string[];
}

export interface LocalThemePipelineSnapshot {
  source: string;
  tradeDate?: string;
  market?: string;
  hotThemeCount: number;
  warmThemeCount: number;
  selectedThemeNames: string[];
  themes: ThemePipelineThemeItem[];
}

export interface ExternalThemePipelineSnapshot {
  source: string;
  tradeDate?: string;
  market?: string;
  acceptedThemeCount: number;
  hotThemeCount: number;
  focusThemeCount: number;
  topThemeNames: string[];
  themes: ThemePipelineThemeItem[];
}

export interface FusedThemePipelineSnapshot {
  tradeDate?: string;
  market?: string;
  activeSources: string[];
  selectedThemeNames: string[];
  mergedThemeCount: number;
  mergedThemes: ThemePipelineThemeItem[];
}

// ============ 候选 ============

export interface ScreeningCandidate {
  code: string;
  name?: string;
  rank: number;
  ruleScore: number;
  selectedForAi: boolean;
  strategyScores?: Record<string, number>;
  ruleHits: string[];
  factorSnapshot: ScreeningFactorSnapshot;
  marketMessage?: string;
  environmentOk?: boolean;
  indexPrice?: number;
  indexMa100?: number;
  themeTag?: string;
  themeScore?: number;
  sectorStrength?: number;
  themeDuration?: string;
  tradeThemeStage?: string;
  leaderStocks?: string[];
  frontStocks?: string[];
  leaderScore?: number;
  relativeStrengthMarket?: number;
  relativeStrengthSector?: number;
  aiQueryId?: string;
  aiSummary?: string;
  aiOperationAdvice?: string;
  hasAiAnalysis?: boolean;
  newsCount?: number;
  newsSummary?: string;
  recommendationSource?: string;
  recommendationReason?: string;
  finalScore?: number;
  finalRank?: number;
  matchedStrategies?: string[];
  // 五层决策系统字段
  tradeStage?: TradeStage;
  setupType?: SetupType;
  strategyFamily?: string;
  entryMaturity?: EntryMaturity;
  riskLevel?: string;
  marketRegime?: MarketRegime;
  themePosition?: ThemePosition;
  candidatePoolLevel?: CandidatePoolLevel;
  setupFreshness?: number;
  setupHitReasons?: string[];
  tradePlan?: TradePlan;
  aiReview?: AiReviewDecision;
  // AI Review Protocol
  aiTradeStage?: string;
  aiReasoning?: string;
  aiConfidence?: number;
  aiEnvironmentOk?: boolean;
  aiThemeAlignment?: boolean;
  aiEntryQuality?: string;
  stageConflict?: boolean;
  resultSource?: string;
  isFallback?: boolean;
  fallbackReason?: string;
  downgradeReasons?: string[];
  initialPosition?: string;
  stopLossRule?: string;
  takeProfitPlan?: string;
  invalidationRule?: string;
  promptVersion?: string;
  modelName?: string;
  parseStatus?: string;
  retryCount?: number;
}

export interface ScreeningCandidateListResponse {
  total: number;
  items: ScreeningCandidate[];
}

export interface ScreeningCandidateDetail extends ScreeningCandidate {
  analysisHistory?: {
    id?: number;
    queryId: string;
    stockCode: string;
    stockName?: string;
    reportType?: string;
    operationAdvice?: string;
    trendPrediction?: string;
    sentimentScore?: number;
    analysisSummary?: string;
    createdAt?: string;
  };
}

export interface HotThemeNewsItem {
  title: string;
  source?: string;
  summary?: string;
  url?: string;
  published_at?: string;
  heat_score?: number;
}

export interface ScreeningPhaseResults {
  phase1_market_and_theme?: boolean;
  phase2_leader_screen?: boolean;
  phase3_core_signal?: boolean;
  phase4_entry_readiness?: boolean;
  phase5_risk_controls?: boolean;
}

export interface ScreeningPhaseExplanation {
  phase_key: keyof ScreeningPhaseResults;
  label: string;
  hit: boolean;
  summary: string;
}

export interface ScreeningRiskParams {
  stop_loss?: number;
  /**
   * A6: 止损依据标签（如 `"123结构低点" / "底背离临界区" / "缺口下沿" /
   * "涨停前trendline" / "MA100×0.95" / "none"`）。
   * 由 `hot_theme_factor_enricher._resolve_stop_loss` 按 `primary_signal` 选定。
   */
  stop_loss_basis?: string;
  position_size?: string;
  take_profit_ratio?: number;
}

export interface TechnicalPatternMetric {
  readonly label: string;
  readonly value: string;
}

export interface TechnicalPattern {
  readonly id: string;
  readonly name: string;
  readonly signalStrength?: number;
  readonly metrics: readonly TechnicalPatternMetric[];
  readonly hitReasons: readonly string[];
}

/**
 * Signal summary for A4's `all_signals` snapshot field.
 * Each entry represents one hit signal with its classification and raw score.
 */
export interface ScreeningSignalSummary {
  name: string;
  kind: string;
  score: number;
}

/**
 * Layered breakdown of extreme_strength_score (A1/A2).
 * Keys are free-form because the backend may evolve sub-contributions.
 */
export type ScreeningStrengthBreakdown = Record<string, number>;

export interface ScreeningFactorSnapshot extends Record<string, unknown> {
  close?: number;
  volume_ratio?: number;
  pct_chg?: number;
  is_hot_theme_stock?: boolean;
  primary_theme?: string;
  theme_heat_score?: number;
  theme_match_score?: number;
  leader_score?: number;
  extreme_strength_score?: number;
  entry_reason?: string;
  core_signal?: string;
  theme_catalyst_summary?: string;
  theme_catalyst_news?: HotThemeNewsItem[];
  phase_results?: ScreeningPhaseResults;
  phase_explanations?: ScreeningPhaseExplanation[];
  risk_params?: ScreeningRiskParams;
  extreme_strength_reasons?: string[];
  bottomDivergenceHitReasons?: string[];
  bottom_divergence_hit_reasons?: string[];
  rule_hits_display?: string[];
  bonus_signals?: string[];

  // A1/A2: layered extreme_strength_score 四桶字段
  theme_pool_score?: number;
  leadership_score?: number;
  entry_signal_score?: number;
  timing_penalty?: number;
  extreme_strength_breakdown?: ScreeningStrengthBreakdown;

  /**
   * A7：leader_score 与其它桶之间可观测的重复加权估算值（已按 0.15 缩放回
   * leader_contribution 的量纲）。仅由 `small_circ_mv / turnover /
   * breakout_strength / trend_strength(above_ma100 下界)` 回推，`theme_match`
   * 因无法从 scalar leader_score 精确还原而不计入。
   */
  leader_double_count?: number;
  /**
   * A7：`extreme_strength_score - leader_double_count` 的净总分，用于可选的
   * 去重视图。`extreme_strength_score` 本身保持不变，所有阈值契约依旧生效。
   */
  extreme_strength_score_deduplicated?: number;

  // A3: 时机评估字段
  stage_label?: string;
  timing_reasons?: string[];
  timing_bars_since_event?: number;
  timing_extended_pct?: number;

  // A4: 统一信号分类字段
  primary_signal?: string | null;
  signal_kind?: string;
  all_signals?: ScreeningSignalSummary[];

  // Bottom divergence pattern fields
  bottom_divergence_double_breakout?: boolean;
  bottom_divergence_state?: string;
  bottom_divergence_pattern_code?: string;
  bottom_divergence_pattern_label?: string;
  bottom_divergence_signal_strength?: number;
  bottom_divergence_entry_price?: number;
  bottom_divergence_stop_loss?: number;
  bottom_divergence_horizontal_breakout?: boolean;
  bottom_divergence_trendline_breakout?: boolean;
  bottom_divergence_sync_breakout?: boolean;
  bottom_divergence_actionable_entry?: boolean;
  bottom_divergence_valid_pattern?: boolean;
  bottom_divergence_ab_bars?: number;
  bottom_divergence_entry_zone?: string;
  bottom_divergence_entry_timing_score?: number;
  bottom_divergence_extended_pct?: number;
  bottom_divergence_validation_status?: string;

  // Pattern 123 fields
  pattern_123_low_trendline?: boolean;
  pattern_123_watchlist?: boolean;
  pattern_123_breakout_ready?: boolean;
  pattern_123_state?: string;
  pattern_123_entry_price?: number;
  pattern_123_stop_loss?: number;
  pattern_123_signal_strength?: number;

  // MA100+Low123 combined fields
  ma100_low123_confirmed?: boolean;
  ma100_low123_watchlist?: boolean;
  ma100_low123_state?: string;
  ma100_low123_pattern_strength?: number;
  ma100_low123_ma_score?: number;
  ma100_low123_entry_timing_score?: number;
  ma100_low123_entry_zone?: string;
  ma100_low123_validation_status?: string;
  ma100_low123_validation_reason?: string;
  ma100_low123_hit_reasons?: string[];
  ma100_low123_watch_hit_reasons?: string[];

  // MA100+60min combined fields
  ma100_60min_confirmed?: boolean;
  ma100_60min_freshness_score?: number;
  ma100_60min_ma_score?: number;
  ma100_60min_hit_reasons?: string[];

  // Simple boolean signal flags
  above_ma100?: boolean;
  gap_breakaway?: boolean;
  is_limit_up?: boolean;
}

// ============ 通知 ============

export interface ScreeningNotifyRequest {
  limit?: number;
  withAiOnly?: boolean;
  force?: boolean;
}

// ============ 辅助 ============

export const SCREENING_STAGES: ScreeningRunStatus[] = [
  'resolving_universe',
  'ingesting',
  'factorizing',
  'screening',
  'ai_enriching',
  'completed',
];

export const STAGE_LABELS: Record<string, string> = {
  pending: '等待中',
  resolving_universe: '解析股票池',
  ingesting: '同步数据',
  factorizing: '构建因子',
  screening: '规则筛选',
  ai_enriching: 'AI 分析',
  completed: '已完成',
  completed_with_ai_degraded: '已完成(AI降级)',
  failed: '失败',
};

export const CATEGORY_LABELS: Record<string, string> = {
  trend: '趋势',
  pattern: '形态',
  reversal: '反转',
  framework: '框架',
};

export const CATEGORY_COLORS: Record<string, string> = {
  trend: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  pattern: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
  reversal: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  framework: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
};

/**
 * The 4 target strategies for the consolidated screening page.
 * Strategies not yet implemented are shown as disabled (hasScreeningRules=false).
 */
export const TARGET_STRATEGIES: ScreeningStrategy[] = [
  {
    name: 'bottom_divergence_double_breakout',
    displayName: '底背离双突破',
    description: '基于DIF/DEA六形态底背离 + 下降趋势线/水平阻力线双突破确认',
    category: 'reversal',
    hasScreeningRules: true,
  },
  {
    name: 'ma100_low123_combined',
    displayName: 'MA100+低位123结构',
    description: '站上MA100均线 + 低位123底部反转形态联合确认',
    category: 'reversal',
    hasScreeningRules: true,
  },
  {
    name: 'ma100_60min_combined',
    displayName: 'MA100+60分钟线',
    description: '日线站上MA100 + 60分钟级别入场信号联合确认',
    category: 'trend',
    hasScreeningRules: true,
  },
  {
    name: 'extreme_momentum_combined',
    displayName: '极端强势组合',
    description: '涨停突破/缺口突破等极端强势形态联合策略',
    category: 'trend',
    hasScreeningRules: false,
  },
];

export function getStageIndex(status: ScreeningRunStatus): number {
  const idx = SCREENING_STAGES.indexOf(status);
  return idx >= 0 ? idx : 0;
}

export function isTerminalStatus(status: ScreeningRunStatus): boolean {
  return ['completed', 'completed_with_ai_degraded', 'failed'].includes(status);
}

// ============ 五层决策标签映射 ============

export const TRADE_STAGE_LABELS: Record<string, string> = {
  probe_entry: '试探入场',
  add_on_strength: '加仓确认',
  focus: '重点关注',
  watch: '持续观察',
  stand_aside: '暂时回避',
  reject: '不参与',
};

export const TRADE_STAGE_COLORS: Record<string, string> = {
  probe_entry: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  add_on_strength: 'bg-green-500/20 text-green-400 border-green-500/30',
  focus: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  watch: 'bg-gray-500/20 text-gray-400 border-gray-500/30',
  stand_aside: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  reject: 'bg-red-500/20 text-red-400 border-red-500/30',
};

export const THEME_POSITION_LABELS: Record<string, string> = {
  main_theme: '主力题材',
  secondary_theme: '次要题材',
  follower_theme: '跟风题材',
  fading_theme: '退潮题材',
  non_theme: '非题材',
};

export const THEME_POSITION_COLORS: Record<string, string> = {
  main_theme: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
  secondary_theme: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  follower_theme: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
  fading_theme: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  non_theme: 'bg-gray-500/20 text-gray-400 border-gray-500/30',
};

export const MARKET_REGIME_LABELS: Record<string, string> = {
  aggressive: '积极进攻',
  balanced: '均衡配置',
  defensive: '防御为主',
  stand_aside: '空仓观望',
};

export const MARKET_REGIME_COLORS: Record<string, string> = {
  aggressive: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  balanced: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  defensive: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  stand_aside: 'bg-red-500/20 text-red-400 border-red-500/30',
};

export const SETUP_TYPE_LABELS: Record<string, string> = {
  bottom_divergence_breakout: '底背离突破',
  low123_breakout: '低位123突破',
  trend_breakout: '趋势突破',
  trend_pullback: '趋势回调',
  gap_breakout: '跳空突破',
  limitup_structure: '涨停结构',
  none: '无',
};

export const POOL_LEVEL_LABELS: Record<string, string> = {
  leader_pool: '龙头池',
  focus_list: '关注池',
  watchlist: '观察池',
};

export const ENTRY_MATURITY_LABELS: Record<string, string> = {
  high: '成熟',
  medium: '发展中',
  low: '早期',
};

// ============ A3：时机阶段标签 ============

/**
 * Timing stage labels emitted by ExtremeStrengthTimingAssessor.
 * Keep keys aligned with ``src/services/extreme_strength_timing_assessor.py::StageLabel``.
 */
export const STAGE_LABEL_LABELS: Record<string, string> = {
  pool_only: '池子层',
  watch_only: '仅观察',
  breakout_day: '突破当日',
  retest_entry: '回踩确认',
  extended_do_not_chase: '已走远·勿追',
  none: '无',
};

export const STAGE_LABEL_COLORS: Record<string, string> = {
  pool_only: 'bg-gray-500/20 text-gray-400 border-gray-500/30',
  watch_only: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  breakout_day: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  retest_entry: 'bg-green-500/20 text-green-400 border-green-500/30',
  extended_do_not_chase: 'bg-red-500/20 text-red-400 border-red-500/30',
  none: 'bg-gray-500/20 text-gray-400 border-gray-500/30',
};

// ============ A4：信号类型标签 ============

/**
 * Signal kinds emitted by CoreSignalIdentifier.classify_signals.
 * Keep keys aligned with ``src/services/core_signal_identifier.py::SignalKind``.
 */
export const SIGNAL_KIND_LABELS: Record<string, string> = {
  structure_low_entry: '低位结构入场',
  momentum_breakout: '动量突破',
  momentum_chase: '动量追涨',
  none: '无信号',
};

export const SIGNAL_KIND_COLORS: Record<string, string> = {
  structure_low_entry: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  momentum_breakout: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  momentum_chase: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  none: 'bg-gray-500/20 text-gray-400 border-gray-500/30',
};
