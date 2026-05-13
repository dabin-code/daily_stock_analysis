import type React from 'react';
import { Badge } from '../common/Badge';
import { Card } from '../common/Card';
import type {
  BacktestRecommendationItem,
  BacktestResultItem,
  BacktestSummaryItem,
  RankingEffectivenessData,
} from '../../types/backtest';
import {
  getEvaluationAbnormalTags,
  type ResearchSampleFocus,
} from './researchFilters';

interface StrategyResearchCanvasProps {
  strategyName: string;
  strategyKey: string;
  strategySelectionKey: string;
  strategyMetaLabel?: string | null;
  conclusion: string;
  warningTag?: string | null;
  summary: BacktestSummaryItem | null;
  cohortSummary: BacktestSummaryItem | null;
  rankingEffectiveness: RankingEffectivenessData | null;
  recommendations: BacktestRecommendationItem[];
  researchDegradedState: {
    active: boolean;
    reasons: string[];
    message: string;
    detail: string;
  };
  representativeEvaluation: BacktestResultItem | null;
  evaluations: BacktestResultItem[];
  sampleFocus: ResearchSampleFocus;
  sampleBucketFilter: string | null;
  entryTimingFilter: string | null;
  onSampleFocusChange: (focus: ResearchSampleFocus) => void;
  onSampleBucketFilterChange: (sampleBucket: string | null) => void;
  onEntryTimingFilterChange: (entryTimingLabel: string | null) => void;
  onAbnormalSampleSelect: (item: BacktestResultItem) => void;
}

function pct(value?: number | null): string {
  if (value == null) return '--';
  return `${value.toFixed(1)}%`;
}

function num(value?: number | null): string {
  if (value == null) return '--';
  return value.toFixed(2);
}

function ratioPct(value?: number | null): string {
  if (value == null) return '--';
  const normalized = Math.abs(value) <= 1 ? value * 100 : value;
  return `${normalized.toFixed(1)}%`;
}

function countBy<T extends string>(items: T[]): Record<string, number> {
  return items.reduce<Record<string, number>>((acc, item) => {
    acc[item] = (acc[item] ?? 0) + 1;
    return acc;
  }, {});
}

function topDistributionLabel(entries: Record<string, number>): string {
  const first = Object.entries(entries).sort((a, b) => b[1] - a[1])[0];
  if (!first) return '--';
  return `${renderValueLabel(first[0])} (${first[1]})`;
}

