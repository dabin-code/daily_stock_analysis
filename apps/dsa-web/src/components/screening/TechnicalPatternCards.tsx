import type React from 'react';
import { CheckCircle2 } from 'lucide-react';

import { Badge, Card } from '../common';
import type { ScreeningFactorSnapshot, TechnicalPattern, TechnicalPatternMetric } from '../../types/screening';

function formatPrice(value: unknown): string {
  if (value == null) return '—';
  if (typeof value === 'number') return value.toFixed(2);
  return String(value);
}

function formatStrength(value: unknown): string {
  if (value == null) return '—';
  if (typeof value === 'number') return (value * 100).toFixed(0) + '%';
  return String(value);
}

function createMetric(label: string, value: unknown): TechnicalPatternMetric {
  return {
    label,
    value: typeof value === 'number' && label.includes('强度') ? formatStrength(value) : formatPrice(value),
  };
}

function formatZoneRange(lower: unknown, upper: unknown): string | null {
  const hasLower = typeof lower === 'number' && Number.isFinite(lower);
  const hasUpper = typeof upper === 'number' && Number.isFinite(upper);
  if (!hasLower && !hasUpper) return null;
  if (hasLower && hasUpper && lower !== upper) {
    return `${formatPrice(lower)}–${formatPrice(upper)}`;
  }
  return formatPrice(hasLower ? lower : upper);
}

function formatEventDays(value: unknown): string | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return null;
  if (value === 0) return '今日触发';
  return `${value}个交易日前触发`;
}

const OBSERVATION_ONLY_NAMES: Record<string, string> = {
  adjustment_unknown: '数据待确认·仅观察',
  stale: '底背离信号已过期·仅观察',
  extended: '底背离已走远·勿追',
  invalidated: '底背离结构已失效·仅观察',
  breakout_failed: '阻力突破失败·仅观察',
};

