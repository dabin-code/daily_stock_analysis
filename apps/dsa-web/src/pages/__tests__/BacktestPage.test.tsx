import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import BacktestPage from '../BacktestPage';

const screeningRunsResponse = {
  total: 2,
  items: [
    {
      runId: 'sr-20260415-002',
      mode: 'balanced',
      status: 'completed',
      tradeDate: '2026-04-15',
      market: 'cn',
      universeSize: 5000,
      candidateCount: 18,
      aiTopK: 10,
      failedSymbols: [],
      warnings: [],
      syncFailureRatio: 0,
      configSnapshot: {},
      notificationAttempts: 0,
      startedAt: '2026-04-15T09:30:00',
      completedAt: '2026-04-15T09:35:00',
    },
    {
      runId: 'sr-20260414-001',
      mode: 'balanced',
      status: 'completed',
      tradeDate: '2026-04-14',
      market: 'cn',
      universeSize: 5000,
      candidateCount: 15,
      aiTopK: 10,
      failedSymbols: [],
      warnings: [],
      syncFailureRatio: 0,
      configSnapshot: {},
      notificationAttempts: 0,
      startedAt: '2026-04-14T09:30:00',
      completedAt: '2026-04-14T09:35:00',
    },
  ],
};

const runResponse = {
  run: {
    backtestRunId: 'flbt-test123456',
    evaluationMode: 'historical_snapshot',
    executionModel: 'conservative',
    tradeDateFrom: '2026-03-01',
    tradeDateTo: '2026-03-31',
    market: 'cn',
    status: 'completed',
    sampleCount: 20,
    completedCount: 20,
    errorCount: 0,
    sampleBaseline: {
      rawSampleCount: 20,
      evaluatedSampleCount: 18,
      aggregatableSampleCount: 14,
      entrySampleCount: 9,
      observationSampleCount: 5,
      suppressedSampleCount: 4,
      suppressedReasons: {
        observation_only: 2,
        threshold_suppressed: 2,
      },
    },
    createdAt: '2026-04-01T10:00:00',
    completedAt: '2026-04-01T10:05:00',
  },
  summaries: [
    {
      groupType: 'overall',
      groupKey: 'all',
      sampleCount: 20,
      avgReturnPct: 2.3,
      winRatePct: 60,
      avgDrawdown: -2.1,
      profitFactor: 1.8,
      avgHoldingDays: 4.2,
      planExecutionRate: 0.65,
      stageAccuracyRate: 0.7,
      systemGrade: 'A',
      timeBucketStability: 0.1,
    },
    {
      groupType: 'setup_type',
      groupKey: 'trend_breakout',
      sampleCount: 10,
      avgReturnPct: 3.1,
      winRatePct: 65,
      avgMae: -1.8,
      profitFactor: 2.2,
      planExecutionRate: 0.7,
      systemGrade: 'A',
    },
    {
      groupType: 'setup_type',
      groupKey: 'low123_breakout',
      sampleCount: 6,
      avgReturnPct: 4.6,
      winRatePct: 58,
      avgMae: -2.4,
      profitFactor: 1.7,
      planExecutionRate: 0.6,
      systemGrade: 'B+',
    },
    {
      groupType: 'trade_stage',
      groupKey: 'probe_entry',
      sampleCount: 8,
      avgReturnPct: 2.8,
      winRatePct: 62.5,
      stageAccuracyRate: 0.75,
    },
    {
      groupType: 'entry_maturity',
      groupKey: 'HIGH',
      sampleCount: 6,
      avgReturnPct: 3.5,
      winRatePct: 66.7,
    },
    {
      groupType: 'strategy_cohort',
      groupKey: 'ps=ma100_low123_combined|sb=core|mr=balanced|cp=leader_pool|em=high',
      sampleCount: 4,
      avgReturnPct: 4.2,
      winRatePct: 75,
      profitFactor: 2.4,
      strategyCohortContext: {
        primaryStrategy: 'ma100_low123_combined',
        sampleBucket: 'core',
        snapshotMarketRegime: 'balanced',
        snapshotCandidatePoolLevel: 'leader_pool',
        snapshotEntryMaturity: 'high',
      },
      familyBreakdown: {
        entry: { sampleCount: 3, avgReturnPct: 5.1 },
      },
    },
    {
      groupType: 'strategy_cohort',
      groupKey: 'ps=ma100_low123_combined|sb=boundary|mr=weak|cp=secondary_pool|em=low',
      sampleCount: 2,
      avgReturnPct: 1.4,
      winRatePct: 50,
      profitFactor: 1.1,
      strategyCohortContext: {
        primaryStrategy: 'ma100_low123_combined',
        sampleBucket: 'boundary',
        snapshotMarketRegime: 'weak',
        snapshotCandidatePoolLevel: 'secondary_pool',
        snapshotEntryMaturity: 'low',
      },
      familyBreakdown: {
        entry: { sampleCount: 1, avgReturnPct: 2.2 },
      },
    },
  ],
  recommendations: [
    {
      recommendationType: 'weight_increase',
      targetScope: 'setup_type',
      targetKey: 'trend_breakout',
      currentRule: 'setup_type=trend_breakout: current weight normal',
      suggestedChange: 'Consider increasing weight/priority for setup_type=trend_breakout',
      recommendationLevel: 'actionable',
      sampleCount: 18,
      confidence: 0.92,
      validationStatus: 'pending',
    },
    {
      recommendationType: 'execution_review',
      targetScope: 'signal_family',
      targetKey: 'entry',
      currentRule: 'signal_family=entry: win_rate positive but returns negative',
      suggestedChange: 'Review execution model for signal_family=entry',
      recommendationLevel: 'hypothesis',
      sampleCount: 12,
      confidence: 0.68,
      validationStatus: 'pending',
    },
    {
      recommendationType: 'weight_decrease',
      targetScope: 'setup_type',
      targetKey: 'low123_breakout',
      currentRule: 'setup_type=low123_breakout: current weight normal',
      suggestedChange: 'Consider decreasing weight/filtering out setup_type=low123_breakout',
      recommendationLevel: 'observation',
      sampleCount: 8,
      confidence: 0.32,
      validationStatus: 'pending',
    },
  ],
};

