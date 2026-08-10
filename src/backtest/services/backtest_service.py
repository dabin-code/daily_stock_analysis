# -*- coding: utf-8 -*-
"""Five-layer backtest service — orchestration layer.

Pipeline: select_candidates → classify → get_forward_bars →
resolve_execution → evaluate → save_evaluations → update_run.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from src.backtest.aggregators.calibration_output_generator import CalibrationOutputGenerator
from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
from src.backtest.aggregators.system_grader import SystemGrader
from src.backtest.classifiers.signal_classifier import SignalClassifier
from src.backtest.evaluators.entry_evaluator import EntrySignalEvaluator
from src.backtest.evaluators.observation_evaluator import ObservationSignalEvaluator
from src.backtest.execution.execution_model_resolver import ExecutionModelResolver
from src.backtest.execution.plan_replay_executor import PlanReplayExecutor
from src.backtest.models.backtest_models import (
    FiveLayerBacktestCalibrationOutput,
    FiveLayerBacktestEvaluation,
    FiveLayerBacktestGroupSummary,
    FiveLayerBacktestRecommendation,
    FiveLayerBacktestRun,
)
from src.backtest.models.enums import EvaluationMode
from src.backtest.recommendations.recommendation_engine import RecommendationEngine
from src.backtest.repositories.calibration_repo import CalibrationRepository
from src.backtest.repositories.evaluation_repo import EvaluationRepository
from src.backtest.repositories.recommendation_repo import RecommendationRepository
from src.backtest.repositories.run_repo import RunRepository
from src.backtest.repositories.summary_repo import SummaryRepository
from src.backtest.services.candidate_selector import CandidateSelector
from src.backtest.services.sample_bucket_service import SampleBucketService
from src.backtest.utils.summary_metrics import get_aggregatable_sample_count
from src.repositories.stock_repo import (
    DEFAULT_GAP_TOLERANCE_FACTOR,
    ForwardBarsMeta,
    StockRepository,
)
from src.schemas.trading_types import (
    CandidatePoolLevel,
    EntryMaturity,
    MarketEnvironment,
    MarketRegime,
    RiskLevel,
    SetupType,
    ThemePosition,
    TradeStage,
)
from src.services.trade_stage_judge import TradeStageJudge
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

_DEFAULT_EVAL_WINDOW = 10
_ENTRY_SIGNAL_STAGES = frozenset({TradeStage.PROBE_ENTRY.value, TradeStage.ADD_ON_STRENGTH.value})
_LOW123_CONSERVATIVE_REJECTION = "confirmed_missing_breakout_bar_index"


def _dump_json(payload: Any) -> Optional[str]:
    if payload is None:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _safe_positive_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _build_candidate_filter(
    *,
    source: str,
    market: str,
    evaluation_mode: str,
    execution_model: str,
    eval_window_days: int,
    trade_date_from: Optional[date],
    trade_date_to: Optional[date],
    screening_run_ids: List[str],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a structured filter snapshot so each backtest run is auditable
    back to the exact set of screening runs it was sourced from (A1).

    ``source`` is either ``by_screening_run`` (the pipeline was driven by an
    explicit screening_run_id) or ``by_date_range`` (the pipeline picked up
    every screening run that landed in the trade-date window).
    """
    payload: Dict[str, Any] = {
        "source": source,
        "market": market,
        "evaluation_mode": evaluation_mode,
        "execution_model": execution_model,
        "eval_window_days": eval_window_days,
        "trade_date_from": trade_date_from.isoformat() if trade_date_from else None,
        "trade_date_to": trade_date_to.isoformat() if trade_date_to else None,
        "screening_run_ids": list(screening_run_ids),
        "screening_run_count": len(screening_run_ids),
    }
    if extra:
        payload.update(extra)
    return payload


def _extract_screening_run_ids(candidates: List[Dict[str, Any]]) -> List[str]:
    """Return deterministically-sorted unique screening_run_ids."""
    ids = {
        c["screening_run_id"]
        for c in candidates
        if c.get("screening_run_id") is not None
    }
    return sorted(ids)


def _detect_candidate_staleness(
    candidates: List[Dict[str, Any]],
    anchor: datetime,
) -> List[Dict[str, Any]]:
    """A4 lite: return candidates whose ``candidate_updated_at`` is later than
    ``anchor`` (typically backtest_run.started_at).

    A non-empty list means the screener rewrote the candidate's stored row
    AFTER the backtest started, so the snapshot fields fed into evaluation
    are not the decision-time payload. Surface this as advisory metadata
    (not an error) so analysts can spot drift without re-running the full
    pipeline.
    """
    stale: List[Dict[str, Any]] = []
    for c in candidates:
        updated_at = c.get("candidate_updated_at")
        if not isinstance(updated_at, datetime):
            continue
        if updated_at <= anchor:
            continue
        stale.append(
            {
                "screening_run_id": c.get("screening_run_id"),
                "code": c.get("code"),
                "candidate_created_at": (
                    c["candidate_created_at"].isoformat()
                    if isinstance(c.get("candidate_created_at"), datetime)
                    else None
                ),
                "candidate_updated_at": updated_at.isoformat(),
            }
        )
    return stale