function extractBottomDivergenceV2Pattern(snapshot: ScreeningFactorSnapshot): TechnicalPattern | null {
  const stage = typeof snapshot.bottom_divergence_v2_stage === 'string'
    ? snapshot.bottom_divergence_v2_stage
    : '';
  if (
    !snapshot.bottom_divergence_v2_candidate
    && (!stage || stage === 'rejected')
  ) return null;

  const status = typeof snapshot.bottom_divergence_v2_actionability_status === 'string'
    ? snapshot.bottom_divergence_v2_actionability_status
    : '';
  const stageStatusAllowed = (
    ((stage === 'early' || stage === 'near_cleared') && status === 'major_not_confirmed')
    || (stage === 'major_actionable' && status === 'actionable')
  );
  const statusRequiresObservation = (
    !stageStatusAllowed
    && !OBSERVATION_ONLY_NAMES[status]
  );
  const observationKey = OBSERVATION_ONLY_NAMES[status]
    ? status
    : (OBSERVATION_ONLY_NAMES[stage] ? stage : '');
  const hasCandidateEvidence = snapshot.bottom_divergence_v2_candidate === true;
  const earlyEvidenceComplete = (
    stage === 'early'
    && hasCandidateEvidence
    && snapshot.bottom_divergence_v2_early_reversal === true
  );
  const nearEvidenceComplete = (
    stage === 'near_cleared'
    && hasCandidateEvidence
    && snapshot.bottom_divergence_v2_near_cleared === true
  );
  const majorEvidenceComplete = (
    stage === 'major_actionable'
    && hasCandidateEvidence
    && snapshot.bottom_divergence_v2_major_actionable_entry === true
  );
  const majorIsCurrentlyActionable = (
    majorEvidenceComplete
    && status === 'actionable'
  );
  const actionableStageEvidenceIncomplete = (
    (stage === 'early' && !earlyEvidenceComplete)
    || (stage === 'near_cleared' && !nearEvidenceComplete)
    || (stage === 'major_actionable' && !majorEvidenceComplete)
  );

  let name = '底背离 v2·仅观察';
  let targetPosition: string | null = null;
  if (observationKey) {
    name = OBSERVATION_ONLY_NAMES[observationKey];
  } else if (statusRequiresObservation) {
    name = '数据待确认·仅观察';
  } else if (actionableStageEvidenceIncomplete) {
    name = '仅观察·证据不完整';
  } else if (majorIsCurrentlyActionable) {
    name = '主要阻力确认·可加仓';
    targetPosition = '目标100%';
  } else if (
    stage === 'major_unverified'
    || stage === 'major_actionable'
    || (snapshot.bottom_divergence_v2_major_breakout && !majorIsCurrentlyActionable)
  ) {
    name = '主要阻力已突破·数据待确认';
  } else if (nearEvidenceComplete) {
    name = '近端阻力突破·加仓';
    targetPosition = '目标50%';
  } else if (earlyEvidenceComplete) {
    name = '底背离早期反转·试仓';
    targetPosition = '目标20%';
  }

  const metrics: TechnicalPatternMetric[] = [];
  if (targetPosition) metrics.push(createMetric('目标仓位', targetPosition));
  if (snapshot.bottom_divergence_v2_pattern_code) {
    metrics.push(createMetric('形态编码', snapshot.bottom_divergence_v2_pattern_code));
  }
  if (snapshot.bottom_divergence_v2_early_strength != null) {
    metrics.push(createMetric('早期强度', snapshot.bottom_divergence_v2_early_strength));
  }
  if (snapshot.bottom_divergence_v2_stop_loss_price != null) {
    metrics.push(createMetric('止损参考', snapshot.bottom_divergence_v2_stop_loss_price));
  }

  const nearZone = formatZoneRange(
    snapshot.bottom_divergence_v2_near_zone_lower,
    snapshot.bottom_divergence_v2_near_zone_upper,
  );
  if (nearZone) metrics.push(createMetric('R1阻力', nearZone));
  const majorZone = formatZoneRange(
    snapshot.bottom_divergence_v2_major_zone_lower,
    snapshot.bottom_divergence_v2_major_zone_upper,
  );
  if (majorZone) metrics.push(createMetric('R2阻力', majorZone));

  if (snapshot.bottom_divergence_v2_near_zone_score != null) {
    metrics.push(createMetric('R1评分', snapshot.bottom_divergence_v2_near_zone_score));
  }
  if (snapshot.bottom_divergence_v2_major_zone_score != null) {
    metrics.push(createMetric('R2评分', snapshot.bottom_divergence_v2_major_zone_score));
  }
  if (snapshot.bottom_divergence_v2_near_cleared) {
    metrics.push(createMetric('R1事件', '已突破并确认'));
  } else if (snapshot.bottom_divergence_v2_near_crossed) {
    metrics.push(createMetric('R1事件', '已突破·待确认'));
  } else if (snapshot.bottom_divergence_v2_near_accepted) {
    metrics.push(createMetric('R1事件', '已进入并承接'));
  } else if (snapshot.bottom_divergence_v2_near_entered) {
    metrics.push(createMetric('R1事件', '已触及阻力区'));
  }
  if (snapshot.bottom_divergence_v2_major_breakout) {
    metrics.push(createMetric('历史突破', '已确认'));
  }
  if (stage === 'major_unverified' || stage === 'major_actionable' || snapshot.bottom_divergence_v2_major_breakout) {
    metrics.push(createMetric('当前可操作', majorIsCurrentlyActionable ? '是' : '否·仅观察'));
  }

  const eventDays = formatEventDays(
    snapshot.bottom_divergence_v2_event_days
      ?? snapshot.bottom_divergence_v2_confirmation_days,
  );
  if (eventDays) metrics.push(createMetric('触发时间', eventDays));
  if (snapshot.bottom_divergence_v2_candidate_version) {
    metrics.push(createMetric('候选版本', snapshot.bottom_divergence_v2_candidate_version));
  }
  if (snapshot.bottom_divergence_v2_zone_version) {
    metrics.push(createMetric('阻力区版本', snapshot.bottom_divergence_v2_zone_version));
  }
  if (snapshot.bottom_divergence_v2_extended_pct != null) {
    metrics.push(createMetric('突破后延伸', `${snapshot.bottom_divergence_v2_extended_pct}%`));
  }

  const hitReasons = Array.isArray(snapshot.bottom_divergence_v2_hit_reasons)
    ? [...snapshot.bottom_divergence_v2_hit_reasons]
    : [];
  if (Array.isArray(snapshot.bottom_divergence_v2_degradation_reasons)) {
    hitReasons.push(...snapshot.bottom_divergence_v2_degradation_reasons.map((reason) => `降级：${reason}`));
  }

  return {
    id: 'bottom_divergence_v2',
    name,
    signalStrength: snapshot.bottom_divergence_v2_early_strength,
    metrics,
    hitReasons,
  };
}