const resultsResponse = {
  backtestRunId: 'flbt-test123456',
  total: 2,
  page: 1,
  limit: 20,
  items: [
    {
      id: 1,
      backtestRunId: 'flbt-test123456',
      code: '600519',
      name: '贵州茅台',
      tradeDate: '2026-03-15',
      signalFamily: 'entry',
      evaluatorType: 'entry',
      signalType: 'buy',
      snapshotTradeStage: 'probe_entry',
      snapshotSetupType: 'trend_breakout',
      entryFillPrice: 1800,
      plannedEntryPrice: 1800,
      plannedStopLossPrice: 1746,
      plannedTakeProfitPrice: 1890,
      actualEntryPrice: 1800,
      actualEntryDate: '2026-03-16',
      actualExitPrice: 1890,
      actualExitDate: '2026-03-18',
      exitReason: 'take_profit',
      tradeReturnPct: 5.0,
      tradeReplayStatus: 'completed',
      forwardReturn5d: 2.5,
      forwardReturn10d: 4.1,
      mae: -1.2,
      mfe: 5.6,
      outcome: 'win',
      planSuccess: true,
      signalQualityScore: 0.78,
      primaryStrategy: 'bottom_divergence_double_breakout',
      contributingStrategies: ['volume_breakout'],
      sampleBucket: 'core',
      entryTimingLabel: 'on_time',
      snapshotMarketRegime: 'balanced',
      snapshotCandidatePoolLevel: 'leader_pool',
      snapshotEntryMaturity: 'high',
      factorSnapshotJson: '{"ma100_breakout_days":3}',
      tradePlanJson: '{"take_profit":5,"stop_loss":-3}',
      evalStatus: 'evaluated',
    },
    {
      id: 3,
      backtestRunId: 'flbt-test123456',
      code: '300750',
      name: '宁德时代',
      tradeDate: '2026-03-18',
      signalFamily: 'entry',
      evaluatorType: 'entry',
      signalType: 'buy',
      snapshotTradeStage: 'probe_entry',
      snapshotSetupType: 'low123_breakout',
      entryFillPrice: 221,
      forwardReturn5d: 6.2,
      forwardReturn10d: 8.9,
      mae: -2.6,
      mfe: 9.8,
      outcome: 'win',
      planSuccess: true,
      signalQualityScore: 0.82,
      primaryStrategy: 'ma100_low123_combined',
      contributingStrategies: ['volume_breakout'],
      sampleBucket: 'boundary',
      entryTimingLabel: 'too_early',
      snapshotMarketRegime: 'weak',
      snapshotCandidatePoolLevel: 'secondary_pool',
      snapshotEntryMaturity: 'low',
      ma100Low123ValidationStatus: 'confirmed_missing_breakout_bar_index',
      matchedStrategies: ['ma100_low123_combined', 'volume_breakout'],
      ruleHits: ['ma100_low123_confirmed:==:True'],
      factorSnapshotJson: '{"ma100_low123_validation_status":"confirmed_missing_breakout_bar_index"}',
      tradePlanJson: '{"take_profit":8,"stop_loss":-4}',
      evalStatus: 'evaluated',
    },
    {
      id: 2,
      backtestRunId: 'flbt-test123456',
      code: '000001',
      name: '平安银行',
      tradeDate: '2026-03-16',
      signalFamily: 'observation',
      evaluatorType: 'observation',
      snapshotTradeStage: 'watch',
      riskAvoidedPct: 3.2,
      opportunityCostPct: 1.1,
      stageSuccess: true,
      outcome: 'correct_wait',
      primaryStrategy: 'ma100_low123_combined',
      contributingStrategies: ['volume_breakout'],
      sampleBucket: 'boundary',
      entryTimingLabel: 'not_applicable',
      snapshotMarketRegime: 'weak',
      snapshotCandidatePoolLevel: 'secondary_pool',
      snapshotEntryMaturity: 'low',
      ma100Low123ValidationStatus: 'confirmed_missing_breakout_bar_index',
      factorSnapshotJson: '{"low_123_state":"structure_only"}',
      tradePlanJson: '{}',
      evalStatus: 'evaluated',
    },
    {
      id: 4,
      backtestRunId: 'flbt-test123456',
      code: '688981',
      name: '中芯国际',
      tradeDate: '2026-03-19',
      signalFamily: 'entry',
      evaluatorType: 'entry',
      signalType: 'buy',
      snapshotTradeStage: 'probe_entry',
      snapshotEntryMaturity: 'high',
      snapshotMarketRegime: 'balanced',
      snapshotCandidatePoolLevel: 'leader_pool',
      forwardReturn5d: 7.1,
      forwardReturn10d: 10.5,
      mae: -2.0,
      mfe: 12.2,
      outcome: 'win',
      planSuccess: true,
      signalQualityScore: 0.84,
      primaryStrategy: 'ma100_low123_combined',
      contributingStrategies: ['volume_breakout'],
      sampleBucket: 'core',
      entryTimingLabel: 'on_time',
      ma100Low123ValidationStatus: 'confirmed',
      matchedStrategies: ['ma100_low123_combined'],
      factorSnapshotJson: '{"ma100_low123_validation_status":"confirmed"}',
      tradePlanJson: '{"take_profit":10,"stop_loss":-4}',
      evalStatus: 'evaluated',
    },
    {
      id: 5,
      backtestRunId: 'flbt-test123456',
      code: '002594',
      name: '比亚迪',
      tradeDate: '2026-03-20',
      signalFamily: 'observation',
      evaluatorType: 'observation',
      snapshotTradeStage: 'watch',
      snapshotSetupType: 'low123_breakout',
      snapshotEntryMaturity: 'low',
      snapshotMarketRegime: 'weak',
      snapshotCandidatePoolLevel: 'secondary_pool',
      riskAvoidedPct: 1.8,
      opportunityCostPct: 4.6,
      stageSuccess: false,
      outcome: 'missed_watch',
      primaryStrategy: 'ma100_low123_combined',
      contributingStrategies: ['volume_breakout'],
      sampleBucket: 'noise',
      entryTimingLabel: 'too_late',
      ma100Low123ValidationStatus: 'unconfirmed_breakout_bar_index',
      matchedStrategies: ['ma100_low123_combined'],
      factorSnapshotJson: '{"low_123_state":"late_breakout"}',
      tradePlanJson: '{}',
      evalStatus: 'evaluated',
    },
  ],
};

