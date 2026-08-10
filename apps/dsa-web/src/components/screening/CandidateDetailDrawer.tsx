import type React from 'react';
import { Brain, FileText, Flame } from 'lucide-react';

import { Badge, Card, Collapsible, Drawer } from '../common';
import { useScreeningStore } from '../../stores/screeningStore';
import { extractTechnicalPatterns, TechnicalPatternCards } from './TechnicalPatternCards';
import {
  SectionL1,
  SectionL2,
  SectionL3,
  SectionL4,
  SectionL5,
  SectionAI,
  LabeledBadge,
  InfoRow,
  hasFiveLayerData,
} from './detail-sections';
import type {
  HotThemeNewsItem,
  ScreeningFactorSnapshot,
  ScreeningPhaseExplanation,
  ScreeningPhaseResults,
  ScreeningCandidateDetail,
} from '../../types/screening';
import {
  TRADE_STAGE_LABELS,
  TRADE_STAGE_COLORS,
  STAGE_LABEL_LABELS,
  STAGE_LABEL_COLORS,
  SIGNAL_KIND_LABELS,
  SIGNAL_KIND_COLORS,
} from '../../types/screening';

const EMPTY_VALUE = '--';

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

const RULE_HIT_LABELS: Record<string, string> = {
  is_hot_theme_stock: '热点题材股',
  above_ma100: '站上 MA100',
  pattern_123_low_trendline: '低位 123 趋势线突破',
  gap_breakaway: '跳空突破',
  is_limit_up: '涨停',
  bottom_divergence_double_breakout: '底背离双突破',
  strategy: '极端强势组合',
};

const TECHNICAL_RULE_KEYS = new Set([
  'above_ma100',
  'pattern_123_low_trendline',
  'gap_breakaway',
  'is_limit_up',
  'bottom_divergence_double_breakout',
]);

const HIDDEN_FACTOR_SNAPSHOT_KEYS = new Set([
  'bottom_divergence_v2_candidate_records',
]);

function formatPrimitiveValue(value: unknown): string {
  if (value == null) return EMPTY_VALUE;
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(2);
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return String(value);
}

function formatStructuredValue(value: unknown): string {
  if (value == null || typeof value === 'number' || typeof value === 'string' || typeof value === 'boolean') {
    return formatPrimitiveValue(value);
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]';
    return value.map((item) => formatStructuredValue(item)).join('\n');
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return '{}';
    return entries
      .map(([key, entryValue]) => {
        const formattedEntry =
          entryValue != null && typeof entryValue === 'object'
            ? formatStructuredValue(entryValue)
            : formatPrimitiveValue(entryValue);
        return `${key}: ${formattedEntry}`;
      })
      .join('\n');
  }
  return String(value);
}

function parseRuleHit(ruleHit: string): { key: string; value?: string } {
  if (ruleHit.includes(':==:')) {
    const [key, value] = ruleHit.split(':==:');
    return { key, value };
  }
  if (ruleHit.includes(':')) {
    const [key, ...rest] = ruleHit.split(':');
    return { key, value: rest.join(':') };
  }
  return { key: ruleHit };
}

function translateRuleHit(ruleHit: string): string {
  const { key, value } = parseRuleHit(ruleHit);
  const translatedKey = RULE_HIT_LABELS[key];
  if (!translatedKey) return ruleHit;
  if (key === 'strategy' && value === 'extreme_strength_combo') return translatedKey;
  if (value == null || value === '' || value === 'True') return translatedKey;
  if (value === 'False') return `未命中${translatedKey}`;
  return `${translatedKey}: ${value}`;
}

function getPhaseDescription(label: string, isHit: boolean, snapshot: ScreeningFactorSnapshot): string {
  if (!isHit) return '未命中';
  if (label === '阶段1: 市场与题材') return '已确认热点题材匹配';
  if (label === '阶段2: 龙头筛选') return `龙头评分: ${snapshot.leader_score ?? EMPTY_VALUE}`;
  // A4：阶段 3 优先展示 primary_signal，其次回退到 core_signal
  if (label === '阶段3: 核心信号') return snapshot.primary_signal ?? snapshot.core_signal ?? '已命中强势信号';
  if (label === '阶段4: 入场准备') return snapshot.entry_reason ?? '已形成入场方案';
  // A6：止损描述附加 stop_loss_basis 依据（如 "缺口下沿" / "123结构低点"）。
  const stopLoss = snapshot.risk_params?.stop_loss?.toFixed(2) ?? EMPTY_VALUE;
  const basis = snapshot.risk_params?.stop_loss_basis;
  const stopLossDisplay = basis && basis !== 'none' ? `${stopLoss}（${basis}）` : stopLoss;
  return `止损: ${stopLossDisplay} | 仓位: ${snapshot.risk_params?.position_size ?? EMPTY_VALUE}`;
}