function extractBottomDivergencePattern(snapshot: ScreeningFactorSnapshot): TechnicalPattern | null {
  if (!snapshot.bottom_divergence_double_breakout) return null;
  if (snapshot.bottom_divergence_actionable_entry === false) return null;

  const metrics: TechnicalPatternMetric[] = [];
  if (snapshot.bottom_divergence_pattern_label) {
    metrics.push(createMetric('形态类型', snapshot.bottom_divergence_pattern_label));
  }
  if (snapshot.bottom_divergence_entry_price != null) {
    metrics.push(createMetric('入场参考', snapshot.bottom_divergence_entry_price));
  }
  if (snapshot.bottom_divergence_stop_loss != null) {
    metrics.push(createMetric('止损参考', snapshot.bottom_divergence_stop_loss));
  }
  if (snapshot.bottom_divergence_horizontal_breakout) {
    metrics.push(createMetric('水平突破', '✓'));
  }
  if (snapshot.bottom_divergence_trendline_breakout) {
    metrics.push(createMetric('趋势线突破', '✓'));
  }
  if (snapshot.bottom_divergence_sync_breakout) {
    metrics.push(createMetric('双突破同步', '✓'));
  }
  if (snapshot.bottom_divergence_entry_timing_score != null) {
    metrics.push(createMetric('入场时机', snapshot.bottom_divergence_entry_timing_score));
  }
  if (snapshot.bottom_divergence_extended_pct != null) {
    metrics.push(createMetric('偏离确认价', snapshot.bottom_divergence_extended_pct + '%'));
  }

  const hitReasons = Array.isArray(snapshot.bottom_divergence_hit_reasons)
    ? snapshot.bottom_divergence_hit_reasons
    : [];

  return {
    id: 'bottom_divergence',
    name: '底背离双突破',
    signalStrength: snapshot.bottom_divergence_signal_strength,
    metrics,
    hitReasons,
  };
}

function extractMA100Low123Pattern(snapshot: ScreeningFactorSnapshot): TechnicalPattern | null {
  if (!snapshot.ma100_low123_confirmed) return null;

  const metrics: TechnicalPatternMetric[] = [];
  if (snapshot.ma100_low123_pattern_strength != null) {
    metrics.push(createMetric('形态强度', snapshot.ma100_low123_pattern_strength));
  }
  if (snapshot.ma100_low123_ma_score != null) {
    metrics.push(createMetric('MA评分', snapshot.ma100_low123_ma_score));
  }
  if (snapshot.ma100_low123_entry_timing_score != null) {
    metrics.push(createMetric('入场时机', snapshot.ma100_low123_entry_timing_score));
  }

  const hitReasons = Array.isArray(snapshot.ma100_low123_hit_reasons)
    ? snapshot.ma100_low123_hit_reasons
    : [];

  return {
    id: 'ma100_low123',
    name: snapshot.ma100_low123_entry_zone === 'between_p3_p2'
      ? 'MA100+低位123最佳观察区'
      : 'MA100+低位123刚突破P2',
    signalStrength: snapshot.ma100_low123_pattern_strength,
    metrics,
    hitReasons,
  };
}