const rankingResponse = {
  comparisons: [
    {
      dimension: 'entry_maturity',
      tierHigh: 'HIGH',
      tierLow: 'LOW',
      highAvgReturn: 3.5,
      lowAvgReturn: 1.2,
      excessReturnPct: 2.3,
      highWinRate: 66.7,
      lowWinRate: 45,
      highSampleCount: 6,
      lowSampleCount: 4,
      isEffective: true,
    },
    {
      dimension: 'candidate_pool_level',
      tierHigh: 'CORE_POOL',
      tierLow: 'EDGE_POOL',
      highAvgReturn: 1.4,
      lowAvgReturn: 1.1,
      excessReturnPct: 0.3,
      highWinRate: 52,
      lowWinRate: 49,
      highSampleCount: 8,
      lowSampleCount: 7,
      isEffective: false,
    },
  ],
  overallEffectivenessRatio: 0.5,
  topKHitRate: 0.6,
  excessReturnPct: 1.2,
  rankingConsistency: 0.5,
};

const apiMock = vi.hoisted(() => ({
  run: vi.fn(),
  runByScreeningRun: vi.fn(),
  getResults: vi.fn(),
  getRunDetail: vi.fn(),
  getSummaries: vi.fn(),
  getRecommendations: vi.fn(),
  getRankingEffectiveness: vi.fn(),
}));

