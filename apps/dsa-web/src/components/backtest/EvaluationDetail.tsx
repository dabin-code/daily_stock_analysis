import type React from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Badge } from '../common/Badge';
import { Card } from '../common/Card';
import type { BacktestResultItem } from '../../types/backtest';

interface EvaluationDetailProps {
  evaluations: BacktestResultItem[];
  isLoading?: boolean;
  title?: string;
  subtitle?: string;
  targetEvaluation?: BacktestResultItem | null;
  researchWarning?: string | null;
}

function pct(value?: number | null): string {
  if (value == null) return '--';
  return `${value.toFixed(1)}%`;
}

function price(value?: number | null): string {
  if (value == null) return '--';
  return value.toFixed(2);
}

function numberFromPayload(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function entryMetric(item: BacktestResultItem): string {
  return pct(item.tradeReturnPct ?? item.forwardReturn5d);
}

function tryParse(value?: string | null): Record<string, unknown> | null {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as Record<string, unknown>;
    if (parsed && typeof parsed === 'object') {
      return parsed;
    }
    return null;
  } catch {
    return null;
  }
}

function renderFactorSummary(payload: Record<string, unknown> | null): Array<{ label: string; value: string }> {
  if (!payload) return [];
  return [
    {
      label: 'MA100突破',
      value: typeof payload.ma100_breakout_days === 'number'
        ? `突破${payload.ma100_breakout_days}日`
        : '未突破',
    },
    {
      label: '底背离',
      value: String(payload.bottom_divergence_state ?? '--'),
    },
    {
      label: '趋势线突破',
      value: payload.trendline_breakout ? '已突破' : '未突破',
    },
    {
      label: '突破性缺口',
      value: payload.gap_is_breakaway ? '有' : '无',
    },
    {
      label: '低位结构',
      value: String(payload.low_123_state ?? '--'),
    },
  ];
}

function renderAttributionSummary(item: BacktestResultItem): Array<{ label: string; value: string }> {
  return [
    {
      label: '主策略归因',
      value: item.primaryStrategy ?? '--',
    },
    {
      label: '辅助策略',
      value: item.contributingStrategies && item.contributingStrategies.length > 0
        ? item.contributingStrategies.join(', ')
        : '--',
    },
    {
      label: '样本分层',
      value: renderValueLabel(item.sampleBucket),
    },
    {
      label: '买点时机',
      value: renderValueLabel(item.entryTimingLabel),
    },
    {
      label: 'Low123校验',
      value: item.ma100Low123ValidationStatus ?? '--',
    },
  ];
}

function renderValueLabel(value?: string | null): string {
  switch (value) {
    case 'core':
      return '核心样本';
    case 'boundary':
      return '边界样本';
    case 'noise':
      return '噪音样本';
    case 'on_time':
      return '时机合适';
    case 'too_early':
      return '偏早';
    case 'too_late':
      return '偏晚';
    case 'not_applicable':
      return '不适用';
    case 'correct_wait':
      return '观望正确';
    case 'missed_opportunity':
      return '错过机会';
    case 'missed_watch':
      return '观察失误';
    case 'win':
      return '盈利';
    case 'loss':
      return '亏损';
    default:
      return value || '--';
  }
}

function renderReplayStatus(value?: string | null): string {
  switch (value) {
    case 'completed':
      return '已完成';
    case 'entry_not_filled':
      return '未成交';
    case 'missing_structured_trade_plan':
      return '缺少结构化计划';
    case 'no_forward_bars':
      return '缺少后续行情';
    default:
      return value || '--';
  }
}

function renderExitReason(value?: string | null): string {
  switch (value) {
    case 'take_profit':
      return '止盈离场';
    case 'stop_loss':
      return '止损离场';
    case 'ambiguous_stop_loss':
      return '同日触发，按止损离场';
    case 'time_stop':
      return '时间止损/到期离场';
    default:
      return value || '--';
  }
}

function getEvaluationKey(item: BacktestResultItem): string {
  return String(item.id ?? `${item.code}-${item.tradeDate ?? 'unknown'}-${item.signalFamily}`);
}