function extractMA100Low123WatchlistPattern(snapshot: ScreeningFactorSnapshot): TechnicalPattern | null {
  if (snapshot.ma100_low123_confirmed) return null;
  if (!snapshot.ma100_low123_watchlist) return null;

  const metrics: TechnicalPatternMetric[] = [];
  if (snapshot.ma100_low123_pattern_strength != null) {
    metrics.push(createMetric('形态强度', snapshot.ma100_low123_pattern_strength));
  }
  if (snapshot.ma100_low123_ma_score != null) {
    metrics.push(createMetric('MA评分', snapshot.ma100_low123_ma_score));
  }

  const hitReasons = Array.isArray(snapshot.ma100_low123_watch_hit_reasons)
    ? snapshot.ma100_low123_watch_hit_reasons
    : [];

  return {
    id: 'ma100_low123_watchlist',
    name: 'MA100+低位123观察池',
    signalStrength: snapshot.ma100_low123_pattern_strength,
    metrics,
    hitReasons,
  };
}

function extractMA10060minPattern(snapshot: ScreeningFactorSnapshot): TechnicalPattern | null {
  if (!snapshot.ma100_60min_confirmed) return null;

  const metrics: TechnicalPatternMetric[] = [];
  if (snapshot.ma100_60min_freshness_score != null) {
    metrics.push(createMetric('新鲜度', snapshot.ma100_60min_freshness_score));
  }
  if (snapshot.ma100_60min_ma_score != null) {
    metrics.push(createMetric('MA评分', snapshot.ma100_60min_ma_score));
  }

  const hitReasons = Array.isArray(snapshot.ma100_60min_hit_reasons)
    ? snapshot.ma100_60min_hit_reasons
    : [];

  return {
    id: 'ma100_60min',
    name: 'MA100+60分钟线',
    signalStrength: snapshot.ma100_60min_freshness_score,
    metrics,
    hitReasons,
  };
}

function extractPattern123Pattern(snapshot: ScreeningFactorSnapshot): TechnicalPattern | null {
  const state = snapshot.pattern_123_state;
  const isBreakoutReady = snapshot.pattern_123_breakout_ready ?? snapshot.pattern_123_low_trendline ?? state === 'breakout_ready';
  const isWatching = snapshot.pattern_123_watchlist ?? state === 'watching';
  if (!isBreakoutReady && !isWatching) return null;
  if (snapshot.ma100_low123_confirmed) return null;
  if (snapshot.ma100_low123_watchlist) return null;

  const metrics: TechnicalPatternMetric[] = [];
  if (state) {
    metrics.push(createMetric('状态', state === 'breakout_ready' ? '突破成熟' : state === 'watching' ? '观察中' : state));
  }
  if (snapshot.pattern_123_entry_price != null) {
    metrics.push(createMetric('入场参考', snapshot.pattern_123_entry_price));
  }
  if (snapshot.pattern_123_stop_loss != null) {
    metrics.push(createMetric('止损参考', snapshot.pattern_123_stop_loss));
  }
  if (snapshot.pattern_123_signal_strength != null) {
    metrics.push(createMetric('信号强度', snapshot.pattern_123_signal_strength));
  }

  return {
    id: 'pattern_123',
    name: isWatching ? '低位123观察中' : '低位123突破成熟',
    signalStrength: snapshot.pattern_123_signal_strength,
    metrics,
    hitReasons: [],
  };
}

function extractSimplePatterns(snapshot: ScreeningFactorSnapshot): TechnicalPattern[] {
  const patterns: TechnicalPattern[] = [];

  if (snapshot.gap_breakaway) {
    patterns.push({
      id: 'gap_breakaway',
      name: '跳空突破',
      metrics: [],
      hitReasons: [],
    });
  }

  if (snapshot.is_limit_up) {
    patterns.push({
      id: 'is_limit_up',
      name: '涨停',
      metrics: [],
      hitReasons: [],
    });
  }

  if (
    snapshot.above_ma100
    && !snapshot.ma100_low123_confirmed
    && !snapshot.ma100_low123_watchlist
    && !snapshot.ma100_60min_confirmed
  ) {
    patterns.push({
      id: 'above_ma100',
      name: '站上MA100',
      metrics: [],
      hitReasons: [],
    });
  }

  return patterns;
}

