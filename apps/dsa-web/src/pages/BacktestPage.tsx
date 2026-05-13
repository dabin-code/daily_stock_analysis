import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { backtestApi } from '../api/backtest';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import { screeningApi } from '../api/screening';
import { ApiErrorAlert, Badge, Card } from '../components/common';
import { EvaluationDetail } from '../components/backtest/EvaluationDetail';
import { StrategyResearchCanvas } from '../components/backtest/StrategyResearchCanvas';
import { StrategyWorkbenchSidebar, type StrategySidebarItem } from '../components/backtest/StrategyWorkbenchSidebar';
import {
  getPrimaryResearchSampleFocus,
  matchesResearchSampleFocus,
  type ResearchSampleFocus,
} from '../components/backtest/researchFilters';
import type {
  BacktestFullPipelineResponse,
  BacktestRecommendationItem,
  BacktestResultItem,
  BacktestRunResponse,
  BacktestSampleBaseline,
  BacktestSummaryItem,
  FiveLayerEvaluationMode,
  FiveLayerExecutionModel,
  FiveLayerMarket,
  RankingEffectivenessData,
} from '../types/backtest';
import type { ScreeningRun } from '../types/screening';

const EVALUATION_MODE_OPTIONS: Array<{ value: FiveLayerEvaluationMode; label: string }> = [
  { value: 'historical_snapshot', label: '历史快照' },
  { value: 'rule_replay', label: '规则回放' },
  { value: 'parameter_calibration', label: '参数校准' },
];

const EXECUTION_MODEL_OPTIONS: Array<{ value: FiveLayerExecutionModel; label: string }> = [
  { value: 'conservative', label: '保守' },
  { value: 'baseline', label: '基准' },
  { value: 'optimistic', label: '乐观' },
];

const MARKET_OPTIONS: Array<{ value: FiveLayerMarket; label: string }> = [
  { value: 'cn', label: 'A股' },
  { value: 'hk', label: '港股' },
  { value: 'us', label: '美股' },
];

function toDateInputValue(date: Date): string {
  return date.toISOString().slice(0, 10);
}

type BacktestEntryMode = 'screening' | 'replay';

interface ResearchDegradedState {
  active: boolean;
  reasons: string[];
  message: string;
  detail: string;
}

interface SampleQualityNotice {
  title: string;
  messages: string[];
  severity: 'warning' | 'info';
}

const ENTRY_MODE_OPTIONS: Array<{ value: BacktestEntryMode; label: string }> = [
  { value: 'screening', label: '研究模式（按选股运行）' },
  { value: 'replay', label: '回放模式（按日期区间）' },
];

const MIN_RESEARCH_SAMPLE_COUNT = 30;
const MIN_ENTRY_SAMPLE_COUNT = 5;

function defaultRange(): { from: string; to: string } {
  const today = new Date();
  const from = new Date(today);
  from.setDate(today.getDate() - 30);
  return { from: toDateInputValue(from), to: toDateInputValue(today) };
}

function getDefaultScreeningRunId(items: ScreeningRun[]): string {
  const preferred = items.find((item) => item.status === 'completed' || item.status === 'completed_with_ai_degraded');
  return preferred?.runId ?? items[0]?.runId ?? '';
}

function buildFallbackSampleBaseline(
  runResult: BacktestRunResponse | null,
  evaluations: BacktestResultItem[],
  overallSummary: BacktestSummaryItem | null,
  evaluationTotal: number | null,
): BacktestSampleBaseline | null {
  if (!runResult && evaluations.length === 0 && !overallSummary) {
    return null;
  }
  if (evaluationTotal != null && evaluationTotal > evaluations.length) {
    return null;
  }
  const evaluatedSampleCount = evaluations.length;
  const entrySampleCount = evaluations.filter((item) => item.signalFamily === 'entry').length;
  const observationSampleCount = evaluations.filter((item) => item.signalFamily === 'observation').length;
  const aggregatableSampleCount = overallSummary?.sampleCount ?? evaluatedSampleCount;
  const rawSampleCount = Math.max(runResult?.sampleCount ?? 0, evaluatedSampleCount);

  return {
    rawSampleCount,
    evaluatedSampleCount,
    aggregatableSampleCount,
    entrySampleCount,
    observationSampleCount,
    suppressedSampleCount: Math.max(evaluatedSampleCount - aggregatableSampleCount, 0),
    suppressedReasons: {},
  };
}

function pct(value?: number | null): string {
  if (value == null) return '--';
  return `${value.toFixed(1)}%`;
}

function entryComparableReturn(item: BacktestResultItem): number {
  return item.tradeReturnPct ?? item.forwardReturn5d ?? item.riskAvoidedPct ?? -Infinity;
}

function renderEvaluationModeLabel(value?: string | null): string {
  switch (value) {
    case 'historical_snapshot':
      return '历史快照';
    case 'rule_replay':
      return '规则回放';
    case 'parameter_calibration':
      return '参数校准';
    default:
      return value || '--';
  }
}

function renderExecutionModelLabel(value?: string | null): string {
  switch (value) {
    case 'conservative':
      return '保守执行';
    case 'baseline':
      return '基准执行';
    case 'optimistic':
      return '乐观执行';
    default:
      return value || '--';
  }
}

function renderMarketLabel(value?: string | null): string {
  switch ((value || '').toLowerCase()) {
    case 'cn':
      return 'A股';
    case 'hk':
      return '港股';
    case 'us':
      return '美股';
    default:
      return value?.toUpperCase() || '--';
  }
}

function renderSuppressionReasonLabel(reason: string): string {
  switch (reason) {
    case 'observation_only':
      return '仅观察样本';
    case 'threshold_suppressed':
      return '样本阈值不足';
    case 'no_forward_bars':
      return '缺少后续K线';
    case 'gap_too_long':
      return '后续窗口停牌过长';
    case 'insufficient_forward_bars':
      return '后续K线不足';
    case 'forward_window_immature':
      return '观察窗口未成熟';
    case 'missing_forward_return_5d':
      return '缺少5日收益';
    case 'missing_risk_avoided_pct':
      return '缺少避险指标';
    case 'missing_primary_metric':
      return '缺少主指标';
    case 'exit_family_not_implemented':
      return '卖出信号暂未评估';
    default:
      if (reason.startsWith('exec_not_filled:')) {
        return `未成交: ${reason.replace('exec_not_filled:', '')}`;
      }
      if (reason.startsWith('exception:')) {
        return `评估异常: ${reason.replace('exception:', '')}`;
      }
      return reason;
  }
}

