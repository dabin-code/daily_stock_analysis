import type React from 'react';
import {
  TRADE_STAGE_LABELS,
  TRADE_STAGE_COLORS,
  THEME_POSITION_LABELS,
  THEME_POSITION_COLORS,
  MARKET_REGIME_LABELS,
  MARKET_REGIME_COLORS,
  POOL_LEVEL_LABELS,
} from '../../../types/screening';

export function LabeledBadge({ value, labelMap, colorMap }: {
  value?: string;
  labelMap: Record<string, string>;
  colorMap: Record<string, string>;
}) {
  if (!value) return <span className="text-xs text-secondary-text/40">--</span>;
  const label = labelMap[value] ?? value;
  const colorClass = colorMap[value] ?? 'bg-gray-500/20 text-gray-400 border-gray-500/30';
  return (
    <span className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] font-medium ${colorClass}`}>
      {label}
    </span>
  );
}

export function InfoRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 text-xs">
      <span className="shrink-0 text-secondary-text">{label}</span>
      <span className="text-right font-mono text-foreground">{children}</span>
    </div>
  );
}

export {
  TRADE_STAGE_LABELS,
  TRADE_STAGE_COLORS,
  THEME_POSITION_LABELS,
  THEME_POSITION_COLORS,
  MARKET_REGIME_LABELS,
  MARKET_REGIME_COLORS,
  POOL_LEVEL_LABELS,
};