// Shared by the detail drawer and focused extractor tests.
// eslint-disable-next-line react-refresh/only-export-components
export function extractTechnicalPatterns(
  snapshot: ScreeningFactorSnapshot,
  technicalHitsFromRules: string[] = [],
): TechnicalPattern[] {
  const patterns: TechnicalPattern[] = [];

  const bottomDivV2 = extractBottomDivergenceV2Pattern(snapshot);
  if (bottomDivV2) patterns.push(bottomDivV2);

  const bottomDiv = extractBottomDivergencePattern(snapshot);
  if (bottomDiv) patterns.push(bottomDiv);

  const ma100Low123 = extractMA100Low123Pattern(snapshot);
  if (ma100Low123) patterns.push(ma100Low123);

  const ma100Low123Watchlist = extractMA100Low123WatchlistPattern(snapshot);
  if (ma100Low123Watchlist) patterns.push(ma100Low123Watchlist);

  const ma10060min = extractMA10060minPattern(snapshot);
  if (ma10060min) patterns.push(ma10060min);

  const pattern123 = extractPattern123Pattern(snapshot);
  if (pattern123) patterns.push(pattern123);

  patterns.push(...extractSimplePatterns(snapshot));

  if (patterns.length === 0 && technicalHitsFromRules.length > 0) {
    return technicalHitsFromRules.map((hit) => ({
      id: `fallback_${hit}`,
      name: hit,
      metrics: [],
      hitReasons: [],
    }));
  }

  return patterns;
}

interface PatternCardProps {
  readonly pattern: TechnicalPattern;
}

function PatternCard({ pattern }: PatternCardProps) {
  const isRich = pattern.metrics.length > 0 || pattern.hitReasons.length > 0;

  if (!isRich) {
    return (
      <Badge variant="default" size="sm" className="bg-orange/10 text-orange border-orange/30">
        {pattern.name}
      </Badge>
    );
  }

  return (
    <Card variant="default" padding="sm" className="border-orange/30 bg-orange/5">
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h5 className="text-xs font-semibold text-foreground">{pattern.name}</h5>
          {pattern.signalStrength != null && (
            <span className="text-xs text-secondary-text">
              信号强度: {formatStrength(pattern.signalStrength)}
            </span>
          )}
        </div>

        {pattern.metrics.length > 0 && (
          <div className="grid grid-cols-2 gap-2 text-xs">
            {pattern.metrics.map((metric, idx) => (
              <div key={idx} className="flex justify-between gap-2">
                <span className="text-secondary-text">{metric.label}</span>
                <span className="font-mono text-foreground">{metric.value}</span>
              </div>
            ))}
          </div>
        )}

        {pattern.hitReasons.length > 0 && (
          <div className="space-y-1 border-t border-orange/20 pt-2">
            {pattern.hitReasons.map((reason, idx) => (
              <div key={idx} className="flex items-start gap-2 text-xs text-secondary-text">
                <CheckCircle2 className="h-3 w-3 shrink-0 text-orange mt-0.5" />
                <span className="wrap-break-word">{reason}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </Card>
  );
}

interface TechnicalPatternCardsProps {
  readonly patterns: readonly TechnicalPattern[];
}

export const TechnicalPatternCards: React.FC<TechnicalPatternCardsProps> = ({ patterns }) => {
  if (patterns.length === 0) return null;

  return (
    <div className="space-y-2">
      {patterns.map((pattern) => (
        <PatternCard key={pattern.id} pattern={pattern} />
      ))}
    </div>
  );
};
