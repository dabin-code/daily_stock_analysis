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
