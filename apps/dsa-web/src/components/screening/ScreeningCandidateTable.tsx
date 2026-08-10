import type React from 'react';
import { useState, useMemo } from 'react';
import { Eye, ArrowUpDown, ArrowUp, ArrowDown, Brain, Bell } from 'lucide-react';
import { Card, Button, Loading } from '../common';
import { useScreeningStore } from '../../stores/screeningStore';
import { cn } from '../../utils/cn';
import type { ScreeningCandidate } from '../../types/screening';
import {
  TRADE_STAGE_LABELS,
  TRADE_STAGE_COLORS,
  THEME_POSITION_LABELS,
  THEME_POSITION_COLORS,
  SETUP_TYPE_LABELS,
  ENTRY_MATURITY_LABELS,
} from '../../types/screening';

type SortKey = 'rank' | 'ruleScore' | 'finalScore' | 'code' | 'tradeStage' | 'themePosition';
type SortDir = 'asc' | 'desc';

const TRADE_STAGE_ORDER: Record<string, number> = {
  add_on_strength: 0,
  probe_entry: 1,
  focus: 2,
  watch: 3,
  stand_aside: 4,
  reject: 5,
};

const THEME_POSITION_ORDER: Record<string, number> = {
  main_theme: 0,
  secondary_theme: 1,
  follower_theme: 2,
  fading_theme: 3,
  non_theme: 4,
};

function sortCandidates(items: ScreeningCandidate[], key: SortKey, dir: SortDir): ScreeningCandidate[] {
  return [...items].sort((a, b) => {
    let va: number | string;
    let vb: number | string;
    switch (key) {
      case 'rank':
        va = a.rank; vb = b.rank; break;
      case 'ruleScore':
        va = a.ruleScore; vb = b.ruleScore; break;
      case 'finalScore':
        va = a.finalScore ?? 0; vb = b.finalScore ?? 0; break;
      case 'tradeStage':
        va = TRADE_STAGE_ORDER[a.tradeStage ?? ''] ?? 99;
        vb = TRADE_STAGE_ORDER[b.tradeStage ?? ''] ?? 99;
        break;
      case 'themePosition':
        va = THEME_POSITION_ORDER[a.themePosition ?? ''] ?? 99;
        vb = THEME_POSITION_ORDER[b.themePosition ?? ''] ?? 99;
        break;
      case 'code':
      default:
        va = a.code; vb = b.code; break;
    }
    if (typeof va === 'string') return dir === 'asc' ? va.localeCompare(vb as string) : (vb as string).localeCompare(va);
    return dir === 'asc' ? (va as number) - (vb as number) : (vb as number) - (va as number);
  });
}

function SortIcon({ active, dir }: { active: boolean; dir: SortDir }) {
  if (!active) return <ArrowUpDown className="h-3 w-3 text-secondary-text/40" />;
  return dir === 'asc' ? <ArrowUp className="h-3 w-3 text-cyan" /> : <ArrowDown className="h-3 w-3 text-cyan" />;
}

function TradeStageCell({ stage }: { stage?: string }) {
  if (!stage) return <span className="text-secondary-text/30">--</span>;
  const label = TRADE_STAGE_LABELS[stage] ?? stage;
  const colorClass = TRADE_STAGE_COLORS[stage] ?? 'bg-gray-500/20 text-gray-400 border-gray-500/30';
  return (
    <span className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] font-medium ${colorClass}`}>
      {label}
    </span>
  );
}

function ThemePositionCell({ position }: { position?: string }) {
  if (!position) return <span className="text-secondary-text/30">--</span>;
  const label = THEME_POSITION_LABELS[position] ?? position;
  const colorClass = THEME_POSITION_COLORS[position] ?? 'bg-gray-500/20 text-gray-400 border-gray-500/30';
  return (
    <span className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] font-medium ${colorClass}`}>
      {label}
    </span>
  );
}

function SetupTypeCell({ setup }: { setup?: string }) {
  if (!setup || setup === 'none') return <span className="text-secondary-text/30">--</span>;
  const label = SETUP_TYPE_LABELS[setup] ?? setup;
  return (
    <span className="inline-flex rounded border border-border/40 bg-elevated/40 px-1.5 py-0.5 text-[10px] text-secondary-text">
      {label}
    </span>
  );
}

function EntryMaturityCell({ maturity }: { maturity?: string }) {
  if (!maturity) return <span className="text-secondary-text/30">--</span>;
  const label = ENTRY_MATURITY_LABELS[maturity] ?? maturity;
  const bars = maturity === 'high' ? 3 : maturity === 'medium' ? 2 : 1;
  const barColor = maturity === 'high' ? 'bg-emerald-400' : maturity === 'medium' ? 'bg-yellow-400' : 'bg-gray-500';
  return (
    <span className="inline-flex items-center gap-1 text-[10px] text-secondary-text">
      <span className="inline-flex gap-0.5">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className={`h-2 w-1 rounded-sm ${i < bars ? barColor : 'bg-border/40'}`}
          />
        ))}
      </span>
      {label}
    </span>
  );
}

function AiStageCell({ stage, confidence }: { stage?: string; confidence?: number }) {
  if (!stage) return <span className="text-secondary-text/30">--</span>;
  const stageLabel = TRADE_STAGE_LABELS[stage] ?? stage;
  return (
    <span className="inline-flex items-center gap-1 text-[10px]">
      <span className="text-purple">{stageLabel}</span>
      {confidence != null && (
        <span className="text-secondary-text/60">{(confidence * 100).toFixed(0)}%</span>
      )}
    </span>
  );
}