function isSafeUrl(url: string): boolean {
  return url.startsWith('https://') || url.startsWith('http://');
}

function FactorRow({ label, value }: { label: string; value: unknown }) {
  const display = formatStructuredValue(value);
  return (
    <div className="flex items-start justify-between gap-4 border-b border-border/20 py-1.5 text-xs">
      <span className="shrink-0 text-secondary-text">{label}</span>
      <span className="whitespace-pre-wrap text-right font-mono text-foreground">{display}</span>
    </div>
  );
}

/** Legacy layout for runs without five-layer data */
function LegacyLayout({
  candidate,
  factorSnapshot,
  generalRuleHits,
  technicalPatterns,
  phaseResults,
  phaseExplanations,
  catalystNews,
}: {
  candidate: ScreeningCandidateDetail;
  factorSnapshot: ScreeningFactorSnapshot;
  generalRuleHits: Array<{ key: string; translated: string }>;
  technicalPatterns: ReturnType<typeof extractTechnicalPatterns>;
  phaseResults?: ScreeningPhaseResults;
  phaseExplanations?: ScreeningPhaseExplanation[];
  catalystNews: HotThemeNewsItem[];
}) {
  return (
    <>
      {factorSnapshot.is_hot_theme_stock && (
        <Card variant="default" padding="sm" className="border-orange/30 bg-orange/5">
          <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-orange">
            <Flame className="h-3.5 w-3.5" /> 热点题材
          </h4>
          <div className="space-y-1.5 text-xs">
            {factorSnapshot.primary_theme && (
              <InfoRow label="主题">{factorSnapshot.primary_theme}</InfoRow>
            )}
            {factorSnapshot.theme_heat_score != null && (
              <InfoRow label="题材热度">{factorSnapshot.theme_heat_score.toFixed(1)}</InfoRow>
            )}
            {factorSnapshot.theme_match_score != null && (
              <InfoRow label="题材匹配度">{(factorSnapshot.theme_match_score * 100).toFixed(0)}%</InfoRow>
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
                  <span
                    className="text-amber-300"
                    title="扣除 leader_score 与其它桶之间可观测的重复加权估算值"
                  >
                    {factorSnapshot.extreme_strength_score_deduplicated.toFixed(1)}
                    <span className="ml-1 text-xs text-secondary-text">
                      (−{factorSnapshot.leader_double_count.toFixed(1)})
                    </span>
                  </span>
                </InfoRow>
              )}
            {factorSnapshot.stage_label && factorSnapshot.stage_label !== 'none' && (
              <div className="flex items-center gap-2 text-xs">
                <span className="shrink-0 text-secondary-text">阶段</span>
                <LabeledBadge
                  value={factorSnapshot.stage_label}
                  labelMap={STAGE_LABEL_LABELS}
                  colorMap={STAGE_LABEL_COLORS}
                />
              </div>
            )}
            {factorSnapshot.entry_reason && (
              <InfoRow label="入选原因">{factorSnapshot.entry_reason}</InfoRow>
            )}
            {factorSnapshot.primary_signal && (
              <div className="flex items-center gap-2 text-xs">
                <span className="shrink-0 text-secondary-text">主信号</span>
                <span className="inline-flex rounded border border-border/40 bg-elevated/40 px-1.5 py-0.5 text-[10px] font-medium text-foreground">
                  {factorSnapshot.primary_signal}
                </span>
                {factorSnapshot.signal_kind && factorSnapshot.signal_kind !== 'none' && (
                  <LabeledBadge
                    value={factorSnapshot.signal_kind}
                    labelMap={SIGNAL_KIND_LABELS}
                    colorMap={SIGNAL_KIND_COLORS}
                  />
                )}
              </div>
            )}
            {factorSnapshot.core_signal && (
              <InfoRow label="核心技术信号">{factorSnapshot.core_signal}</InfoRow>
            )}
            {factorSnapshot.theme_catalyst_summary && (
              <div className="mt-2 space-y-1">
                <div className="text-xs font-semibold text-orange">催化摘要</div>
                <div className="rounded-lg border border-orange/20 bg-orange/5 px-2 py-1.5 text-xs text-orange">
                  {factorSnapshot.theme_catalyst_summary}
                </div>
              </div>
            )}
            {catalystNews.length > 0 && (
              <div className="mt-2 space-y-1.5">
                <div className="text-xs font-semibold text-orange">热点新闻</div>
                {catalystNews.map((news, idx) => (
                  <div key={`${news.title}-${idx}`} className="rounded border border-orange/10 bg-orange/3 p-1.5 text-xs">
                    <div className="font-mono text-foreground">{news.title}</div>
                    {news.summary && <div className="mt-1 text-secondary-text">{news.summary}</div>}
                    <div className="mt-0.5 flex items-center justify-between text-secondary-text">
                      <span>{news.source}</span>
                      {news.url && isSafeUrl(news.url) && (
                        <a href={news.url} target="_blank" rel="noopener noreferrer" className="text-orange hover:underline">
                          查看
                        </a>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Card>
      )}

      {phaseResults && (
        <Card variant="default" padding="sm" className="border-cyan/30 bg-cyan/5">
          <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-cyan">
            <Brain className="h-3.5 w-3.5" /> 策略命中说明
          </h4>
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
        </Card>
      )}

      {factorSnapshot.entry_reason && (
        <Card variant="default" padding="sm" className="border-green/30 bg-green/5">
          <h4 className="mb-2 text-xs font-semibold text-green">入场方案</h4>
          <div className="text-xs text-secondary-text">
            <div className="mb-1.5">{factorSnapshot.entry_reason}</div>
          </div>
        </Card>
      )}

      {generalRuleHits.length > 0 && (
        <Card variant="default" padding="sm">
          <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-foreground">
            <FileText className="h-3.5 w-3.5 text-cyan" /> 命中规则
          </h4>
          <div className="flex flex-wrap gap-1.5">
            {generalRuleHits.map((item, idx) => (
              <Badge key={`${item.key}-${idx}`} variant="default" size="sm">{item.translated}</Badge>
            ))}
          </div>
        </Card>
      )}

      {technicalPatterns.length > 0 && (
        <Card variant="default" padding="sm" className="border-orange/30 bg-orange/5">
          <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-orange">
            <Flame className="h-3.5 w-3.5" /> 技术形态命中
          </h4>
          <TechnicalPatternCards patterns={technicalPatterns} />
        </Card>
      )}

      {candidate.aiSummary && (
        <Card variant="default" padding="sm">
          <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-foreground">
            <Brain className="h-3.5 w-3.5 text-purple" /> AI 分析
          </h4>
          <p className="text-xs leading-relaxed text-secondary-text">
            {candidate.aiSummary}
          </p>
          {candidate.aiOperationAdvice && (
            <div className="mt-2 rounded-lg border border-cyan/20 bg-cyan/5 px-3 py-2 text-xs text-cyan">
              {candidate.aiOperationAdvice}
            </div>
          )}
        </Card>
      )}
    </>
  );
}

export const CandidateDetailDrawer: React.FC = () => {
  const { selectedCandidate, clearSelectedCandidate } = useScreeningStore();
  const isOpen = selectedCandidate != null;
  const factorSnapshot = (selectedCandidate?.factorSnapshot ?? {}) as ScreeningFactorSnapshot;
  const phaseResults = factorSnapshot.phase_results;
  const phaseExplanations = Array.isArray(factorSnapshot.phase_explanations)
    ? (factorSnapshot.phase_explanations as ScreeningPhaseExplanation[])
    : undefined;
  const catalystNews = Array.isArray(factorSnapshot.theme_catalyst_news)
    ? (factorSnapshot.theme_catalyst_news as HotThemeNewsItem[])
    : [];
  const rawRuleHits = Array.isArray(factorSnapshot.rule_hits_display) && factorSnapshot.rule_hits_display.length > 0
    ? factorSnapshot.rule_hits_display
    : selectedCandidate?.ruleHits ?? [];
  const translatedRuleHits = rawRuleHits.map((ruleHit) => {
    const parsed = parseRuleHit(ruleHit);
    return {
      key: parsed.key,
      translated: translateRuleHit(ruleHit),
    };
  });
  const generalRuleHits = translatedRuleHits.filter((item) => !TECHNICAL_RULE_KEYS.has(item.key));
  const technicalHitsFromRules = translatedRuleHits
    .filter((item) => TECHNICAL_RULE_KEYS.has(item.key))
    .map((item) => item.translated);
  const technicalPatterns = extractTechnicalPatterns(factorSnapshot, technicalHitsFromRules);
  const visibleFactorEntries = Object.entries(factorSnapshot)
    .filter(([key]) => !HIDDEN_FACTOR_SNAPSHOT_KEYS.has(key));

  const useFiveLayer = selectedCandidate != null && hasFiveLayerData(selectedCandidate);

  return (
    <Drawer
      isOpen={isOpen}
      onClose={clearSelectedCandidate}
      title={selectedCandidate ? `${selectedCandidate.code} ${selectedCandidate.name || ''}` : ''}
    >
      {selectedCandidate && (
        <div className="flex flex-col gap-4" data-testid="candidate-detail">
          {/* Header badges */}
          <div className="flex flex-wrap gap-2">
            <Badge variant="info" size="md">排名 #{selectedCandidate.rank}</Badge>
            {useFiveLayer && selectedCandidate.tradeStage && (
              <LabeledBadge
                value={selectedCandidate.tradeStage}
                labelMap={TRADE_STAGE_LABELS}
                colorMap={TRADE_STAGE_COLORS}
              />
            )}
            <Badge variant={selectedCandidate.ruleScore >= 70 ? 'success' : 'default'} size="md">
              规则评分: {selectedCandidate.ruleScore.toFixed(1)}
            </Badge>
            {selectedCandidate.finalScore != null && (
              <Badge variant="history" size="md">
                综合评分: {selectedCandidate.finalScore.toFixed(1)}
              </Badge>
            )}
            {selectedCandidate.selectedForAi && (
              <Badge variant="info" size="md" glow>
                <Brain className="h-3 w-3" /> AI 已分析
              </Badge>
            )}
          </div>

          {useFiveLayer ? (
            <>
              <SectionL1 candidate={selectedCandidate} />
              <SectionL2 candidate={selectedCandidate} factorSnapshot={factorSnapshot} />
              <SectionL3 candidate={selectedCandidate} />
              <SectionL4
                candidate={selectedCandidate}
                factorSnapshot={factorSnapshot}
                technicalPatterns={technicalPatterns}
                phaseResults={phaseResults}
                phaseExplanations={phaseExplanations}
              />
              <SectionL5 candidate={selectedCandidate} />
              <SectionAI candidate={selectedCandidate} />

              {generalRuleHits.length > 0 && (
                <Card variant="default" padding="sm">
                  <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-foreground">
                    <FileText className="h-3.5 w-3.5 text-cyan" /> 命中规则
                  </h4>
                  <div className="flex flex-wrap gap-1.5">
                    {generalRuleHits.map((item, idx) => (
                      <Badge key={`${item.key}-${idx}`} variant="default" size="sm">{item.translated}</Badge>
                    ))}
                  </div>
                </Card>
              )}

              {selectedCandidate.matchedStrategies && selectedCandidate.matchedStrategies.length > 0 && (
                <Card variant="default" padding="sm">
                  <h4 className="mb-2 text-xs font-semibold text-foreground">审计证据: 匹配策略</h4>
                  <div className="flex flex-wrap gap-1.5">
                    {selectedCandidate.matchedStrategies.map((strategy) => (
                      <Badge key={strategy} variant="info" size="sm">{strategy}</Badge>
                    ))}
                  </div>
                </Card>
              )}
            </>
          ) : (
            <LegacyLayout
              candidate={selectedCandidate}
              factorSnapshot={factorSnapshot}
              generalRuleHits={generalRuleHits}
              technicalPatterns={technicalPatterns}
              phaseResults={phaseResults}
              phaseExplanations={phaseExplanations}
              catalystNews={catalystNews}
            />
          )}

          {/* Factor snapshot (collapsed for five-layer, expanded for legacy) */}
          {visibleFactorEntries.length > 0 && (
            <Collapsible title="因子快照" defaultOpen={!useFiveLayer}>
              <div className="max-h-60 overflow-y-auto">
                {visibleFactorEntries.map(([key, value]) => (
                  <FactorRow key={key} label={key} value={value} />
                ))}
              </div>
            </Collapsible>
          )}
        </div>
      )}
    </Drawer>
  );
};