def _is_forward_gap_check_enabled() -> bool:
    """Return True when B5 forward-bar quality check should be applied.

    Default: enabled. Set ``BACKTEST_FORWARD_GAP_CHECK_ENABLED=false`` to
    fall back to the legacy behaviour where gap_too_long /
    insufficient_forward_bars samples were silently kept and produced
    misleading forward_return_5d / risk_avoided_pct values.
    """
    raw = os.getenv("BACKTEST_FORWARD_GAP_CHECK_ENABLED", "true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


def _get_forward_gap_tolerance_factor() -> float:
    """Tolerance factor for converting eval_window_days to a calendar-day
    cutoff (eval_window_days * factor). Default 2.0 covers weekends + small
    Chinese holidays on a 5d window. Configurable via
    ``BACKTEST_FORWARD_GAP_TOLERANCE_FACTOR``.
    """
    raw = os.getenv("BACKTEST_FORWARD_GAP_TOLERANCE_FACTOR")
    if not raw:
        return DEFAULT_GAP_TOLERANCE_FACTOR
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError("tolerance factor must be positive")
        return value
    except (TypeError, ValueError):
        logger.warning(
            "Invalid BACKTEST_FORWARD_GAP_TOLERANCE_FACTOR=%r; "
            "falling back to default %.1f",
            raw,
            DEFAULT_GAP_TOLERANCE_FACTOR,
        )
        return DEFAULT_GAP_TOLERANCE_FACTOR


def _summarise_adj_factor_sources(forward_bars: List[Any]) -> Dict[str, int]:
    """Return ``{adj_factor_source: count}`` for the given bars.

    Used by ``_process_candidate`` to record per-sample adj_factor coverage
    in ``metrics_json.forward_window``, and by run-level aggregation to
    populate ``run.data_version``. Bars without the ``adj_factor_source``
    attribute (legacy mocks, in-memory test fixtures) are bucketed as
    ``"unknown"``.
    """
    counter: Dict[str, int] = {}
    for bar in forward_bars:
        source = getattr(bar, "adj_factor_source", None) or "unknown"
        counter[source] = counter.get(source, 0) + 1
    return counter


def _aggregate_adj_factor_distribution(
    evaluations: List["FiveLayerBacktestEvaluation"],
) -> Dict[str, int]:
    """Sum the per-sample ``adj_factor_sources`` blocks from evaluations
    into a single run-level distribution.

    Tolerates rows whose ``metrics_json`` is missing, malformed, or did
    not include a ``forward_window.adj_factor_sources`` block — those
    samples simply don't contribute to the distribution. Used to build
    ``run.data_version`` without re-querying ``stock_daily`` (every bar
    that contributed to an evaluation already left its trace here).
    """
    distribution: Dict[str, int] = {}
    for evaluation in evaluations:
        raw = evaluation.metrics_json
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        forward_window = payload.get("forward_window") if isinstance(payload, dict) else None
        if not isinstance(forward_window, dict):
            continue
        sources = forward_window.get("adj_factor_sources")
        if not isinstance(sources, dict):
            continue
        for source, count in sources.items():
            if not isinstance(count, (int, float)):
                continue
            key = str(source)
            distribution[key] = distribution.get(key, 0) + int(count)
    return distribution


def _build_data_version(
    distribution: Dict[str, int],
    schema_tag: str = "adj1",
) -> str:
    """Compose a compact, human-readable ``data_version`` string from the
    aggregate adj_factor_source distribution across a run.

    Format: ``"adj1|<source1>:<n1>,<source2>:<n2>,...|total:<N>"`` where
    ``schema_tag`` lets future B6 phases bump the version without
    parser-side coordination.

    Examples:
        ``"adj1|fetcher_provided:480,legacy_assume_one:20|total:500"``
        ``"adj1|legacy_assume_one:500|total:500"``  (corpus pre-dates B6)
    """
    if not distribution:
        return f"{schema_tag}|empty"
    total = sum(distribution.values())
    parts = ",".join(
        f"{source}:{count}"
        for source, count in sorted(distribution.items())
    )
    return f"{schema_tag}|{parts}|total:{total}"


def _resolve_forward_quality_reason(meta: ForwardBarsMeta) -> Optional[str]:
    """Translate a :class:`ForwardBarsMeta` into a suppression reason or
    ``None`` if the window is healthy enough to evaluate.

    Order matters: ``gap_too_long`` is tested before
    ``insufficient_forward_bars`` because a halted stock often returns 0-1
    bars (which would also satisfy ``insufficient_bars``); attributing
    the suppression to gap_too_long is more actionable than the generic
    "not enough bars" code.
    """
    if meta.actual_bar_count == 0:
        return "no_forward_bars"
    if meta.gap_too_long:
        return "gap_too_long"
    if meta.insufficient_bars:
        return "insufficient_forward_bars"
    return None


def _resolve_grade_metrics_source(
    overall: FiveLayerBacktestGroupSummary,
) -> Tuple[Optional[float], Optional[float], Optional[float], str]:
    """Pick the family-correct (win_rate, profit_factor, time_bucket_stability)
    triple that should drive the system-level grade.

    The legacy ``overall.win_rate_pct`` / ``overall.profit_factor`` are mixed
    across entry's ``forward_return_5d`` and observation's ``risk_avoided_pct``
    (see C3/C4/C5 in the backtest defect list). Feeding the mixed numbers
    into ``SystemGrader`` lets observation samples inflate profit_factor —
    because ``risk_avoided_pct`` is non-negative by construction — and
    silently boosts the headline grade.

    Priority:
      1. ``family_breakdown.entry`` — entry signals are the tradable story
         the grade is meant to summarise.
      2. ``family_breakdown.observation`` — when the run is observation-only
         (no entry sample reached evaluation), grade off observation but
         tag the source so the headline is auditable.
      3. Mixed summary fields — last-resort backwards-compatible fallback.

    Returns ``(win_rate_pct, profit_factor, time_bucket_stability, source)``.
    """
    metrics_payload: Dict[str, Any] = {}
    if overall.metrics_json:
        try:
            metrics_payload = json.loads(overall.metrics_json) or {}
        except (TypeError, ValueError, json.JSONDecodeError):
            metrics_payload = {}

    family_breakdown = metrics_payload.get("family_breakdown") or {}
    if isinstance(family_breakdown, dict):
        entry = family_breakdown.get("entry")
        if isinstance(entry, dict) and entry.get("win_rate_pct") is not None:
            return (
                entry.get("win_rate_pct"),
                entry.get("profit_factor"),
                entry.get("time_bucket_stability"),
                "entry",
            )
        observation = family_breakdown.get("observation")
        if isinstance(observation, dict) and observation.get("win_rate_pct") is not None:
            return (
                observation.get("win_rate_pct"),
                observation.get("profit_factor"),
                observation.get("time_bucket_stability"),
                "observation",
            )

    return (
        overall.win_rate_pct,
        overall.profit_factor,
        overall.time_bucket_stability,
        "mixed",
    )


def _build_run_sample_baseline(
    raw_candidate_count: int,
    evaluations: List[FiveLayerBacktestEvaluation],
) -> Dict[str, Any]:
    """Build run-level sample baseline.

    Counts aggregatable / suppressed / error samples and groups suppression
    reasons. Prefers the explicit ``suppression_reason`` column written by
    the evaluators (E2 in the backtest defect list); falls back to inferring
    the reason from missing metric columns for backwards compatibility with
    rows produced before the column was introduced.
    """
    entry_count = sum(1 for item in evaluations if item.signal_family == "entry")
    observation_count = sum(1 for item in evaluations if item.signal_family == "observation")
    error_count = sum(1 for item in evaluations if (item.eval_status or "") == "error")
    suppressed_reasons: Dict[str, int] = {}
    aggregatable_count = 0

    for evaluation in evaluations:
        if (
            evaluation.trade_return_pct is not None
            or evaluation.forward_return_5d is not None
            or evaluation.risk_avoided_pct is not None
        ):
            aggregatable_count += 1
            continue
        reason = evaluation.suppression_reason
        if not reason:
            if evaluation.signal_family == "observation":
                reason = "missing_risk_avoided_pct"
            elif evaluation.signal_family == "entry":
                reason = "missing_forward_return_5d"
            else:
                reason = "missing_primary_metric"
        suppressed_reasons[reason] = suppressed_reasons.get(reason, 0) + 1

    return {
        "raw_sample_count": raw_candidate_count,
        "evaluated_sample_count": len(evaluations),
        "aggregatable_sample_count": aggregatable_count,
        "entry_sample_count": entry_count,
        "observation_sample_count": observation_count,
        "error_sample_count": error_count,
        "suppressed_sample_count": len(evaluations) - aggregatable_count,
        "suppressed_reasons": suppressed_reasons,
    }


def _parse_enum_value(enum_cls: type, raw_value: Any):
    if raw_value is None:
        return None
    try:
        return enum_cls(str(raw_value))
    except ValueError:
        return None


def _has_stop_loss_anchor(trade_plan: Any) -> bool:
    if not isinstance(trade_plan, dict):
        return False
    return (
        trade_plan.get("stop_loss") is not None
        or trade_plan.get("stop_loss_price") is not None
        or bool(trade_plan.get("stop_loss_rule"))
    )


class FiveLayerBacktestService:
    """Orchestrates a five-layer backtest run."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()
        self.run_repo = RunRepository(self.db)
        self.eval_repo = EvaluationRepository(self.db)
        self.summary_repo = SummaryRepository(self.db)
        self.calibration_repo = CalibrationRepository(self.db)
        self.recommendation_repo = RecommendationRepository(self.db)
        self.candidate_selector = CandidateSelector(self.db)
        self.stock_repo = StockRepository(self.db)
        self.aggregator = GroupSummaryAggregator(self.eval_repo, self.summary_repo)
        self.recommendation_engine = RecommendationEngine(
            self.summary_repo, self.eval_repo, self.recommendation_repo,
        )
        self.trade_stage_judge = TradeStageJudge()

    def create_run(
        self,
        evaluation_mode: str,
        execution_model: str,
        trade_date_from: date,
        trade_date_to: date,
        market: str = "cn",
        **kwargs,
    ) -> FiveLayerBacktestRun:
        backtest_run_id = f"flbt-{uuid.uuid4().hex[:12]}"
        return self.run_repo.create_run(
            backtest_run_id=backtest_run_id,
            evaluation_mode=evaluation_mode,
            execution_model=execution_model,
            trade_date_from=trade_date_from,
            trade_date_to=trade_date_to,
            market=market,
            **kwargs,
        )

    def get_run(self, backtest_run_id: str) -> Optional[FiveLayerBacktestRun]:
        return self.run_repo.get_run(backtest_run_id)

    def run_backtest(
        self,
        evaluation_mode: str,
        execution_model: str,
        trade_date_from: date,
        trade_date_to: date,
        market: str = "cn",
        eval_window_days: int = _DEFAULT_EVAL_WINDOW,
        **kwargs,
    ) -> FiveLayerBacktestRun:
        """Full pipeline across a date range: selects all candidates in
        [trade_date_from, trade_date_to], evaluates each, and saves results.

        A1: The set of screening runs this backtest sourced from is recorded
        in ``run.candidate_filter_json`` so a later audit can answer
        "which selection runs did this backtest cover?" without re-querying
        screening tables by date.
        """
        candidates = self.candidate_selector.select_candidates_by_date_range(
            date_from=trade_date_from,
            date_to=trade_date_to,
            market=market,
        )

        run = self.create_run(
            evaluation_mode=evaluation_mode,
            execution_model=execution_model,
            trade_date_from=trade_date_from,
            trade_date_to=trade_date_to,
            market=market,
            **kwargs,
        )

        screening_run_ids = _extract_screening_run_ids(candidates)
        candidate_filter = _build_candidate_filter(
            source="by_date_range",
            market=market,
            evaluation_mode=evaluation_mode,
            execution_model=execution_model,
            eval_window_days=eval_window_days,
            trade_date_from=trade_date_from,
            trade_date_to=trade_date_to,
            screening_run_ids=screening_run_ids,
        )

        if not candidates:
            self.run_repo.update_run_status(
                run.backtest_run_id,
                status="completed",
                sample_count=0,
                completed_count=0,
                error_count=0,
                candidate_filter_json=_dump_json(candidate_filter),
                completed_at=datetime.now(),
            )
            return self.run_repo.get_run(run.backtest_run_id)

        started_at = datetime.now()
        self.run_repo.update_run_status(
            run.backtest_run_id,
            status="running",
            sample_count=len(candidates),
            candidate_filter_json=_dump_json(candidate_filter),
            started_at=started_at,
        )

        evaluations, error_count = self._evaluate_candidates(
            run=run,
            candidates=candidates,
            execution_model=execution_model,
            evaluation_mode=evaluation_mode,
            eval_window_days=eval_window_days,
        )

        sample_baseline = _build_run_sample_baseline(len(candidates), evaluations)
        candidate_staleness = _detect_candidate_staleness(candidates, started_at)

        # B6 (3a): compute data_version BEFORE save_batch — once the rows
        # are committed they detach from the session and metrics_json is
        # no longer eagerly loadable. metrics_json is a plain string at
        # this point so the aggregation works without any session.
        adj_distribution = _aggregate_adj_factor_distribution(evaluations)
        data_version = _build_data_version(adj_distribution)

        if evaluations:
            self.eval_repo.save_batch(evaluations)

        self.run_repo.update_run_status(
            run.backtest_run_id,
            status="completed",
            sample_count=len(candidates),
            completed_count=len(evaluations),
            error_count=error_count,
            config_json=_dump_json(
                {
                    "sample_baseline": sample_baseline,
                    "candidate_staleness": {
                        "anchor": started_at.isoformat(),
                        "stale_count": len(candidate_staleness),
                        "stale_samples": candidate_staleness[:50],
                    },
                    "adj_factor_distribution": adj_distribution,
                }
            ),
            data_version=data_version,
            completed_at=datetime.now(),
        )

        return self.run_repo.get_run(run.backtest_run_id)

    def run_backtest_pipeline(
        self,
        screening_run_id: str,
        evaluation_mode: str,
        execution_model: str,
        market: str = "cn",
        eval_window_days: int = _DEFAULT_EVAL_WINDOW,
    ) -> FiveLayerBacktestRun:
        """Full pipeline driven by a single screening_run_id.

        A1: ``run.candidate_filter_json`` records ``source='by_screening_run'``
        and the requested ``screening_run_id`` so every backtest run is
        traceable back to its candidate source even when 0 candidates
        matched.
        """
        candidates = self.candidate_selector.select_candidates(screening_run_id)

        # Derive date range from candidates
        trade_dates = [c["trade_date"] for c in candidates if c.get("trade_date")]
        trade_date_from = min(trade_dates) if trade_dates else date.today()
        trade_date_to = max(trade_dates) if trade_dates else date.today()

        run = self.create_run(
            evaluation_mode=evaluation_mode,
            execution_model=execution_model,
            trade_date_from=trade_date_from,
            trade_date_to=trade_date_to,
            market=market,
        )

        # Always anchor the requested screening_run_id, even when 0
        # candidates matched (so empty runs stay attributable).
        observed_ids = _extract_screening_run_ids(candidates)
        screening_run_ids = sorted({screening_run_id, *observed_ids})
        candidate_filter = _build_candidate_filter(
            source="by_screening_run",
            market=market,
            evaluation_mode=evaluation_mode,
            execution_model=execution_model,
            eval_window_days=eval_window_days,
            trade_date_from=trade_date_from if candidates else None,
            trade_date_to=trade_date_to if candidates else None,
            screening_run_ids=screening_run_ids,
            extra={"requested_screening_run_id": screening_run_id},
        )

        if not candidates:
            self.run_repo.update_run_status(
                run.backtest_run_id,
                status="completed",
                sample_count=0,
                completed_count=0,
                error_count=0,
                candidate_filter_json=_dump_json(candidate_filter),
                completed_at=datetime.now(),
            )
            return self.run_repo.get_run(run.backtest_run_id)

        started_at = datetime.now()
        self.run_repo.update_run_status(
            run.backtest_run_id,
            status="running",
            sample_count=len(candidates),
            candidate_filter_json=_dump_json(candidate_filter),
            started_at=started_at,
        )

        evaluations, error_count = self._evaluate_candidates(
            run=run,
            candidates=candidates,
            execution_model=execution_model,
            evaluation_mode=evaluation_mode,
            eval_window_days=eval_window_days,
        )

        sample_baseline = _build_run_sample_baseline(len(candidates), evaluations)
        candidate_staleness = _detect_candidate_staleness(candidates, started_at)

        # B6 (3a): same data_version composition as run_backtest above —
        # mirrored here so screening_run-driven backtests are equally
        # auditable for adj_factor coverage. Computed BEFORE save_batch
        # to keep evaluations attached to their session.
        adj_distribution = _aggregate_adj_factor_distribution(evaluations)
        data_version = _build_data_version(adj_distribution)

        if evaluations:
            self.eval_repo.save_batch(evaluations)

        self.run_repo.update_run_status(
            run.backtest_run_id,
            status="completed",
            sample_count=len(candidates),
            completed_count=len(evaluations),
            error_count=error_count,
            config_json=_dump_json(
                {
                    "sample_baseline": sample_baseline,
                    "candidate_staleness": {
                        "anchor": started_at.isoformat(),
                        "stale_count": len(candidate_staleness),
                        "stale_samples": candidate_staleness[:50],
                    },
                    "adj_factor_distribution": adj_distribution,
                }
            ),
            data_version=data_version,
            completed_at=datetime.now(),
        )

        return self.run_repo.get_run(run.backtest_run_id)

    def _evaluate_candidates(
        self,
        run: FiveLayerBacktestRun,
        candidates: List[Dict[str, Any]],
        execution_model: str,
        evaluation_mode: str,
        eval_window_days: int,
    ) -> tuple:
        """Process all candidates. Returns (evaluations_list, error_count).

        Failed candidates are NOT silently dropped: we persist a placeholder
        evaluation row with ``eval_status='error'`` and
        ``suppression_reason='exception:<ExceptionClass>'`` so post-mortem can
        locate the offending candidate without re-running the pipeline.

        B1 enrichment: prefetch ``InstrumentMaster`` metadata (is_st / market
        / exchange) once per run and inject it into every candidate dict so
        the per-board limit-up / limit-down threshold can be resolved
        downstream without N+1 queries.
        """
        # Prefetch instrument metadata for every code in this batch — single
        # query, then mutate candidate dicts in place. Falls back to "no
        # metadata" gracefully if the table is empty (legacy DBs); the
        # downstream resolver will then use the ±10 % default.
        codes = sorted({
            c.get("code") for c in candidates if c.get("code")
        })
        instrument_metadata = self._load_instrument_metadata(codes) if codes else {}
        for candidate in candidates:
            meta = instrument_metadata.get(candidate.get("code") or "")
            if meta is None:
                continue
            # Only fill in fields that aren't already on the candidate dict
            # (defensive — candidate_selector might start providing them
            # itself in a later refactor).
            candidate.setdefault("is_st", bool(meta.get("is_st", False)))
            candidate.setdefault("market", meta.get("market") or "cn")
            candidate.setdefault("exchange", meta.get("exchange"))

        evaluations: List[FiveLayerBacktestEvaluation] = []
        error_count = 0

        for candidate in candidates:
            try:
                evaluation = self._process_candidate(
                    run=run,
                    candidate=candidate,
                    execution_model=execution_model,
                    evaluation_mode=evaluation_mode,
                    eval_window_days=eval_window_days,
                )
                evaluations.append(evaluation)
            except Exception as exc:  # noqa: BLE001 - we deliberately want to capture all
                error_count += 1
                logger.exception(
                    "Failed to evaluate candidate %s", candidate.get("code"),
                )
                evaluations.append(
                    self._build_error_evaluation(
                        run=run,
                        candidate=candidate,
                        evaluation_mode=evaluation_mode,
                        execution_model=execution_model,
                        exc=exc,
                    )
                )

        return evaluations, error_count

    def _load_instrument_metadata(
        self,
        codes: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        """Prefetch ``InstrumentMaster`` rows for the given codes.

        Returns ``{code: {is_st, market, exchange}}`` for the rows that
        exist; codes missing from ``InstrumentMaster`` are simply absent
        from the returned dict (callers fall back to defaults).

        Single SELECT regardless of input size — kept here rather than in a
        repository to keep B1 changes localised; if more callers need this
        we can extract a proper repo method later.
        """
        if not codes:
            return {}
        try:
            from src.storage import InstrumentMaster
            with self.db.get_session() as session:
                rows = (
                    session.query(InstrumentMaster)
                    .filter(InstrumentMaster.code.in_(codes))
                    .all()
                )
                return {
                    row.code: {
                        "is_st": bool(row.is_st),
                        "market": row.market or "cn",
                        "exchange": row.exchange,
                    }
                    for row in rows
                }
        except Exception:  # noqa: BLE001 - InstrumentMaster missing is non-fatal
            logger.warning(
                "Failed to prefetch InstrumentMaster for %d codes; "
                "falling back to default ±10 %% limit thresholds",
                len(codes),
                exc_info=True,
            )
            return {}

    @staticmethod
    def _build_error_evaluation(
        run: FiveLayerBacktestRun,
        candidate: Dict[str, Any],
        evaluation_mode: str,
        execution_model: str,
        exc: BaseException,
    ) -> FiveLayerBacktestEvaluation:
        """Construct a placeholder row so failed candidates remain auditable."""
        return FiveLayerBacktestEvaluation(
            backtest_run_id=run.backtest_run_id,
            screening_run_id=candidate.get("screening_run_id"),
            screening_candidate_id=candidate.get("screening_candidate_id"),
            trade_date=candidate.get("trade_date"),
            code=candidate.get("code") or "UNKNOWN",
            name=candidate.get("name"),
            evaluation_mode=evaluation_mode,
            signal_family="unknown",
            evaluator_type="unknown",
            execution_model=execution_model,
            snapshot_source="screening_candidate",
            eval_status="error",
            suppression_reason=f"exception:{type(exc).__name__}"[:64],
            metrics_json=_dump_json(
                {
                    "error_message": str(exc)[:500],
                    "error_class": type(exc).__name__,
                }
            ),
        )

    def _process_candidate(
        self,
        run: FiveLayerBacktestRun,
        candidate: Dict[str, Any],
        execution_model: str,
        evaluation_mode: str,
        eval_window_days: int,
    ) -> FiveLayerBacktestEvaluation:
        """Process a single candidate through classify → execute → evaluate.

        Snapshot/replay boundary (A2 + A3):
        - ``historical_snapshot``: classify using the persisted decision-time
          ``trade_stage`` only. Never invent a different stage from snapshot
          fields, because that would silently rewrite history and inflate
          entry sample counts that never existed at decision time.
        - ``rule_replay`` / ``parameter_calibration``: re-derive the stage
          from snapshot fields using the current rules and write it to
          ``replayed_trade_stage`` (so the snapshot fields stay verbatim
          and a side-by-side diff is auditable).
        """
        trade_date = candidate["trade_date"]
        code = candidate["code"]

        snapshot_trade_stage = candidate.get("trade_stage")
        replayed_trade_stage: Optional[str] = None
        is_replay_mode = evaluation_mode in {
            EvaluationMode.RULE_REPLAY.value,
            EvaluationMode.PARAMETER_CALIBRATION.value,
        }
        if is_replay_mode:
            replayed_trade_stage = self._replay_trade_stage_from_snapshot(candidate)
            classify_stage = replayed_trade_stage
        else:
            classify_stage = snapshot_trade_stage

        # Classify signal
        has_exit_plan = False
        trade_plan = candidate.get("trade_plan")
        if trade_plan and isinstance(trade_plan, dict):
            has_exit_plan = bool(trade_plan.get("exit_signal"))

        classification = SignalClassifier.classify(
            trade_stage=classify_stage,
            ai_trade_stage=candidate.get("ai_trade_stage"),
            ai_confidence=candidate.get("ai_confidence"),
            has_exit_plan=has_exit_plan,
        )

        # B5: Get forward bars with quality metadata so we can detect
        # suspended-trading samples (the requested 5d window actually
        # spans several weeks because the stock was halted) and
        # under-resourced samples (data source returned fewer bars than
        # asked for) — both make ``forward_return_5d`` and
        # ``risk_avoided_pct`` denominators meaningless.
        forward_bars, forward_meta = self.stock_repo.get_forward_bars_with_meta(
            code=code,
            analysis_date=trade_date,
            eval_window_days=eval_window_days,
            tolerance_factor=_get_forward_gap_tolerance_factor(),
        )

        # Build evaluation record. ``eval_status`` defaults to ``pending``
        # and is set by the family-specific evaluator below to either
        # ``evaluated`` (fully aggregatable) or ``suppressed`` (no metrics
        # produced — see ``suppression_reason`` for why).
        # ``snapshot_source`` reports which side of the dual-track was used
        # to drive classification; ``replayed`` is True only when the rule
        # replay actually produced a different stage from the snapshot.
        evaluation = FiveLayerBacktestEvaluation(
            backtest_run_id=run.backtest_run_id,
            screening_run_id=candidate.get("screening_run_id"),
            screening_candidate_id=candidate.get("screening_candidate_id"),
            trade_date=trade_date,
            code=code,
            name=candidate.get("name"),
            evaluation_mode=evaluation_mode,
            signal_family=classification.signal_family,
            signal_type=classification.effective_trade_stage,
            evaluator_type=classification.evaluator_type,
            execution_model=execution_model,
            snapshot_source="replayed" if is_replay_mode else "screening_candidate",
            replayed=bool(
                is_replay_mode
                and replayed_trade_stage is not None
                and replayed_trade_stage != snapshot_trade_stage
            ),
            eval_status="pending",
        )

        # Snapshot fields: ALWAYS verbatim from the screening candidate so
        # historic decisions remain auditable regardless of evaluation_mode.
        evaluation.snapshot_trade_stage = snapshot_trade_stage
        evaluation.snapshot_setup_type = candidate.get("setup_type")
        evaluation.snapshot_entry_maturity = candidate.get("entry_maturity")
        evaluation.snapshot_market_regime = candidate.get("market_regime")
        evaluation.snapshot_theme_position = candidate.get("theme_position")
        evaluation.snapshot_candidate_pool_level = candidate.get("candidate_pool_level")
        evaluation.snapshot_risk_level = candidate.get("risk_level")
        evaluation.factor_snapshot_json = _dump_json(candidate.get("factor_snapshot"))
        evaluation.trade_plan_json = _dump_json(candidate.get("trade_plan"))
        factor_snapshot = candidate.get("factor_snapshot")
        if not isinstance(factor_snapshot, dict):
            factor_snapshot = {}
        evaluation.evidence_json = _dump_json(
            {
                "matched_strategies": candidate.get("matched_strategies", []),
                "rule_hits": candidate.get("rule_hits", []),
                "primary_strategy": candidate.get("primary_strategy"),
                "contributing_strategies": candidate.get("contributing_strategies", []),
                "strategy_scores": candidate.get("strategy_scores", {}),
                "candidate_version": factor_snapshot.get(
                    "bottom_divergence_v2_candidate_version"
                ),
                "zone_version": factor_snapshot.get(
                    "bottom_divergence_v2_zone_version"
                ),
                "ai_trade_stage": candidate.get("ai_trade_stage"),
                "ai_confidence": candidate.get("ai_confidence"),
            },
        )

        # Replayed fields are populated ONLY in rule_replay /
        # parameter_calibration mode. historical_snapshot mode keeps them
        # NULL so downstream consumers can detect "this row is verbatim"
        # without needing to inspect evaluation_mode.
        if is_replay_mode:
            evaluation.replayed_trade_stage = replayed_trade_stage
            # The rest of the snapshot dimensions are not yet re-derived;
            # leave them NULL until parameter_calibration P1 work lands.

        # B5: forward-bar quality gate. Runs BEFORE family evaluation so
        # the suppression reason is the data quality issue (gap_too_long /
        # insufficient_forward_bars), not a downstream "missing 5d return"
        # symptom that would have masked the root cause.
        gap_check_enabled = _is_forward_gap_check_enabled()
        forward_quality_reason: Optional[str] = None
        if gap_check_enabled:
            forward_quality_reason = _resolve_forward_quality_reason(forward_meta)

        if forward_quality_reason is not None:
            evaluation.eval_status = "suppressed"
            evaluation.suppression_reason = forward_quality_reason
        else:
            # Execute + evaluate based on signal family. Each evaluator is
            # responsible for setting ``eval_status`` and, when suppressing,
            # ``suppression_reason``.
            if classification.signal_family == "entry":
                self._evaluate_entry(evaluation, forward_bars, execution_model, candidate)
            elif classification.signal_family == "observation":
                self._evaluate_observation(evaluation, forward_bars)
            else:
                # exit family: framework only, no metrics produced yet.
                evaluation.eval_status = "suppressed"
                evaluation.suppression_reason = "exit_family_not_implemented"

        evaluation.metrics_json = _dump_json(
            self._build_metrics_payload(
                candidate=candidate,
                evaluation=evaluation,
                classification=classification,
                forward_meta=forward_meta,
                forward_bars=forward_bars,
            ),
        )

        return evaluation

    def _replay_trade_stage_from_snapshot(self, candidate: Dict[str, Any]) -> Optional[str]:
        """Recompute trade_stage from snapshot fields using current rules.

        Used **only** in ``rule_replay`` / ``parameter_calibration`` modes;
        ``historical_snapshot`` keeps the persisted decision-time stage
        verbatim. Returns the persisted stage when current rules cannot
        improve it (so the diff against snapshot stays minimal).
        """
        persisted_stage = str(candidate.get("trade_stage") or "").lower() or None
        if persisted_stage in _ENTRY_SIGNAL_STAGES:
            return persisted_stage

        factor_snapshot = candidate.get("factor_snapshot")
        if not isinstance(factor_snapshot, dict):
            factor_snapshot = {}

        if factor_snapshot.get("ma100_low123_validation_status") == _LOW123_CONSERVATIVE_REJECTION:
            return persisted_stage

        regime = _parse_enum_value(MarketRegime, candidate.get("market_regime"))
        setup_type = _parse_enum_value(SetupType, candidate.get("setup_type"))
        entry_maturity = _parse_enum_value(EntryMaturity, candidate.get("entry_maturity"))
        pool_level = _parse_enum_value(
            CandidatePoolLevel,
            candidate.get("candidate_pool_level"),
        )
        theme_position = _parse_enum_value(ThemePosition, candidate.get("theme_position"))
        risk_level = _parse_enum_value(RiskLevel, candidate.get("risk_level")) or RiskLevel.MEDIUM

        if None in (regime, setup_type, entry_maturity, pool_level, theme_position):
            return persisted_stage

        derived_stage = self.trade_stage_judge.judge(
            env=MarketEnvironment(regime=regime, risk_level=risk_level),
            setup_type=setup_type,
            entry_maturity=entry_maturity,
            pool_level=pool_level,
            theme_position=theme_position,
            has_stop_loss=_has_stop_loss_anchor(candidate.get("trade_plan")),
        ).value

        if derived_stage in _ENTRY_SIGNAL_STAGES:
            return TradeStage.PROBE_ENTRY.value

        return persisted_stage

    def _evaluate_entry(
        self,
        evaluation: FiveLayerBacktestEvaluation,
        forward_bars: List[Any],
        execution_model: str,
        candidate: Dict[str, Any],
    ) -> None:
        """Fill entry execution + evaluation metrics.

        Sets ``eval_status`` to ``evaluated`` only when the execution model
        actually filled and metrics were produced; otherwise marks the row
        ``suppressed`` with a code in ``suppression_reason`` so downstream
        diagnostics can attribute the missing metric correctly.
        """
        if not forward_bars:
            evaluation.eval_status = "suppressed"
            evaluation.suppression_reason = "no_forward_bars"
            return

        trade_plan = candidate.get("trade_plan") or {}
        structured_entry_price = _safe_positive_float(trade_plan.get("entry_price"))
        if structured_entry_price is not None:
            self._evaluate_structured_trade_plan(
                evaluation=evaluation,
                forward_bars=forward_bars,
                trade_plan=trade_plan,
            )
            return

        tp_pct = trade_plan.get("take_profit")
        sl_pct = trade_plan.get("stop_loss")

        # B1: pass per-board metadata so the resolver picks the right
        # limit-up / limit-down threshold (科创/创业 ±20 %, 北交所 ±30 %,
        # ST ±5 %, 主板 ±10 %). Fields are populated by
        # ``_evaluate_candidates`` from ``InstrumentMaster``.
        exec_result = ExecutionModelResolver.resolve(
            execution_model=execution_model,
            forward_bars=forward_bars,
            take_profit_pct=tp_pct,
            stop_loss_pct=sl_pct,
            code=candidate.get("code"),
            is_st=bool(candidate.get("is_st", False)),
            market=candidate.get("market") or "cn",
            exchange=candidate.get("exchange"),
        )

        evaluation.entry_fill_status = exec_result.fill_status
        evaluation.entry_fill_price = exec_result.fill_price
        evaluation.entry_fill_date = exec_result.fill_date
        evaluation.limit_blocked = exec_result.limit_blocked
        evaluation.gap_adjusted = exec_result.gap_adjusted
        evaluation.ambiguous_intraday_order = exec_result.ambiguous_intraday_order

        if exec_result.exit_fill_price is not None:
            evaluation.exit_fill_price = exec_result.exit_fill_price
            evaluation.exit_fill_date = exec_result.exit_fill_date

        if exec_result.fill_status == "filled" and exec_result.fill_price:
            eval_result = EntrySignalEvaluator.evaluate(
                entry_price=exec_result.fill_price,
                forward_bars=forward_bars,
                take_profit_pct=tp_pct,
                stop_loss_pct=sl_pct,
            )
            evaluation.forward_return_1d = eval_result.forward_return_1d
            evaluation.forward_return_3d = eval_result.forward_return_3d
            evaluation.forward_return_5d = eval_result.forward_return_5d
            evaluation.forward_return_10d = eval_result.forward_return_10d
            evaluation.mae = eval_result.mae
            evaluation.mfe = eval_result.mfe
            evaluation.max_drawdown_from_peak = eval_result.max_drawdown_from_peak
            evaluation.optimal_entry_deviation = eval_result.optimal_entry_deviation
            evaluation.optimal_entry_timing = eval_result.optimal_entry_timing
            evaluation.signal_quality_score = eval_result.signal_quality_score
            evaluation.plan_success = eval_result.plan_success
            evaluation.holding_days = eval_result.holding_days
            evaluation.outcome = eval_result.outcome
            if eval_result.forward_return_5d is not None:
                evaluation.eval_status = "evaluated"
            else:
                # Fill succeeded but window was too short for a 5d return.
                evaluation.eval_status = "suppressed"
                evaluation.suppression_reason = "forward_window_immature"
        else:
            evaluation.eval_status = "suppressed"
            fill_status = exec_result.fill_status or "unknown"
            evaluation.suppression_reason = f"exec_not_filled:{fill_status}"[:64]

    def _evaluate_structured_trade_plan(
        self,
        *,
        evaluation: FiveLayerBacktestEvaluation,
        forward_bars: List[Any],
        trade_plan: Dict[str, Any],
    ) -> None:
        """Replay the frozen decision-time buy/sell prices as an actual trade."""
        result = PlanReplayExecutor.replay(
            trade_plan=trade_plan,
            forward_bars=forward_bars,
        )

        evaluation.planned_entry_price = _safe_positive_float(trade_plan.get("entry_price"))
        evaluation.planned_stop_loss_price = _safe_positive_float(trade_plan.get("stop_loss_price"))
        evaluation.planned_take_profit_price = _safe_positive_float(trade_plan.get("take_profit_price"))
        evaluation.trade_replay_status = result.status

        if result.status != "completed" or result.entry_price is None:
            evaluation.eval_status = "suppressed"
            evaluation.suppression_reason = f"trade_replay:{result.status}"[:64]
            return

        evaluation.actual_entry_price = result.entry_price
        evaluation.actual_entry_date = result.entry_date
        evaluation.actual_exit_price = result.exit_price
        evaluation.actual_exit_date = result.exit_date
        evaluation.exit_reason = result.exit_reason
        evaluation.trade_return_pct = result.trade_return_pct
        evaluation.holding_days = result.holding_days
        evaluation.plan_success = (
            result.trade_return_pct is not None and result.trade_return_pct > 0
        )
        evaluation.outcome = (
            "win"
            if result.trade_return_pct is not None and result.trade_return_pct > 0
            else "loss"
        )
        evaluation.entry_fill_status = "filled"
        evaluation.entry_fill_price = result.entry_price
        evaluation.entry_fill_date = result.entry_date
        evaluation.exit_fill_status = "filled" if result.exit_price is not None else None
        evaluation.exit_fill_price = result.exit_price
        evaluation.exit_fill_date = result.exit_date

        entry_index = result.entry_index or 0
        replay_forward_bars = forward_bars[entry_index:]
        eval_result = EntrySignalEvaluator.evaluate(
            entry_price=result.entry_price,
            forward_bars=replay_forward_bars,
        )
        evaluation.forward_return_1d = eval_result.forward_return_1d
        evaluation.forward_return_3d = eval_result.forward_return_3d
        evaluation.forward_return_5d = eval_result.forward_return_5d
        evaluation.forward_return_10d = eval_result.forward_return_10d
        evaluation.mae = eval_result.mae
        evaluation.mfe = eval_result.mfe
        evaluation.max_drawdown_from_peak = eval_result.max_drawdown_from_peak
        evaluation.optimal_entry_deviation = eval_result.optimal_entry_deviation
        evaluation.optimal_entry_timing = eval_result.optimal_entry_timing
        evaluation.signal_quality_score = eval_result.signal_quality_score
        evaluation.eval_status = "evaluated"

    def _evaluate_observation(
        self,
        evaluation: FiveLayerBacktestEvaluation,
        forward_bars: List[Any],
    ) -> None:
        """Fill observation counterfactual metrics.

        Sets ``eval_status='suppressed'`` with an explicit reason when the
        evaluator cannot produce metrics, instead of silently leaving the row
        in the legacy ``evaluated`` state with no numbers.
        """
        if not forward_bars:
            evaluation.eval_status = "suppressed"
            evaluation.suppression_reason = "no_forward_bars"
            return

        hypothetical_price = forward_bars[0].open if forward_bars else None
        if hypothetical_price is None or hypothetical_price <= 0:
            evaluation.eval_status = "suppressed"
            evaluation.suppression_reason = "invalid_hypothetical_price"
            return

        eval_result = ObservationSignalEvaluator.evaluate(
            hypothetical_entry_price=hypothetical_price,
            forward_bars=forward_bars,
        )
        evaluation.risk_avoided_pct = eval_result.risk_avoided_pct
        evaluation.opportunity_cost_pct = eval_result.opportunity_cost_pct
        evaluation.stage_success = eval_result.stage_success
        evaluation.holding_days = eval_result.holding_days
        evaluation.outcome = eval_result.outcome
        if eval_result.risk_avoided_pct is not None:
            evaluation.eval_status = "evaluated"
        else:
            evaluation.eval_status = "suppressed"
            evaluation.suppression_reason = "observation_metrics_unavailable"

    @staticmethod
    def _build_metrics_payload(
        candidate: Dict[str, Any],
        evaluation: FiveLayerBacktestEvaluation,
        classification: Any,
        forward_meta: Optional[ForwardBarsMeta] = None,
        forward_bars: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        sample_origin = SampleBucketService.resolve_sample_origin(candidate)
        sample_bucket = SampleBucketService.resolve_sample_bucket(
            signal_family=evaluation.signal_family,
            effective_trade_stage=classification.effective_trade_stage,
            entry_maturity=evaluation.snapshot_entry_maturity,
        )
        timing = SampleBucketService.resolve_entry_timing(
            signal_family=evaluation.signal_family,
            entry_fill_status=evaluation.entry_fill_status,
            mae=evaluation.mae,
            mfe=evaluation.mfe,
            forward_return_5d=evaluation.forward_return_5d,
        )
        payload: Dict[str, Any] = {
            "sample_origin": sample_origin,
            "sample_bucket": sample_bucket,
            "effective_trade_stage": classification.effective_trade_stage,
            "ai_overridden": classification.ai_overridden,
            **timing,
        }
        if forward_meta is not None:
            # B5: persist forward-bar quality metadata so analysts can audit
            # why a sample was suppressed (or why it was kept) without
            # re-running the pipeline. Kept under a dedicated key to avoid
            # colliding with the legacy fields above.
            payload["forward_window"] = {
                "requested_window_days": forward_meta.requested_window_days,
                "actual_bar_count": forward_meta.actual_bar_count,
                "actual_span_days": forward_meta.actual_span_days,
                "gap_threshold_days": forward_meta.gap_threshold_days,
                "gap_too_long": forward_meta.gap_too_long,
                "insufficient_bars": forward_meta.insufficient_bars,
                "tolerance_factor": forward_meta.tolerance_factor,
            }
            # B6 (3a): persist the adj_factor_source distribution for each
            # sample so the run-level aggregation can compose data_version
            # without re-querying stock_daily, and so analysts can audit
            # which sub-population is affected by legacy_assume_one rows.
            if forward_bars is not None and forward_bars:
                payload["forward_window"]["adj_factor_sources"] = (
                    _summarise_adj_factor_sources(forward_bars)
                )
        return payload

    # ── Phase 3: Aggregation & Recommendations ──────────────────────────

    def compute_summaries(
        self,
        backtest_run_id: str,
    ) -> List[FiveLayerBacktestGroupSummary]:
        """Compute all group summaries for a completed run.

        Produces overall, single-dimension, and combo summaries with
        stability metrics and sample threshold checks. Uses snapshot
        fields for historical_snapshot mode grouping.
        """
        summaries = self.aggregator.compute_all_summaries(backtest_run_id)

        # Compute ranking effectiveness and update overall summary
        if summaries:
            ranking = RankingEffectivenessCalculator.compute(summaries)
            overall = next(
                (s for s in summaries if s.group_type == "overall"), None,
            )
            if overall is not None:
                aggregatable = get_aggregatable_sample_count(overall)
                # Resolve family-correct grading metrics. The legacy
                # overall.win_rate_pct / overall.profit_factor mix entry's
                # forward_return_5d with observation's risk_avoided_pct, which
                # is non-negative by construction and silently inflates the
                # headline grade. See _resolve_grade_metrics_source() for the
                # full rationale.
                grade_win_rate, grade_profit_factor, grade_stability, grade_source = (
                    _resolve_grade_metrics_source(overall)
                )
                grade_result = SystemGrader.grade_with_reasons(
                    win_rate_pct=grade_win_rate,
                    profit_factor=grade_profit_factor,
                    time_bucket_stability=grade_stability,
                    sample_count=aggregatable,
                    raw_sample_count=overall.sample_count,
                )

                # Merge grade reasons into the existing overall metrics_json
                # (computed by the aggregator) instead of overwriting it,
                # so sample_baseline / threshold_check / family_breakdown
                # remain intact.
                overall_metrics = json.loads(overall.metrics_json) if overall.metrics_json else {}
                overall_metrics["system_grade_breakdown"] = {
                    "grade": grade_result.grade,
                    "raw_grade": grade_result.raw_grade,
                    "downgraded": grade_result.downgraded,
                    "aggregatable_ratio": grade_result.aggregatable_ratio,
                    "reasons": grade_result.reasons,
                    "metric_source": grade_source,
                    "metric_inputs": {
                        "win_rate_pct": grade_win_rate,
                        "profit_factor": grade_profit_factor,
                        "time_bucket_stability": grade_stability,
                    },
                }

                # D1: persist the family-correct ranking view alongside the
                # mixed-legacy alias values so analysts can compare the two
                # without re-running the aggregator. The mixed alias values
                # are stashed inside metrics_json (no schema column for them
                # — they are an audit artifact, not a primary metric).
                overall_metrics["ranking_effectiveness"] = {
                    "family_scope": ranking.family_scope,
                    "leader_pool_win_share": ranking.leader_pool_win_share,
                    "excess_return_pct": ranking.excess_return_pct,
                    "ranking_consistency": ranking.ranking_consistency,
                    "leader_pool_win_share_mixed": ranking.leader_pool_win_share_mixed,
                    "excess_return_pct_mixed": ranking.excess_return_pct_mixed,
                    "metric_sources": sorted({
                        c.metric_source for c in ranking.comparisons
                    }),
                }

                # D2 + D1: keep both the canonical column name and the legacy
                # alias populated so dashboards on either column keep working;
                # both now hold the family-correct value as the authoritative
                # number. The mixed alias values stay in metrics_json for audit.
                self.summary_repo.upsert_summary(
                    backtest_run_id=backtest_run_id,
                    group_type="overall",
                    group_key="all",
                    sample_count=overall.sample_count,
                    leader_pool_win_share=ranking.leader_pool_win_share,
                    top_k_hit_rate=ranking.leader_pool_win_share,
                    excess_return_pct=ranking.excess_return_pct,
                    ranking_consistency=ranking.ranking_consistency,
                    system_grade=grade_result.grade,
                    metrics_json=_dump_json(overall_metrics),
                )

        logger.info("Computed %d summaries for run %s", len(summaries), backtest_run_id)
        return summaries

    def get_ranking_effectiveness(self, backtest_run_id: str):
        """Return ranking effectiveness for an existing run."""
        summaries = self.summary_repo.get_by_run(backtest_run_id)
        if not summaries:
            return None
        return RankingEffectivenessCalculator.compute(summaries)

    def generate_recommendations(
        self,
        backtest_run_id: str,
    ) -> List[FiveLayerBacktestRecommendation]:
        """Generate graded recommendations based on summaries.

        ONLY outputs suggestions. NEVER modifies rules/thresholds/parameters.
        Must be called after compute_summaries().
        """
        return self.recommendation_engine.generate_recommendations(backtest_run_id)

    # ── Phase 3: Calibration comparison ────────────────────────────────

    def run_calibration_comparison(
        self,
        baseline_run_id: str,
        candidate_run_id: str,
        calibration_name: str,
        baseline_config: Optional[Dict[str, Any]] = None,
        candidate_config: Optional[Dict[str, Any]] = None,
    ) -> Optional[FiveLayerBacktestCalibrationOutput]:
        """Compare two completed runs and produce a calibration output.

        Both runs must have overall summaries computed (call compute_summaries
        first). Returns a CalibrationOutput with decision and confidence.
        """
        baseline_summaries = self.summary_repo.get_by_run(
            baseline_run_id, group_type="overall",
        )
        candidate_summaries = self.summary_repo.get_by_run(
            candidate_run_id, group_type="overall",
        )

        if not baseline_summaries or not candidate_summaries:
            logger.warning(
                "Cannot compare runs %s / %s — missing overall summaries",
                baseline_run_id, candidate_run_id,
            )
            return None

        output = CalibrationOutputGenerator.generate(
            backtest_run_id=candidate_run_id,
            calibration_name=calibration_name,
            baseline_summary=baseline_summaries[0],
            candidate_summary=candidate_summaries[0],
            baseline_config=baseline_config or {},
            candidate_config=candidate_config or {},
        )
        return self.calibration_repo.save(output)

    # ── Convenience: full pipeline (run + summaries + recommendations) ─

    def run_full_pipeline(
        self,
        evaluation_mode: str,
        execution_model: str,
        trade_date_from: date,
        trade_date_to: date,
        market: str = "cn",
        eval_window_days: int = _DEFAULT_EVAL_WINDOW,
        generate_recommendations: bool = True,
        **kwargs,
    ) -> Dict[str, Any]:
        """Run backtest → compute summaries → generate recommendations.

        Returns dict with 'run', 'summaries', 'recommendations' keys.
        """
        run = self.run_backtest(
            evaluation_mode=evaluation_mode,
            execution_model=execution_model,
            trade_date_from=trade_date_from,
            trade_date_to=trade_date_to,
            market=market,
            eval_window_days=eval_window_days,
            **kwargs,
        )

        summaries = self.compute_summaries(run.backtest_run_id)

        recommendations = []
        if generate_recommendations and summaries:
            recommendations = self.generate_recommendations(run.backtest_run_id)

        logger.info(
            "Full pipeline complete: run=%s evals=%d summaries=%d recs=%d",
            run.backtest_run_id,
            run.completed_count or 0,
            len(summaries),
            len(recommendations),
        )

        return {
            "run": run,
            "summaries": summaries,
            "recommendations": recommendations,
        }