export const ScreeningCandidateTable: React.FC = () => {
  const { currentRun, candidates, candidatesLoading, selectCandidate, sendNotification } =
    useScreeningStore();
  const [sortKey, setSortKey] = useState<SortKey>('rank');
  const [sortDir, setSortDir] = useState<SortDir>('asc');

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  };

  const sorted = useMemo(
    () => sortCandidates(candidates, sortKey, sortDir),
    [candidates, sortKey, sortDir],
  );

  const hasFiveLayerData = useMemo(
    () => candidates.some((c) => c.tradeStage != null),
    [candidates],
  );

  if (!currentRun || candidates.length === 0) {
    if (candidatesLoading) return <Loading label="加载候选..." />;
    return null;
  }

  const thClass = 'px-3 py-2.5 text-left text-xs font-medium text-secondary-text cursor-pointer select-none hover:text-foreground transition-colors';
  const thStatic = cn(thClass, 'cursor-default hover:text-secondary-text');

  return (
    <Card variant="bordered" padding="none" data-testid="candidate-table">
      <div className="flex items-center justify-between px-5 py-3 border-b border-border/40">
        <h3 className="text-sm font-semibold text-foreground">
          候选结果 <span className="text-secondary-text font-normal">({candidates.length})</span>
        </h3>
        <div className="flex items-center gap-2">
          {currentRun.status === 'completed' && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void sendNotification(currentRun.runId)}
            >
              <Bell className="h-3.5 w-3.5" />
              推送通知
            </Button>
          )}
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="border-b border-border/30 bg-elevated/30">
              <th className={thClass} onClick={() => toggleSort('rank')}>
                <span className="inline-flex items-center gap-1">排名 <SortIcon active={sortKey === 'rank'} dir={sortDir} /></span>
              </th>
              <th className={thStatic}>代码</th>
              <th className={thStatic}>名称</th>
              {hasFiveLayerData && (
                <>
                  <th className={thClass} onClick={() => toggleSort('tradeStage')}>
                    <span className="inline-flex items-center gap-1">交易阶段 <SortIcon active={sortKey === 'tradeStage'} dir={sortDir} /></span>
                  </th>
                  <th className={thStatic}>买点类型</th>
                  <th className={thStatic}>成熟度</th>
                  <th className={thClass} onClick={() => toggleSort('themePosition')}>
                    <span className="inline-flex items-center gap-1">题材地位 <SortIcon active={sortKey === 'themePosition'} dir={sortDir} /></span>
                  </th>
                </>
              )}
              <th className={thClass} onClick={() => toggleSort('ruleScore')}>
                <span className="inline-flex items-center gap-1">规则评分 <SortIcon active={sortKey === 'ruleScore'} dir={sortDir} /></span>
              </th>
              {hasFiveLayerData && (
                <th className={thStatic}>AI 阶段</th>
              )}
              {!hasFiveLayerData && (
                <>
                  <th className={thClass} onClick={() => toggleSort('finalScore')}>
                    <span className="inline-flex items-center gap-1">综合评分 <SortIcon active={sortKey === 'finalScore'} dir={sortDir} /></span>
                  </th>
                  <th className={thStatic}>AI</th>
                </>
              )}
              <th className={cn(thStatic, 'text-center')}>操作</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((c) => (
              <tr
                key={c.code}
                data-testid={`candidate-row-${c.code}`}
                className="border-b border-border/20 transition-colors hover:bg-hover/50"
              >
                <td className="px-3 py-2.5 text-xs font-medium text-foreground">{c.rank}</td>
                <td className="px-3 py-2.5 text-xs font-mono text-cyan">{c.code}</td>
                <td className="px-3 py-2.5 text-xs text-foreground">{c.name || '--'}</td>
                {hasFiveLayerData && (
                  <>
                    <td className="px-3 py-2.5"><TradeStageCell stage={c.tradeStage} /></td>
                    <td className="px-3 py-2.5"><SetupTypeCell setup={c.setupType} /></td>
                    <td className="px-3 py-2.5"><EntryMaturityCell maturity={c.entryMaturity} /></td>
                    <td className="px-3 py-2.5"><ThemePositionCell position={c.themePosition} /></td>
                  </>
                )}
                <td className="px-3 py-2.5 text-xs">
                  <span className="font-medium text-foreground">{c.ruleScore.toFixed(1)}</span>
                </td>
                {hasFiveLayerData && (
                  <td className="px-3 py-2.5">
                    <AiStageCell stage={c.aiTradeStage} confidence={c.aiConfidence} />
                  </td>
                )}
                {!hasFiveLayerData && (
                  <>
                    <td className="px-3 py-2.5 text-xs">
                      {c.finalScore != null ? (
                        <span className="font-medium text-foreground">{c.finalScore.toFixed(1)}</span>
                      ) : (
                        <span className="text-secondary-text/40">--</span>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-center">
                      {c.selectedForAi ? (
                        <Brain className="mx-auto h-3.5 w-3.5 text-purple" />
                      ) : (
                        <span className="text-secondary-text/30">--</span>
                      )}
                    </td>
                  </>
                )}
                <td className="px-3 py-2.5 text-center">
                  <button
                    type="button"
                    onClick={() => void selectCandidate(currentRun.runId, c.code)}
                    className="inline-flex h-7 w-7 items-center justify-center rounded-lg text-secondary-text transition-colors hover:bg-cyan/10 hover:text-cyan"
                    aria-label={`查看 ${c.code} 详情`}
                  >
                    <Eye className="h-3.5 w-3.5" />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
};
