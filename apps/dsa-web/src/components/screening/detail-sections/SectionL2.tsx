import type React from 'react';
import { Flame } from 'lucide-react';
import { Card } from '../../common';
import type { ScreeningCandidateDetail, ScreeningFactorSnapshot } from '../../../types/screening';
import { LabeledBadge, InfoRow, THEME_POSITION_LABELS, THEME_POSITION_COLORS } from './shared';

export const SectionL2: React.FC<{
  candidate: ScreeningCandidateDetail;
  factorSnapshot: ScreeningFactorSnapshot;
}> = ({ candidate, factorSnapshot }) => {
  if (!candidate.themePosition && !factorSnapshot.is_hot_theme_stock) return null;
  return (
    <Card variant="default" padding="sm" className="border-purple-500/20 bg-purple-500/3">
      <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-purple-400">
        <Flame className="h-3.5 w-3.5" /> L2 题材地位
      </h4>
      <div className="space-y-1.5">
        {candidate.themePosition && (
          <div className="flex items-center gap-2">
            <LabeledBadge value={candidate.themePosition} labelMap={THEME_POSITION_LABELS} colorMap={THEME_POSITION_COLORS} />
          </div>
        )}
        {candidate.themeTag && (
          <InfoRow label="主叙事题材">{candidate.themeTag}</InfoRow>
        )}
        {candidate.themeScore != null && (
          <InfoRow label="题材评分">{candidate.themeScore.toFixed(1)}</InfoRow>
        )}
        {candidate.themeDuration && (
          <InfoRow label="题材阶段">{candidate.themeDuration}</InfoRow>
        )}
        {factorSnapshot.primary_theme && (
          <InfoRow label="主题">{factorSnapshot.primary_theme}</InfoRow>
        )}
        {factorSnapshot.theme_heat_score != null && (
          <InfoRow label="板块热度">{factorSnapshot.theme_heat_score.toFixed(1)}</InfoRow>
        )}
        {factorSnapshot.leader_score != null && (
          <InfoRow label="龙头特征分">{factorSnapshot.leader_score.toFixed(0)}</InfoRow>
        )}
        {factorSnapshot.extreme_strength_score != null && (
          <InfoRow label="极端强势分">{factorSnapshot.extreme_strength_score.toFixed(1)}</InfoRow>
        )}
        {factorSnapshot.theme_pool_score != null && (
          <InfoRow label="· 题材池分">{factorSnapshot.theme_pool_score.toFixed(1)}</InfoRow>
        )}
        {factorSnapshot.leadership_score != null && (
          <InfoRow label="· 龙头分">{factorSnapshot.leadership_score.toFixed(1)}</InfoRow>
        )}
        {factorSnapshot.entry_signal_score != null && (
          <InfoRow label="· 入场信号分">{factorSnapshot.entry_signal_score.toFixed(1)}</InfoRow>
        )}
        {factorSnapshot.timing_penalty != null && factorSnapshot.timing_penalty < 0 && (
          <InfoRow label="· 时机惩罚">
            <span className="text-red-400">{factorSnapshot.timing_penalty.toFixed(1)}</span>
          </InfoRow>
        )}
        {factorSnapshot.leader_double_count != null &&
          factorSnapshot.leader_double_count > 0 &&
          factorSnapshot.extreme_strength_score_deduplicated != null && (
            <InfoRow label="· 去重净分">
              <span className="text-amber-300" title="扣除 leader_score 与其它桶之间可观测的重复加权估算值">
                {factorSnapshot.extreme_strength_score_deduplicated.toFixed(1)}
                <span className="ml-1 text-xs text-secondary-text">
                  (−{factorSnapshot.leader_double_count.toFixed(1)})
                </span>
              </span>
            </InfoRow>
          )}
        {candidate.leaderStocks && candidate.leaderStocks.length > 0 && (
          <InfoRow label="龙头股">{candidate.leaderStocks.join(' / ')}</InfoRow>
        )}
      </div>
    </Card>
  );
};