const screeningApiMock = vi.hoisted(() => ({
  listRuns: vi.fn(),
}));

vi.mock('../../api/backtest', () => ({
  backtestApi: apiMock,
}));

vi.mock('../../api/screening', () => ({
  screeningApi: screeningApiMock,
}));

vi.mock('../../api/error', () => ({
  getParsedApiError: vi.fn((error: unknown) => error),
}));

describe('BacktestPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.run.mockResolvedValue(runResponse);
    apiMock.runByScreeningRun.mockResolvedValue(runResponse);
    apiMock.getResults.mockResolvedValue(resultsResponse);
    apiMock.getRankingEffectiveness.mockResolvedValue(rankingResponse);
    apiMock.getRunDetail.mockResolvedValue(runResponse.run);
    apiMock.getSummaries.mockResolvedValue({ backtestRunId: 'flbt-test123456', items: runResponse.summaries });
    apiMock.getRecommendations.mockResolvedValue({ backtestRunId: 'flbt-test123456', items: runResponse.recommendations });
    screeningApiMock.listRuns.mockResolvedValue(screeningRunsResponse);
  });

  it('loads recent screening runs and uses the latest run as the default backtest anchor', async () => {
    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    expect(apiMock.runByScreeningRun).toHaveBeenCalledWith({
      screeningRunId: 'sr-20260415-002',
      evaluationMode: 'historical_snapshot',
      executionModel: 'conservative',
      market: 'cn',
      evalWindowDays: 10,
      generateRecommendations: true,
    });
    expect(apiMock.run).not.toHaveBeenCalled();
  });

  it('keeps date-range replay mode available as an explicit fallback entry', async () => {
    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.change(screen.getByRole('combobox', { name: '运行入口' }), {
      target: { value: 'replay' },
    });
    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.run).toHaveBeenCalledTimes(1);
    });

    expect(apiMock.runByScreeningRun).not.toHaveBeenCalled();
  });

  it('renders four-layer backtest layout after running a backtest', async () => {
    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    expect(await screen.findByText('研究工作台')).toBeInTheDocument();
    expect(screen.getByText('策略分布')).toBeInTheDocument();
    expect(screen.getByText('研究画布')).toBeInTheDocument();
    expect(screen.getByText('样本浏览器')).toBeInTheDocument();
    expect(screen.getByText('当前策略结论')).toBeInTheDocument();
    expect(screen.getByText('策略表现')).toBeInTheDocument();
    expect(screen.getByText('证据链与归因')).toBeInTheDocument();
    expect(screen.getAllByText('趋势突破').length).toBeGreaterThan(0);
    expect(screen.queryByText('研究降级态')).not.toBeInTheDocument();
  });

  const setLayeredSetupPayload = (primaryStrategies: Array<string | null>) => {
    const layeredResponse = {
      ...runResponse,
      summaries: [
        ...runResponse.summaries.filter((item) => item.groupType !== 'setup_type'),
        {
          groupType: 'setup_type',
          groupKey: 'bottom_divergence_layered_entry',
          sampleCount: 7,
          avgReturnPct: 2.8,
          winRatePct: 57.1,
          profitFactor: 1.4,
        },
      ],
    };
    apiMock.runByScreeningRun.mockResolvedValue(layeredResponse);
    apiMock.getResults.mockResolvedValue({
      ...resultsResponse,
      total: primaryStrategies.length,
      items: primaryStrategies.map((primaryStrategy, index) => ({
        ...resultsResponse.items[0],
        id: 99 + index,
        code: `00133${index}`,
        name: '四川黄金',
        snapshotSetupType: 'bottom_divergence_layered_entry',
        primaryStrategy,
        matchedStrategies: primaryStrategy ? [primaryStrategy] : [],
      })),
    });
  };

  const renderLayeredSetup = async () => {
    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });
    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));
    await screen.findAllByText('底背离分层入场');
  };

  it('does not infer setup attribution from 200 uniquely attributed evaluations', async () => {
    setLayeredSetupPayload(Array.from(
      { length: 200 },
      () => 'bottom_divergence_layered_entry_v2',
    ));

    await renderLayeredSetup();

    expect(screen.queryByText(/主策略：底背离分层入场 v2/)).not.toBeInTheDocument();
    expect(screen.queryByText('bottom_divergence_layered_entry_v2', { selector: 'button' })).not.toBeInTheDocument();
    expect(screen.getByText('策略主归因')).toBeInTheDocument();
    expect(screen.getAllByText('bottom_divergence_layered_entry_v2').length).toBeGreaterThan(0);
  });

  it('does not infer mixed setup attribution from 200 paginated evaluations', async () => {
    setLayeredSetupPayload(Array.from(
      { length: 200 },
      (_, index) => (
        index % 2 === 0
          ? 'bottom_divergence_double_breakout'
          : 'bottom_divergence_layered_entry_v2'
      ),
    ));

    await renderLayeredSetup();

    expect(screen.queryByText(/主策略：混合策略/)).not.toBeInTheDocument();
    expect(screen.queryByText(/主策略：底背离/)).not.toBeInTheDocument();
  });

  it('shows dual run and research context after loading a run', async () => {
    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    expect(await screen.findByText('运行上下文')).toBeInTheDocument();
    expect(screen.getByText('研究上下文')).toBeInTheDocument();
    expect(screen.getByText('原始样本')).toBeInTheDocument();
    expect(screen.getByText('已评估')).toBeInTheDocument();
    expect(screen.getByText('可汇总')).toBeInTheDocument();
    expect(screen.getAllByText('入场样本').length).toBeGreaterThan(0);
    expect(screen.getByText('观察样本')).toBeInTheDocument();
    expect(screen.getByText('已抑制')).toBeInTheDocument();
    expect(screen.getByText('仅观察样本 2')).toBeInTheDocument();
    expect(screen.getByText('样本阈值不足 2')).toBeInTheDocument();
  });

  it('explains low-sample observation-only runs in Chinese instead of implying tradable returns', async () => {
    const observationOnlyRunResponse = {
      ...runResponse,
      run: {
        ...runResponse.run,
        sampleCount: 10,
        completedCount: 10,
        sampleBaseline: {
          rawSampleCount: 10,
          evaluatedSampleCount: 10,
          aggregatableSampleCount: 10,
          entrySampleCount: 0,
          observationSampleCount: 10,
          suppressedSampleCount: 0,
          suppressedReasons: {},
        },
      },
      summaries: runResponse.summaries.map((item) => ({
        ...item,
        familyBreakdown: { observation: { sampleCount: item.sampleCount, avgReturnPct: item.avgReturnPct } },
      })),
    };
    const observationOnlyResultsResponse = {
      ...resultsResponse,
      total: 10,
      items: resultsResponse.items.slice(0, 2).map((item) => ({
        ...item,
        signalFamily: 'observation',
        forwardReturn5d: null,
        riskAvoidedPct: 2.1,
        entryTimingLabel: 'not_applicable',
        outcome: 'correct_wait',
      })),
    };

    apiMock.runByScreeningRun.mockResolvedValue(observationOnlyRunResponse);
    apiMock.getResults.mockResolvedValue(observationOnlyResultsResponse);

    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    expect(await screen.findByText('样本不足，结论仅供观察')).toBeInTheDocument();
    expect(screen.getByText('本次回测没有入场样本，不能用于判断买入收益或买点有效性。')).toBeInTheDocument();
    expect(screen.getByText('观察样本只说明“等待/观望是否避开风险”，不是买入后的收益。')).toBeInTheDocument();
    expect(screen.getByText('收益类指标已降级为观察指标')).toBeInTheDocument();
    expect(screen.getByText('观察避险')).toBeInTheDocument();
  });

  it('elevates ranking effectiveness into the main research narrative', async () => {
    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    expect(apiMock.getRankingEffectiveness).toHaveBeenCalledWith('flbt-test123456');

    expect(await screen.findByText('分级有效性结论')).toBeInTheDocument();
    expect(screen.getByText('本区展示的是本次回测运行的全样本分级汇总，不是当前策略子集专属结论。')).toBeInTheDocument();
    expect(screen.getByText('入场成熟度')).toBeInTheDocument();
    expect(screen.getByText('候选池层级')).toBeInTheDocument();
    expect(screen.getByText('有效')).toBeInTheDocument();
    expect(screen.getByText('仍需观察')).toBeInTheDocument();
    expect(screen.getByText('HIGH vs LOW')).toBeInTheDocument();
    expect(screen.getByText('CORE_POOL vs EDGE_POOL')).toBeInTheDocument();
    expect(screen.getByText('样本 6 vs 4')).toBeInTheDocument();
    expect(screen.getByText('样本 8 vs 7')).toBeInTheDocument();
    expect(screen.getByText('超额 2.3%')).toBeInTheDocument();
    expect(screen.getByText('超额 0.3%')).toBeInTheDocument();
  });

  it('connects recommendations into the research conclusion area', async () => {
    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    expect(await screen.findByText('研究动作建议')).toBeInTheDocument();
    expect(screen.getByText('优先动作')).toBeInTheDocument();
    expect(screen.getByText('继续观察')).toBeInTheDocument();
    expect(screen.getByText('仅展示')).toBeInTheDocument();
    expect(screen.getByText('建议加权')).toBeInTheDocument();
    expect(screen.getByText('复核执行')).toBeInTheDocument();
    expect(screen.getByText('建议降权')).toBeInTheDocument();
    expect(screen.getAllByText('趋势突破').length).toBeGreaterThan(0);
    expect(screen.getByText('信号族 · 入场')).toBeInTheDocument();
    expect(screen.getAllByText('低位123').length).toBeGreaterThan(0);
    expect(screen.getByText('仅到展示层级，暂不形成动作。')).toBeInTheDocument();
    expect(screen.getByText('当前策略')).toBeInTheDocument();
    expect(screen.getAllByText('运行级').length).toBeGreaterThan(0);
  });

  it('reloads recommendations on refresh and keeps the recommendation buckets visible', async () => {
    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(await screen.findByRole('button', { name: /刷新运行/i }));

    await waitFor(() => {
      expect(apiMock.getRecommendations).toHaveBeenCalledWith('flbt-test123456');
    });

    expect(await screen.findByText('研究动作建议')).toBeInTheDocument();
    expect(screen.getByText('优先动作')).toBeInTheDocument();
    expect(screen.getByText('继续观察')).toBeInTheDocument();
    expect(screen.getByText('仅展示')).toBeInTheDocument();
  });

  it('shows degraded research state when observation samples dominate and attribution is incomplete', async () => {
    const degradedResultsResponse = {
      ...resultsResponse,
      items: resultsResponse.items.map((item) => ({
        ...item,
        signalFamily: 'observation',
        primaryStrategy: null,
        contributingStrategies: [],
        entryTimingLabel: 'not_applicable',
        outcome: 'correct_wait',
        forwardReturn5d: null,
        riskAvoidedPct: 1.5,
      })),
    };

    apiMock.getResults.mockResolvedValue(degradedResultsResponse);

    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    expect(await screen.findByText('研究降级态')).toBeInTheDocument();
    expect(screen.getByText('观察样本主导')).toBeInTheDocument();
    expect(screen.getByText('无主策略归因')).toBeInTheDocument();
    expect(screen.getByText('买点语义不足')).toBeInTheDocument();
    expect(screen.getByText('当前仅适合观察研究，不适合买点结论。')).toBeInTheDocument();
    expect(screen.getAllByText('当前运行以观察样本为主，缺少稳定主策略归因，买点时机标签仍不可用。').length).toBeGreaterThan(0);
  });

  it('keeps loaded run context stable while editing rerun draft anchors', async () => {
    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(await screen.findByRole('button', { name: /重新运行回测/i }));
    fireEvent.change(screen.getByRole('combobox', { name: '运行入口' }), {
      target: { value: 'replay' },
    });
    fireEvent.change(screen.getByRole('combobox', { name: '选股运行' }), {
      target: { value: 'sr-20260414-001' },
    });

    expect(screen.getByText('研究模式')).toBeInTheDocument();
    expect(screen.getAllByText('研究模式（按选股运行）').length).toBeGreaterThan(0);
    expect(screen.getByText('sr-20260415-002')).toBeInTheDocument();
  });

  it('collapses rerun controls after a run is available', async () => {
    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    expect(await screen.findByRole('button', { name: /重新运行回测/i })).toBeInTheDocument();
    expect(screen.queryByText('开始日期')).not.toBeInTheDocument();
    expect(screen.queryByText('结束日期')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /重新运行回测/i }));

    expect(await screen.findByText('开始日期')).toBeInTheDocument();
    expect(screen.getByText('结束日期')).toBeInTheDocument();
  });

  it('keeps research context bound to the loaded run while editing rerun draft inputs', async () => {
    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(await screen.findByRole('button', { name: /重新运行回测/i }));
    fireEvent.change(screen.getByRole('combobox', { name: '市场' }), { target: { value: 'us' } });

    expect(screen.getAllByText('A股').length).toBeGreaterThan(0);
  });

  it('switches research canvas when selecting another strategy', async () => {
    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(await screen.findByRole('button', { name: /低位123/i }));

    expect(await screen.findByText('MA100+123 组合当前更偏向高弹性研究标的')).toBeInTheDocument();
    expect(screen.getByText('ma100_low123_combined')).toBeInTheDocument();
    expect(screen.getByText('300750')).toBeInTheDocument();
    expect(screen.getByText('边界样本偏多')).toBeInTheDocument();
  });

  it('shows structured attribution details in sample browser drawer', async () => {
    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(await screen.findByRole('button', { name: /600519 贵州茅台/i }));

    expect(await screen.findByText('主策略归因')).toBeInTheDocument();
    expect(screen.getByText('bottom_divergence_double_breakout')).toBeInTheDocument();
    expect(screen.getAllByText('样本分层').length).toBeGreaterThan(0);
    expect(screen.getAllByText('核心样本').length).toBeGreaterThan(0);
    expect(screen.getByText('买点时机')).toBeInTheDocument();
    expect(screen.getByText('时机合适')).toBeInTheDocument();
    expect(screen.getByText('真实交易回放')).toBeInTheDocument();
    expect(screen.getByText('真实收益')).toBeInTheDocument();
    expect(screen.getAllByText('5.0%').length).toBeGreaterThan(0);
    expect(screen.getByText('止盈离场')).toBeInTheDocument();
  });

  it('renders strategy cohort insights when only cohort summaries exist', async () => {
    const cohortOnlyResponse = {
      ...runResponse,
      summaries: runResponse.summaries.filter((item) => item.groupType !== 'setup_type'),
    };
    apiMock.run.mockResolvedValue(cohortOnlyResponse);
    apiMock.runByScreeningRun.mockResolvedValue(cohortOnlyResponse);
    apiMock.getSummaries.mockResolvedValue({
      backtestRunId: 'flbt-test123456',
      items: runResponse.summaries.filter((item) => item.groupType !== 'setup_type'),
    });

    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    expect(await screen.findByText('策略分布')).toBeInTheDocument();
    expect(screen.getAllByText(/MA100\+123 组合/i).length).toBeGreaterThan(0);
    expect(screen.getByText('研究画布')).toBeInTheDocument();
    expect(screen.getByText('P0 策略群组')).toBeInTheDocument();
  });

  it('filters cohort samples by cohort context when multiple cohorts share one primary strategy', async () => {
    const cohortOnlyResponse = {
      ...runResponse,
      summaries: runResponse.summaries.filter((item) => item.groupType !== 'setup_type'),
    };
    apiMock.run.mockResolvedValue(cohortOnlyResponse);
    apiMock.runByScreeningRun.mockResolvedValue(cohortOnlyResponse);
    apiMock.getSummaries.mockResolvedValue({
      backtestRunId: 'flbt-test123456',
      items: runResponse.summaries.filter((item) => item.groupType !== 'setup_type'),
    });

    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    const cohortStrategyButtons = await screen.findAllByText(/MA100\+123 组合/i);
    fireEvent.click(cohortStrategyButtons[0].closest('button') as HTMLButtonElement);

    expect(await screen.findByText('688981')).toBeInTheDocument();
    expect(screen.queryByText('300750')).not.toBeInTheDocument();
  });

  it('renders deeper sample structure, validation, and abnormal sample insights for current strategy', async () => {
    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(await screen.findByRole('button', { name: /低位123/i }));

    expect(await screen.findByText('样本结构拆解')).toBeInTheDocument();
    expect(screen.getByText('市场环境分布')).toBeInTheDocument();
    expect(screen.getByText('均衡: 0')).toBeInTheDocument();
    expect(screen.getByText('偏弱: 2')).toBeInTheDocument();
    expect(screen.getByText('买点时机分布')).toBeInTheDocument();
    expect(screen.getByText(/偏早:\s*1/)).toBeInTheDocument();
    expect(screen.getByText(/偏晚:\s*1/)).toBeInTheDocument();
    expect(screen.getByText('异常样本区')).toBeInTheDocument();
    expect(screen.getByText('002594')).toBeInTheDocument();
    expect(screen.getByText('噪音样本')).toBeInTheDocument();
  });

  it('links research filters to the sample browser', async () => {
    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(await screen.findByRole('button', { name: /低位123/i }));
    fireEvent.click(screen.getByRole('button', { name: '时机异常' }));
    fireEvent.click(screen.getByRole('button', { name: '时机: 偏晚' }));

    expect(await screen.findByText(/002594 比亚迪/i)).toBeInTheDocument();
    expect(screen.queryByText(/300750 宁德时代/i)).not.toBeInTheDocument();
  });

  it('navigates from an abnormal sample card into the sample browser', async () => {
    render(<BacktestPage />);

    await waitFor(() => {
      expect(screeningApiMock.listRuns).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /运行五层回测/i }));

    await waitFor(() => {
      expect(apiMock.runByScreeningRun).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(await screen.findByRole('button', { name: /低位123/i }));
    fireEvent.click(screen.getByRole('button', { name: /002594 比亚迪/i }));

    expect(await screen.findByText(/002594 比亚迪/i)).toBeInTheDocument();
    expect(screen.queryByText(/300750 宁德时代/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '观察信号' })).toHaveClass('ring-1');
    expect(screen.getByText('late_breakout')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '全部样本' }));
    fireEvent.click(screen.getByRole('button', { name: '全部分层' }));
    fireEvent.click(screen.getByRole('button', { name: '全部时机' }));
    fireEvent.click(screen.getByRole('button', { name: '入场信号' }));
    expect(screen.getByRole('button', { name: '入场信号' })).toHaveClass('ring-1');
  });
});
