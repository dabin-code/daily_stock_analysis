import type React from "react";
import { useState } from "react";
import { Play, RotateCcw, RefreshCw, Settings2, ChevronDown, ChevronUp } from "lucide-react";
import { Button, Card, ConfirmDialog, Select } from "../common";
import { useScreeningStore } from "../../stores/screeningStore";
import { cn } from "../../utils/cn";
import type { ScreeningMode } from "../../types/screening";

const MODE_OPTIONS = [
  { value: "balanced", label: "均衡模式" },
  { value: "aggressive", label: "激进模式" },
  { value: "quality", label: "质量模式" },
];

export const ScreeningControlBar: React.FC = () => {
  const {
    mode,
    setMode,
    tradeDate,
    setTradeDate,
    candidateLimit,
    setCandidateLimit,
    aiTopK,
    setAiTopK,
    isRunning,
    isBackfilling,
    blockingDialog,
    clearBlockingDialog,
    startScreening,
    backfillStocksToTradeDate,
    reset,
  } = useScreeningStore();

  const [showAdvanced, setShowAdvanced] = useState(false);

  const handleStart = () => {
    void startScreening();
  };

  const handleBackfill = () => {
    void backfillStocksToTradeDate();
  };

  return (
    <>
      <Card variant="bordered" padding="md" data-testid="screening-control-bar">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <Button
              variant="primary"
              onClick={handleStart}
              isLoading={isRunning}
              disabled={isBackfilling}
              loadingText="筛选中..."
              glow
            >
              <Play className="h-4 w-4" />
              开始筛选
            </Button>
            <Button variant="ghost" onClick={reset} disabled={isRunning || isBackfilling}>
              <RotateCcw className="h-4 w-4" />
              重置
            </Button>
            <Button
              variant="ghost"
              onClick={handleBackfill}
              disabled={isRunning || isBackfilling}
              isLoading={isBackfilling}
              loadingText="回填中..."
            >
              <RefreshCw className="h-4 w-4" />
              回填至该日
            </Button>
          </div>

          <div className="flex items-center gap-3">
            <Select
              label=""
              value={mode}
              onChange={(value) => setMode(value as ScreeningMode)}
              options={MODE_OPTIONS}
            />
            <div className="flex flex-col">
              <input
                id="trade-date"
                type="date"
                value={tradeDate}
                onChange={(event) => setTradeDate(event.target.value)}
                aria-label="交易日"
                className={cn(
                  "h-11 w-full rounded-xl border border-white/10 bg-card px-4 py-2.5 text-sm text-foreground",
                  "shadow-soft-card transition-all focus:outline-none focus:ring-4 focus:ring-cyan/15 focus:border-cyan/40",
                )}
              />
            </div>
            <button
              type="button"
              onClick={() => setShowAdvanced((prev) => !prev)}
              className="flex items-center gap-1 rounded-lg px-2 py-1.5 text-xs text-secondary-text transition-colors hover:bg-hover/50 hover:text-foreground"
            >
              <Settings2 className="h-3.5 w-3.5" />
              高级
              {showAdvanced ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            </button>
          </div>
        </div>

        {showAdvanced && (
          <div className="mt-4 grid grid-cols-2 gap-3 border-t border-border/30 pt-4 sm:grid-cols-4">
            <div className="flex flex-col">
              <label htmlFor="candidate-limit" className="mb-2 text-sm font-medium text-foreground">
                候选上限
              </label>
              <input
                id="candidate-limit"
                type="number"
                min={1}
                max={200}
                value={candidateLimit}
                onChange={(event) => setCandidateLimit(Number(event.target.value))}
                className={cn(
                  "h-11 w-full rounded-xl border border-white/10 bg-card px-4 py-2.5 text-sm text-foreground",
                  "shadow-soft-card transition-all focus:outline-none focus:ring-4 focus:ring-cyan/15 focus:border-cyan/40",
                )}
              />
            </div>
            <div className="flex flex-col">
              <label htmlFor="ai-top-k" className="mb-2 text-sm font-medium text-foreground">
                AI 分析数
              </label>
              <input
                id="ai-top-k"
                type="number"
                min={0}
                max={50}
                value={aiTopK}
                onChange={(event) => setAiTopK(Number(event.target.value))}
                className={cn(
                  "h-11 w-full rounded-xl border border-white/10 bg-card px-4 py-2.5 text-sm text-foreground",
                  "shadow-soft-card transition-all focus:outline-none focus:ring-4 focus:ring-cyan/15 focus:border-cyan/40",
                )}
              />
            </div>
            <div className="col-span-2 text-xs text-secondary-text pt-3">
              五层决策引擎会根据当日市场环境自动调度策略：环境门控 → 板块热度 → 策略过滤 → 买点收敛 → 阶段裁决
            </div>
          </div>
        )}
      </Card>

      <ConfirmDialog
        isOpen={Boolean(blockingDialog)}
        title={blockingDialog?.title || ""}
        message={blockingDialog?.message || ""}
        confirmText="我知道了"
        cancelText="关闭"
        onConfirm={clearBlockingDialog}
        onCancel={clearBlockingDialog}
      />
    </>
  );
};
