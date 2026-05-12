import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import DataHealthPage from '../DataHealthPage';

const store = {
  summary: {
    market: 'cn',
    activeInstrumentCount: 1,
    expectedUniverseCount: 1,
    stExcludedCount: 0,
    stockDataStartDate: '2025-06-03',
    stockDataEndDate: '2026-05-08',
    stockDataTradeDateCount: 180,
    latestTradeDate: '2026-05-08',
    latestCompleteDate: '2026-05-08',
    latestAuditPassedDate: '2026-05-08',
    latestTradeDateSyncedCount: 1,
    latestTradeDateCoverageRatio: 1,
    openGapCount: 0,
    pendingRetryGapCount: 0,
    candidateSkipGapCount: 0,
    screeningReady: true,
    screeningReadyDate: '2026-05-08',
  },
  coverage: {
    market: 'cn',
    expectedCount: 1,
    items: Array.from({ length: 13 }, (_, index) => ({
      tradeDate: `2026-05-${String(index + 1).padStart(2, '0')}`,
      syncedCount: 1,
      expectedCount: 1,
      coverageRatio: 1,
      isComplete: false,
    })),
    ma100ReadyCount: 0,
    ma200ReadyCount: 0,
  },
  gaps: { market: 'cn', total: 0, items: [] },
  tasks: [],
  latestTask: null,
  targetDate: '',
  isLoading: false,
  isSubmitting: false,
  error: null,
  setTargetDate: vi.fn(),
  loadDashboard: vi.fn(),
  submitOperation: vi.fn(),
};

vi.mock('../../stores/dataHealthStore', () => {
  return {
    useDataHealthStore: (selector?: (state: typeof store) => unknown) =>
      selector ? selector(store) : store,
  };
});

describe('DataHealthPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders data health dashboard and operation buttons', () => {
    render(<DataHealthPage />);

    expect(screen.getByText('本地股票数据健康')).toBeInTheDocument();
    expect(screen.getByText('K线覆盖范围')).toBeInTheDocument();
    expect(screen.getByText('2025-06-03 ~ 2026-05-08')).toBeInTheDocument();
    expect(screen.getByText('回填到目标日')).toBeInTheDocument();
    expect(screen.getByText('重试失败股票')).toBeInTheDocument();
    expect(screen.getByText('修复缺口')).toBeInTheDocument();
    expect(screen.getByText('重新审计')).toBeInTheDocument();
  });

  it('loads dashboard on mount', () => {
    render(<DataHealthPage />);

    expect(store.loadDashboard).toHaveBeenCalledTimes(1);
  });

  it('renders full coverage list before gap details', () => {
    render(<DataHealthPage />);

    expect(screen.getByText('2026-05-01')).toBeInTheDocument();
    const coverageTitle = screen.getByText('覆盖率趋势');
    const gapsTitle = screen.getByText('缺口明细');
    expect(
      coverageTitle.compareDocumentPosition(gapsTitle) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
