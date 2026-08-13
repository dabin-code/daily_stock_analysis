# -*- coding: utf-8 -*-
"""Stable public facade for bottom-divergence v2 validation."""
from .bottom_divergence_v2_models import (
    CandidateEventEvidence,
    SampleSplit,
    ValidationInputError,
    ValidationSample,
    assign_tertile,
    build_parameter_snapshots,
    canonical_parameter_hash,
    chronological_split,
    compute_conversion_metrics,
    compute_equity_max_drawdown,
    compute_mae_mfe,
    compute_sample_returns,
    fit_tertile_boundaries,
    is_false_breakout,
    is_mature_sample,
    resolved_position_weight,
    wilson_lower_bound,
)
from .bottom_divergence_v2_metrics import (
    evaluate_noninferiority_gates,
    summarize_validation_samples,
)
from .bottom_divergence_v2_selection import (
    BottomDivergenceV2Validator,
    minimum_trading_days,
    purge_would_empty,
)

__all__ = [
    "BottomDivergenceV2Validator",
    "CandidateEventEvidence",
    "SampleSplit",
    "ValidationInputError",
    "ValidationSample",
    "assign_tertile",
    "build_parameter_snapshots",
    "canonical_parameter_hash",
    "chronological_split",
    "compute_conversion_metrics",
    "compute_equity_max_drawdown",
    "compute_mae_mfe",
    "compute_sample_returns",
    "evaluate_noninferiority_gates",
    "fit_tertile_boundaries",
    "is_false_breakout",
    "is_mature_sample",
    "minimum_trading_days",
    "purge_would_empty",
    "resolved_position_weight",
    "summarize_validation_samples",
    "wilson_lower_bound",
]
