# -*- coding: utf-8 -*-
"""Overall grading helpers for five-layer backtest summaries."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


# ── Letter-grade ladder (high -> low) used for sample-quality downgrades ────
_GRADE_ORDER: List[str] = ["A+", "A", "B+", "B", "C", "D"]

# When < AGGREGATABLE_RATIO_DOWNGRADE_THRESHOLD of raw samples could be
# aggregated, the grade is forced one step lower because the headline metrics
# are computed on a small biased subset (lots of suppressed/error rows).
AGGREGATABLE_RATIO_DOWNGRADE_THRESHOLD = 0.6


@dataclass(frozen=True)
class GradeResult:
    """Structured grading output: letter + reasons + downgrade trail."""

    grade: str
    raw_grade: str
    reasons: List[str] = field(default_factory=list)
    downgraded: bool = False
    aggregatable_ratio: Optional[float] = None


class SystemGrader:
    """Maps summary metrics to a simple user-facing letter grade.

    ``grade()`` keeps the legacy ``str`` contract for callers that only need
    the letter. ``grade_with_reasons()`` is the richer entry point used by
    persistence so we can surface *why* a grade landed where it did
    (sample-quality downgrades, missing inputs, etc.).
    """

    @staticmethod
    def grade(
        win_rate_pct: float | None,
        profit_factor: float | None,
        time_bucket_stability: float | None,
        sample_count: int,
        raw_sample_count: int | None = None,
    ) -> str:
        return SystemGrader.grade_with_reasons(
            win_rate_pct=win_rate_pct,
            profit_factor=profit_factor,
            time_bucket_stability=time_bucket_stability,
            sample_count=sample_count,
            raw_sample_count=raw_sample_count,
        ).grade

    @staticmethod
    def grade_with_reasons(
        win_rate_pct: float | None,
        profit_factor: float | None,
        time_bucket_stability: float | None,
        sample_count: int,
        raw_sample_count: int | None = None,
    ) -> GradeResult:
        """Compute the grade plus the per-axis reasons that produced it.

        Args:
            sample_count: Aggregatable sample count (rows that contributed to
                ``win_rate_pct`` / ``profit_factor``).
            raw_sample_count: Total raw sample count (including suppressed /
                error rows). When provided and the aggregatable ratio falls
                below ``AGGREGATABLE_RATIO_DOWNGRADE_THRESHOLD``, the final
                grade is forced one step lower so a heavily-suppressed run
                cannot quietly hit "A".
        """
        reasons: List[str] = []

        if sample_count < 10:
            reasons.append(
                f"sample_count={sample_count} < 10 → grade not computed"
            )
            return GradeResult(
                grade="N/A",
                raw_grade="N/A",
                reasons=reasons,
                aggregatable_ratio=_compute_ratio(sample_count, raw_sample_count),
            )

        score = 0.0

        if win_rate_pct is not None:
            if win_rate_pct >= 60:
                score += 40
                reasons.append(f"win_rate_pct={win_rate_pct:.1f} ≥ 60 → +40")
            elif win_rate_pct >= 55:
                score += 35
                reasons.append(f"win_rate_pct={win_rate_pct:.1f} ≥ 55 → +35")
            elif win_rate_pct >= 50:
                score += 25
                reasons.append(f"win_rate_pct={win_rate_pct:.1f} ≥ 50 → +25")
            elif win_rate_pct >= 45:
                score += 15
                reasons.append(f"win_rate_pct={win_rate_pct:.1f} ≥ 45 → +15")
            else:
                score += 5
                reasons.append(f"win_rate_pct={win_rate_pct:.1f} < 45 → +5")
        else:
            reasons.append("win_rate_pct missing → 0")

        if profit_factor is not None:
            if profit_factor >= 2.0:
                score += 40
                reasons.append(f"profit_factor={profit_factor:.2f} ≥ 2.0 → +40")
            elif profit_factor >= 1.5:
                score += 35
                reasons.append(f"profit_factor={profit_factor:.2f} ≥ 1.5 → +35")
            elif profit_factor >= 1.2:
                score += 25
                reasons.append(f"profit_factor={profit_factor:.2f} ≥ 1.2 → +25")
            elif profit_factor >= 1.0:
                score += 15
                reasons.append(f"profit_factor={profit_factor:.2f} ≥ 1.0 → +15")
            else:
                score += 5
                reasons.append(f"profit_factor={profit_factor:.2f} < 1.0 → +5")
        else:
            reasons.append("profit_factor missing → 0")

        if time_bucket_stability is not None:
            if time_bucket_stability <= 0.08:
                score += 20
                reasons.append(
                    f"time_bucket_stability={time_bucket_stability:.3f} ≤ 0.08 → +20"
                )
            elif time_bucket_stability <= 0.12:
                score += 15
                reasons.append(
                    f"time_bucket_stability={time_bucket_stability:.3f} ≤ 0.12 → +15"
                )
            elif time_bucket_stability <= 0.15:
                score += 10
                reasons.append(
                    f"time_bucket_stability={time_bucket_stability:.3f} ≤ 0.15 → +10"
                )
            else:
                score += 5
                reasons.append(
                    f"time_bucket_stability={time_bucket_stability:.3f} > 0.15 → +5"
                )
        else:
            reasons.append("time_bucket_stability missing → 0")

        raw_grade = _score_to_letter(score)
        ratio = _compute_ratio(sample_count, raw_sample_count)

        downgraded = False
        final_grade = raw_grade
        if (
            ratio is not None
            and ratio < AGGREGATABLE_RATIO_DOWNGRADE_THRESHOLD
            and raw_grade != "N/A"
        ):
            downgraded_grade = _step_down(raw_grade)
            if downgraded_grade != raw_grade:
                final_grade = downgraded_grade
                downgraded = True
                reasons.append(
                    f"aggregatable_ratio={ratio:.2f} < "
                    f"{AGGREGATABLE_RATIO_DOWNGRADE_THRESHOLD:.2f} "
                    f"→ downgrade {raw_grade} → {final_grade}"
                )

        return GradeResult(
            grade=final_grade,
            raw_grade=raw_grade,
            reasons=reasons,
            downgraded=downgraded,
            aggregatable_ratio=ratio,
        )


def _score_to_letter(score: float) -> str:
    if score >= 90:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 70:
        return "B+"
    if score >= 55:
        return "B"
    if score >= 40:
        return "C"
    return "D"


def _step_down(grade: str) -> str:
    """Return the next letter below ``grade`` (capped at the lowest)."""
    try:
        idx = _GRADE_ORDER.index(grade)
    except ValueError:
        return grade
    return _GRADE_ORDER[min(idx + 1, len(_GRADE_ORDER) - 1)]


def _compute_ratio(
    aggregatable: int,
    raw: int | None,
) -> Optional[float]:
    if raw is None or raw <= 0:
        return None
    return round(aggregatable / raw, 4)
