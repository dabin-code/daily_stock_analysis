export type DataHealthOperationType =
  | 'backfill_to_date'
  | 'repair_gaps'
  | 'rerun_audit'
  | 'retry_failed';

export type DataHealthTaskStatus = 'pending' | 'processing' | 'completed' | 'failed';

export interface DataHealthSummary {
  market: string;
  activeInstrumentCount: number;
  expectedUniverseCount: number;
  stExcludedCount: number;
  stockDataStartDate?: string | null;
  stockDataEndDate?: string | null;
  stockDataTradeDateCount?: number;
  latestTradeDate?: string | null;
  latestCompleteDate?: string | null;
  latestAuditPassedDate?: string | null;
  latestTradeDateSyncedCount: number;
  latestTradeDateCoverageRatio: number;
  openGapCount: number;
  pendingRetryGapCount: number;
  candidateSkipGapCount: number;
  screeningReady: boolean;
  screeningReadyDate?: string | null;
}

export interface DataHealthCoverageItem {
  tradeDate: string;
  syncedCount: number;
  expectedCount: number;
  coverageRatio: number;
  isComplete: boolean;
}

export interface DataHealthCoverage {
  market: string;
  expectedCount: number;
  items: DataHealthCoverageItem[];
  ma100ReadyCount: number;
  ma200ReadyCount: number;
}

export interface DataHealthGap {
  gapKey: string;
  sourceRunId: string;
  market: string;
  gapScope: string;
  code?: string | null;
  tradeDate?: string | null;
  missingDateFrom?: string | null;
  missingDateTo?: string | null;
  status: string;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface DataHealthGapListResponse {
  market: string;
  total: number;
  items: DataHealthGap[];
}

export interface DataHealthTask {
  taskId: string;
  operationType: DataHealthOperationType | string;
  market: string;
  status: DataHealthTaskStatus;
  progress: number;
  message?: string | null;
  result?: Record<string, unknown> | null;
  error?: string | null;
  createdAt: string;
  startedAt?: string | null;
  completedAt?: string | null;
}

export interface DataHealthTaskListResponse {
  total: number;
  items: DataHealthTask[];
}

export interface SubmitDataHealthOperationRequest {
  operationType: DataHealthOperationType;
  market?: 'cn';
  tradeDate?: string;
  stockCodes?: string[];
}
