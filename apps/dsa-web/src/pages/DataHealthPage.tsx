import type React from 'react';
import { useEffect } from 'react';
import { Activity, Database, RefreshCw, RotateCcw, ShieldCheck, Wrench } from 'lucide-react';
import { AppPage, Badge, Button, Input, PageHeader, SectionCard, StatCard } from '../components/common';
import { useDataHealthStore } from '../stores/dataHealthStore';
import type { DataHealthOperationType } from '../types/dataHealth';

const formatPercent = (value?: number | null): string => {
  if (value == null) {
    return '-';
  }
  return `${(value * 100).toFixed(1)}%`;
};

const taskStatusVariant = (status?: string): 'default' | 'success' | 'warning' | 'danger' | 'info' => {
  if (status === 'completed') {
    return 'success';
  }
  if (status === 'failed') {
    return 'danger';
  }
  if (status === 'processing') {
    return 'info';
  }
  if (status === 'pending') {
    return 'warning';
  }
  return 'default';
};

const operationLabels: Record<DataHealthOperationType, string> = {
  backfill_to_date: '回填到目标日',
  retry_failed: '重试失败股票',
  repair_gaps: '修复缺口',
  rerun_audit: '重新审计',
};

const DataHealthPage: React.FC = () => {
  const summary = useDataHealthStore((s) => s.summary);
  const coverage = useDataHealthStore((s) => s.coverage);
  const gaps = useDataHealthStore((s) => s.gaps);
  const tasks = useDataHealthStore((s) => s.tasks);
  const latestTask = useDataHealthStore((s) => s.latestTask);
  const targetDate = useDataHealthStore((s) => s.targetDate);
  const isLoading = useDataHealthStore((s) => s.isLoading);
  const isSubmitting = useDataHealthStore((s) => s.isSubmitting);
  const error = useDataHealthStore((s) => s.error);
  const setTargetDate = useDataHealthStore((s) => s.setTargetDate);
  const loadDashboard = useDataHealthStore((s) => s.loadDashboard);
  const submitOperation = useDataHealthStore((s) => s.submitOperation);

  useEffect(() => {
    void loadDashboard();
  }, [loadDashboard]);

  const effectiveTargetDate = targetDate || summary?.latestTradeDate || '';
  const runOperation = (operationType: DataHealthOperationType) => {
    const needsDate = operationType === 'backfill_to_date' || operationType === 'rerun_audit';
    void submitOperation({
      operationType,
      market: 'cn',
      tradeDate: needsDate ? effectiveTargetDate : undefined,
    });
  };
  const unresolvedGapCount = (
    (summary?.openGapCount ?? 0)
    + (summary?.pendingRetryGapCount ?? 0)
    + (summary?.candidateSkipGapCount ?? 0)
  );
  const visibleGapCount = gaps?.total ?? unresolvedGapCount;

  return (
    <AppPage>
      <div className="flex flex-col gap-5">
        <PageHeader
          eyebrow="DATA HEALTH"
          title="本地股票数据健康"
          description="检查本地 K 线覆盖、审计缺口与选股可用性，并提交回填、重试、修复和重新审计任务"
          actions={(
            <Button variant="secondary" onClick={() => void loadDashboard()} isLoading={isLoading}>
              <RefreshCw className="h-4 w-4" />
              刷新
            </Button>
          )}
        />

        {error ? (
          <SectionCard title="加载失败">
            <p className="text-sm text-danger">{error.message}</p>
          </SectionCard>
        ) : null}

        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <StatCard
            label="K线覆盖范围"
            value={
              summary?.stockDataStartDate && summary?.stockDataEndDate
                ? `${summary.stockDataStartDate} ~ ${summary.stockDataEndDate}`
                : '-'
            }
            hint={`本地库覆盖 ${summary?.stockDataTradeDateCount ?? 0} 个交易日，形态选股需足够长的历史 K 线`}
            icon={<Database className="h-5 w-5" />}
            tone="primary"
            className="md:col-span-2 xl:col-span-2"
          />
          <StatCard
            label="可选股交易日"
            value={summary?.screeningReadyDate || '-'}
            hint={summary?.screeningReady ? '本地数据满足选股前置条件' : '等待同步与审计通过'}
            icon={<ShieldCheck className="h-5 w-5" />}
            tone={summary?.screeningReady ? 'success' : 'warning'}
          />
          <StatCard
            label="最新覆盖率"
            value={formatPercent(summary?.latestTradeDateCoverageRatio)}
            hint={`${summary?.latestTradeDateSyncedCount ?? 0}/${summary?.expectedUniverseCount ?? 0} 只已同步`}
            icon={<Activity className="h-5 w-5" />}
            tone="primary"
          />
          <StatCard
            label="股票池口径"
            value={summary?.expectedUniverseCount ?? '-'}
            hint={`active ${summary?.activeInstrumentCount ?? 0}，排除 ST ${summary?.stExcludedCount ?? 0}`}
            icon={<Database className="h-5 w-5" />}
          />
          <StatCard
            label="未关闭缺口"
            value={visibleGapCount}
            hint={`当前表格口径；全库 open ${summary?.openGapCount ?? 0}，pending ${summary?.pendingRetryGapCount ?? 0}，candidate ${summary?.candidateSkipGapCount ?? 0}`}
            icon={<Wrench className="h-5 w-5" />}
            tone={visibleGapCount > 0 ? 'danger' : 'default'}
          />
        </div>

        <SectionCard
          title="数据操作"
          subtitle="OPERATIONS"
          actions={latestTask ? <Badge variant={taskStatusVariant(latestTask.status)}>最新任务：{latestTask.status}</Badge> : null}
        >
          <div className="grid gap-4 lg:grid-cols-[minmax(220px,320px)_1fr]">
            <Input
              type="date"
              label="目标交易日"
              value={effectiveTargetDate}
              onChange={(event) => setTargetDate(event.target.value)}
              hint="回填、重试和重新审计会使用该日期"
            />
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <Button
                variant="primary"
                disabled={!effectiveTargetDate}
                isLoading={isSubmitting}
                onClick={() => runOperation('backfill_to_date')}
              >
                <Database className="h-4 w-4" />
                {operationLabels.backfill_to_date}
              </Button>
              <Button
                variant="secondary"
                isLoading={isSubmitting}
                onClick={() => runOperation('retry_failed')}
              >
                <RotateCcw className="h-4 w-4" />
                {operationLabels.retry_failed}
              </Button>
              <Button
                variant="secondary"
                isLoading={isSubmitting}
                onClick={() => runOperation('repair_gaps')}
              >
                <Wrench className="h-4 w-4" />
                {operationLabels.repair_gaps}
              </Button>
              <Button
                variant="outline"
                disabled={!effectiveTargetDate}
                isLoading={isSubmitting}
                onClick={() => runOperation('rerun_audit')}
              >
                <ShieldCheck className="h-4 w-4" />
                {operationLabels.rerun_audit}
              </Button>
            </div>
          </div>
          {latestTask ? (
            <div className="mt-4 rounded-2xl border border-border/60 bg-elevated/40 p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="text-sm font-medium text-foreground">{latestTask.operationType}</p>
                  <p className="mt-1 text-xs text-secondary-text">{latestTask.message || latestTask.taskId}</p>
                </div>
                <Badge variant={taskStatusVariant(latestTask.status)}>{latestTask.progress}%</Badge>
              </div>
              {latestTask.error ? <p className="mt-3 text-sm text-danger">{latestTask.error}</p> : null}
            </div>
          ) : null}
        </SectionCard>

        <SectionCard
          title="覆盖率趋势"
          subtitle="COVERAGE"
          actions={<Badge variant="info">最多展示最近 250 个交易日</Badge>}
        >
          <div className="mb-3 grid gap-3 sm:grid-cols-2">
            <StatCard
              label="满足 MA100"
              value={coverage?.ma100ReadyCount ?? 0}
              hint="至少 100 根 K 线，可覆盖中期均线形态"
            />
            <StatCard
              label="满足 MA200"
              value={coverage?.ma200ReadyCount ?? 0}
              hint="至少 200 根 K 线，可覆盖更长周期形态"
            />
          </div>
          <div className="max-h-[520px] overflow-auto">
            <table className="w-full min-w-[520px] text-left text-sm">
              <thead className="sticky top-0 bg-card text-xs uppercase tracking-[0.18em] text-secondary-text">
                <tr>
                  <th className="py-2">交易日</th>
                  <th className="py-2">已同步</th>
                  <th className="py-2">覆盖率</th>
                  <th className="py-2">状态</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/50">
                {(coverage?.items || []).map((item) => (
                  <tr key={item.tradeDate}>
                    <td className="py-2 text-foreground">{item.tradeDate}</td>
                    <td className="py-2 text-secondary-text">{item.syncedCount}/{item.expectedCount}</td>
                    <td className="py-2 text-secondary-text">{formatPercent(item.coverageRatio)}</td>
                    <td className="py-2">
                      <Badge variant={item.isComplete ? 'success' : 'warning'}>
                        {item.isComplete ? '完整' : '未满'}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-6 border-t border-border/60 pt-5">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
              <div>
                <span className="label-uppercase">GAPS</span>
                <h3 className="mt-1 text-base font-semibold text-foreground">缺口明细</h3>
              </div>
              <Badge variant={(gaps?.total ?? 0) > 0 ? 'warning' : 'success'}>
                {gaps?.total ?? 0} 个未关闭缺口
              </Badge>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[620px] text-left text-sm">
                <thead className="text-xs uppercase tracking-[0.18em] text-secondary-text">
                  <tr>
                    <th className="py-2">股票/日期</th>
                    <th className="py-2">范围</th>
                    <th className="py-2">类型</th>
                    <th className="py-2">状态</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/50">
                  {(gaps?.items || []).slice(0, 20).map((gap) => (
                    <tr key={gap.gapKey}>
                      <td className="py-2 text-foreground">{gap.code || gap.tradeDate || '-'}</td>
                      <td className="py-2 text-secondary-text">
                        {gap.missingDateFrom || gap.tradeDate || '-'} ~ {gap.missingDateTo || gap.tradeDate || '-'}
                      </td>
                      <td className="py-2 text-secondary-text">{gap.gapScope}</td>
                      <td className="py-2"><Badge variant="warning">{gap.status}</Badge></td>
                    </tr>
                  ))}
                  {gaps?.items.length === 0 ? (
                    <tr>
                      <td className="py-6 text-center text-secondary-text" colSpan={4}>暂无缺口</td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          </div>
        </SectionCard>

        <SectionCard title="最近任务" subtitle="TASKS">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {tasks.slice(0, 6).map((task) => (
              <div key={task.taskId} className="rounded-2xl border border-border/60 bg-card/70 p-4">
                <div className="flex items-center justify-between gap-3">
                  <p className="truncate text-sm font-medium text-foreground">{task.operationType}</p>
                  <Badge variant={taskStatusVariant(task.status)}>{task.status}</Badge>
                </div>
                <p className="mt-2 text-xs text-secondary-text">{task.createdAt}</p>
                {task.error ? <p className="mt-2 text-xs text-danger">{task.error}</p> : null}
              </div>
            ))}
            {tasks.length === 0 ? <p className="text-sm text-secondary-text">暂无后台任务</p> : null}
          </div>
        </SectionCard>
      </div>
    </AppPage>
  );
};

export default DataHealthPage;
