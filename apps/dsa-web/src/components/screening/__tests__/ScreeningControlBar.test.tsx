import { render, screen, fireEvent } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ScreeningControlBar } from '../ScreeningControlBar';

const mockStore = {
  mode: 'balanced' as const,
  setMode: vi.fn(),
  tradeDate: '2026-03-18',
  setTradeDate: vi.fn(),
  candidateLimit: 5,
  setCandidateLimit: vi.fn(),
  aiTopK: 2,
  setAiTopK: vi.fn(),
  isRunning: false,
  blockingDialog: null as null | { title: string; message: string },
  clearBlockingDialog: vi.fn(),
  startScreening: vi.fn().mockResolvedValue(undefined),
  backfillStocksToTradeDate: vi.fn().mockResolvedValue(undefined),
  isBackfilling: false,
  reset: vi.fn(),
};

vi.mock('../../../stores/screeningStore', () => ({
  useScreeningStore: () => mockStore,
}));

describe('ScreeningControlBar', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockStore.isRunning = false;
    mockStore.isBackfilling = false;
    mockStore.blockingDialog = null;
  });

  it('shows start button that is always enabled', () => {
    render(<ScreeningControlBar />);
    const btn = screen.getByRole('button', { name: /开始筛选/ });
    expect(btn).toBeInTheDocument();
    expect(btn).not.toBeDisabled();
  });

  it('shows reset button', () => {
    render(<ScreeningControlBar />);
    expect(screen.getByRole('button', { name: /重置/ })).toBeInTheDocument();
  });

  it('shows backfill button and triggers backfill to selected date', () => {
    render(<ScreeningControlBar />);

    fireEvent.click(screen.getByRole('button', { name: /回填至该日/ }));

    expect(mockStore.backfillStocksToTradeDate).toHaveBeenCalledTimes(1);
  });

  it('shows loading state when running', () => {
    mockStore.isRunning = true;
    render(<ScreeningControlBar />);
    expect(screen.getByRole('button', { name: /筛选中/ })).toBeDisabled();
  });

  it('disables backfill button while backfilling', () => {
    mockStore.isBackfilling = true;
    render(<ScreeningControlBar />);
    expect(screen.getByRole('button', { name: /回填中/ })).toBeDisabled();
  });

  it('disables start and reset buttons while backfilling', () => {
    mockStore.isBackfilling = true;
    render(<ScreeningControlBar />);
    expect(screen.getByRole('button', { name: /开始筛选/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /重置/ })).toBeDisabled();
  });

  it('renders date input', () => {
    render(<ScreeningControlBar />);
    expect(screen.getByLabelText('交易日')).toBeInTheDocument();
  });

  it('shows advanced settings when toggled', () => {
    render(<ScreeningControlBar />);
    const advBtn = screen.getByText('高级');
    fireEvent.click(advBtn);
    expect(document.getElementById('candidate-limit')).toHaveValue(5);
    expect(document.getElementById('ai-top-k')).toHaveValue(2);
  });

  it('hides advanced settings by default', () => {
    render(<ScreeningControlBar />);
    expect(document.getElementById('candidate-limit')).toBeNull();
  });

  it('renders blocking dialog when present', () => {
    mockStore.blockingDialog = {
      title: '今日数据未就绪',
      message: '当前时间未到 15:00',
    };
    render(<ScreeningControlBar />);
    expect(screen.getByText('今日数据未就绪')).toBeInTheDocument();
    expect(screen.getByText('当前时间未到 15:00')).toBeInTheDocument();
  });
});
