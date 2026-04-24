import type React from 'react';
import { Target } from 'lucide-react';
import { Card } from '../../common';
import type {
  ScreeningCandidateDetail,
  ScreeningFactorSnapshot,
  ScreeningPhaseResults,
  ScreeningPhaseExplanation,
} from '../../../types/screening';
import {
  SETUP_TYPE_LABELS,
  ENTRY_MATURITY_LABELS,
  STAGE_LABEL_LABELS,
  STAGE_LABEL_COLORS,
  SIGNAL_KIND_LABELS,
  SIGNAL_KIND_COLORS,
} from '../../../types/screening';
import { LabeledBadge } from './shared';
import { extractTechnicalPatterns, TechnicalPatternCards } from '../TechnicalPatternCards';

const PHASE_DEFINITIONS = [
  { key: 'phase1_market_and_theme', label: '阶段1: 市场与题材' },
  { key: 'phase2_leader_screen', label: '阶段2: 龙头筛选' },
  { key: 'phase3_core_signal', label: '阶段3: 核心信号' },
  { key: 'phase4_entry_readiness', label: '阶段4: 入场准备' },
  { key: 'phase5_risk_controls', label: '阶段5: 风险控制' },
] as const satisfies ReadonlyArray<{
  key: keyof ScreeningPhaseResults;
  label: string;
}>;

const EMPTY_VALUE = '--';

function getPhaseDescription(label: string, isHit: boolean, snapshot: ScreeningFactorSnapshot): string {
  if (!isHit) return '未命中';
  if (label === '阶段1: 市场与题材') return '已确认热点题材匹配';
  if (label === '阶段2: 龙头筛选') return `龙头评分: ${snapshot.leader_score ?? EMPTY_VALUE}`;
  // A4：阶段 3 优先展示 primary_signal（跨 core/bonus 选出的主信号），其次回退到 core_signal
  if (label === '阶段3: 核心信号') return snapshot.primary_signal ?? snapshot.core_signal ?? '已命中强势信号';
  if (label === '阶段4: 入场准备') return snapshot.entry_reason ?? '已形成入场方案';
  // A6：止损描述附加 stop_loss_basis 依据（如 "缺口下沿" / "123结构低点"），
  // 让面板上一眼能看出止损基准来自哪个子信号，而不是模板化的 MA100×0.95。
  const stopLoss = snapshot.risk_params?.stop_loss?.toFixed(2) ?? EMPTY_VALUE;
  const basis = snapshot.risk_params?.stop_loss_basis;
  const stopLossDisplay = basis && basis !== 'none' ? `${stopLoss}（${basis}）` : stopLoss;
  return `止损: ${stopLossDisplay} | 仓位: ${snapshot.risk_params?.position_size ?? EMPTY_VALUE}`;
}

export const SectionL4: React.FC<{
  candidate: ScreeningCandidateDetail;
  factorSnapshot: ScreeningFactorSnapshot;
  technicalPatterns: ReturnType<typeof extractTechnicalPatterns>;
  phaseResults?: ScreeningPhaseResults;
  phaseExplanations?: ScreeningPhaseExplanation[];
}> = ({ candidate, factorSnapshot, technicalPatterns, phaseResults, phaseExplanations }) => {
  const hasSetup = candidate.setupType && candidate.setupType !== 'none';
  const hasPatterns = technicalPatterns.length > 0;
  const hasPhases = phaseResults != null;
  const stageLabel = factorSnapshot.stage_label;
  const signalKind = factorSnapshot.signal_kind;
  const primarySignal = factorSnapshot.primary_signal;
  const timingPenalty = factorSnapshot.timing_penalty;
  const timingReasons = factorSnapshot.timing_reasons ?? [];
  const hasStageOrSignal =
    (stageLabel && stageLabel !== 'none') ||
    (signalKind && signalKind !== 'none') ||
    (primarySignal != null && primarySignal !== '');

  if (!hasSetup && !hasPatterns && !hasPhases && !hasStageOrSignal) return null;

  return (
    <Card variant="default" padding="sm" className="border-orange/20 bg-orange/3">
      <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-orange">
        <Target className="h-3.5 w-3.5" /> L4 入场信号
      </h4>
      <div className="space-y-2">
        {hasStageOrSignal && (
          <div className="flex flex-wrap items-center gap-2">
            {stageLabel && stageLabel !== 'none' && (
              <>
                <span className="text-xs text-secondary-text">阶段:</span>
                <LabeledBadge
                  value={stageLabel}
                  labelMap={STAGE_LABEL_LABELS}
                  colorMap={STAGE_LABEL_COLORS}
                />
              </>
            )}
            {primarySignal && (
              <>
                <span className="text-xs text-secondary-text">主信号:</span>
                <span className="inline-flex rounded border border-border/40 bg-elevated/40 px-1.5 py-0.5 text-[10px] font-medium text-foreground">
                  {primarySignal}
                </span>
              </>
            )}
            {signalKind && signalKind !== 'none' && (
              <LabeledBadge
                value={signalKind}
                labelMap={SIGNAL_KIND_LABELS}
                colorMap={SIGNAL_KIND_COLORS}
              />
            )}
          </div>
        )}
        {timingPenalty != null && timingPenalty < 0 && (
          <div className="rounded border border-red-500/30 bg-red-500/5 px-2 py-1 text-xs text-red-400">
            时机惩罚 {timingPenalty.toFixed(1)}
            {timingReasons.length > 0 && ` · ${timingReasons.join(' / ')}`}
          </div>
        )}
        {hasSetup && (
          <div className="flex items-center gap-2">
            <span className="text-xs text-secondary-text">买点:</span>
            <span className="inline-flex rounded border border-border/40 bg-elevated/40 px-1.5 py-0.5 text-[10px] font-medium text-foreground">
              {SETUP_TYPE_LABELS[candidate.setupType!] ?? candidate.setupType}
            </span>
            {candidate.entryMaturity && (
              <>
                <span className="text-xs text-secondary-text">成熟度:</span>
                <span className="text-xs font-medium text-foreground">
                  {ENTRY_MATURITY_LABELS[candidate.entryMaturity] ?? candidate.entryMaturity}
                </span>
              </>
            )}
            {candidate.setupFreshness != null && (
              <>
                <span className="text-xs text-secondary-text">新鲜度:</span>
                <span className="text-xs font-medium text-foreground">
                  {(candidate.setupFreshness * 100).toFixed(0)}%
                </span>
              </>
            )}
          </div>
        )}
        {candidate.setupHitReasons && candidate.setupHitReasons.length > 0 && (
          <div className="rounded border border-border/40 bg-elevated/20 px-2 py-1.5 text-xs text-secondary-text">
            {candidate.setupHitReasons.join(' / ')}
          </div>
        )}

        {hasPhases && (
          <div className="space-y-1 text-xs">
            {PHASE_DEFINITIONS.map((phase) => {
              const backendExplanation = phaseExplanations?.find((item) => item.phase_key === phase.key);
              const isHit = backendExplanation?.hit ?? Boolean(phaseResults?.[phase.key]);
              return (
                <div key={phase.label} className="flex items-center justify-between gap-4">
                  <span className="text-secondary-text">{backendExplanation?.label ?? phase.label}</span>
                  <span className="text-right font-mono text-foreground">
                    {backendExplanation?.summary ?? getPhaseDescription(phase.label, isHit, factorSnapshot)}
                  </span>
                </div>
              );
            })}
          </div>
        )}

        {hasPatterns && (
          <div className="mt-1">
            <TechnicalPatternCards patterns={technicalPatterns} />
          </div>
        )}
      </div>
    </Card>
  );
};