function renderEntryTimingLabel(value?: string | null): string {
  switch (value) {
    case 'on_time':
      return '时机合适';
    case 'too_early':
      return '偏早';
    case 'too_late':
      return '偏晚';
    case 'not_applicable':
      return '不适用';
    default:
      return value || '--';
  }
}

function renderSampleBucketLabel(value?: string | null): string {
  switch (value) {
    case 'core':
      return '核心样本';
    case 'boundary':
      return '边界样本';
    case 'noise':
      return '噪音样本';
    default:
      return value || '--';
  }
}

function formatDateTime(value?: string | null): string {
  if (!value) return '--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function getEvaluationKey(item: BacktestResultItem): string {
  return String(item.id ?? `${item.code}-${item.tradeDate ?? 'unknown'}-${item.signalFamily}`);
}

function statusBadge(status?: string | null) {
  switch (status) {
    case 'completed':
      return <Badge variant="success">已完成</Badge>;
    case 'running':
    case 'started':
      return <Badge variant="warning">运行中</Badge>;
    case 'failed':
    case 'error':
      return <Badge variant="danger">失败</Badge>;
    default:
      return <Badge variant="default">{status || '--'}</Badge>;
  }
}

const STRATEGY_LABELS: Record<string, string> = {
  trend_breakout: '趋势突破',
  low123_breakout: '低位123',
  bottom_divergence_breakout: '底背离双突破',
  bottom_divergence_double_breakout: '底背离双突破',
  ma100_low123_combined: 'MA100+123 组合',
};

function getStrategyDisplayName(key: string): string {
  return STRATEGY_LABELS[key] ?? key;
}

function renderRankingDimensionLabel(value?: string | null): string {
  switch (value) {
    case 'entry_maturity':
      return '入场成熟度';
    case 'candidate_pool_level':
      return '候选池层级';
    case 'trade_stage':
      return '交易阶段';
    case 'market_regime':
      return '市场环境';
    case 'theme_position':
      return '题材位置';
    default:
      return value || '--';
  }
}

function getStrategyMetaLabel(key: string): string {
  switch (key) {
    case 'trend_breakout':
      return '偏执行型趋势研究，适合查看收益与时机是否一致。';
    case 'low123_breakout':
      return '偏结构型突破研究，适合结合样本分层与归因证据一起看。';
    case 'bottom_divergence_breakout':
    case 'bottom_divergence_double_breakout':
      return '偏拐点确认研究，需重点看边界样本与观察样本比例。';
    case 'ma100_low123_combined':
      return '偏组合确认研究，需重点核对归因链与 Low123 校验状态。';
    default:
      return '围绕当前策略查看表现、样本结构与证据链。';
  }
}

function getStrategyConclusion(
  selectedStrategyKey: string,
  representativeEvaluation: BacktestResultItem | null,
  summary: BacktestSummaryItem | null,
): string {
  if (
    selectedStrategyKey === 'low123_breakout'
    || representativeEvaluation?.primaryStrategy === 'ma100_low123_combined'
  ) {
    return 'MA100+123 组合当前更偏向高弹性研究标的';
  }
  if (selectedStrategyKey === 'bottom_divergence_breakout' || selectedStrategyKey === 'bottom_divergence_double_breakout') {
    return '底背离双突破当前具备较强的结构性研究价值，适合结合观察信号与关键命中原因一起复盘。';
  }
  if (summary?.winRatePct != null && summary.winRatePct >= 60) {
    return `${getStrategyDisplayName(selectedStrategyKey)} 当前表现较稳，可优先查看代表样本与异常样本之间的差异。`;
  }
  return `${getStrategyDisplayName(selectedStrategyKey)} 目前更适合做样本结构与归因质量研究，不建议只看单一收益结论。`;
}

function buildResearchDegradedState(evaluations: BacktestResultItem[]): ResearchDegradedState {
  if (evaluations.length === 0) {
    return {
      active: false,
      reasons: [],
      message: '',
      detail: '',
    };
  }

  const entryCount = evaluations.filter((item) => item.signalFamily === 'entry').length;
  const observationCount = evaluations.filter((item) => item.signalFamily === 'observation').length;
  const hasPrimaryStrategy = evaluations.some((item) => Boolean(item.primaryStrategy));
  const hasEvaluableEntryTiming = evaluations.some((item) => (
    item.signalFamily === 'entry'
    && item.entryTimingLabel != null
    && item.entryTimingLabel !== 'not_applicable'
  ));

  const reasons: string[] = [];
  const detailParts: string[] = [];
  if (observationCount > 0 && entryCount === 0) {
    reasons.push('观察样本主导');
    detailParts.push('当前运行以观察样本为主');
  }
  if (!hasPrimaryStrategy) {
    reasons.push('无主策略归因');
    detailParts.push('缺少稳定主策略归因');
  }
  if (!hasEvaluableEntryTiming) {
    reasons.push('买点语义不足');
    detailParts.push('买点时机标签仍不可用');
  }

  return {
    active: reasons.length > 0,
    reasons,
    message: reasons.length > 0
      ? '当前仅适合观察研究，不适合买点结论。'
      : '',
    detail: reasons.length > 0
      ? `${detailParts.join('，')}。`
      : '',
  };
}

function buildSampleQualityNotice(baseline: BacktestSampleBaseline | null): SampleQualityNotice | null {
  if (!baseline) {
    return null;
  }

  const messages: string[] = [];
  const rawCount = baseline.rawSampleCount ?? 0;
  const entryCount = baseline.entrySampleCount ?? 0;
  const observationCount = baseline.observationSampleCount ?? 0;

  if (rawCount > 0 && rawCount < MIN_RESEARCH_SAMPLE_COUNT) {
    messages.push(`本次只有 ${rawCount} 个原始样本，低于 ${MIN_RESEARCH_SAMPLE_COUNT} 个研究样本的建议门槛，分组胜率和排名有效性容易被个别样本影响。`);
  }
  if (entryCount === 0 && observationCount > 0) {
    messages.push('本次回测没有入场样本，不能用于判断买入收益或买点有效性。');
    messages.push('观察样本只说明“等待/观望是否避开风险”，不是买入后的收益。');
  } else if (entryCount > 0 && entryCount < MIN_ENTRY_SAMPLE_COUNT) {
    messages.push(`本次只有 ${entryCount} 个入场样本，买点收益结论还不稳定，建议扩大历史样本后再判断。`);
  }

  if (messages.length === 0) {
    return null;
  }

  return {
    title: entryCount === 0 ? '样本不足，结论仅供观察' : '样本偏少，结论需谨慎',
    messages,
    severity: 'warning',
  };
}