export const EvaluationDetail: React.FC<EvaluationDetailProps> = ({
  evaluations,
  isLoading = false,
  title = '个股明细',
  subtitle = 'Drill-down',
  targetEvaluation = null,
  researchWarning = null,
}) => {
  const [requestedTab, setRequestedTab] = useState<'entry' | 'observation'>('entry');
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const rowRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const entryCount = useMemo(
    () => evaluations.filter((item) => item.signalFamily === 'entry').length,
    [evaluations],
  );
  const observationCount = useMemo(
    () => evaluations.filter((item) => item.signalFamily === 'observation').length,
    [evaluations],
  );
  const activeTab = useMemo<'entry' | 'observation'>(() => {
    if (requestedTab === 'entry' && entryCount === 0 && observationCount > 0) {
      return 'observation';
    }
    if (requestedTab === 'observation' && observationCount === 0 && entryCount > 0) {
      return 'entry';
    }
    return requestedTab;
  }, [entryCount, observationCount, requestedTab]);
  const filtered = useMemo(
    () => evaluations.filter((item) => item.signalFamily === activeTab),
    [activeTab, evaluations],
  );
  const targetKey = targetEvaluation ? getEvaluationKey(targetEvaluation) : null;
  const effectiveExpandedKey = expandedKey ?? targetKey;

  useEffect(() => {
    if (!targetEvaluation || !targetKey) {
      return;
    }
    const targetVisible = filtered.some((item) => getEvaluationKey(item) === targetKey);
    if (!targetVisible) {
      return;
    }
    const targetElement = rowRefs.current[targetKey];
    targetElement?.scrollIntoView?.({ block: 'nearest' });
  }, [filtered, targetEvaluation, targetKey]);

  return (
    <Card title={title} subtitle={subtitle} variant="gradient">
      {researchWarning ? (
        <div className="mb-4 rounded-2xl border border-warning/20 bg-warning/5 px-4 py-3 text-sm text-secondary-text">
          {researchWarning}
        </div>
      ) : null}

      <div className="mb-4 flex gap-2">
        <button type="button" className={`btn-secondary ${activeTab === 'entry' ? 'ring-1 ring-cyan/40' : ''}`} onClick={() => setRequestedTab('entry')}>
          入场信号
        </button>
        <button type="button" className={`btn-secondary ${activeTab === 'observation' ? 'ring-1 ring-cyan/40' : ''}`} onClick={() => setRequestedTab('observation')}>
          观察信号
        </button>
      </div>

      {isLoading ? (
        <p className="text-sm text-secondary-text">正在加载评估明细...</p>
      ) : filtered.length === 0 ? (
        <p className="text-sm text-secondary-text">当前标签下暂无评估数据。</p>
      ) : (
        <div className="space-y-3">
          {filtered.map((item) => {
            const factorPayload = tryParse(item.factorSnapshotJson);
            const planPayload = tryParse(item.tradePlanJson);
            const itemKey = getEvaluationKey(item);
            const isExpanded = effectiveExpandedKey === itemKey;
            const isTarget = targetEvaluation ? getEvaluationKey(targetEvaluation) === itemKey : false;
            return (
              <div
                key={itemKey}
                className={`rounded-2xl border ${isTarget ? 'border-cyan/40 shadow-lg shadow-cyan/10' : 'border-white/8'}`}
              >
                <button
                  type="button"
                  ref={(element) => {
                    rowRefs.current[itemKey] = element;
                  }}
                  className={`flex w-full flex-wrap items-center justify-between gap-3 px-4 py-3 text-left hover:bg-white/5 ${isTarget ? 'bg-cyan/10' : ''}`}
                  onClick={() => setExpandedKey(isExpanded ? null : itemKey)}
                >
                  <div>
                    <div className="text-sm font-semibold text-foreground">{item.code} {item.name ?? ''}</div>
                    <div className="mt-1 text-xs text-secondary-text">{item.tradeDate ?? '--'} · {item.snapshotSetupType ?? item.snapshotTradeStage ?? '--'}</div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <Badge variant={item.outcome === 'win' || item.outcome === 'correct_wait' ? 'success' : 'default'}>
                      {renderValueLabel(item.outcome)}
                    </Badge>
                    <span className="font-mono text-secondary-text">
                      {activeTab === 'entry' ? entryMetric(item) : pct(item.riskAvoidedPct)}
                    </span>
                  </div>
                </button>

                {isExpanded ? (
                  <div className="border-t border-white/8 px-4 py-4">
                    <div className="grid gap-4 lg:grid-cols-2">
                      <div>
                        <h4 className="mb-3 text-sm font-semibold text-white">因子快照</h4>
                        <div className="space-y-2">
                          {renderFactorSummary(factorPayload).map((row) => (
                            <div key={row.label} className="flex items-center justify-between rounded-xl bg-white/5 px-3 py-2 text-sm">
                              <span className="text-secondary-text">{row.label}</span>
                              <span className="font-mono text-foreground">{row.value}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                      <div>
                        <h4 className="mb-3 text-sm font-semibold text-white">归因与验证</h4>
                        <div className="mb-4 space-y-2 rounded-xl bg-white/5 p-3 text-sm">
                          {renderAttributionSummary(item).map((row) => (
                            <div key={row.label} className="flex items-center justify-between gap-3">
                              <span className="text-secondary-text">{row.label}</span>
                              <span className="font-mono text-foreground">{row.value}</span>
                            </div>
                          ))}
                        </div>
                        <h4 className="mb-3 text-sm font-semibold text-white">交易计划</h4>
                        <div className="space-y-2 rounded-xl bg-white/5 p-3 text-sm">
                          <div className="flex items-center justify-between">
                            <span className="text-secondary-text">计划买点</span>
                            <span className="font-mono text-foreground">{price(item.plannedEntryPrice ?? numberFromPayload(planPayload?.entry_price))}</span>
                          </div>
                          <div className="flex items-center justify-between">
                            <span className="text-secondary-text">止盈目标</span>
                            <span className="font-mono text-foreground">{price(item.plannedTakeProfitPrice ?? numberFromPayload(planPayload?.take_profit_price))}</span>
                          </div>
                          <div className="flex items-center justify-between">
                            <span className="text-secondary-text">止损线</span>
                            <span className="font-mono text-foreground">{price(item.plannedStopLossPrice ?? numberFromPayload(planPayload?.stop_loss_price))}</span>
                          </div>
                          <div className="flex items-center justify-between">
                            <span className="text-secondary-text">执行结果</span>
                            <span className="font-mono text-foreground">{item.planSuccess == null ? '--' : item.planSuccess ? '成功' : '失败'}</span>
                          </div>
                        </div>
                        <h4 className="mb-3 mt-4 text-sm font-semibold text-white">真实交易回放</h4>
                        <div className="space-y-2 rounded-xl bg-white/5 p-3 text-sm">
                          <div className="flex items-center justify-between">
                            <span className="text-secondary-text">回放状态</span>
                            <span className="font-mono text-foreground">{renderReplayStatus(item.tradeReplayStatus)}</span>
                          </div>
                          <div className="flex items-center justify-between">
                            <span className="text-secondary-text">实际买入</span>
                            <span className="font-mono text-foreground">{price(item.actualEntryPrice)} · {item.actualEntryDate ?? '--'}</span>
                          </div>
                          <div className="flex items-center justify-between">
                            <span className="text-secondary-text">实际卖出</span>
                            <span className="font-mono text-foreground">{price(item.actualExitPrice)} · {item.actualExitDate ?? '--'}</span>
                          </div>
                          <div className="flex items-center justify-between">
                            <span className="text-secondary-text">离场原因</span>
                            <span className="font-mono text-foreground">{renderExitReason(item.exitReason)}</span>
                          </div>
                          <div className="flex items-center justify-between">
                            <span className="text-secondary-text">真实收益</span>
                            <span className="font-mono text-foreground">{pct(item.tradeReturnPct)}</span>
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
};