function renderValueLabel(value?: string | null): string {
  switch (value) {
    case 'entry':
      return '入场';
    case 'observation':
      return '观察';
    case 'exit':
      return '卖出';
    case 'core':
      return '核心样本';
    case 'boundary':
      return '边界样本';
    case 'noise':
      return '噪音样本';
    case 'balanced':
      return '均衡';
    case 'weak':
      return '偏弱';
    case 'bull':
    case 'aggressive':
      return '进攻';
    case 'bear':
    case 'defensive':
      return '防守';
    case 'stand_aside':
      return '空仓观望';
    case 'leader_pool':
      return '龙头池';
    case 'focus_list':
      return '重点观察池';
    case 'watchlist':
      return '观察池';
    case 'high':
    case 'HIGH':
      return '高';
    case 'medium':
    case 'MEDIUM':
      return '中';
    case 'low':
    case 'LOW':
      return '低';
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

function renderPrimaryStrategy(value?: string | null): string {
  if (!value) return '--';
  if (value === 'ma100_low123_combined') {
    return value;
  }
  if (value === 'bottom_divergence_double_breakout') {
    return '底背离双突破';
  }
  return value;
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

function renderRecommendationType(value?: string | null): string {
  switch (value) {
    case 'weight_increase':
      return '建议加权';
    case 'weight_decrease':
      return '建议降权';
    case 'execution_review':
      return '复核执行';
    default:
      return value || '--';
  }
}

function renderRecommendationScope(value?: string | null): string {
  switch (value) {
    case 'setup_type':
      return 'Setup';
    case 'signal_family':
      return '信号族';
    case 'trade_stage':
      return '交易阶段';
    case 'entry_maturity':
      return '入场成熟度';
    case 'candidate_pool_level':
      return '候选池层级';
    case 'market_regime':
      return '市场环境';
    case 'strategy_cohort':
      return '策略分组';
    default:
      return value || '--';
  }
}

function renderSignalFamily(value?: string | null): string {
  switch (value) {
    case 'entry':
      return '入场';
    case 'observation':
      return '观察';
    case 'exit':
      return '卖出';
    default:
      return value || '--';
  }
}

function renderRecommendationTarget(scope?: string | null, key?: string | null): string {
  if (!key) {
    return '--';
  }
  if (scope === 'setup_type') {
    if (key === 'trend_breakout') return '趋势突破';
    if (key === 'low123_breakout') return '低位123';
    if (key === 'bottom_divergence_breakout' || key === 'bottom_divergence_double_breakout') return '底背离双突破';
    if (key === 'ma100_low123_combined') return 'MA100+123 组合';
  }
  if (scope === 'signal_family') {
    return `信号族 · ${renderSignalFamily(key)}`;
  }
  if (scope === 'entry_maturity' || scope === 'candidate_pool_level' || scope === 'trade_stage' || scope === 'market_regime') {
    return `${renderRecommendationScope(scope)} · ${key}`;
  }
  return key;
}

function renderValidationStatus(value?: string | null): string {
  switch (value) {
    case 'pending':
      return '待验证';
    case 'confirmed':
      return '已确认';
    case 'rejected':
      return '已拒绝';
    default:
      return value || '--';
  }
}

function isRecommendationRelevantToStrategy(
  recommendation: BacktestRecommendationItem,
  strategyKey: string,
  strategySelectionKey: string,
): boolean {
  if (recommendation.targetScope === 'setup_type') {
    return recommendation.targetKey === strategyKey;
  }
  if (recommendation.targetScope === 'strategy_cohort') {
    return recommendation.targetKey === strategySelectionKey;
  }
  return false;
}

function countWithPreset(
  items: Array<string | null | undefined>,
  presets: string[],
): Array<{ label: string; count: number }> {
  const counts = countBy(items.filter((value): value is string => Boolean(value)));
  return presets.map((label) => ({
    label,
    count: counts[label] ?? 0,
  }));
}

function isSameEvaluation(
  left: BacktestResultItem,
  right: BacktestResultItem | null,
): boolean {
  if (!right) {
    return false;
  }
  if (left.id != null && right.id != null) {
    return left.id === right.id;
  }
  return left.code === right.code && left.tradeDate === right.tradeDate;
}

export const StrategyResearchCanvas: React.FC<StrategyResearchCanvasProps> = ({
  strategyName,
  strategyKey,
  strategySelectionKey,
  strategyMetaLabel,
  conclusion,
  warningTag,
  summary,
  cohortSummary,
  rankingEffectiveness,
  recommendations,
  researchDegradedState,
  representativeEvaluation,
  evaluations,
  sampleFocus,
  sampleBucketFilter,
  entryTimingFilter,
  onSampleFocusChange,
  onSampleBucketFilterChange,
  onEntryTimingFilterChange,
  onAbnormalSampleSelect,
}) => {
  const performanceSummary = summary ?? cohortSummary;
  const entryEvaluations = evaluations.filter((item) => item.signalFamily === 'entry');
  const hasRealTradeReplay = entryEvaluations.some((item) => item.tradeReturnPct != null);
  const allEntrySamplesReplayed = entryEvaluations.length > 0
    && entryEvaluations.every((item) => item.tradeReturnPct != null);
  const sampleBuckets = countBy(
    evaluations
      .map((item) => item.sampleBucket)
      .filter((value): value is string => Boolean(value)),
  );
  const signalFamilies = countBy(
    evaluations
      .map((item) => item.signalFamily)
      .filter((value): value is string => Boolean(value)),
  );
  const marketRegimes = countWithPreset(
    evaluations.map((item) => item.snapshotMarketRegime),
    ['balanced', 'weak', 'bull', 'bear'],
  );
  const abnormalSamples = evaluations.filter((item) => (
    !isSameEvaluation(item, representativeEvaluation)
    && (
      getEvaluationAbnormalTags(item).length > 0
    )
  ));
  const sampleBucketOptions = Array.from(new Set(
    evaluations
      .map((item) => item.sampleBucket)
      .filter((value): value is string => Boolean(value)),
  ));
  const entryTimingOptions = Array.from(new Set(
    evaluations
      .map((item) => item.entryTimingLabel)
      .filter((value): value is string => Boolean(value)),
  ));
  const abnormalCount = abnormalSamples.length;
  const rankingComparisons = rankingEffectiveness?.comparisons ?? [];
  const entrySampleCount = evaluations.filter((item) => item.signalFamily === 'entry').length;
  const observationSampleCount = evaluations.filter((item) => item.signalFamily === 'observation').length;
  const isObservationOnly = evaluations.length > 0 && entrySampleCount === 0 && observationSampleCount > 0;
  const sortRecommendations = (items: BacktestRecommendationItem[]) => [...items].sort((left, right) => {
    const leftRelevant = isRecommendationRelevantToStrategy(left, strategyKey, strategySelectionKey) ? 1 : 0;
    const rightRelevant = isRecommendationRelevantToStrategy(right, strategyKey, strategySelectionKey) ? 1 : 0;
    if (rightRelevant !== leftRelevant) {
      return rightRelevant - leftRelevant;
    }
    return (right.confidence ?? -Infinity) - (left.confidence ?? -Infinity);
  });
  const actionableRecommendations = sortRecommendations(
    recommendations.filter((item) => item.recommendationLevel === 'actionable'),
  );
  const hypothesisRecommendations = sortRecommendations(
    recommendations.filter((item) => item.recommendationLevel === 'hypothesis'),
  );
  const observationRecommendations = sortRecommendations(
    recommendations.filter((item) => ['observation', 'display'].includes(item.recommendationLevel)),
  );
  const uncategorizedRecommendations = sortRecommendations(
    recommendations.filter((item) => !['actionable', 'hypothesis', 'observation', 'display'].includes(item.recommendationLevel)),
  );

  return (
    <Card title="研究画布" subtitle="Research Canvas" variant="gradient" className="shadow-[0_20px_60px_rgba(3,8,20,0.2)]">
      <div className="space-y-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="max-w-3xl">
            <div className="label-uppercase">当前策略结论</div>
            <h2 className="mt-2 text-2xl font-semibold text-white">{strategyName}研究</h2>
            <p className="mt-2 text-sm leading-6 text-secondary-text">
              {strategyMetaLabel || '围绕当前策略查看表现、样本结构与归因证据。'}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            {warningTag ? <Badge variant="warning">{warningTag}</Badge> : null}
            {cohortSummary?.strategyCohortContext?.primaryStrategy ? <Badge variant="info">P0 策略群组</Badge> : null}
            {summary?.systemGrade ? <Badge variant="success">评级 {summary.systemGrade}</Badge> : null}
            <Badge variant="default">{abnormalCount} 个异常样本</Badge>
          </div>
        </div>

        <div className="rounded-3xl border border-cyan/20 bg-[linear-gradient(135deg,rgba(0,212,255,0.12),rgba(255,255,255,0.02))] p-5">
          <p className="text-sm leading-6 text-white/90">{conclusion}</p>
        </div>

        {researchDegradedState.active ? (
          <div className="rounded-3xl border border-warning/20 bg-warning/5 p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="text-sm font-semibold text-white">研究降级态</div>
                <div className="mt-1 text-xs text-secondary-text">{researchDegradedState.detail}</div>
              </div>
              <Badge variant="warning">观察优先</Badge>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              {researchDegradedState.reasons.map((reason) => (
                <Badge key={reason} variant="warning">{reason}</Badge>
              ))}
            </div>
          </div>
        ) : null}

        {recommendations.length > 0 ? (
          <div className="rounded-3xl border border-white/8 bg-white/3 p-5">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="text-sm font-semibold text-white">研究动作建议</div>
                <div className="mt-1 text-xs text-secondary-text">把本次运行返回的研究建议拆成优先动作、继续观察和仅展示三层；当前策略标记只表示建议直接命中当前策略或策略分组。</div>
              </div>
              <div className="flex flex-wrap gap-2">
                <Badge variant="success">{`优先动作 ${actionableRecommendations.length}`}</Badge>
                <Badge variant="info">{`继续观察 ${hypothesisRecommendations.length}`}</Badge>
                <Badge variant="warning">{`仅展示 ${observationRecommendations.length}`}</Badge>
                {uncategorizedRecommendations.length > 0 ? <Badge variant="danger">{`未分类 ${uncategorizedRecommendations.length}`}</Badge> : null}
              </div>
            </div>

            <div className={`grid gap-3 ${uncategorizedRecommendations.length > 0 ? 'xl:grid-cols-4' : 'xl:grid-cols-3'}`}>
              {[
                {
                  title: '优先动作',
                  hint: '达到 actionable，可转成加权、降权或执行复核动作。',
                  items: actionableRecommendations,
                  empty: '当前没有可直接执行的动作建议。',
                },
                {
                  title: '继续观察',
                  hint: '引擎标记为 hypothesis，表示存在方向性假设，但还不适合立即改规则。',
                  items: hypothesisRecommendations,
                  empty: '当前没有需要持续跟踪的 hypothesis 建议。',
                },
                {
                  title: '仅展示',
                  hint: '当前只到观察或展示层级，通常代表样本或稳定性仍不足。',
                  items: observationRecommendations,
                  empty: '当前没有仅展示层级的建议。',
                },
                {
                  title: '未分类',
                  hint: '后端返回了当前前端未完全识别的 recommendationLevel，需要人工复核。',
                  items: uncategorizedRecommendations,
                  empty: '当前没有未分类建议。',
                },
              ].map((bucket) => (
                <div key={bucket.title} className="rounded-2xl border border-white/8 bg-black/10 p-4">
                  <div className="text-sm font-semibold text-white">{bucket.title}</div>
                  <div className="mt-1 text-xs text-secondary-text">
                    {bucket.title === '仅展示' ? '仅到展示层级，暂不形成动作。' : bucket.hint}
                  </div>
                  <div className="mt-3 space-y-3">
                    {bucket.items.length === 0 ? (
                      <div className="rounded-2xl border border-dashed border-white/10 px-3 py-4 text-sm text-secondary-text">
                        {bucket.empty}
                      </div>
                    ) : bucket.items.map((item, index) => {
                      const isCurrentStrategy = isRecommendationRelevantToStrategy(item, strategyKey, strategySelectionKey);
                      return (
                        <div key={`${bucket.title}-${item.targetScope ?? 'scope'}-${item.targetKey ?? 'key'}-${item.recommendationType}-${item.recommendationLevel}-${index}`} className="rounded-2xl border border-white/8 bg-white/4 p-3">
                          <div className="flex flex-wrap items-start justify-between gap-2">
                            <div>
                              <div className="text-sm font-semibold text-white">{renderRecommendationType(item.recommendationType)}</div>
                              <div className="mt-1 text-xs text-secondary-text">{renderRecommendationTarget(item.targetScope, item.targetKey)}</div>
                            </div>
                            <div className="flex flex-wrap gap-2">
                              <Badge variant={isCurrentStrategy ? 'success' : 'history'}>{isCurrentStrategy ? '当前策略' : '运行级'}</Badge>
                              <Badge variant="default">{renderValidationStatus(item.validationStatus)}</Badge>
                            </div>
                          </div>
                          <div className="mt-3 flex flex-wrap gap-2 text-xs text-secondary-text">
                            <span className="rounded-full border border-white/10 bg-black/10 px-2 py-1">{renderRecommendationScope(item.targetScope)}</span>
                            <span className="rounded-full border border-white/10 bg-black/10 px-2 py-1">{`样本 ${item.sampleCount ?? '--'}`}</span>
                            <span className="rounded-full border border-white/10 bg-black/10 px-2 py-1">{`置信 ${ratioPct(item.confidence)}`}</span>
                          </div>
                          <div className="mt-3 text-sm leading-6 text-secondary-text">
                            {item.suggestedChange || item.currentRule || '--'}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>
        ) : null}

        {rankingComparisons.length > 0 ? (
          <div className="rounded-3xl border border-white/8 bg-white/3 p-5">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="text-sm font-semibold text-white">分级有效性结论</div>
                <div className="mt-1 text-xs text-secondary-text">本区展示的是本次回测运行的全样本分级汇总，不是当前策略子集专属结论。</div>
              </div>
              <div className="flex flex-wrap gap-2">
                <Badge variant="history">运行级</Badge>
                <Badge variant="info">{`整体有效性 ${ratioPct(rankingEffectiveness?.overallEffectivenessRatio)}`}</Badge>
                <Badge variant="default">{`TopK ${ratioPct(rankingEffectiveness?.topKHitRate)}`}</Badge>
                <Badge variant="default">{`维度 ${rankingComparisons.length}`}</Badge>
              </div>
            </div>

            <div className="grid gap-3 lg:grid-cols-3">
              {rankingComparisons.map((item) => (
                <div key={`${item.dimension}-${item.tierHigh}-${item.tierLow}`} className="rounded-2xl border border-white/8 bg-black/10 p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <div className="text-sm font-semibold text-white">{renderRankingDimensionLabel(item.dimension)}</div>
                      <div className="mt-1 text-xs font-mono text-secondary-text">{`${item.tierHigh} vs ${item.tierLow}`}</div>
                    </div>
                    <Badge variant={item.isEffective ? 'success' : 'warning'}>
                      {item.isEffective ? '有效' : '仍需观察'}
                    </Badge>
                  </div>

                  <div className="mt-4 grid gap-2 text-sm">
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-secondary-text">样本</span>
                      <span className="font-mono text-foreground">{`样本 ${item.highSampleCount} vs ${item.lowSampleCount}`}</span>
                    </div>
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-secondary-text">平均收益</span>
                      <span className="font-mono text-foreground">{`${pct(item.highAvgReturn)} vs ${pct(item.lowAvgReturn)}`}</span>
                    </div>
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-secondary-text">胜率</span>
                      <span className="font-mono text-foreground">{`${pct(item.highWinRate)} vs ${pct(item.lowWinRate)}`}</span>
                    </div>
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-secondary-text">超额</span>
                      <span className="font-mono text-foreground">{`超额 ${pct(item.excessReturnPct)}`}</span>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        ) : null}

        <div>
          <div className="mb-3 text-sm font-semibold text-white">策略表现</div>
          {isObservationOnly ? (
            <div className="mb-3 rounded-2xl border border-warning/20 bg-warning/5 px-4 py-3 text-sm text-secondary-text">
              <div className="font-semibold text-white">收益类指标已降级为观察指标</div>
              <div className="mt-1">当前没有入场样本，平均收益位置展示的是观察避险效果，不代表买入收益。</div>
            </div>
          ) : null}
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {[
              { label: '胜率', value: pct(performanceSummary?.winRatePct) },
              { label: isObservationOnly ? '观察避险' : allEntrySamplesReplayed ? '真实交易收益' : hasRealTradeReplay ? '平均收益（含真实回放）' : '平均收益', value: pct(performanceSummary?.avgReturnPct) },
              { label: '盈亏比', value: num(performanceSummary?.profitFactor) },
              { label: '平均MAE', value: pct(performanceSummary?.avgMae) },
            ].map((item) => (
              <div key={item.label} className="rounded-2xl border border-white/8 bg-white/4 px-4 py-4">
                <div className="text-[11px] uppercase tracking-[0.18em] text-secondary-text">{item.label}</div>
                <div className="mt-2 text-2xl font-semibold text-white">{item.value}</div>
              </div>
            ))}
          </div>
        </div>

        <div className="grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
          <div className="rounded-3xl border border-white/8 bg-white/3 p-5">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div className="text-sm font-semibold text-white">样本结构拆解</div>
              <span className="text-xs text-secondary-text">{evaluations.length} 条研究样本</span>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-2xl bg-black/10 px-4 py-4">
                <div className="text-xs uppercase tracking-[0.18em] text-secondary-text">样本分层主导</div>
                <div className="mt-2 text-lg font-semibold text-white">{topDistributionLabel(sampleBuckets)}</div>
              </div>
              <div className="rounded-2xl bg-black/10 px-4 py-4">
                <div className="text-xs uppercase tracking-[0.18em] text-secondary-text">信号族主导</div>
                <div className="mt-2 text-lg font-semibold text-white">{topDistributionLabel(signalFamilies)}</div>
              </div>
              <div className="rounded-2xl bg-black/10 px-4 py-4">
                <div className="text-xs uppercase tracking-[0.18em] text-secondary-text">样本总数</div>
                <div className="mt-2 text-lg font-semibold text-white">{evaluations.length}</div>
              </div>
              <div className="rounded-2xl bg-black/10 px-4 py-4">
                <div className="text-xs uppercase tracking-[0.18em] text-secondary-text">入场样本</div>
                <div className="mt-2 text-lg font-semibold text-white">{cohortSummary?.familyBreakdown?.entry?.sampleCount ?? '--'}</div>
              </div>
              <div className="rounded-2xl bg-black/10 px-4 py-4 sm:col-span-2">
                <div className="text-xs uppercase tracking-[0.18em] text-secondary-text">市场环境分布</div>
                <div className="mt-3 grid gap-2 sm:grid-cols-2">
                  {marketRegimes.map((item) => (
                    <div key={item.label} className="rounded-xl border border-white/8 bg-white/3 px-3 py-2 text-sm">
                      <span className="font-mono text-foreground">{renderValueLabel(item.label)}: {item.count}</span>
                    </div>
                  ))}
                </div>
              </div>
              <div className="rounded-2xl bg-black/10 px-4 py-4 sm:col-span-2">
                <div className="text-xs uppercase tracking-[0.18em] text-secondary-text">候选池与成熟度</div>
                <div className="mt-3 flex flex-wrap gap-2 text-sm">
                  {Array.from(new Set(
                    evaluations.map((item) => `${renderValueLabel(item.snapshotCandidatePoolLevel)} / ${renderValueLabel(item.snapshotEntryMaturity)}`),
                  )).map((item) => (
                    <span key={item} className="rounded-full border border-white/10 bg-white/3 px-3 py-1 text-secondary-text">
                      {item}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          </div>

          <div className="rounded-3xl border border-white/8 bg-white/3 p-5">
            <div className="mb-3 text-sm font-semibold text-white">证据链与归因</div>
            <div className="space-y-3 rounded-2xl bg-black/10 p-4 text-sm">
              <div className="flex items-center justify-between gap-3">
                <span className="text-secondary-text">策略主归因</span>
                <span className="font-mono text-foreground">{renderPrimaryStrategy(representativeEvaluation?.primaryStrategy)}</span>
              </div>
              <div className="flex items-center justify-between gap-3">
                <span className="text-secondary-text">辅助策略</span>
                <span className="font-mono text-foreground">{representativeEvaluation?.contributingStrategies?.join(', ') || '--'}</span>
              </div>
              <div className="flex items-center justify-between gap-3">
                <span className="text-secondary-text">策略命中数</span>
                <span className="font-mono text-foreground">{representativeEvaluation?.matchedStrategies?.length ?? 0}</span>
              </div>
              <div className="flex items-center justify-between gap-3">
                <span className="text-secondary-text">Low123校验</span>
                <span className="font-mono text-foreground">{representativeEvaluation?.ma100Low123ValidationStatus ?? '--'}</span>
              </div>
              <div className="flex items-center justify-between gap-3">
                <span className="text-secondary-text">代表样本</span>
                <span className="font-mono text-foreground">{representativeEvaluation?.code ?? '--'}</span>
              </div>
            </div>

            <div className="mt-4 rounded-2xl bg-black/10 p-4">
              <div className="mb-2 text-sm font-semibold text-white">研究筛选</div>
              <div className="text-xs text-secondary-text">从异常样本与时机标签快速切到样本浏览器目标子集。</div>
            </div>
          </div>
        </div>

        <div className="rounded-3xl border border-white/8 bg-white/3 p-5">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-white">异常样本区</div>
              <div className="mt-1 text-xs text-secondary-text">点击异常卡片可直接联动到底部样本浏览器。</div>
            </div>
            <Badge variant="warning">{abnormalCount} 个待复核样本</Badge>
          </div>

          <div className="mb-4 space-y-3 rounded-2xl bg-black/10 p-4">
            <div>
              <div className="text-xs uppercase tracking-[0.18em] text-secondary-text">联动样本浏览器</div>
              <div className="mt-3 flex flex-wrap gap-2">
                {[
                  { key: 'all' as const, label: '全部样本' },
                  { key: 'abnormal' as const, label: '仅异常' },
                  { key: 'noise_boundary' as const, label: '边界/噪声' },
                  { key: 'timing_issue' as const, label: '时机异常' },
                  { key: 'validation_risk' as const, label: '校验风险' },
                ].map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    onClick={() => onSampleFocusChange(item.key)}
                    className={`rounded-full border px-3 py-1 text-sm ${
                      sampleFocus === item.key
                        ? 'border-cyan/40 bg-cyan/10 text-cyan'
                        : 'border-white/10 bg-black/10 text-secondary-text'
                    }`}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <div className="text-xs uppercase tracking-[0.18em] text-secondary-text">样本分层筛选</div>
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => onSampleBucketFilterChange(null)}
                  className={`rounded-full border px-3 py-1 text-sm ${
                    sampleBucketFilter == null
                      ? 'border-cyan/40 bg-cyan/10 text-cyan'
                      : 'border-white/10 bg-black/10 text-secondary-text'
                  }`}
                >
                  全部分层
                </button>
                {sampleBucketOptions.map((item) => (
                  <button
                    key={item}
                    type="button"
                    onClick={() => onSampleBucketFilterChange(item)}
                    className={`rounded-full border px-3 py-1 text-sm ${
                      sampleBucketFilter === item
                        ? 'border-cyan/40 bg-cyan/10 text-cyan'
                        : 'border-white/10 bg-black/10 text-secondary-text'
                    }`}
                  >
                    {`分层: ${renderValueLabel(item)}`}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <div className="text-xs uppercase tracking-[0.18em] text-secondary-text">买点时机筛选</div>
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => onEntryTimingFilterChange(null)}
                  className={`rounded-full border px-3 py-1 text-sm ${
                    entryTimingFilter == null
                      ? 'border-cyan/40 bg-cyan/10 text-cyan'
                      : 'border-white/10 bg-black/10 text-secondary-text'
                  }`}
                >
                  全部时机
                </button>
                {entryTimingOptions.map((item) => (
                  <button
                    key={item}
                    type="button"
                    onClick={() => onEntryTimingFilterChange(item)}
                    className={`rounded-full border px-3 py-1 text-sm ${
                      entryTimingFilter === item
                        ? 'border-cyan/40 bg-cyan/10 text-cyan'
                        : 'border-white/10 bg-black/10 text-secondary-text'
                    }`}
                  >
                    {`时机: ${renderValueLabel(item)}`}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {abnormalSamples.length === 0 ? (
            <p className="text-sm text-secondary-text">当前策略下暂无明显异常样本。</p>
          ) : (
            <div className="grid gap-3 lg:grid-cols-2">
              {abnormalSamples.map((item) => (
                <button
                  key={`${item.id ?? item.code}-${item.tradeDate ?? 'unknown'}`}
                  type="button"
                  onClick={() => onAbnormalSampleSelect(item)}
                  className="rounded-2xl border border-warning/20 bg-warning/5 p-4 text-left transition hover:bg-warning/10"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <div className="text-sm font-semibold text-foreground">{item.code}</div>
                      <div className="mt-1 text-xs text-secondary-text">{item.name ?? '--'} · {item.tradeDate ?? '--'}</div>
                    </div>
                    <Badge variant="warning">{renderValueLabel(item.sampleBucket ?? 'abnormal')}</Badge>
                  </div>
                  <div className="mt-3 grid gap-2 text-sm">
                    <div className="flex items-center justify-between">
                      <span className="text-secondary-text">买点时机</span>
                      <span className="font-mono text-foreground">{renderValueLabel(item.entryTimingLabel)}</span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-secondary-text">Low123校验</span>
                      <span className="font-mono text-foreground">{item.ma100Low123ValidationStatus ?? '--'}</span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-secondary-text">信号族</span>
                      <span className="font-mono text-foreground">{renderSignalFamily(item.signalFamily)}</span>
                    </div>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </Card>
  );
};
