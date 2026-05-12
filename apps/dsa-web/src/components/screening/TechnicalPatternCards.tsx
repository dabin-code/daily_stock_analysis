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

function extractBottomDivergencePattern(snapshot: ScreeningFactorSnapshot): TechnicalPattern | null {
  if (!snapshot.bottom_divergence_double_breakout) return null;

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

export function extractTechnicalPatterns(
  snapshot: ScreeningFactorSnapshot,
  technicalHitsFromRules: string[] = [],
): TechnicalPattern[] {
  const patterns: TechnicalPattern[] = [];

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
                <span className="break-words">{reason}</span>
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
