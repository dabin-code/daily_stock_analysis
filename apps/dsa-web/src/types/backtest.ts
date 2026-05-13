/**
 * Five-layer backtest API type definitions
 * Mirrors api/v1/schemas/five_layer_backtest.py
 */

export type FiveLayerEvaluationMode =
  | 'historical_snapshot'
  | 'rule_replay'
  | 'parameter_calibration';

export type FiveLayerExecutionModel =
  | 'conservative'
  | 'baseline'
  | 'optimistic';

export type FiveLayerMarket = 'cn' | 'us' | 'hk';

export interface BacktestRunRequest {
  evaluationMode?: FiveLayerEvaluationMode;
  executionModel?: FiveLayerExecutionModel;
  tradeDateFrom: string;
  tradeDateTo: string;
  market?: FiveLayerMarket;
  evalWindowDays?: number;
  generateRecommendations?: boolean;
}

export interface BacktestScreeningRunRequest {
  screeningRunId: string;
  evaluationMode?: FiveLayerEvaluationMode;
  executionModel?: FiveLayerExecutionModel;
  market?: FiveLayerMarket;
  evalWindowDays?: number;
  generateRecommendations?: boolean;
}

export interface BacktestSampleBaseline {
  rawSampleCount: number;
  evaluatedSampleCount?: number | null;
  aggregatableSampleCount: number;
  entrySampleCount: number;
  observationSampleCount: number;
  suppressedSampleCount: number;
  suppressedReasons: Record<string, number>;
}

export interface BacktestRunResponse {
  backtestRunId: string;
  evaluationMode: string;
  executionModel: string;
  tradeDateFrom?: string | null;
  tradeDateTo?: string | null;
  market: string;
  status: string;
  sampleCount: number;
  completedCount: number;
  errorCount: number;
  dataVersion?: string | null;
  marketDataVersion?: string | null;
  themeMappingVersion?: string | null;
  candidateSnapshotVersion?: string | null;
  rulesVersion?: string | null;
  sampleBaseline?: BacktestSampleBaseline | null;
  createdAt?: string | null;
  startedAt?: string | null;
  completedAt?: string | null;
}

export interface BacktestResultItem {
  id?: number | null;
  backtestRunId: string;
  tradeDate?: string | null;
  code: string;
  name?: string | null;
  signalFamily: string;
  signalType?: string | null;
  evaluatorType: string;
  evaluationMode?: string | null;
  executionModel?: string | null;
  snapshotSource?: string | null;
  replayed?: boolean | null;
  snapshotTradeStage?: string | null;
  snapshotSetupType?: string | null;
  snapshotEntryMaturity?: string | null;
  snapshotMarketRegime?: string | null;
  snapshotThemePosition?: string | null;
  snapshotCandidatePoolLevel?: string | null;
  snapshotRiskLevel?: string | null;
  entryFillStatus?: string | null;
  entryFillPrice?: number | null;
  exitFillPrice?: number | null;
  plannedEntryPrice?: number | null;
  plannedStopLossPrice?: number | null;
  plannedTakeProfitPrice?: number | null;
  actualEntryPrice?: number | null;
  actualEntryDate?: string | null;
  actualExitPrice?: number | null;
  actualExitDate?: string | null;
  exitReason?: string | null;
  tradeReturnPct?: number | null;
  tradeReplayStatus?: string | null;
  limitBlocked?: boolean | null;
  gapAdjusted?: boolean | null;
  forwardReturn1d?: number | null;
  forwardReturn3d?: number | null;
  forwardReturn5d?: number | null;
  forwardReturn10d?: number | null;
  mae?: number | null;
  mfe?: number | null;
  maxDrawdownFromPeak?: number | null;
  holdingDays?: number | null;
  planSuccess?: boolean | null;
  signalQualityScore?: number | null;
  riskAvoidedPct?: number | null;
  opportunityCostPct?: number | null;
  outcome?: string | null;
  stageSuccess?: boolean | null;
  evalStatus?: string | null;
  metricsJson?: string | null;
  evidenceJson?: string | null;
  factorSnapshotJson?: string | null;
  tradePlanJson?: string | null;
  primaryStrategy?: string | null;
  contributingStrategies?: string[];
  matchedStrategies?: string[];
  ruleHits?: string[];
  sampleBucket?: string | null;
  entryTimingLabel?: string | null;
  ma100Low123ValidationStatus?: string | null;
  ma100Low123DataComplete?: boolean | null;
}

export interface BacktestResultsResponse {
  backtestRunId: string;
  total: number;
  page: number;
  limit: number;
  items: BacktestResultItem[];
}

export interface BacktestFamilyBreakdownMetrics {
  sampleCount?: number | null;
  avgReturnPct?: number | null;
  winRatePct?: number | null;
}

export interface BacktestStrategyCohortContext {
  primaryStrategy?: string | null;
  sampleBucket?: string | null;
  snapshotMarketRegime?: string | null;
  snapshotCandidatePoolLevel?: string | null;
  snapshotEntryMaturity?: string | null;
}

export interface BacktestSummaryItem {
  groupType: string;
  groupKey: string;
  sampleCount: number;
  avgReturnPct?: number | null;
  medianReturnPct?: number | null;
  winRatePct?: number | null;
  avgMae?: number | null;
  avgMfe?: number | null;
  avgDrawdown?: number | null;
  topKHitRate?: number | null;
  excessReturnPct?: number | null;
  rankingConsistency?: number | null;
  p25ReturnPct?: number | null;
  p75ReturnPct?: number | null;
  extremeSampleRatio?: number | null;
  timeBucketStability?: number | null;
  profitFactor?: number | null;
  avgHoldingDays?: number | null;
  maxConsecutiveLosses?: number | null;
  planExecutionRate?: number | null;
  stageAccuracyRate?: number | null;
  systemGrade?: string | null;
  metricsJson?: string | null;
  familyBreakdown?: Record<string, BacktestFamilyBreakdownMetrics> | null;
  strategyCohortContext?: BacktestStrategyCohortContext | null;
  sampleBaseline?: BacktestSampleBaseline | null;
}

export interface BacktestSummariesResponse {
  backtestRunId: string;
  items: BacktestSummaryItem[];
}

export interface BacktestRecommendationItem {
  recommendationType: string;
  targetScope?: string | null;
  targetKey?: string | null;
  currentRule?: string | null;
  suggestedChange?: string | null;
  recommendationLevel: string;
  sampleCount?: number | null;
  confidence?: number | null;
  validationStatus?: string | null;
  evidenceJson?: string | null;
}

export interface BacktestRecommendationsResponse {
  backtestRunId: string;
  items: BacktestRecommendationItem[];
}

export interface BacktestFullPipelineResponse {
  run: BacktestRunResponse;
  summaries: BacktestSummaryItem[];
  recommendations: BacktestRecommendationItem[];
}

export interface RankingComparisonItem {
  dimension: string;
  tierHigh: string;
  tierLow: string;
  highAvgReturn?: number | null;
  lowAvgReturn?: number | null;
  excessReturnPct?: number | null;
  highWinRate?: number | null;
  lowWinRate?: number | null;
  highSampleCount: number;
  lowSampleCount: number;
  isEffective: boolean;
}

export interface RankingEffectivenessData {
  comparisons: RankingComparisonItem[];
  overallEffectivenessRatio: number;
  topKHitRate?: number | null;
  excessReturnPct?: number | null;
  rankingConsistency?: number | null;
}
