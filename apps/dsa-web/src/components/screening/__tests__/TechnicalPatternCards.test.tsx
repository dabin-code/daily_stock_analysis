import { describe, it, expect } from 'vitest';
import { extractTechnicalPatterns } from '../TechnicalPatternCards';
import type { ScreeningFactorSnapshot } from '../../../types/screening';

describe('extractTechnicalPatterns', () => {
  describe('bottom divergence pattern', () => {
    it('extracts bottom divergence pattern with all metrics', () => {
      const snapshot: ScreeningFactorSnapshot = {
        bottom_divergence_double_breakout: true,
        bottom_divergence_pattern_label: '价格持平-MACD抬升',
        bottom_divergence_signal_strength: 0.59,
        bottom_divergence_entry_price: 8.11,
        bottom_divergence_stop_loss: 7.66,
        bottom_divergence_horizontal_breakout: true,
        bottom_divergence_trendline_breakout: true,
        bottom_divergence_sync_breakout: true,
        bottom_divergence_hit_reasons: [
          '【底背离形态】价格持平-MACD抬升',
          '【前置跌幅】26.6%',
        ],
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(1);
      expect(patterns[0]).toMatchObject({
        id: 'bottom_divergence',
        name: '底背离双突破',
        signalStrength: 0.59,
      });
      expect(patterns[0].metrics).toHaveLength(6);
      expect(patterns[0].hitReasons).toEqual([
        '【底背离形态】价格持平-MACD抬升',
        '【前置跌幅】26.6%',
      ]);
    });

    it('returns empty hit reasons when not provided', () => {
      const snapshot: ScreeningFactorSnapshot = {
        bottom_divergence_double_breakout: true,
        bottom_divergence_pattern_label: '价格持平-MACD抬升',
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns[0].hitReasons).toEqual([]);
    });

    it('suppresses non-actionable bottom divergence cards', () => {
      const snapshot: ScreeningFactorSnapshot = {
        bottom_divergence_double_breakout: true,
        bottom_divergence_actionable_entry: false,
        bottom_divergence_validation_status: 'extended_not_entry',
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(0);
    });

    it('renders an early v2 probe card without requiring the v1 double breakout', () => {
      const snapshot: ScreeningFactorSnapshot = {
        bottom_divergence_v2_candidate: true,
        bottom_divergence_v2_stage: 'early',
        bottom_divergence_v2_actionability_status: 'major_not_confirmed',
        bottom_divergence_v2_early_reversal: true,
        bottom_divergence_v2_early_strength: 0.72,
        bottom_divergence_v2_near_zone_lower: 39.08,
        bottom_divergence_v2_near_zone_upper: 39.2,
        bottom_divergence_v2_near_entered: true,
        bottom_divergence_v2_stop_loss_price: 37.66,
        bottom_divergence_v2_event_days: 0,
        bottom_divergence_v2_candidate_version: 'candidate-v2',
        bottom_divergence_v2_zone_version: 'zone-v2',
        bottom_divergence_v2_degradation_reasons: ['复权状态待确认'],
        bottom_divergence_v2_hit_reasons: ['早期反转结构成立'],
        bottom_divergence_v2_candidate_records: [{ should_not_render: true }],
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(1);
      expect(patterns[0]).toMatchObject({
        id: 'bottom_divergence_v2',
        name: '底背离早期反转·试仓',
        signalStrength: 0.72,
      });
      expect(patterns[0].metrics).toEqual(expect.arrayContaining([
        { label: '目标仓位', value: '目标20%' },
        { label: '早期强度', value: '72%' },
        { label: '止损参考', value: '37.66' },
        { label: 'R1阻力', value: '39.08–39.20' },
        { label: '触发时间', value: '今日触发' },
        { label: '候选版本', value: 'candidate-v2' },
        { label: '阻力区版本', value: 'zone-v2' },
        { label: 'R1事件', value: '已触及阻力区' },
      ]));
      expect(patterns[0].hitReasons).toEqual(expect.arrayContaining([
        '早期反转结构成立',
        '降级：复权状态待确认',
      ]));
      expect(patterns[0].name).not.toContain('candidate-v2');
      expect(JSON.stringify(patterns[0])).not.toContain('should_not_render');
    });

    it('renders a near-cleared v2 add card with R1 and R2 ranges', () => {
      const snapshot: ScreeningFactorSnapshot = {
        bottom_divergence_v2_candidate: true,
        bottom_divergence_v2_stage: 'near_cleared',
        bottom_divergence_v2_actionability_status: 'major_not_confirmed',
        bottom_divergence_v2_near_zone_lower: 39.08,
        bottom_divergence_v2_near_zone_upper: 39.2,
        bottom_divergence_v2_near_zone_score: 0.81,
        bottom_divergence_v2_near_crossed: true,
        bottom_divergence_v2_near_cleared: true,
        bottom_divergence_v2_major_zone_lower: 41.2,
        bottom_divergence_v2_major_zone_upper: 42.9,
        bottom_divergence_v2_major_zone_score: 0.66,
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns[0].name).toBe('近端阻力突破·加仓');
      expect(patterns[0].metrics).toEqual(expect.arrayContaining([
        { label: '目标仓位', value: '目标50%' },
        { label: 'R1阻力', value: '39.08–39.20' },
        { label: 'R2阻力', value: '41.20–42.90' },
        { label: 'R1事件', value: '已突破并确认' },
      ]));
    });

    it('shows accepted R1 evidence before a confirmed cross', () => {
      const snapshot: ScreeningFactorSnapshot = {
        bottom_divergence_v2_stage: 'early',
        bottom_divergence_v2_near_accepted: true,
      };

      expect(extractTechnicalPatterns(snapshot)[0].metrics).toContainEqual({
        label: 'R1事件',
        value: '已进入并承接',
      });
    });

    it('renders a currently actionable major-breakout v2 card', () => {
      const snapshot: ScreeningFactorSnapshot = {
        bottom_divergence_v2_candidate: true,
        bottom_divergence_v2_stage: 'major_actionable',
        bottom_divergence_v2_major_zone_lower: 41.2,
        bottom_divergence_v2_major_zone_upper: 42.9,
        bottom_divergence_v2_major_breakout: true,
        bottom_divergence_v2_major_actionable_entry: true,
        bottom_divergence_v2_actionability_status: 'actionable',
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns[0].name).toBe('主要阻力确认·可加仓');
      expect(patterns[0].metrics).toEqual(expect.arrayContaining([
        { label: '目标仓位', value: '目标100%' },
        { label: 'R2阻力', value: '41.20–42.90' },
        { label: '历史突破', value: '已确认' },
        { label: '当前可操作', value: '是' },
      ]));
    });

    it.each([
      ['early', undefined, true],
      ['early', true, false],
      ['near_cleared', undefined, true],
      ['near_cleared', true, false],
      ['major_actionable', undefined, true],
      ['major_actionable', true, false],
    ])(
      'fails closed for %s when candidate=%s and stage evidence=%s',
      (stage, candidate, stageEvidence) => {
        const snapshot: ScreeningFactorSnapshot = {
          bottom_divergence_v2_stage: stage,
          bottom_divergence_v2_candidate: candidate,
          bottom_divergence_v2_early_reversal: stage === 'early' ? stageEvidence : undefined,
          bottom_divergence_v2_near_cleared: stage === 'near_cleared' ? stageEvidence : undefined,
          bottom_divergence_v2_major_actionable_entry: stage === 'major_actionable' ? stageEvidence : undefined,
          bottom_divergence_v2_actionability_status: stage === 'major_actionable'
            ? 'actionable'
            : 'major_not_confirmed',
        };

        const pattern = extractTechnicalPatterns(snapshot)[0];
        const renderedText = `${pattern.name} ${pattern.metrics.map((metric) => `${metric.label}${metric.value}`).join(' ')}`;

        expect(pattern.name).toBe('仅观察·证据不完整');
        expect(renderedText).not.toMatch(/试仓|可加仓|目标仓位|目标\d+%/);
        expect(renderedText).not.toMatch(/近端阻力突破·加仓/);
      },
    );

    it('keeps an historical major breakout fail-closed when current actionability is unverified', () => {
      const snapshot: ScreeningFactorSnapshot = {
        bottom_divergence_v2_stage: 'major_unverified',
        bottom_divergence_v2_major_breakout: true,
        bottom_divergence_v2_major_actionable_entry: false,
        bottom_divergence_v2_actionability_status: 'adjustment_unknown',
        bottom_divergence_v2_major_zone_lower: 41.2,
        bottom_divergence_v2_major_zone_upper: 42.9,
      };

      const pattern = extractTechnicalPatterns(snapshot)[0];
      const renderedText = `${pattern.name} ${pattern.metrics.map((metric) => `${metric.label}${metric.value}`).join(' ')}`;

      expect(pattern.name).toBe('数据待确认·仅观察');
      expect(pattern.metrics).toEqual(expect.arrayContaining([
        { label: '历史突破', value: '已确认' },
        { label: '当前可操作', value: '否·仅观察' },
      ]));
      expect(renderedText).not.toContain('可加仓');
      expect(renderedText).not.toContain('目标仓位');
    });

    it.each(['early', 'near_cleared'])(
      'renders real unknown-provenance %s evidence as observation-only',
      (stage) => {
        const snapshot: ScreeningFactorSnapshot = {
          bottom_divergence_v2_candidate: true,
          bottom_divergence_v2_stage: stage,
          bottom_divergence_v2_actionability_status: 'adjustment_unknown',
          bottom_divergence_v2_early_reversal: stage === 'early',
          bottom_divergence_v2_near_cleared: stage === 'near_cleared',
        };

        const pattern = extractTechnicalPatterns(snapshot)[0];
        const renderedText = `${pattern.name} ${pattern.metrics.map((metric) => `${metric.label}${metric.value}`).join(' ')}`;

        expect(pattern.name).toBe('数据待确认·仅观察');
        expect(renderedText).not.toMatch(/试仓|加仓|目标仓位|目标20%|目标50%/);
      },
    );

    it.each(['', 'unknown', 'unexpected_status'])(
      'fails closed for unrecognized actionability status %s',
      (status) => {
        const snapshot: ScreeningFactorSnapshot = {
          bottom_divergence_v2_candidate: true,
          bottom_divergence_v2_stage: 'early',
          bottom_divergence_v2_actionability_status: status,
          bottom_divergence_v2_early_reversal: true,
        };

        const pattern = extractTechnicalPatterns(snapshot)[0];
        const renderedText = `${pattern.name} ${pattern.metrics.map((metric) => `${metric.label}${metric.value}`).join(' ')}`;

        expect(pattern.name).toBe('数据待确认·仅观察');
        expect(renderedText).not.toMatch(/试仓|加仓|目标仓位|目标20%|目标50%/);
      },
    );

    it.each([
      ['stale', 'stale', '底背离信号已过期·仅观察'],
      ['major_actionable', 'extended', '底背离已走远·勿追'],
      ['invalidated', 'invalidated', '底背离结构已失效·仅观察'],
      ['major_actionable', 'breakout_failed', '阻力突破失败·仅观察'],
    ])('uses observation-only wording for %s/%s', (stage, status, expectedName) => {
      const snapshot: ScreeningFactorSnapshot = {
        bottom_divergence_v2_stage: stage,
        bottom_divergence_v2_actionability_status: status,
        bottom_divergence_v2_major_breakout: true,
        bottom_divergence_v2_major_actionable_entry: false,
      };

      const pattern = extractTechnicalPatterns(snapshot)[0];
      const renderedText = `${pattern.name} ${pattern.metrics.map((metric) => `${metric.label}${metric.value}`).join(' ')}`;

      expect(pattern.name).toBe(expectedName);
      expect(renderedText).not.toMatch(/可加仓|目标仓位|目标\d+%/);
    });

    it('does not invent a resistance range when only one boundary exists', () => {
      const snapshot: ScreeningFactorSnapshot = {
        bottom_divergence_v2_stage: 'near_cleared',
        bottom_divergence_v2_near_zone_lower: 39.08,
        bottom_divergence_v2_major_zone_upper: 42.9,
      };

      const pattern = extractTechnicalPatterns(snapshot)[0];

      expect(pattern.metrics).toEqual(expect.arrayContaining([
        { label: 'R1阻力', value: '39.08' },
        { label: 'R2阻力', value: '42.90' },
      ]));
      expect(pattern.metrics.find((metric) => metric.label === 'R1阻力')?.value).not.toContain('–');
    });

    it('does not render the stable disabled/rejected v2 default as a pattern', () => {
      const snapshot: ScreeningFactorSnapshot = {
        bottom_divergence_v2_candidate: false,
        bottom_divergence_v2_stage: 'rejected',
        bottom_divergence_v2_actionability_status: 'disabled',
      };

      expect(extractTechnicalPatterns(snapshot)).toHaveLength(0);
    });

    it('keeps genuine v1 and v2 cards independent', () => {
      const snapshot: ScreeningFactorSnapshot = {
        bottom_divergence_v2_candidate: true,
        bottom_divergence_v2_stage: 'early',
        bottom_divergence_double_breakout: true,
        bottom_divergence_pattern_label: '价格持平-MACD抬升',
      };

      const patterns = extractTechnicalPatterns(snapshot, ['底背离双突破']);

      expect(patterns.map((pattern) => pattern.id)).toEqual([
        'bottom_divergence_v2',
        'bottom_divergence',
      ]);
    });
  });

  describe('MA100+Low123 pattern', () => {
    it('extracts MA100+Low123 pattern', () => {
      const snapshot: ScreeningFactorSnapshot = {
        ma100_low123_confirmed: true,
        ma100_low123_pattern_strength: 0.75,
        ma100_low123_ma_score: 0.85,
        ma100_low123_hit_reasons: ['【形态确认】低位123突破', '【MA确认】站上MA100'],
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(1);
      expect(patterns[0]).toMatchObject({
        id: 'ma100_low123',
        name: 'MA100+低位123刚突破P2',
        signalStrength: 0.75,
      });
      expect(patterns[0].metrics).toHaveLength(2);
    });

    it('labels MA100+Low123 pre-P2 entry zone distinctly', () => {
      const snapshot: ScreeningFactorSnapshot = {
        ma100_low123_confirmed: true,
        ma100_low123_watchlist: true,
        ma100_low123_entry_zone: 'between_p3_p2',
        ma100_low123_entry_timing_score: 0.7,
        ma100_low123_pattern_strength: 0.61,
        ma100_low123_ma_score: 0.72,
        ma100_low123_hit_reasons: ['【最佳买点】最新K线位于P3-P2之间，等待突破P2触发'],
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(1);
      expect(patterns[0]).toMatchObject({
        id: 'ma100_low123',
        name: 'MA100+低位123最佳观察区',
      });
      expect(patterns[0].metrics).toEqual(
        expect.arrayContaining([
          expect.objectContaining({ label: '入场时机', value: '0.70' }),
        ]),
      );
    });

    it('suppresses standalone pattern_123 when MA100+Low123 is confirmed', () => {
      const snapshot: ScreeningFactorSnapshot = {
        ma100_low123_confirmed: true,
        pattern_123_low_trendline: true,
        pattern_123_entry_price: 8.0,
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(1);
      expect(patterns[0].id).toBe('ma100_low123');
    });

    it('extracts MA100+Low123 watchlist pattern', () => {
      const snapshot: ScreeningFactorSnapshot = {
        above_ma100: true,
        ma100_low123_watchlist: true,
        ma100_low123_pattern_strength: 0.61,
        ma100_low123_ma_score: 0.72,
        ma100_low123_watch_hit_reasons: ['【观察池】最新收盘价已大于P3但尚未突破P2，纳入重点观察'],
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(1);
      expect(patterns[0]).toMatchObject({
        id: 'ma100_low123_watchlist',
        name: 'MA100+低位123观察池',
        signalStrength: 0.61,
      });
      expect(patterns[0].hitReasons).toEqual([
        '【观察池】最新收盘价已大于P3但尚未突破P2，纳入重点观察',
      ]);
      expect(patterns.map((pattern) => pattern.id)).not.toContain('above_ma100');
    });
  });

  describe('MA100+60min pattern', () => {
    it('extracts MA100+60min pattern', () => {
      const snapshot: ScreeningFactorSnapshot = {
        ma100_60min_confirmed: true,
        ma100_60min_freshness_score: 0.8,
        ma100_60min_ma_score: 0.9,
        ma100_60min_hit_reasons: ['【60分钟】入场信号确认'],
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(1);
      expect(patterns[0]).toMatchObject({
        id: 'ma100_60min',
        name: 'MA100+60分钟线',
      });
    });

    it('suppresses standalone above_ma100 when MA100+60min is confirmed', () => {
      const snapshot: ScreeningFactorSnapshot = {
        ma100_60min_confirmed: true,
        above_ma100: true,
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(1);
      expect(patterns[0].id).toBe('ma100_60min');
    });
  });

  describe('standalone pattern_123', () => {
    it('extracts standalone pattern_123 when not part of combo', () => {
      const snapshot: ScreeningFactorSnapshot = {
        pattern_123_low_trendline: true,
        pattern_123_state: 'breakout_ready',
        pattern_123_entry_price: 8.0,
        pattern_123_stop_loss: 7.5,
        pattern_123_signal_strength: 0.7,
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(1);
      expect(patterns[0]).toMatchObject({
        id: 'pattern_123',
        name: '低位123突破成熟',
      });
    });

    it('extracts standalone watching pattern when not above ma100 combo', () => {
      const snapshot: ScreeningFactorSnapshot = {
        pattern_123_state: 'watching',
        pattern_123_signal_strength: 0.52,
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(1);
      expect(patterns[0]).toMatchObject({
        id: 'pattern_123',
        name: '低位123观察中',
        signalStrength: 0.52,
      });
    });
  });

  describe('simple patterns', () => {
    it('extracts gap_breakaway pattern', () => {
      const snapshot: ScreeningFactorSnapshot = {
        gap_breakaway: true,
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(1);
      expect(patterns[0]).toMatchObject({
        id: 'gap_breakaway',
        name: '跳空突破',
        metrics: [],
        hitReasons: [],
      });
    });

    it('extracts is_limit_up pattern', () => {
      const snapshot: ScreeningFactorSnapshot = {
        is_limit_up: true,
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(1);
      expect(patterns[0]).toMatchObject({
        id: 'is_limit_up',
        name: '涨停',
      });
    });

    it('extracts above_ma100 only when not part of combo', () => {
      const snapshot: ScreeningFactorSnapshot = {
        above_ma100: true,
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(1);
      expect(patterns[0].id).toBe('above_ma100');
    });

    it('suppresses above_ma100 when MA100+Low123 is confirmed', () => {
      const snapshot: ScreeningFactorSnapshot = {
        above_ma100: true,
        ma100_low123_confirmed: true,
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(1);
      expect(patterns[0].id).toBe('ma100_low123');
    });
  });

  describe('multiple patterns', () => {
    it('extracts multiple patterns in priority order', () => {
      const snapshot: ScreeningFactorSnapshot = {
        bottom_divergence_double_breakout: true,
        bottom_divergence_pattern_label: '价格持平-MACD抬升',
        gap_breakaway: true,
        is_limit_up: true,
      };

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(3);
      expect(patterns[0].id).toBe('bottom_divergence');
      expect(patterns[1].id).toBe('gap_breakaway');
      expect(patterns[2].id).toBe('is_limit_up');
    });
  });

  describe('fallback to string-based rendering', () => {
    it('falls back to technicalHitsFromRules when no structured patterns', () => {
      const snapshot: ScreeningFactorSnapshot = {};
      const technicalHitsFromRules = ['跳空突破', '涨停'];

      const patterns = extractTechnicalPatterns(snapshot, technicalHitsFromRules);

      expect(patterns).toHaveLength(2);
      expect(patterns[0].name).toBe('跳空突破');
      expect(patterns[1].name).toBe('涨停');
    });

    it('returns empty array when no patterns and no fallback', () => {
      const snapshot: ScreeningFactorSnapshot = {};

      const patterns = extractTechnicalPatterns(snapshot);

      expect(patterns).toHaveLength(0);
    });
  });

  describe('deduplication', () => {
    it('handles multiple patterns without duplication', () => {
      const snapshot: ScreeningFactorSnapshot = {
        bottom_divergence_double_breakout: true,
        bottom_divergence_pattern_label: '价格持平-MACD抬升',
        ma100_low123_confirmed: true,
        gap_breakaway: true,
      };

      const patterns = extractTechnicalPatterns(snapshot);

      const ids = patterns.map((p) => p.id);
      expect(new Set(ids).size).toBe(ids.length);
    });
  });
});
