import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useDataHealthStore } from '../dataHealthStore';

vi.mock('../../api/dataHealth', () => ({
  dataHealthApi: {
    getSummary: vi.fn(),
    getCoverage: vi.fn(),
    getGaps: vi.fn(),
    listTasks: vi.fn(),
    submitOperation: vi.fn(),
    getTask: vi.fn(),
  },
}));

const { dataHealthApi } = await import('../../api/dataHealth');

describe('dataHealthStore', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useDataHealthStore.getState().reset();
  });

  it('loads dashboard data from all read endpoints', async () => {
    vi.mocked(dataHealthApi.getSummary).mockResolvedValue({
      market: 'cn',
      expectedUniverseCount: 1,
      activeInstrumentCount: 1,
      stExcludedCount: 0,
      latestTradeDate: '2026-05-08',
      latestTradeDateSyncedCount: 1,
      latestTradeDateCoverageRatio: 1,
      openGapCount: 0,
      pendingRetryGapCount: 0,
      candidateSkipGapCount: 0,
      screeningReady: true,
      screeningReadyDate: '2026-05-08',
    });
    vi.mocked(dataHealthApi.getCoverage).mockResolvedValue({
      market: 'cn',
      expectedCount: 1,
      items: [{ tradeDate: '2026-05-08', syncedCount: 1, expectedCount: 1, coverageRatio: 1, isComplete: false }],
      ma100ReadyCount: 0,
      ma200ReadyCount: 0,
    });
    vi.mocked(dataHealthApi.getGaps).mockResolvedValue({ market: 'cn', total: 0, items: [] });
    vi.mocked(dataHealthApi.listTasks).mockResolvedValue({ total: 0, items: [] });

    await useDataHealthStore.getState().loadDashboard();

    expect(useDataHealthStore.getState().summary?.latestTradeDate).toBe('2026-05-08');
    expect(useDataHealthStore.getState().coverage?.items).toHaveLength(1);
    expect(useDataHealthStore.getState().gaps?.items).toEqual([]);
    expect(dataHealthApi.getGaps).toHaveBeenCalledWith({
      market: 'cn',
      status: 'unresolved',
      to: '2026-05-08',
      limit: 100,
    });
    expect(useDataHealthStore.getState().isLoading).toBe(false);
  });

  it('submits operation and stores latest task', async () => {
    vi.mocked(dataHealthApi.submitOperation).mockResolvedValue({
      taskId: 'task-1',
      operationType: 'repair_gaps',
      market: 'cn',
      status: 'pending',
      progress: 0,
      createdAt: '2026-05-08T10:00:00',
    });

    await useDataHealthStore.getState().submitOperation({ operationType: 'repair_gaps', market: 'cn' });

    expect(dataHealthApi.submitOperation).toHaveBeenCalledWith({ operationType: 'repair_gaps', market: 'cn' });
    expect(useDataHealthStore.getState().latestTask?.taskId).toBe('task-1');
    expect(useDataHealthStore.getState().isSubmitting).toBe(false);
  });
});
