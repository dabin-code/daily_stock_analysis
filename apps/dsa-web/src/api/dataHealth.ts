import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  DataHealthCoverage,
  DataHealthGapListResponse,
  DataHealthSummary,
  DataHealthTask,
  DataHealthTaskListResponse,
  SubmitDataHealthOperationRequest,
} from '../types/dataHealth';

export const dataHealthApi = {
  getSummary: async (market = 'cn'): Promise<DataHealthSummary> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/data-health/summary', {
      params: { market },
    });
    return toCamelCase<DataHealthSummary>(response.data);
  },

  getCoverage: async (params?: {
    market?: string;
    from?: string;
    to?: string;
  }): Promise<DataHealthCoverage> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/data-health/coverage', {
      params: {
        market: params?.market || 'cn',
        from: params?.from,
        to: params?.to,
      },
    });
    return toCamelCase<DataHealthCoverage>(response.data);
  },

  getGaps: async (params?: {
    market?: string;
    status?: string;
    from?: string;
    to?: string;
    limit?: number;
  }): Promise<DataHealthGapListResponse> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/data-health/gaps', {
      params: {
        market: params?.market || 'cn',
        status: params?.status,
        from: params?.from,
        to: params?.to,
        limit: params?.limit ?? 100,
      },
    });
    return toCamelCase<DataHealthGapListResponse>(response.data);
  },

  submitOperation: async (params: SubmitDataHealthOperationRequest): Promise<DataHealthTask> => {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/data-health/operations', {
      operation_type: params.operationType,
      market: params.market || 'cn',
      trade_date: params.tradeDate,
      stock_codes: params.stockCodes,
    });
    return toCamelCase<DataHealthTask>(response.data);
  },

  getTask: async (taskId: string): Promise<DataHealthTask> => {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/data-health/tasks/${taskId}`);
    return toCamelCase<DataHealthTask>(response.data);
  },

  listTasks: async (limit = 20): Promise<DataHealthTaskListResponse> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/data-health/tasks', {
      params: { limit },
    });
    return toCamelCase<DataHealthTaskListResponse>(response.data);
  },
};
