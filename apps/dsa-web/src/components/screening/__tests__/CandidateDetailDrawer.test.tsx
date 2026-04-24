import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { CandidateDetailDrawer } from '../CandidateDetailDrawer';
import type { ScreeningCandidateDetail } from '../../../types/screening';

const mockCandidate: ScreeningCandidateDetail = {
  code: '600519',
  name: '贵州茅台',
  rank: 1,
  ruleScore: 85.5,
  selectedForAi: true,
  ruleHits: ['volume_surge', 'ma_crossover'],
  factorSnapshot: { close: 1800.5, volume_ratio: 2.3, pct_chg: 3.5 },
  matchedStrategies: ['volume_breakout', 'ma_golden_cross'],
  aiSummary: '该股近期放量突破，趋势向好',
  aiOperationAdvice: '建议逢低布局',
  finalScore: 92.0,
};

const mockStore = {
  selectedCandidate: null as ScreeningCandidateDetail | null,
  clearSelectedCandidate: vi.fn(),
};

vi.mock('../../../stores/screeningStore', () => ({
  useScreeningStore: () => mockStore,
}));

describe('CandidateDetailDrawer', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockStore.selectedCandidate = null;
  });

  it('renders nothing when no candidate selected', () => {
    const { container } = render(<CandidateDetailDrawer />);
    expect(container.querySelector('[data-testid="candidate-detail"]')).toBeNull();
  });

  it('renders candidate detail when selected', () => {
    mockStore.selectedCandidate = mockCandidate;
    render(<CandidateDetailDrawer />);
    expect(screen.getByTestId('candidate-detail')).toBeInTheDocument();
  });

  it('shows rank badge', () => {
    mockStore.selectedCandidate = mockCandidate;
    render(<CandidateDetailDrawer />);
    expect(screen.getByText('排名 #1')).toBeInTheDocument();
  });

  it('shows rule score badge', () => {
    mockStore.selectedCandidate = mockCandidate;
    render(<CandidateDetailDrawer />);
    expect(screen.getByText('规则评分: 85.5')).toBeInTheDocument();
  });

  it('shows rule hits', () => {
    mockStore.selectedCandidate = mockCandidate;
    render(<CandidateDetailDrawer />);
    expect(screen.getByText('volume_surge')).toBeInTheDocument();
    expect(screen.getByText('ma_crossover')).toBeInTheDocument();
  });

  it('shows factor snapshot', () => {
    mockStore.selectedCandidate = mockCandidate;
    render(<CandidateDetailDrawer />);
    expect(screen.getByText('close')).toBeInTheDocument();
    expect(screen.getByText('1800.50')).toBeInTheDocument();
  });

  it('shows AI summary', () => {
    mockStore.selectedCandidate = mockCandidate;
    render(<CandidateDetailDrawer />);
    expect(screen.getByText('该股近期放量突破，趋势向好')).toBeInTheDocument();
  });

  it('shows AI operation advice', () => {
    mockStore.selectedCandidate = mockCandidate;
    render(<CandidateDetailDrawer />);
    expect(screen.getByText('建议逢低布局')).toBeInTheDocument();
  });

  it('shows matched strategies for five-layer candidates', () => {
    mockStore.selectedCandidate = {
      ...mockCandidate,
      tradeStage: 'probe_entry',
      marketRegime: 'balanced',
      matchedStrategies: ['volume_breakout', 'ma_golden_cross'],
    };
    render(<CandidateDetailDrawer />);
    expect(screen.getByText('volume_breakout')).toBeInTheDocument();
    expect(screen.getByText('ma_golden_cross')).toBeInTheDocument();
  });

  it('shows catalyst summary and hot theme news', () => {
    mockStore.selectedCandidate = {
      ...mockCandidate,
      factorSnapshot: {
        ...mockCandidate.factorSnapshot,
        is_hot_theme_stock: true,
        primary_theme: 'AI芯片',
        theme_catalyst_summary: 'AI 芯片板块受政策催化快速升温',
        theme_catalyst_news: [
          {
            title: '政策发布',
            source: '新华社',
            summary: '支持国产 AI 芯片发展',
            url: 'https://example.com/news',
          },
        ],
      },
    };

    render(<CandidateDetailDrawer />);
    expect(screen.getByText('催化摘要')).toBeInTheDocument();
    expect(screen.getAllByText('AI 芯片板块受政策催化快速升温').length).toBeGreaterThan(0);
    expect(screen.getByText('热点新闻')).toBeInTheDocument();
    expect(screen.getByText('政策发布')).toBeInTheDocument();
    expect(screen.getByText('支持国产 AI 芯片发展')).toBeInTheDocument();
  });

  it('shows readable stage explanations for named phase keys', () => {
    mockStore.selectedCandidate = {
      ...mockCandidate,
      factorSnapshot: {
        ...mockCandidate.factorSnapshot,
        phase_results: {
          phase1_market_and_theme: true,
          phase2_leader_screen: true,
          phase3_core_signal: true,
          phase4_entry_readiness: false,
          phase5_risk_controls: true,
        },
        leader_score: 68,
        core_signal: '跳空涨停',
        risk_params: {
          stop_loss: 9.8,
          position_size: '轻仓试错',
        },
      },
    };

    render(<CandidateDetailDrawer />);
    expect(screen.getByText('阶段1: 市场与题材')).toBeInTheDocument();
    expect(screen.getByText('阶段2: 龙头筛选')).toBeInTheDocument();
    expect(screen.getByText('阶段3: 核心信号')).toBeInTheDocument();
    expect(screen.getByText('阶段4: 入场准备')).toBeInTheDocument();
    expect(screen.getByText('阶段5: 风险控制')).toBeInTheDocument();
    expect(screen.getByText(/龙头评分: 68/)).toBeInTheDocument();
    expect(screen.getByText(/止损: 9.80 \| 仓位: 轻仓试错/)).toBeInTheDocument();
  });

  it('prefers backend phase explanations when provided', () => {
    mockStore.selectedCandidate = {
      ...mockCandidate,
      factorSnapshot: {
        ...mockCandidate.factorSnapshot,
        phase_results: {
          phase1_market_and_theme: true,
          phase2_leader_screen: true,
          phase3_core_signal: false,
          phase4_entry_readiness: false,
          phase5_risk_controls: true,
        },
        phase_explanations: [
          { phase_key: 'phase1_market_and_theme', label: '阶段1: 市场与题材', hit: true, summary: '热点题材已锁定' },
          { phase_key: 'phase2_leader_screen', label: '阶段2: 龙头筛选', hit: true, summary: 'leader_score=68' },
          { phase_key: 'phase3_core_signal', label: '阶段3: 核心信号', hit: false, summary: '缺少跳空涨停共振' },
          { phase_key: 'phase4_entry_readiness', label: '阶段4: 入场准备', hit: false, summary: '等待回踩支撑确认' },
          { phase_key: 'phase5_risk_controls', label: '阶段5: 风险控制', hit: true, summary: '止损位=9.80, 轻仓试错' },
        ],
      },
    };

    render(<CandidateDetailDrawer />);
    expect(screen.getByText('热点题材已锁定')).toBeInTheDocument();
    expect(screen.getByText('leader_score=68')).toBeInTheDocument();
    expect(screen.getByText('缺少跳空涨停共振')).toBeInTheDocument();
    expect(screen.getByText('等待回踩支撑确认')).toBeInTheDocument();
    expect(screen.getByText('止损位=9.80, 轻仓试错')).toBeInTheDocument();
  });

  it('shows readable values for object and array fields in factor snapshot', () => {
    mockStore.selectedCandidate = {
      ...mockCandidate,
      factorSnapshot: {
        ...mockCandidate.factorSnapshot,
        phase_results: {
          phase1_market_and_theme: true,
        },
        phase_explanations: [
          {
            phase_key: 'phase1_market_and_theme',
            label: '阶段1: 市场与题材',
            hit: true,
            summary: '热点题材已确认',
          },
        ],
        risk_params: {
          stop_loss: 9.8,
          position_size: '轻仓试错',
        },
      },
    };

    render(<CandidateDetailDrawer />);
    expect(screen.queryByText('[object Object]')).not.toBeInTheDocument();
    expect(screen.getByText(/phase1_market_and_theme: true/)).toBeInTheDocument();
    expect(screen.getAllByText(/热点题材已确认/).length).toBeGreaterThan(0);
    expect(screen.getByText(/stop_loss: 9.8/)).toBeInTheDocument();
  });

  it('shows technical pattern cards for bottom divergence', () => {
    mockStore.selectedCandidate = {
      ...mockCandidate,
      ruleHits: ['bottom_divergence_double_breakout:==:True'],
      factorSnapshot: {
        ...mockCandidate.factorSnapshot,
        bottom_divergence_double_breakout: true,
        bottom_divergence_pattern_label: '价格持平-MACD抬升',
        bottom_divergence_signal_strength: 0.59,
        bottom_divergence_entry_price: 8.11,
        bottom_divergence_stop_loss: 7.66,
        bottom_divergence_hit_reasons: [
          '【底背离形态】价格持平-MACD抬升',
          '【前置跌幅】从高点跌幅 26.6%',
          '【双突破同步】水平阻力线与下降趋势线同步突破',
        ],
      },
    };

    render(<CandidateDetailDrawer />);

    expect(screen.getByText('技术形态命中')).toBeInTheDocument();
    expect(screen.getByText('底背离双突破')).toBeInTheDocument();
    expect(screen.queryAllByText(/价格持平-MACD抬升/).length).toBeGreaterThan(0);
  });

  it('shows merged technical hit reasons for extreme strength combo', () => {
    mockStore.selectedCandidate = {
      ...mockCandidate,
      ruleHits: [
        'strategy:extreme_strength_combo',
        'is_hot_theme_stock:==:True',
        'above_ma100:==:True',
        'pattern_123_low_trendline:==:True',
        'gap_breakaway:==:True',
        'is_limit_up:==:True',
      ],
      matchedStrategies: ['extreme_strength_combo'],
      factorSnapshot: {
        ...mockCandidate.factorSnapshot,
        bottom_divergence_double_breakout: true,
        bottom_divergence_pattern_label: '价格持平-MACD抬升',
        bottom_divergence_hit_reasons: [
          '【底背离形态】价格持平-MACD抬升',
          '【前置跌幅】从高点跌幅 26.6%',
          '【双突破同步】水平阻力线与下降趋势线同步突破',
        ],
        gap_breakaway: true,
        is_limit_up: true,
        above_ma100: true,
        pattern_123_low_trendline: true,
      },
    };

    render(<CandidateDetailDrawer />);

    expect(screen.getByText('技术形态命中')).toBeInTheDocument();
    expect(screen.getByText('底背离双突破')).toBeInTheDocument();
    expect(screen.getByText('跳空突破')).toBeInTheDocument();
    expect(screen.getByText('涨停')).toBeInTheDocument();
    expect(screen.queryAllByText(/【底背离形态】价格持平-MACD抬升/).length).toBeGreaterThan(0);
  });

  it('shows layered extreme strength scores and stage/signal kind in legacy hot-theme card', () => {
    mockStore.selectedCandidate = {
      ...mockCandidate,
      factorSnapshot: {
        ...mockCandidate.factorSnapshot,
        is_hot_theme_stock: true,
        primary_theme: 'AI芯片',
        extreme_strength_score: 78.5,
        theme_pool_score: 40.0,
        leadership_score: 30.0,
        entry_signal_score: 8.5,
        timing_penalty: -6.0,
        timing_reasons: ['距离突破已过 4 根K线', 'MA100 距离 18% 偏远'],
        stage_label: 'extended_do_not_chase',
        primary_signal: '低位123结构',
        signal_kind: 'structure_low_entry',
        all_signals: [
          { name: '低位123结构', kind: 'structure_low_entry', score: 8.0 },
          { name: '涨停', kind: 'momentum_chase', score: 6.0 },
        ],
      },
    };

    render(<CandidateDetailDrawer />);

    expect(screen.getByText('· 题材池分')).toBeInTheDocument();
    expect(screen.getByText('40.0')).toBeInTheDocument();
    expect(screen.getByText('· 龙头分')).toBeInTheDocument();
    expect(screen.getByText('30.0')).toBeInTheDocument();
    expect(screen.getByText('· 入场信号分')).toBeInTheDocument();
    expect(screen.getByText('8.5')).toBeInTheDocument();
    expect(screen.getByText('· 时机惩罚')).toBeInTheDocument();
    expect(screen.getByText('-6.0')).toBeInTheDocument();

    // stage_label 与 signal_kind 徽章都应展示文字
    expect(screen.getAllByText('已走远·勿追').length).toBeGreaterThan(0);
    expect(screen.getAllByText('低位结构入场').length).toBeGreaterThan(0);
    expect(screen.getAllByText('低位123结构').length).toBeGreaterThan(0);
  });

  it('phase 3 prefers primary_signal over core_signal in legacy layout', () => {
    mockStore.selectedCandidate = {
      ...mockCandidate,
      factorSnapshot: {
        ...mockCandidate.factorSnapshot,
        phase_results: {
          phase1_market_and_theme: true,
          phase2_leader_screen: true,
          phase3_core_signal: true,
          phase4_entry_readiness: true,
          phase5_risk_controls: true,
        },
        leader_score: 70,
        core_signal: '跳空涨停',
        primary_signal: '低位123结构',
        signal_kind: 'structure_low_entry',
        risk_params: { stop_loss: 10.2, position_size: '轻仓' },
      },
    };

    render(<CandidateDetailDrawer />);
    // 新契约：primary_signal 覆盖 core_signal
    expect(screen.getAllByText('低位123结构').length).toBeGreaterThan(0);
  });

  it('shows low123 watchlist pattern in detail drawer', () => {
    mockStore.selectedCandidate = {
      ...mockCandidate,
      factorSnapshot: {
        ...mockCandidate.factorSnapshot,
        ma100_low123_watchlist: true,
        ma100_low123_pattern_strength: 0.63,
        ma100_low123_ma_score: 0.71,
        ma100_low123_watch_hit_reasons: [
          '【观察池】最新收盘价已大于P3但尚未突破P2，纳入重点观察',
        ],
      },
    };

    render(<CandidateDetailDrawer />);

    expect(screen.getByText('MA100+低位123观察池')).toBeInTheDocument();
    expect(screen.getAllByText('【观察池】最新收盘价已大于P3但尚未突破P2，纳入重点观察').length).toBeGreaterThan(0);
  });
});