function getWarningTag(
  evaluations: BacktestResultItem[],
  cohortSummary: BacktestSummaryItem | null,
): string | null {
  const boundaryCount = evaluations.filter((item) => item.sampleBucket === 'boundary').length;
  if (evaluations.length > 0 && boundaryCount / evaluations.length >= 0.5) {
    return '边界样本偏多';
  }
  if (cohortSummary?.strategyCohortContext?.sampleBucket) {
    return `${renderSampleBucketLabel(cohortSummary.strategyCohortContext.sampleBucket)}主导`;
  }
  if (evaluations.length > 0 && evaluations.every((item) => item.signalFamily === 'observation')) {
    return '观察信号主导';
  }
  return null;
}

function filterEvaluationsByCohortContext(
  evaluations: BacktestResultItem[],
  cohortSummary: BacktestSummaryItem | null,
): BacktestResultItem[] {
  const context = cohortSummary?.strategyCohortContext;
  if (!context) {
    return evaluations;
  }

  const filtered = evaluations.filter((item) => {
    if (context.sampleBucket && item.sampleBucket !== context.sampleBucket) {
      return false;
    }
    if (context.snapshotMarketRegime && item.snapshotMarketRegime !== context.snapshotMarketRegime) {
      return false;
    }
    if (context.snapshotCandidatePoolLevel && item.snapshotCandidatePoolLevel !== context.snapshotCandidatePoolLevel) {
      return false;
    }
    if (context.snapshotEntryMaturity && item.snapshotEntryMaturity !== context.snapshotEntryMaturity) {
      return false;
    }
    return true;
  });

  return filtered.length > 0 ? filtered : evaluations;
}

function getCohortMatchScore(
  evaluations: BacktestResultItem[],
  cohortSummary: BacktestSummaryItem,
): number {
  const context = cohortSummary.strategyCohortContext;
  if (!context) {
    return -1;
  }

  return evaluations.reduce((score, item) => {
    let itemScore = 0;
    if (context.primaryStrategy && item.primaryStrategy === context.primaryStrategy) {
      itemScore += 4;
    }
    if (context.sampleBucket && item.sampleBucket === context.sampleBucket) {
      itemScore += 3;
    }
    if (context.snapshotMarketRegime && item.snapshotMarketRegime === context.snapshotMarketRegime) {
      itemScore += 2;
    }
    if (context.snapshotCandidatePoolLevel && item.snapshotCandidatePoolLevel === context.snapshotCandidatePoolLevel) {
      itemScore += 2;
    }
    if (context.snapshotEntryMaturity && item.snapshotEntryMaturity === context.snapshotEntryMaturity) {
      itemScore += 2;
    }
    return score + itemScore;
  }, 0);
}

const EmptySection: React.FC<{ title: string; hint: string }> = ({ title, hint }) => (
  <Card title={title} variant="gradient">
    <p className="text-sm text-secondary-text">{hint}</p>
  </Card>
);

