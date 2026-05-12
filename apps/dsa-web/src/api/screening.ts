import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  CreateScreeningRunRequest,
  ScreeningBackfillToDateRequest,
  ScreeningBackfillToDateResponse,
  ScreeningCandidateDetail,
  ScreeningCandidateListResponse,
  ScreeningNotifyRequest,
  ScreeningRun,
  ScreeningRunListResponse,
  ScreeningStrategyListResponse,
} from '../types/screening';

export const screeningApi = {
  getStrategies: async (): Promise<ScreeningStrategyListResponse> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/screening/strategies');
    return toCamelCase<ScreeningStrategyListResponse>(response.data);
  },

  createRun: async (params: CreateScreeningRunRequest): Promise<ScreeningRun> => {
    const requestData = {
      trade_date: params.tradeDate,
      stock_codes: params.stockCodes,
      mode: params.mode,
      candidate_limit: params.candidateLimit,
      ai_top_k: params.aiTopK,
      strategies: params.strategies,
      rerun_failed: params.rerunFailed,
      resume_from: params.resumeFrom,
      market: params.market || 'cn',
    };
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/screening/runs', requestData);
    return toCamelCase<ScreeningRun>(response.data);
  },

  backfillToDate: async (params: ScreeningBackfillToDateRequest): Promise<ScreeningBackfillToDateResponse> => {
    const requestData = {
      trade_date: params.tradeDate,
      market: params.market || 'cn',
    };
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/screening/backfill-to-date', requestData, {
      timeout: 15 * 60 * 1000,
    });
    return toCamelCase<ScreeningBackfillToDateResponse>(response.data);
  },

  listRuns: async (limit = 20): Promise<ScreeningRunListResponse> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/screening/runs', {
      params: { limit },
    });
    return toCamelCase<ScreeningRunListResponse>(response.data);
  },

  getRun: async (runId: string): Promise<ScreeningRun> => {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/screening/runs/${runId}`);
    return toCamelCase<ScreeningRun>(response.data);
  },

  getCandidates: async (
    runId: string,
    limit = 100,
    withAiOnly = false,
  ): Promise<ScreeningCandidateListResponse> => {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/screening/runs/${runId}/candidates`,
      { params: { limit, with_ai_only: withAiOnly } },
    );
    return toCamelCase<ScreeningCandidateListResponse>(response.data);
  },

  getCandidateDetail: async (runId: string, code: string): Promise<ScreeningCandidateDetail> => {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/screening/runs/${runId}/candidates/${code}`,
    );
    return toCamelCase<ScreeningCandidateDetail>(response.data);
  },

  notifyRun: async (runId: string, params?: ScreeningNotifyRequest): Promise<{ success: boolean; message: string }> => {
    const requestData = params
      ? { limit: params.limit, with_ai_only: params.withAiOnly, force: params.force }
      : {};
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/screening/runs/${runId}/notify`,
      requestData,
    );
    return toCamelCase<{ success: boolean; message: string }>(response.data);
  },

  clearRuns: async (): Promise<{ success: boolean; message: string }> => {
    const response = await apiClient.delete<Record<string, unknown>>('/api/v1/screening/runs');
    return toCamelCase<{ success: boolean; message: string }>(response.data);
  },

  deleteRun: async (runId: string): Promise<{ success: boolean; message: string }> => {
    const response = await apiClient.delete<Record<string, unknown>>(`/api/v1/screening/runs/${runId}`);
    return toCamelCase<{ success: boolean; message: string }>(response.data);
  },
};
