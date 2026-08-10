# -*- coding: utf-8 -*-
"""Bounded-memory caches and resumable artifacts for validation replay."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta
from concurrent.futures import ProcessPoolExecutor
import gzip
import hashlib
import multiprocessing
from pathlib import Path
import pickle
from tempfile import TemporaryDirectory
import time
from typing import Any, Callable, Mapping, Optional, Sequence

import pandas as pd

from src.indicators.causal_bottom_divergence_detector import (
    ALGORITHM_VERSION as CAUSAL_ALGORITHM_VERSION,
)
from src.indicators.resistance_zone_detector import (
    ALGORITHM_VERSION as ZONE_ALGORITHM_VERSION,
)

from .bottom_divergence_v2_checkpoint import (  # noqa: F401
    DEFAULT_V1_STRATEGY_PATH,
    DEFAULT_V2_STRATEGY_PATH,
    CanonicalCheckpointStore,
    CheckpointCorruptionError,
    CheckpointMismatchError,
    validation_checkpoint_config_hash,
)
from .bottom_divergence_v2_dataset import iter_query_batches
from .bottom_divergence_v2_report import canonical_json_dumps
from .bottom_divergence_v2_validation import canonical_parameter_hash


FROZEN_EVIDENCE_ALGORITHM_VERSION = (
    f"{CAUSAL_ALGORITHM_VERSION}+{ZONE_ALGORITHM_VERSION}"
)


def _evaluate_factor_task(
    task: tuple[str, Any, pd.DataFrame, Any],
) -> tuple[str, Any, dict[str, Any]]:
    code, config, group, frozen = task
    from src.services.factor_service import FactorService

    service = FactorService(db_manager=object(), config=config)
    if frozen is None:
        frozen = service.freeze_bottom_divergence_v2_evidence(group)
    factors = service.compute_bottom_divergence_v2_factors(
        group,
        frozen_evidence=frozen,
    )
    return code, frozen, factors


def _build_base_factor_task(
    task: tuple[str, Any, pd.DataFrame, dict[str, Any], date],
) -> Optional[dict[str, Any]]:
    code, config, group, info, trade_date = task
    from src.services.factor_service import FactorService

    service = FactorService(db_manager=object(), config=config)
    universe = pd.DataFrame([{"code": code, **info}])
    snapshot = service.build_factor_snapshot_from_groups(
        universe,
        {code: group},
        trade_date=trade_date,
        persist=False,
    )
    return (
        dict(snapshot.iloc[0])
        if not snapshot.empty
        else None
    )


@dataclass(frozen=True)
class FrozenEvidenceCacheKey:
    data_version: str
    code: str
    candidate_version: str
    as_of_index: int
    algorithm_version: str
    config_hash: str
    parameter_hash: Optional[str] = None


class ValidationProgress:
    """Stable elapsed/ETA reporting with a configurable event interval."""

    def __init__(
        self,
        total: int,
        *,
        every: int = 100,
        callback: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        self.total = max(int(total), 0)
        self.every = max(int(every), 1)
        self.callback = callback
        self.completed = 0
        self.started = time.perf_counter()

    def advance(self, amount: int = 1) -> None:
        self.completed += amount
        if (
            self.callback is None
            or (
                self.completed % self.every != 0
                and self.completed < self.total
            )
        ):
            return
        elapsed = time.perf_counter() - self.started
        rate = self.completed / elapsed if elapsed > 0 else 0.0
        remaining = max(self.total - self.completed, 0)
        self.callback({
            "completed": self.completed,
            "total": self.total,
            "elapsed_seconds": round(elapsed, 3),
            "eta_seconds": (
                round(remaining / rate, 3) if rate > 0 else None
            ),
        })


class ValidationFactorCache:
    """Share OHLCV, base factors, and frozen v2 evidence across the grid."""

    def __init__(
        self,
        *,
        data_version: str,
        trade_dates: Sequence[date],
        bar_groups: Mapping[str, pd.DataFrame],
        sql_bar_queries: int,
        progress_every: int = 100,
        progress_callback: Optional[
            Callable[[dict[str, Any]], None]
        ] = None,
        cache_directory: Optional[Path] = None,
        workers: int = 1,
    ) -> None:
        self.data_version = data_version
        self.trade_dates = tuple(sorted(set(trade_dates)))
        self._bar_groups = {
            str(code): self._compact_bar_frame(frame)
            for code, frame in sorted(bar_groups.items())
        }
        self._temporary_directory = (
            TemporaryDirectory(prefix="validation-factor-cache-")
            if cache_directory is None
            else None
        )
        self._cache_directory = (
            Path(self._temporary_directory.name)
            if self._temporary_directory is not None
            else Path(cache_directory)
        )
        self._cache_directory.mkdir(parents=True, exist_ok=True)
        self._frozen: dict[FrozenEvidenceCacheKey, Any] = {}
        self._frozen_lookup: dict[tuple[Any, ...], Any] = {}
        self._evaluated: dict[FrozenEvidenceCacheKey, dict[str, Any]] = {}
        self._active_frozen_date: Optional[date] = None
        self.stats = {
            "sql_bar_queries": sql_bar_queries,
            "base_snapshot_builds": 0,
            "frozen_evidence_builds": 0,
            "parameter_evaluations": 0,
            "parameter_evaluations_by_hash": {},
        }
        self.progress_every = progress_every
        self.progress_callback = progress_callback
        self.workers = max(int(workers), 1)
        self._executor: Optional[ProcessPoolExecutor] = None

    @staticmethod
    def _compact_bar_frame(frame: pd.DataFrame) -> pd.DataFrame:
        compact = frame.sort_values("date").reset_index(drop=True).copy()
        compact = compact.drop(columns=["code"], errors="ignore")
        compact["date"] = pd.to_datetime(compact["date"])
        for field_name in ("data_source", "adj_factor_source"):
            if field_name in compact:
                compact[field_name] = compact[field_name].astype("category")
        return compact

    @classmethod
    def from_groups(
        cls,
        *,
        data_version: str,
        trade_dates: Sequence[date],
        bar_groups: Mapping[str, pd.DataFrame],
        progress_every: int = 100,
        progress_callback: Optional[
            Callable[[dict[str, Any]], None]
        ] = None,
        workers: int = 1,
    ) -> "ValidationFactorCache":
        return cls(
            data_version=data_version,
            trade_dates=trade_dates,
            bar_groups=bar_groups,
            sql_bar_queries=0,
            progress_every=progress_every,
            progress_callback=progress_callback,
            workers=workers,
        )

    @classmethod
    def from_database(
        cls,
        *,
        db_manager: Any,
        data_version: str,
        trade_dates: Sequence[date],
        codes: Sequence[str],
        lookback_days: int,
        progress_every: int = 100,
        progress_callback: Optional[
            Callable[[dict[str, Any]], None]
        ] = None,
        workers: int = 1,
    ) -> "ValidationFactorCache":
        from sqlalchemy import select

        from src.storage import StockDaily

        ordered_dates = tuple(sorted(set(trade_dates)))
        if not ordered_dates:
            raise ValueError("trade_dates must not be empty")
        start = ordered_dates[0] - timedelta(days=lookback_days * 2)
        end = ordered_dates[-1]
        statement = (
            select(*StockDaily.__table__.columns)
            .where(
                StockDaily.code.in_(sorted(set(codes))),
                StockDaily.date >= start,
                StockDaily.date <= end,
            )
            .order_by(StockDaily.code, StockDaily.date)
        )
        groups: dict[str, pd.DataFrame] = {}
        current_code: Optional[str] = None
        current_rows: list[dict[str, Any]] = []

        def flush() -> None:
            nonlocal current_rows
            if current_code is not None and current_rows:
                groups[current_code] = pd.DataFrame(current_rows)
            current_rows = []

        with db_manager.get_session() as session:
            for batch in iter_query_batches(session, statement):
                for row in batch:
                    code = str(row["code"])
                    if current_code is not None and code != current_code:
                        flush()
                    current_code = code
                    row.pop("id", None)
                    current_rows.append(row)
            flush()
        return cls(
            data_version=data_version,
            trade_dates=ordered_dates,
            bar_groups=groups,
            sql_bar_queries=1,
            progress_every=progress_every,
            progress_callback=progress_callback,
            workers=workers,
        )

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    def _worker_pool(self) -> ProcessPoolExecutor:
        if self._executor is None:
            self._executor = ProcessPoolExecutor(
                max_workers=self.workers,
                mp_context=multiprocessing.get_context("spawn"),
            )
        return self._executor

    def _base_path(self, trade_date: date, config_hash: str) -> Path:
        return self._cache_directory / (
            f"base-{trade_date.isoformat()}-{config_hash[:16]}.pkl.gz"
        )

    def _frozen_path(self, trade_date: date) -> Path:
        return self._cache_directory / (
            f"frozen-{trade_date.isoformat()}.pkl.gz"
        )

    def _switch_frozen_partition(self, trade_date: date) -> None:
        if self._active_frozen_date == trade_date:
            return
        if self._active_frozen_date is not None and (
            self._frozen_lookup or self._evaluated
        ):
            with gzip.open(
                self._frozen_path(self._active_frozen_date),
                "wb",
            ) as handle:
                pickle.dump(
                    {
                        "frozen": self._frozen,
                        "lookup": self._frozen_lookup,
                        "evaluated": self._evaluated,
                    },
                    handle,
                    protocol=5,
                )
        self._frozen = {}
        self._frozen_lookup = {}
        self._evaluated = {}
        target = self._frozen_path(trade_date)
        if target.exists():
            with gzip.open(target, "rb") as handle:
                payload = pickle.load(handle)
            self._frozen = payload["frozen"]
            self._frozen_lookup = payload["lookup"]
            self._evaluated = payload["evaluated"]
        self._active_frozen_date = trade_date

    @staticmethod
    def _candidate_versions(frozen: Any) -> tuple[str, ...]:
        payload = frozen.decode_payload()
        versions = tuple(sorted({
            str(item["candidate_version"])
            for item in payload.get("candidate_evidence", ())
        }))
        if versions:
            return versions
        return (f"none:{frozen.content_hash}",)

    @staticmethod
    def _temporary_frozen_key(
        *,
        data_version: str,
        code: str,
        as_of_index: int,
        config_hash: str,
    ) -> tuple[Any, ...]:
        return (
            data_version,
            code,
            as_of_index,
            FROZEN_EVIDENCE_ALGORITHM_VERSION,
            config_hash,
        )

    def _partition_cache_keys(
        self,
        field_name: str,
    ) -> tuple[FrozenEvidenceCacheKey, ...]:
        keys = set(getattr(self, field_name))
        for path in sorted(self._cache_directory.glob("frozen-*.pkl.gz")):
            date_text = path.name[len("frozen-"):-len(".pkl.gz")]
            if (
                self._active_frozen_date is not None
                and date_text == self._active_frozen_date.isoformat()
            ):
                continue
            with gzip.open(path, "rb") as handle:
                payload = pickle.load(handle)
            keys.update(payload[field_name.lstrip("_")])
        return tuple(sorted(keys, key=repr))

    @property
    def frozen_cache_keys(self) -> tuple[FrozenEvidenceCacheKey, ...]:
        return self._partition_cache_keys("_frozen")

    @property
    def evaluation_cache_keys(self) -> tuple[FrozenEvidenceCacheKey, ...]:
        return self._partition_cache_keys("_evaluated")

    @staticmethod
    def _config_hash(config: Any, *, include_grid: bool) -> str:
        payload = asdict(config)
        if not include_grid:
            for field_name in (
                "bottom_divergence_v2_enabled",
                "bottom_divergence_v2_cluster_pct",
                "bottom_divergence_v2_atr_gap_multiplier",
                "bottom_divergence_v2_zone_score_min",
            ):
                payload.pop(field_name, None)
        return hashlib.sha256(
            canonical_json_dumps(payload).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _parameter_hash(config: Any) -> str:
        return canonical_parameter_hash({
            "cluster_pct": config.bottom_divergence_v2_cluster_pct,
            "atr_gap_multiplier": (
                config.bottom_divergence_v2_atr_gap_multiplier
            ),
            "zone_score_min": config.bottom_divergence_v2_zone_score_min,
        })

    def _window(
        self,
        code: str,
        trade_date: date,
        lookback_days: int,
    ) -> pd.DataFrame:
        frame = self._bar_groups.get(code)
        if frame is None:
            return pd.DataFrame()
        start = pd.Timestamp(
            trade_date - timedelta(days=lookback_days * 2)
        )
        end = pd.Timestamp(trade_date)
        mask = (frame["date"] >= start) & (frame["date"] <= end)
        return frame.loc[mask].reset_index(drop=True)

    def build_factor_snapshot(
        self,
        *,
        config: Any,
        universe: pd.DataFrame,
        trade_date: date,
    ) -> pd.DataFrame:
        from src.services.factor_service import FactorService

        codes = sorted(str(code) for code in universe["code"].tolist())
        windows = {
            code: self._window(
                code,
                trade_date,
                config.screening_factor_lookback_days,
            )
            for code in codes
        }
        base_hash = self._config_hash(config, include_grid=False)
        base_path = self._base_path(trade_date, base_hash)
        if base_path.exists():
            base_snapshot = pd.read_pickle(base_path, compression="gzip")
        else:
            base_config = replace(
                config,
                bottom_divergence_v2_enabled=False,
            )
            if self.workers > 1 and len(windows) > 1:
                info_by_code = universe.set_index("code").to_dict("index")
                tasks = [
                    (
                        code,
                        base_config,
                        windows[code],
                        info_by_code.get(code, {}),
                        trade_date,
                    )
                    for code in sorted(windows)
                ]
                rows = [
                    row
                    for row in self._worker_pool().map(
                        _build_base_factor_task,
                        tasks,
                        chunksize=1,
                    )
                    if row is not None
                ]
                base_snapshot = pd.DataFrame(rows)
            else:
                base_service = FactorService(config=base_config)
                base_snapshot = (
                    base_service.build_factor_snapshot_from_groups(
                        universe,
                        windows,
                        trade_date=trade_date,
                        persist=False,
                    )
                )
            base_snapshot = (
                base_snapshot.sort_values("code").reset_index(drop=True)
            )
            base_snapshot.to_pickle(base_path, compression="gzip")
            self.stats["base_snapshot_builds"] += 1
        snapshot = base_snapshot.copy(deep=True)
        if not config.bottom_divergence_v2_enabled:
            return snapshot

        parameter_hash = self._parameter_hash(config)
        self._switch_frozen_partition(trade_date)
        progress = ValidationProgress(
            len(snapshot),
            every=self.progress_every,
            callback=self.progress_callback,
        )
        row_by_code = {
            str(row["code"]): index
            for index, row in snapshot.iterrows()
        }
        tasks = []
        temporary_keys = {}
        ready_results = []
        for code in sorted(row_by_code):
            group = windows[code]
            if len(group) < 60:
                progress.advance()
                continue
            as_of_index = len(group) - 1
            temporary_key = self._temporary_frozen_key(
                data_version=self.data_version,
                code=code,
                as_of_index=as_of_index,
                config_hash=base_hash,
            )
            temporary_keys[code] = temporary_key
            frozen = self._frozen_lookup.get(temporary_key)
            if frozen is not None:
                evaluation_keys = tuple(
                    FrozenEvidenceCacheKey(
                        data_version=self.data_version,
                        code=code,
                        candidate_version=candidate_version,
                        as_of_index=as_of_index,
                        algorithm_version=(
                            FROZEN_EVIDENCE_ALGORITHM_VERSION
                        ),
                        config_hash=base_hash,
                        parameter_hash=parameter_hash,
                    )
                    for candidate_version in self._candidate_versions(frozen)
                )
                cached = self._evaluated.get(evaluation_keys[0])
                if (
                    cached is not None
                    and all(key in self._evaluated for key in evaluation_keys)
                ):
                    ready_results.append((code, frozen, cached, False))
                    continue
            tasks.append((
                code,
                config,
                group,
                frozen,
            ))
        if self.workers > 1 and len(tasks) > 1:
            results = self._worker_pool().map(
                _evaluate_factor_task,
                tasks,
                chunksize=1,
            )
        else:
            results = map(_evaluate_factor_task, tasks)
        computed_results = (
            (code, frozen, factors, True)
            for code, frozen, factors in results
        )
        for code, frozen, factors, was_computed in (
            *ready_results,
            *computed_results,
        ):
            temporary_key = temporary_keys[code]
            as_of_index = temporary_key[2]
            candidate_versions = self._candidate_versions(frozen)
            frozen_keys = tuple(
                FrozenEvidenceCacheKey(
                    data_version=self.data_version,
                    code=code,
                    candidate_version=candidate_version,
                    as_of_index=as_of_index,
                    algorithm_version=FROZEN_EVIDENCE_ALGORITHM_VERSION,
                    config_hash=base_hash,
                )
                for candidate_version in candidate_versions
            )
            if temporary_key not in self._frozen_lookup:
                self._frozen_lookup[temporary_key] = frozen
                for frozen_key in frozen_keys:
                    self._frozen[frozen_key] = frozen
                self.stats["frozen_evidence_builds"] += 1
            if was_computed:
                for frozen_key in frozen_keys:
                    evaluation_key = replace(
                        frozen_key,
                        parameter_hash=parameter_hash,
                    )
                    self._evaluated[evaluation_key] = factors
                self.stats["parameter_evaluations"] += 1
                by_hash = self.stats["parameter_evaluations_by_hash"]
                by_hash[parameter_hash] = by_hash.get(parameter_hash, 0) + 1
            row_index = row_by_code[code]
            for field_name, value in factors.items():
                snapshot.at[row_index, field_name] = value
            progress.advance()
        return snapshot.sort_values("code").reset_index(drop=True)


class CachedValidationFactorService:
    """FactorService-compatible facade backed by a shared validation cache."""

    def __init__(self, config: Any, cache: ValidationFactorCache) -> None:
        self.config = config
        self.cache = cache

    def build_factor_snapshot(
        self,
        universe: pd.DataFrame,
        trade_date: date,
        persist: bool = False,
    ) -> pd.DataFrame:
        if persist:
            raise ValueError("validation factor cache is read-only")
        return self.cache.build_factor_snapshot(
            config=self.config,
            universe=universe,
            trade_date=trade_date,
        )


def _checkpoint_json_value(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _checkpoint_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_checkpoint_json_value(item) for item in value]
    return value


def replay_batch_to_payload(batch: Any) -> dict[str, Any]:
    """Serialize a replay batch into canonical checkpoint-compatible data."""
    return {
        "samples": [
            _checkpoint_json_value(asdict(item)) for item in batch.samples
        ],
        "opportunity_counts": {
            key.isoformat(): value
            for key, value in sorted(batch.opportunity_counts.items())
        },
        "event_evidence": [
            _checkpoint_json_value(asdict(item))
            for item in batch.event_evidence
        ],
    }


def replay_batch_from_payload(payload: Mapping[str, Any]) -> Any:
    """Restore a replay batch without silently mutating selection evidence."""
    from .bottom_divergence_v2_models import (
        CandidateEventEvidence,
        ValidationSample,
    )
    from .bottom_divergence_v2_replay import ReplayBatch

    date_fields = {
        "signal_date",
        "early_event_date",
        "near_cleared_event_date",
        "major_breakout_event_date",
    }
    samples = []
    for raw in payload.get("samples", []):
        item = dict(raw)
        for field_name in date_fields:
            if item.get(field_name):
                item[field_name] = date.fromisoformat(item[field_name])
        item["future_trade_dates_20d"] = tuple(
            date.fromisoformat(value)
            for value in item.get("future_trade_dates_20d", [])
        )
        for field_name in (
            "future_closes_20d",
            "future_highs_20d",
            "future_lows_20d",
        ):
            item[field_name] = tuple(item.get(field_name, []))
        samples.append(ValidationSample(**item))
    evidence = []
    for raw in payload.get("event_evidence", []):
        item = dict(raw)
        for field_name in (
            "near_cleared_event_date",
            "major_breakout_event_date",
        ):
            if item.get(field_name):
                item[field_name] = date.fromisoformat(item[field_name])
        evidence.append(CandidateEventEvidence(**item))
    return ReplayBatch(
        samples=tuple(samples),
        opportunity_counts={
            date.fromisoformat(key): int(value)
            for key, value in payload.get(
                "opportunity_counts",
                {},
            ).items()
        },
        event_evidence=tuple(evidence),
    )
