import { create } from 'zustand';
import { dataHealthApi } from '../api/dataHealth';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import type {
  DataHealthCoverage,
  DataHealthGapListResponse,
  DataHealthSummary,
  DataHealthTask,
  SubmitDataHealthOperationRequest,
} from '../types/dataHealth';

interface DataHealthState {
  summary: DataHealthSummary | null;
  coverage: DataHealthCoverage | null;
  gaps: DataHealthGapListResponse | null;
  tasks: DataHealthTask[];
  latestTask: DataHealthTask | null;
  targetDate: string;
  isLoading: boolean;
  isSubmitting: boolean;
  pollingTimer: ReturnType<typeof setInterval> | null;
  error: ParsedApiError | null;
  setTargetDate: (value: string) => void;
  loadDashboard: () => Promise<void>;
  submitOperation: (request: SubmitDataHealthOperationRequest) => Promise<void>;
  pollTask: (taskId: string) => void;
  stopPolling: () => void;
  reset: () => void;
}

const isTerminalTaskStatus = (status: string): boolean => status === 'completed' || status === 'failed';

const initialState = {
  summary: null,
  coverage: null,
  gaps: null,
  tasks: [],
  latestTask: null,
  targetDate: '',
  isLoading: false,
  isSubmitting: false,
  pollingTimer: null,
  error: null,
};

export const useDataHealthStore = create<DataHealthState>((set, get) => ({
  ...initialState,

  setTargetDate: (value) => set({ targetDate: value }),

  loadDashboard: async () => {
    set({ isLoading: true, error: null });
    try {
      const summary = await dataHealthApi.getSummary('cn');
      const gapToDate = get().targetDate || summary.screeningReadyDate || summary.latestCompleteDate || summary.latestTradeDate || undefined;
      const [coverage, gaps, taskList] = await Promise.all([
        dataHealthApi.getCoverage({ market: 'cn' }),
        dataHealthApi.getGaps({ market: 'cn', status: 'unresolved', to: gapToDate, limit: 100 }),
        dataHealthApi.listTasks(20),
      ]);
      set({
        summary,
        coverage,
        gaps,
        tasks: taskList.items,
        latestTask: taskList.items[0] ?? get().latestTask,
        isLoading: false,
      });
    } catch (err) {
      set({ isLoading: false, error: getParsedApiError(err) });
    }
  },

  submitOperation: async (request) => {
    set({ isSubmitting: true, error: null });
    try {
      const task = await dataHealthApi.submitOperation({ market: 'cn', ...request });
      set({ latestTask: task, isSubmitting: false });
      if (!isTerminalTaskStatus(task.status)) {
        get().pollTask(task.taskId);
      } else {
        await get().loadDashboard();
      }
    } catch (err) {
      set({ isSubmitting: false, error: getParsedApiError(err) });
    }
  },

  pollTask: (taskId) => {
    get().stopPolling();
    let pollCount = 0;
    const timer = setInterval(async () => {
      pollCount += 1;
      if (pollCount > 240) {
        get().stopPolling();
        return;
      }
      try {
        const task = await dataHealthApi.getTask(taskId);
        set({ latestTask: task });
        if (isTerminalTaskStatus(task.status)) {
          get().stopPolling();
          await get().loadDashboard();
        }
      } catch (err) {
        get().stopPolling();
        set({ error: getParsedApiError(err) });
      }
    }, 5000);
    set({ pollingTimer: timer });
  },

  stopPolling: () => {
    const { pollingTimer } = get();
    if (pollingTimer) {
      clearInterval(pollingTimer);
      set({ pollingTimer: null });
    }
  },

  reset: () => {
    get().stopPolling();
    set(initialState);
  },
}));
