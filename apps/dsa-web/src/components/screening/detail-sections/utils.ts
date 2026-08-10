import type { ScreeningCandidateDetail } from '../../../types/screening';

export function hasFiveLayerData(candidate: ScreeningCandidateDetail): boolean {
  return candidate.tradeStage != null || candidate.marketRegime != null;
}