const BacktestPage: React.FC = () => {
  const range = defaultRange();
  const [tradeDateFrom, setTradeDateFrom] = useState(range.from);
  const [tradeDateTo, setTradeDateTo] = useState(range.to);
  const [entryMode, setEntryMode] = useState<BacktestEntryMode>('screening');
  const [screeningRuns, setScreeningRuns] = useState<ScreeningRun[]>([]);
  const [selectedScreeningRunId, setSelectedScreeningRunId] = useState('');
  const [evaluationMode, setEvaluationMode] = useState<FiveLayerEvaluationMode>('historical_snapshot');
  const [executionModel, setExecutionModel] = useState<FiveLayerExecutionModel>('conservative');
  const [market, setMarket] = useState<FiveLayerMarket>('cn');
  const [evalDays, setEvalDays] = useState('10');
  const [generateRecommendations, setGenerateRecommendations] = useState(true);
  const [isRunning, setIsRunning] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [runError, setRunError] = useState<ParsedApiError | null>(null);
  const [pageError, setPageError] = useState<ParsedApiError | null>(null);
  const [runResult, setRunResult] = useState<BacktestRunResponse | null>(null);
  const [summaries, setSummaries] = useState<BacktestSummaryItem[]>([]);
  const [evaluations, setEvaluations] = useState<BacktestResultItem[]>([]);
  const [evaluationTotal, setEvaluationTotal] = useState<number | null>(null);
  const [rankingEffectiveness, setRankingEffectiveness] = useState<RankingEffectivenessData | null>(null);
  const [recommendations, setRecommendations] = useState<BacktestRecommendationItem[]>([]);
  const [selectedStrategyKey, setSelectedStrategyKey] = useState<string | null>(null);
  const [sampleFocus, setSampleFocus] = useState<ResearchSampleFocus>('all');
  const [sampleBucketFilter, setSampleBucketFilter] = useState<string | null>(null);
  const [entryTimingFilter, setEntryTimingFilter] = useState<string | null>(null);
  const [targetEvaluation, setTargetEvaluation] = useState<BacktestResultItem | null>(null);
  const [showRunPanel, setShowRunPanel] = useState(true);
  const [loadedRunEntryMode, setLoadedRunEntryMode] = useState<BacktestEntryMode | null>(null);
  const [loadedRunScreeningContext, setLoadedRunScreeningContext] = useState<{ runId: string; tradeDate?: string | null } | null>(null);
  const displayedEntryMode = runResult ? (loadedRunEntryMode ?? entryMode) : entryMode;
  const displayedMarket = renderMarketLabel(runResult?.market ?? market);
  const displayedDateRange = runResult
    ? `${runResult.tradeDateFrom ?? '--'} - ${runResult.tradeDateTo ?? '--'}`
    : `${tradeDateFrom} - ${tradeDateTo}`;
  const displayedEvaluationMode = renderEvaluationModeLabel(runResult?.evaluationMode ?? evaluationMode);
  const displayedExecutionModel = renderExecutionModelLabel(runResult?.executionModel ?? executionModel);

  useEffect(() => {
    document.title = '五层回测 - 每日股票分析';
  }, []);

  useEffect(() => {
    let active = true;

    const loadScreeningRuns = async () => {
      try {
        const response = await screeningApi.listRuns(20);
        if (!active) {
          return;
        }
        const items = response.items || [];
        setScreeningRuns(items);
        setSelectedScreeningRunId((current) => current || getDefaultScreeningRunId(items));
        if (items.length === 0) {
          setEntryMode('replay');
        }
      } catch (error) {
        if (!active) {
          return;
        }
        setPageError(getParsedApiError(error));
      }
    };

    void loadScreeningRuns();

    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (runResult?.backtestRunId) {
      setShowRunPanel(false);
    }
  }, [runResult?.backtestRunId]);

  const selectedScreeningRun = useMemo(
    () => screeningRuns.find((item) => item.runId === selectedScreeningRunId) ?? null,
    [screeningRuns, selectedScreeningRunId],
  );

  const loadRunArtifacts = useCallback(async (
    runId: string,
    payload?: BacktestFullPipelineResponse,
    runContext?: {
      entryMode: BacktestEntryMode;
      screeningRun?: { runId: string; tradeDate?: string | null } | null;
    },
  ) => {
    try {
      const [run, summaryResponse, resultsResponse, rankingResponse, recommendationResponse] = await Promise.all([
        payload ? Promise.resolve(payload.run) : backtestApi.getRunDetail(runId),
        payload ? Promise.resolve({ backtestRunId: runId, items: payload.summaries }) : backtestApi.getSummaries(runId),
        backtestApi.getResults({ backtestRunId: runId, page: 1, limit: 200 }),
        backtestApi.getRankingEffectiveness(runId),
        payload ? Promise.resolve({ backtestRunId: runId, items: payload.recommendations }) : backtestApi.getRecommendations(runId),
      ]);
      setTradeDateFrom(run.tradeDateFrom || tradeDateFrom);
      setTradeDateTo(run.tradeDateTo || tradeDateTo);
      setEvaluationMode((run.evaluationMode as FiveLayerEvaluationMode) || evaluationMode);
      setExecutionModel((run.executionModel as FiveLayerExecutionModel) || executionModel);
      setMarket((run.market as FiveLayerMarket) || market);
      setRunResult(run);
      setSummaries(summaryResponse.items || []);
      setEvaluations(resultsResponse.items || []);
      setEvaluationTotal(resultsResponse.total ?? resultsResponse.items?.length ?? null);
      setRankingEffectiveness(rankingResponse);
      setRecommendations(recommendationResponse.items || []);
      if (runContext) {
        setLoadedRunEntryMode(runContext.entryMode);
        setLoadedRunScreeningContext(runContext.screeningRun ?? null);
      }
      setPageError(null);
    } catch (error) {
      setPageError(getParsedApiError(error));
    }
  }, [evaluationMode, executionModel, market, tradeDateFrom, tradeDateTo]);

  const handleRun = async () => {
    const parsedEvalDays = parseInt(evalDays || '10', 10);
    if (!Number.isFinite(parsedEvalDays) || parsedEvalDays < 1) {
      setRunError({
        title: '参数无效',
        message: '评估窗口必须是大于 0 的整数。',
        rawMessage: 'Invalid evalWindowDays',
        category: 'unknown',
      });
      return;
    }

    if (entryMode === 'screening' && !selectedScreeningRunId) {
      setRunError({
        title: '缺少选股运行',
        message: '研究模式需要先选择一个可用的选股运行。',
        rawMessage: 'Missing screeningRunId',
        category: 'unknown',
      });
      return;
    }

    setIsRunning(true);
    setRunError(null);
    try {
      const activeRunContext = {
        entryMode,
        screeningRun: entryMode === 'screening' && selectedScreeningRun
          ? { runId: selectedScreeningRun.runId, tradeDate: selectedScreeningRun.tradeDate }
          : null,
      };
      const payload = entryMode === 'screening' && selectedScreeningRunId
        ? await backtestApi.runByScreeningRun({
          screeningRunId: selectedScreeningRunId,
          evaluationMode,
          executionModel,
          market,
          evalWindowDays: parsedEvalDays,
          generateRecommendations,
        })
        : await backtestApi.run({
          tradeDateFrom,
          tradeDateTo,
          evaluationMode,
          executionModel,
          market,
          evalWindowDays: parsedEvalDays,
          generateRecommendations,
        });
      await loadRunArtifacts(payload.run.backtestRunId, payload, activeRunContext);
    } catch (error) {
      setRunError(getParsedApiError(error));
    } finally {
      setIsRunning(false);
    }
  };

  const handleRefresh = async () => {
    if (!runResult) return;
    setIsRefreshing(true);
    try {
      await loadRunArtifacts(runResult.backtestRunId);
    } finally {
      setIsRefreshing(false);
    }
  };

  const strategySummaries = useMemo(
    () => summaries.filter((item) => item.groupType === 'setup_type'),
    [summaries],
  );
  const strategyCohortSummaries = useMemo(
    () => summaries.filter((item) => item.groupType === 'strategy_cohort'),
    [summaries],
  );
  const tradeStageSummaries = useMemo(
    () => summaries
      .filter((item) => item.groupType === 'trade_stage')
      .sort((left, right) => right.sampleCount - left.sampleCount),
    [summaries],
  );
  const maturitySummaries = useMemo(
    () => summaries
      .filter((item) => item.groupType === 'entry_maturity')
      .sort((left, right) => right.sampleCount - left.sampleCount),
    [summaries],
  );
  const strategySidebarItems = useMemo<StrategySidebarItem[]>(() => {
    if (strategySummaries.length > 0) {
      return strategySummaries.map((item) => {
        const relatedEval = evaluations.find((evaluation) => evaluation.snapshotSetupType === item.groupKey);
        return {
          key: item.groupKey,
          strategyKey: item.groupKey,
          displayName: getStrategyDisplayName(item.groupKey),
          metaLabel: getStrategyMetaLabel(item.groupKey),
          sampleCount: item.sampleCount,
          winRatePct: item.winRatePct,
          avgReturnPct: item.avgReturnPct,
          profitFactor: item.profitFactor,
          warningTag: relatedEval?.sampleBucket === 'boundary' ? '边界样本偏多' : null,
        };
      });
    }

    return strategyCohortSummaries.map((item) => {
      const primary = item.strategyCohortContext?.primaryStrategy ?? item.groupKey;
      return {
        key: item.groupKey,
        strategyKey: primary,
        displayName: getStrategyDisplayName(primary),
        metaLabel: item.strategyCohortContext?.sampleBucket
          ? `策略分组 · ${renderSampleBucketLabel(item.strategyCohortContext.sampleBucket)}`
          : '策略分组',
        sampleCount: item.sampleCount,
        winRatePct: item.winRatePct,
        avgReturnPct: item.avgReturnPct,
        profitFactor: item.profitFactor,
        warningTag: item.strategyCohortContext?.sampleBucket === 'boundary' ? '边界样本偏多' : null,
      };
    });
  }, [evaluations, strategyCohortSummaries, strategySummaries]);

  useEffect(() => {
    if (strategySidebarItems.length === 0) {
      setSelectedStrategyKey(null);
      return;
    }
    setSelectedStrategyKey((current) => (
      current && strategySidebarItems.some((item) => item.key === current)
        ? current
        : strategySidebarItems[0].key
    ));
  }, [strategySidebarItems]);

  const effectiveSelectedStrategyKey = selectedStrategyKey ?? strategySidebarItems[0]?.key ?? null;

  useEffect(() => {
    setSampleFocus('all');
    setSampleBucketFilter(null);
    setEntryTimingFilter(null);
    setTargetEvaluation(null);
  }, [effectiveSelectedStrategyKey]);

  const selectedStrategyItem = useMemo(
    () => strategySidebarItems.find((item) => item.key === effectiveSelectedStrategyKey) ?? null,
    [effectiveSelectedStrategyKey, strategySidebarItems],
  );

  const selectedStrategySummary = useMemo(
    () => strategySummaries.find((item) => item.groupKey === selectedStrategyItem?.strategyKey) ?? null,
    [selectedStrategyItem?.strategyKey, strategySummaries],
  );

  const overallSummary = useMemo(
    () => summaries.find((item) => item.groupType === 'overall') ?? null,
    [summaries],
  );

  const hasAuthoritativeSampleBaseline = Boolean(runResult?.sampleBaseline);
  const researchSampleBaseline = useMemo(
    () => runResult?.sampleBaseline ?? buildFallbackSampleBaseline(runResult, evaluations, overallSummary, evaluationTotal),
    [evaluationTotal, evaluations, overallSummary, runResult],
  );

  const suppressionReasonEntries = useMemo(
    () => Object.entries(researchSampleBaseline?.suppressedReasons ?? {}).sort((left, right) => right[1] - left[1]),
    [researchSampleBaseline?.suppressedReasons],
  );
  const sampleQualityNotice = useMemo(
    () => buildSampleQualityNotice(researchSampleBaseline),
    [researchSampleBaseline],
  );

  const selectedStrategyCohort = useMemo(
    () => {
      const directMatch = strategyCohortSummaries.find((item) => item.groupKey === selectedStrategyItem?.key);
      if (directMatch) {
        return directMatch;
      }

      const setupEvaluations = evaluations.filter((item) => item.snapshotSetupType === selectedStrategyItem?.strategyKey);
      if (setupEvaluations.length > 0) {
        const ranked = strategyCohortSummaries
          .map((item) => ({
            item,
            score: getCohortMatchScore(setupEvaluations, item),
          }))
          .filter((entry) => entry.score > 0)
          .sort((left, right) => right.score - left.score);
        if (ranked.length > 0) {
          return ranked[0].item;
        }
      }

      return strategyCohortSummaries.find((item) => item.strategyCohortContext?.primaryStrategy === selectedStrategyItem?.strategyKey)
        ?? null;
    },
    [evaluations, selectedStrategyItem?.key, selectedStrategyItem?.strategyKey, strategyCohortSummaries],
  );

  const filteredEvaluations = useMemo(() => {
    if (!selectedStrategyItem?.strategyKey) return evaluations;
    const bySetup = evaluations.filter((item) => item.snapshotSetupType === selectedStrategyItem.strategyKey);
    if (bySetup.length > 0) return bySetup;
    const byPrimary = evaluations.filter((item) => item.primaryStrategy === selectedStrategyItem.strategyKey);
    if (byPrimary.length > 0 && selectedStrategyCohort) {
      return filterEvaluationsByCohortContext(byPrimary, selectedStrategyCohort);
    }
    if (byPrimary.length > 0) return byPrimary;
    const byMatched = evaluations.filter((item) => item.matchedStrategies?.includes(selectedStrategyItem.strategyKey));
    if (byMatched.length > 0 && selectedStrategyCohort) {
      return filterEvaluationsByCohortContext(byMatched, selectedStrategyCohort);
    }
    return byMatched;
  }, [evaluations, selectedStrategyCohort, selectedStrategyItem?.strategyKey]);

  const representativeEvaluation = useMemo(() => {
    if (filteredEvaluations.length === 0) return null;
    return [...filteredEvaluations].sort(
      (left, right) => entryComparableReturn(right) - entryComparableReturn(left),
    )[0];
  }, [filteredEvaluations]);

  const selectedStrategyConclusion = useMemo(
    () => getStrategyConclusion(selectedStrategyItem?.strategyKey ?? 'unknown', representativeEvaluation, selectedStrategySummary),
    [representativeEvaluation, selectedStrategyItem?.strategyKey, selectedStrategySummary],
  );
  const researchDegradedState = useMemo(
    () => buildResearchDegradedState(filteredEvaluations),
    [filteredEvaluations],
  );

  const selectedStrategyWarning = useMemo(
    () => getWarningTag(filteredEvaluations, selectedStrategyCohort),
    [filteredEvaluations, selectedStrategyCohort],
  );
  const selectedStrategyEntryTimingDistribution = useMemo(() => {
    const counts = filteredEvaluations.reduce<Record<string, number>>((acc, item) => {
      const label = item.entryTimingLabel ?? 'not_applicable';
      acc[label] = (acc[label] ?? 0) + 1;
      return acc;
    }, {});

    return ['on_time', 'too_early', 'too_late', 'not_applicable'].map((label) => ({
      label,
      count: counts[label] ?? 0,
    }));
  }, [filteredEvaluations]);
  const effectiveRankingComparisons = useMemo(
    () => [...(rankingEffectiveness?.comparisons ?? [])]
      .filter((item) => item.isEffective)
      .sort((left, right) => (right.excessReturnPct ?? -Infinity) - (left.excessReturnPct ?? -Infinity)),
    [rankingEffectiveness?.comparisons],
  );
  const watchRankingComparisons = useMemo(
    () => [...(rankingEffectiveness?.comparisons ?? [])]
      .filter((item) => !item.isEffective)
      .sort((left, right) => (right.highSampleCount + right.lowSampleCount) - (left.highSampleCount + left.lowSampleCount)),
    [rankingEffectiveness?.comparisons],
  );
  const browserEvaluations = useMemo(
    () => filteredEvaluations.filter((item) => {
      if (!matchesResearchSampleFocus(item, sampleFocus)) {
        return false;
      }
      if (sampleBucketFilter && item.sampleBucket !== sampleBucketFilter) {
        return false;
      }
      if (entryTimingFilter && item.entryTimingLabel !== entryTimingFilter) {
        return false;
      }
      return true;
    }),
    [entryTimingFilter, filteredEvaluations, sampleBucketFilter, sampleFocus],
  );

  useEffect(() => {
    if (!targetEvaluation) {
      return;
    }
    const targetKey = getEvaluationKey(targetEvaluation);
    const stillVisible = browserEvaluations.some((item) => getEvaluationKey(item) === targetKey);
    if (!stillVisible) {
      setTargetEvaluation(null);
    }
  }, [browserEvaluations, targetEvaluation]);

  const handleAbnormalSampleSelect = (item: BacktestResultItem) => {
    setSampleFocus(getPrimaryResearchSampleFocus(item));
    setSampleBucketFilter(item.sampleBucket ?? null);
    setEntryTimingFilter(item.entryTimingLabel ?? null);
    setTargetEvaluation(item);
  };

  return (
    <div className="min-h-full space-y-4">
      <header className="rounded-[28px] border border-white/8 bg-[linear-gradient(180deg,rgba(255,255,255,0.05),rgba(255,255,255,0.02))] p-5 shadow-[0_20px_60px_rgba(3,8,20,0.16)]">
        <div className="mb-4 flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="label-uppercase">Backtest Research Workbench</div>
            <h1 className="mt-2 text-2xl font-semibold text-foreground">研究工作台</h1>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-secondary-text">
              围绕策略分布、研究结论与样本证据链，完成单次回测的深度分析，并为后续跨运行对比预留统一结构。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {runResult ? statusBadge(runResult.status) : null}
            {runResult ? (
              <button type="button" className="btn-secondary" onClick={() => void handleRefresh()} disabled={isRefreshing || isRunning}>
                {isRefreshing ? '刷新中...' : '刷新运行'}
              </button>
            ) : null}
          </div>
        </div>

        <div className="mb-4 flex flex-wrap items-center gap-2">
          <Badge variant="info">{displayedEntryMode === 'replay' ? '回放模式' : '研究模式'}</Badge>
          {runResult ? <Badge variant="history">{runResult.backtestRunId}</Badge> : null}
          <Badge variant="default">{displayedMarket}</Badge>
          <Badge variant="default">{displayedDateRange}</Badge>
          <Badge variant="default">{displayedEvaluationMode}</Badge>
          <Badge variant="default">{displayedExecutionModel}</Badge>
          {runResult ? <Badge variant="default">样本 {runResult.sampleCount}</Badge> : null}
          {runResult ? <Badge variant="default">完成 {runResult.completedCount}</Badge> : null}
          {runResult ? <Badge variant="default">错误 {runResult.errorCount}</Badge> : null}
        </div>

        {runResult ? (
          <div className="mb-4 grid gap-4 xl:grid-cols-[1.05fr_0.95fr]">
            <div className="rounded-3xl border border-white/8 bg-white/4 p-4">
              <div className="text-sm font-semibold text-foreground">运行上下文</div>
              <div className="mt-3 grid gap-3 sm:grid-cols-2">
                <div className="rounded-2xl bg-black/10 px-4 py-3">
                  <div className="text-[11px] uppercase tracking-[0.18em] text-secondary-text">回测运行</div>
                  <div className="mt-2 break-all font-mono text-xs text-cyan">{runResult.backtestRunId}</div>
                </div>
                <div className="rounded-2xl bg-black/10 px-4 py-3">
                  <div className="text-[11px] uppercase tracking-[0.18em] text-secondary-text">运行入口</div>
                  <div className="mt-2 text-sm text-foreground">
                    {displayedEntryMode === 'replay' ? '回放模式（按日期区间）' : '研究模式（按选股运行）'}
                  </div>
                </div>
                <div className="rounded-2xl bg-black/10 px-4 py-3">
                  <div className="text-[11px] uppercase tracking-[0.18em] text-secondary-text">选股运行</div>
                  <div className="mt-2 break-all font-mono text-xs text-foreground">
                    {displayedEntryMode === 'screening' ? loadedRunScreeningContext?.runId ?? '--' : '--'}
                  </div>
                </div>
                <div className="rounded-2xl bg-black/10 px-4 py-3">
                  <div className="text-[11px] uppercase tracking-[0.18em] text-secondary-text">选股日期</div>
                  <div className="mt-2 text-sm text-foreground">
                    {displayedEntryMode === 'screening' ? loadedRunScreeningContext?.tradeDate ?? '--' : '--'}
                  </div>
                </div>
                <div className="rounded-2xl bg-black/10 px-4 py-3">
                  <div className="text-[11px] uppercase tracking-[0.18em] text-secondary-text">日期区间</div>
                  <div className="mt-2 font-mono text-sm text-foreground">{runResult.tradeDateFrom} - {runResult.tradeDateTo}</div>
                </div>
                <div className="rounded-2xl bg-black/10 px-4 py-3">
                  <div className="text-[11px] uppercase tracking-[0.18em] text-secondary-text">完成时间</div>
                  <div className="mt-2 font-mono text-sm text-foreground">{formatDateTime(runResult.completedAt)}</div>
                </div>
              </div>
            </div>

            <div className="rounded-3xl border border-white/8 bg-white/4 p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="text-sm font-semibold text-foreground">研究上下文</div>
                <Badge variant="default">
                  {hasAuthoritativeSampleBaseline
                    ? (researchSampleBaseline?.suppressedSampleCount ? `${researchSampleBaseline.suppressedSampleCount} 个样本已抑制` : '样本口径已对齐')
                    : '估算口径'}
                </Badge>
              </div>
              <div className="mt-3 grid gap-3 sm:grid-cols-3">
                {[
                  { label: '原始样本', value: researchSampleBaseline?.rawSampleCount ?? '--' },
                  { label: '已评估', value: researchSampleBaseline?.evaluatedSampleCount ?? '--' },
                  { label: '可汇总', value: researchSampleBaseline?.aggregatableSampleCount ?? '--' },
                  { label: '入场样本', value: researchSampleBaseline?.entrySampleCount ?? '--' },
                  { label: '观察样本', value: researchSampleBaseline?.observationSampleCount ?? '--' },
                  { label: '已抑制', value: researchSampleBaseline?.suppressedSampleCount ?? '--' },
                ].map((item) => (
                  <div key={item.label} className="rounded-2xl bg-black/10 px-4 py-3">
                    <div className="text-[11px] uppercase tracking-[0.18em] text-secondary-text">{item.label}</div>
                    <div className="mt-2 text-xl font-semibold text-white">{item.value}</div>
                  </div>
                ))}
              </div>
              {suppressionReasonEntries.length > 0 ? (
                <div className="mt-4">
                  <div className="text-[11px] uppercase tracking-[0.18em] text-secondary-text">抑制原因</div>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {suppressionReasonEntries.map(([reason, count]) => (
                      <Badge key={reason} variant="warning">{`${renderSuppressionReasonLabel(reason)} ${count}`}</Badge>
                    ))}
                  </div>
                </div>
              ) : !hasAuthoritativeSampleBaseline ? (
                <div className="mt-4 text-xs text-secondary-text">
                  当前运行未返回完整 sample baseline，顶部研究上下文为前端估算值，仅用于辅助阅读。
                </div>
              ) : (
                <div className="mt-4 text-xs text-secondary-text">
                  当前运行没有额外抑制原因，页面看到的样本口径可以直接用于研究阅读。
                </div>
              )}
            </div>
          </div>
        ) : null}

        {sampleQualityNotice ? (
          <div className="mb-4 rounded-3xl border border-warning/20 bg-warning/5 p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="text-sm font-semibold text-white">{sampleQualityNotice.title}</div>
              <Badge variant="warning">{sampleQualityNotice.severity === 'warning' ? '请谨慎解读' : '阅读提示'}</Badge>
            </div>
            <div className="mt-3 space-y-2 text-sm leading-6 text-secondary-text">
              {sampleQualityNotice.messages.map((message) => (
                <p key={message}>{message}</p>
              ))}
            </div>
          </div>
        ) : null}

        {runResult ? (
          <div className="rounded-2xl border border-white/8 bg-black/10 p-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="text-sm font-semibold text-foreground">重新运行回测</div>
                <div className="mt-1 text-xs text-secondary-text">需要调整区间或模型时再展开，不再占用研究页主视区。</div>
              </div>
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setShowRunPanel((current) => !current)}
                aria-expanded={showRunPanel}
                aria-controls={showRunPanel ? 'rerun-params-panel' : undefined}
              >
                {showRunPanel ? '收起运行参数' : '重新运行回测'}
              </button>
            </div>
          </div>
        ) : null}

        {(showRunPanel || !runResult) ? (
          <div id="rerun-params-panel" className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <div className="rounded-2xl border border-white/8 bg-white/4 px-4 py-3">
              <label className="mb-1 block text-xs text-secondary-text">运行入口</label>
              <select
                aria-label="运行入口"
                value={entryMode}
                onChange={(event) => setEntryMode(event.target.value as BacktestEntryMode)}
                className="w-full bg-transparent text-sm outline-none"
              >
                {ENTRY_MODE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value} className="bg-base text-foreground">{option.label}</option>
                ))}
              </select>
            </div>
            <div className="rounded-2xl border border-white/8 bg-white/4 px-4 py-3">
              <label className="mb-1 block text-xs text-secondary-text">选股运行</label>
              <select
                aria-label="选股运行"
                value={selectedScreeningRunId}
                onChange={(event) => setSelectedScreeningRunId(event.target.value)}
                className="w-full bg-transparent text-sm outline-none"
                disabled={entryMode !== 'screening' || screeningRuns.length === 0}
              >
                {screeningRuns.length === 0 ? (
                  <option value="" className="bg-base text-foreground">暂无可用选股运行</option>
                ) : screeningRuns.map((item) => (
                  <option key={item.runId} value={item.runId} className="bg-base text-foreground">
                    {`${item.runId} | ${item.tradeDate ?? '--'} | ${item.status}`}
                  </option>
                ))}
              </select>
              <div className="mt-2 text-xs text-secondary-text">
                {entryMode === 'screening'
                  ? '研究模式默认围绕最近一次选股运行发起回测。'
                  : '回放模式会按日期区间重新运行历史回测。'}
              </div>
            </div>
            <label className="rounded-2xl border border-white/8 bg-white/4 px-4 py-3">
              <span className="mb-2 block text-xs text-secondary-text">开始日期</span>
              <input aria-label="开始日期" type="date" value={tradeDateFrom} onChange={(event) => setTradeDateFrom(event.target.value)} className="input-terminal w-full border-0 bg-transparent px-0 py-0 shadow-none" />
            </label>
            <label className="rounded-2xl border border-white/8 bg-white/4 px-4 py-3">
              <span className="mb-2 block text-xs text-secondary-text">结束日期</span>
              <input aria-label="结束日期" type="date" value={tradeDateTo} onChange={(event) => setTradeDateTo(event.target.value)} className="input-terminal w-full border-0 bg-transparent px-0 py-0 shadow-none" />
            </label>
            <div className="rounded-2xl border border-white/8 bg-white/4 px-4 py-3">
              <label className="mb-1 block text-xs text-secondary-text">评估模式</label>
              <select aria-label="评估模式" value={evaluationMode} onChange={(event) => setEvaluationMode(event.target.value as FiveLayerEvaluationMode)} className="w-full bg-transparent text-sm outline-none">
                {EVALUATION_MODE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value} className="bg-base text-foreground">{option.label}</option>
                ))}
              </select>
            </div>
            <div className="rounded-2xl border border-white/8 bg-white/4 px-4 py-3">
              <label className="mb-1 block text-xs text-secondary-text">执行模型</label>
              <select aria-label="执行模型" value={executionModel} onChange={(event) => setExecutionModel(event.target.value as FiveLayerExecutionModel)} className="w-full bg-transparent text-sm outline-none">
                {EXECUTION_MODEL_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value} className="bg-base text-foreground">{option.label}</option>
                ))}
              </select>
            </div>
            <div className="rounded-2xl border border-white/8 bg-white/4 px-4 py-3">
              <label className="mb-1 block text-xs text-secondary-text">市场</label>
              <select aria-label="市场" value={market} onChange={(event) => setMarket(event.target.value as FiveLayerMarket)} className="w-full bg-transparent text-sm outline-none">
                {MARKET_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value} className="bg-base text-foreground">{option.label}</option>
                ))}
              </select>
            </div>
            <div className="rounded-2xl border border-white/8 bg-white/4 px-4 py-3">
              <label className="mb-1 block text-xs text-secondary-text">评估窗口</label>
              <input aria-label="评估窗口" type="number" min={1} max={120} value={evalDays} onChange={(event) => setEvalDays(event.target.value)} className="w-full bg-transparent text-sm outline-none" />
            </div>
            <label className="flex items-center gap-2 rounded-2xl border border-white/8 bg-white/4 px-4 py-3 text-sm text-secondary-text">
              <input type="checkbox" checked={generateRecommendations} onChange={(event) => setGenerateRecommendations(event.target.checked)} />
              生成建议
            </label>
            <button type="button" className="btn-primary" onClick={() => void handleRun()} disabled={isRunning}>
              {isRunning ? '运行中...' : '运行五层回测'}
            </button>
          </div>
        ) : null}

        {runError ? <ApiErrorAlert error={runError} className="mt-3" /> : null}
      </header>

      {pageError ? <ApiErrorAlert error={pageError} /> : null}

      <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
        <div className="space-y-4">
          <StrategyWorkbenchSidebar
            items={strategySidebarItems}
            selectedKey={effectiveSelectedStrategyKey}
            onSelect={setSelectedStrategyKey}
          />
        </div>

        <div className="space-y-4">
          {selectedStrategyItem ? (
            <StrategyResearchCanvas
              strategyName={selectedStrategyItem.displayName}
              strategyMetaLabel={selectedStrategyItem.metaLabel}
              conclusion={researchDegradedState.active ? researchDegradedState.message : selectedStrategyConclusion}
              warningTag={selectedStrategyWarning}
              summary={selectedStrategySummary}
              cohortSummary={selectedStrategyCohort}
              rankingEffectiveness={rankingEffectiveness}
              strategyKey={selectedStrategyItem.strategyKey}
              strategySelectionKey={selectedStrategyItem.key}
              recommendations={recommendations}
              researchDegradedState={researchDegradedState}
              representativeEvaluation={representativeEvaluation}
              evaluations={filteredEvaluations}
              sampleFocus={sampleFocus}
              sampleBucketFilter={sampleBucketFilter}
              entryTimingFilter={entryTimingFilter}
              onSampleFocusChange={setSampleFocus}
              onSampleBucketFilterChange={setSampleBucketFilter}
              onEntryTimingFilterChange={setEntryTimingFilter}
              onAbnormalSampleSelect={handleAbnormalSampleSelect}
            />
          ) : (
            <EmptySection title="研究画布" hint="运行回测后，这里会围绕当前策略展示结论、结构、验证与证据链。" />
          )}

          {(tradeStageSummaries.length > 0 || maturitySummaries.length > 0) ? (
            <Card title="判断验证" subtitle="Validation Matrix" variant="gradient">
              <div className="grid gap-3 lg:grid-cols-4">
                <div className="rounded-2xl bg-white/5 p-4 text-sm">
                  <div className="label-uppercase">交易阶段</div>
                  <div className="mt-2 font-semibold text-white">
                    {tradeStageSummaries[0]?.groupKey ?? '--'}
                  </div>
                  <div className="mt-3 text-secondary-text">
                    当前最显著阶段准确率 {pct(tradeStageSummaries[0]?.stageAccuracyRate != null ? tradeStageSummaries[0].stageAccuracyRate * 100 : null)}
                  </div>
                </div>
                <div className="rounded-2xl bg-white/5 p-4 text-sm">
                  <div className="label-uppercase">成熟度分级</div>
                  <div className="mt-2 font-semibold text-white">
                    {maturitySummaries[0]?.groupKey ?? '--'}
                  </div>
                  <div className="mt-3 text-secondary-text">
                    高等级样本平均收益 {pct(maturitySummaries[0]?.avgReturnPct)}
                  </div>
                </div>
                <div className="rounded-2xl bg-white/5 p-4 text-sm">
                  <div className="label-uppercase">分级摘要</div>
                  <div className="mt-2 font-semibold text-white">
                    {`${effectiveRankingComparisons.length} 个有效维度 / ${watchRankingComparisons.length} 个观察维度`}
                  </div>
                  <div className="mt-3 text-secondary-text">
                    {effectiveRankingComparisons.length > 0
                      ? `本运行全样本的有效维度示例：${renderRankingDimensionLabel(effectiveRankingComparisons[0]?.dimension)}`
                      : '本运行全样本暂无明确有效维度，优先做观察研究。'}
                  </div>
                  <div className="mt-2 text-secondary-text">
                    {watchRankingComparisons.length > 0
                      ? `本运行全样本优先观察：${renderRankingDimensionLabel(watchRankingComparisons[0]?.dimension)}`
                      : '本运行全样本返回维度均已给出正向分层信号。'}
                  </div>
                </div>
                <div className="rounded-2xl bg-white/5 p-4 text-sm">
                  <div className="label-uppercase">买点时机分布</div>
                  <div className="mt-3 grid gap-2">
                    {selectedStrategyEntryTimingDistribution.map((item) => (
                      <div key={item.label} className="rounded-xl border border-white/8 bg-black/10 px-3 py-2 font-mono text-foreground">
                        {renderEntryTimingLabel(item.label)}: {item.count}
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </Card>
          ) : (
            <EmptySection title="判断验证" hint="这里会展示交易阶段、成熟度和 MAE 精度验证。" />
          )}

          {browserEvaluations.length > 0 ? (
            <EvaluationDetail
              evaluations={browserEvaluations}
              isLoading={isRunning || isRefreshing}
              title="样本浏览器"
              subtitle="Sample Explorer"
              targetEvaluation={targetEvaluation}
              researchWarning={researchDegradedState.active ? researchDegradedState.detail : null}
            />
          ) : (
            <EmptySection title="样本浏览器" hint="当前筛选条件下暂无可下钻样本。" />
          )}
        </div>
      </div>
    </div>
  );
};

export default BacktestPage;
