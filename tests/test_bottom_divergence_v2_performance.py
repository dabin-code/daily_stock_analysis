# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.backtest.services.bottom_divergence_v2_performance import (
    CanonicalCheckpointStore,
    CheckpointCorruptionError,
    CheckpointMismatchError,
    FrozenEvidenceCacheKey,
    ValidationFactorCache,
    replay_batch_from_payload,
    replay_batch_to_payload,
    validation_checkpoint_config_hash,
)
from src.backtest.services.bottom_divergence_v2_metrics import (
    summarize_validation_samples,
)
from src.backtest.services.bottom_divergence_v2_models import (
    CandidateEventEvidence,
    ValidationSample,
    fit_tertile_boundaries,
)
from src.backtest.services.bottom_divergence_v2_replay import (
    ReplayBatch,
    ReplayDependencies,
    _event_dates,
    replay_historical_dates,
)
from src.backtest.services.bottom_divergence_v2_report import (
    canonical_json_dumps,
)
from src.config import Config
from src.services.factor_service import FactorService


def _bars(code: str, size: int = 170) -> pd.DataFrame:
    rng = np.random.RandomState(sum(ord(item) for item in code))
    close = np.linspace(30.0, 38.0, size) + rng.normal(0, 0.8, size)
    return pd.DataFrame({
        "code": code,
        "date": pd.bdate_range("2025-01-01", periods=size).date,
        "open": close - 0.2,
        "high": close + 0.8,
        "low": close - 0.8,
        "close": close,
        "volume": rng.randint(100_000, 500_000, size),
        "amount": close * rng.randint(100_000, 500_000, size),
        "pct_chg": 0.0,
        "data_source": "fixture",
        "adj_factor": 1.0,
        "adj_factor_source": "tushare_native",
    })


def test_factor_cache_matches_uncached_results_and_isolates_parameter_hash():
    groups = {
        "000002": _bars("000002"),
        "000001": _bars("000001"),
    }
    trade_date = groups["000001"].iloc[-2]["date"]
    universe = pd.DataFrame([
        {"code": "000002", "name": "B"},
        {"code": "000001", "name": "A"},
    ])
    base = Config(bottom_divergence_v2_enabled=True)
    first = replace(
        base,
        bottom_divergence_v2_cluster_pct=0.01,
        bottom_divergence_v2_zone_score_min=0.4,
    )
    second = replace(
        base,
        bottom_divergence_v2_cluster_pct=0.02,
        bottom_divergence_v2_zone_score_min=0.5,
    )
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
    )

    for config in (first, second):
        expected = FactorService(
            config=config,
        ).build_factor_snapshot_from_groups(
            universe,
            {
                code: frame[frame["date"] <= trade_date]
                for code, frame in groups.items()
            },
            trade_date=trade_date,
        ).sort_values("code").reset_index(drop=True)
        actual = cache.build_factor_snapshot(
            config=config,
            universe=universe,
            trade_date=trade_date,
        )
        pd.testing.assert_frame_equal(actual, expected)

    assert cache.stats["base_snapshot_builds"] == 1
    assert cache.stats["frozen_evidence_builds"] == 2
    assert cache.stats["parameter_evaluations"] == 4
    assert cache.stats["sql_bar_queries"] == 0
    assert list(actual["code"]) == ["000001", "000002"]


def test_cache_keys_isolate_data_candidate_asof_algorithm_and_parameter():
    group = _bars("000001")
    dates = (group.iloc[-2]["date"], group.iloc[-1]["date"])
    universe = pd.DataFrame([{"code": "000001", "name": "A"}])
    first = Config(
        bottom_divergence_v2_enabled=True,
        bottom_divergence_v2_zone_score_min=0.4,
    )
    second = replace(
        first,
        bottom_divergence_v2_zone_score_min=0.5,
    )
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=dates,
        bar_groups={"000001": group},
    )
    for trade_date in dates:
        for config in (first, second):
            cache.build_factor_snapshot(
                config=config,
                universe=universe,
                trade_date=trade_date,
            )

    frozen_keys = cache.frozen_cache_keys
    evaluation_keys = cache.evaluation_cache_keys
    assert frozen_keys
    assert evaluation_keys
    assert all(isinstance(key, FrozenEvidenceCacheKey) for key in frozen_keys)
    assert all(key.data_version == "data-a" for key in frozen_keys)
    assert all(key.code == "000001" for key in frozen_keys)
    assert all(key.candidate_version != "frozen" for key in frozen_keys)
    assert all(key.algorithm_version for key in frozen_keys)
    assert {key.as_of_index for key in frozen_keys} == {
        len(group[group["date"] <= item]) - 1 for item in dates
    }
    assert all(key.parameter_hash is None for key in frozen_keys)
    assert len({key.parameter_hash for key in evaluation_keys}) == 2
    assert {
        (
            key.data_version,
            key.code,
            key.candidate_version,
            key.as_of_index,
            key.algorithm_version,
        )
        for key in evaluation_keys
    } == {
        (
            key.data_version,
            key.code,
            key.candidate_version,
            key.as_of_index,
            key.algorithm_version,
        )
        for key in frozen_keys
    }


def test_factor_cache_does_not_read_bars_after_trade_date():
    group = _bars("000001")
    trade_date = group.iloc[-5]["date"]
    universe = pd.DataFrame([{"code": "000001", "name": "A"}])
    config = Config(bottom_divergence_v2_enabled=True)
    expected_cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups={"000001": group},
    )
    changed = group.copy()
    changed.loc[changed["date"] > trade_date, "close"] = 9999.0
    changed_cache = ValidationFactorCache.from_groups(
        data_version="data-b",
        trade_dates=(trade_date,),
        bar_groups={"000001": changed},
    )

    expected = expected_cache.build_factor_snapshot(
        config=config,
        universe=universe,
        trade_date=trade_date,
    )
    actual = changed_cache.build_factor_snapshot(
        config=config,
        universe=universe,
        trade_date=trade_date,
    )
    pd.testing.assert_frame_equal(actual, expected)


@pytest.mark.skipif(
    __import__("sys").platform != "win32",
    reason="Windows spawn contract",
)
def test_factor_cache_windows_spawn_workers_keep_stable_order():
    groups = {
        "000002": _bars("000002", 100),
        "000001": _bars("000001", 100),
    }
    trade_date = groups["000001"].iloc[-1]["date"]
    universe = pd.DataFrame([
        {"code": "000002", "name": "B"},
        {"code": "000001", "name": "A"},
    ])
    config = Config(bottom_divergence_v2_enabled=True)
    serial_cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
        workers=1,
    )
    parallel_cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
        workers=12,
    )
    serial = serial_cache.build_factor_snapshot(
        config=config,
        universe=universe,
        trade_date=trade_date,
    )
    parallel = parallel_cache.build_factor_snapshot(
        config=config,
        universe=universe,
        trade_date=trade_date,
    )
    serial_cache.close()
    parallel_cache.close()

    pd.testing.assert_frame_equal(parallel, serial)
    assert list(parallel["code"]) == ["000001", "000002"]


def test_progress_on_and_off_produce_identical_canonical_results():
    groups = {
        "000002": _bars("000002", 100),
        "000001": _bars("000001", 100),
    }
    trade_date = groups["000001"].iloc[-1]["date"]
    universe = pd.DataFrame([
        {"code": "000002", "name": "B"},
        {"code": "000001", "name": "A"},
    ])
    config = Config(bottom_divergence_v2_enabled=True)
    events = []
    silent_cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
        progress_callback=None,
    )
    verbose_cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
        progress_every=1,
        progress_callback=events.append,
    )
    silent = silent_cache.build_factor_snapshot(
        config=config,
        universe=universe,
        trade_date=trade_date,
    )
    verbose = verbose_cache.build_factor_snapshot(
        config=config,
        universe=universe,
        trade_date=trade_date,
    )

    pd.testing.assert_frame_equal(verbose, silent)
    assert events
    assert events[-1]["completed"] == events[-1]["total"] == 2


def test_001337_nonzero_fixture_is_field_equivalent_through_cache_and_resume():
    fixture = pd.read_csv(
        Path(__file__).parent
        / "fixtures"
        / "001337_bottom_divergence_20251201_20260805.csv",
        parse_dates=["date"],
    )
    fixture["code"] = "001337"
    fixture["amount"] = fixture["close"] * fixture["volume"]
    trade_dates = (
        date(2026, 7, 22),
        date(2026, 7, 23),
        date(2026, 8, 5),
    )
    universe = pd.DataFrame([{"code": "001337", "name": "四川黄金"}])
    config = Config(
        bottom_divergence_v2_enabled=True,
        screening_factor_lookback_days=365,
    )
    cache = ValidationFactorCache.from_groups(
        data_version="001337-fixture-v1",
        trade_dates=trade_dates,
        bar_groups={"001337": fixture},
    )
    records_by_date = {}
    for trade_date in trade_dates:
        visible = fixture[fixture["date"].dt.date <= trade_date]
        baseline = FactorService(
            config=config,
        ).build_factor_snapshot_from_groups(
            universe,
            {"001337": visible},
            trade_date=trade_date,
        )
        optimized = cache.build_factor_snapshot(
            config=config,
            universe=universe,
            trade_date=trade_date,
        )
        pd.testing.assert_frame_equal(
            optimized,
            baseline,
            check_dtype=False,
        )
        records = optimized.iloc[0][
            "bottom_divergence_v2_candidate_records"
        ]
        assert records
        records_by_date[trade_date] = records

    final_record = records_by_date[trade_dates[-1]][0]
    candidate_version = final_record["candidate_version"]
    future_dates = tuple(
        trade_dates[-1] + timedelta(days=index)
        for index in range(1, 21)
    )
    sample = ValidationSample(
        code="001337",
        signal_date=trade_dates[0],
        candidate_version=candidate_version,
        strategy_version="v2",
        stage="early",
        entry_close=36.60,
        near_zone_lower=37.46,
        major_zone_lower=40.61,
        early_event_date=trade_dates[0],
        near_cleared_event_date=trade_dates[1],
        major_breakout_event_date=trade_dates[2],
        close_5d=40.0,
        close_10d=42.0,
        close_20d=43.0,
        future_closes_20d=(40.0,) * 20,
        future_highs_20d=(44.0,) * 20,
        future_lows_20d=(35.0,) * 20,
        max_high_20d=44.0,
        min_low_20d=35.0,
        market_regime="balanced",
        volatility=0.02,
        liquidity=10_000_000.0,
        future_trade_dates_20d=future_dates,
        breakout_floor=37.46,
        position_weight=0.2,
    )
    evidence = CandidateEventEvidence(
        code="001337",
        candidate_version=candidate_version,
        near_cleared_event_date=trade_dates[1],
        major_breakout_event_date=trade_dates[2],
    )
    baseline_batch = ReplayBatch(
        samples=(sample,),
        opportunity_counts={trade_dates[0]: 1},
        event_evidence=(evidence,),
    )
    payload = replay_batch_to_payload(baseline_batch)
    restored = replay_batch_from_payload(payload)
    assert [asdict(item) for item in restored.samples] == [
        asdict(item) for item in baseline_batch.samples
    ]
    assert [asdict(item) for item in restored.event_evidence] == [
        asdict(item) for item in baseline_batch.event_evidence
    ]
    assert restored.opportunity_counts == baseline_batch.opportunity_counts
    boundaries = fit_tertile_boundaries((sample,))
    metric_kwargs = {
        "boundaries": boundaries,
        "opportunity_count": 1,
        "buy_cost_bps": 1.0,
        "sell_cost_bps": 1.0,
        "slippage_bps": 1.0,
    }
    baseline_metrics = summarize_validation_samples(
        baseline_batch.samples,
        **metric_kwargs,
    )
    restored_metrics = summarize_validation_samples(
        restored.samples,
        **metric_kwargs,
    )
    assert restored_metrics == baseline_metrics
    assert baseline_metrics["early"]["sample_count"] == 1
    canonical = canonical_json_dumps(payload)
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_event_dates_use_frozen_candidate_record_not_dataframe_index_dtype():
    version = "candidate-a"

    class ForbiddenRepository:
        def get_range(self, *_args, **_kwargs):
            raise AssertionError("frozen candidate dates must be authoritative")

    result = _event_dates(
        factor={
            "bottom_divergence_v2_candidate_version": version,
            "bottom_divergence_v2_early_event_index": 101.0,
            "bottom_divergence_v2_near_event_index": 102.0,
            "bottom_divergence_v2_major_event_index": 111.0,
            "bottom_divergence_v2_candidate_records": [{
                "candidate_version": version,
                "early_reversal": {"date": "2026-07-22"},
                "near_zone_events": {
                    "cleared_confirmed": {"date": "2026-07-23"},
                },
                "major_zone_breakout": {
                    "date": "2026-08-05",
                    "confirmed": True,
                },
            }],
        },
        code="001337",
        signal_date=date(2026, 8, 5),
        config=Config(bottom_divergence_v2_enabled=True),
        stock_repository=ForbiddenRepository(),
    )
    assert result == {
        "early": date(2026, 7, 22),
        "r1": date(2026, 7, 23),
        "r2": date(2026, 8, 5),
    }


def test_unconfirmed_major_bar_is_not_an_r2_event():
    version = "candidate-a"

    class ForbiddenRepository:
        def get_range(self, *_args, **_kwargs):
            raise AssertionError("frozen candidate dates must be authoritative")

    result = _event_dates(
        factor={
            "bottom_divergence_v2_candidate_version": version,
            "bottom_divergence_v2_candidate_records": [{
                "candidate_version": version,
                "early_reversal": {"date": "2026-07-22"},
                "near_zone_events": {
                    "cleared_confirmed": {"date": "2026-07-23"},
                },
                "major_zone_breakout": {
                    "date": "2026-08-05",
                    "bar_index": 164,
                    "confirmed": False,
                },
            }],
        },
        code="001337",
        signal_date=date(2026, 8, 5),
        config=Config(bottom_divergence_v2_enabled=True),
        stock_repository=ForbiddenRepository(),
    )
    assert result == {
        "early": date(2026, 7, 22),
        "r1": date(2026, 7, 23),
        "r2": None,
    }


@pytest.mark.parametrize(
    ("confirmed", "expected_samples", "expected_event"),
    [
        (False, 0, None),
        (True, 1, date(2026, 8, 5)),
    ],
)
def test_major_confirmation_controls_replay_sample_and_event_evidence(
    confirmed,
    expected_samples,
    expected_event,
):
    signal_date = date(2026, 8, 5)
    version = "candidate-a"
    factor = {
        "code": "001337",
        "close": 43.27,
        "bottom_divergence_v2_stage": "major",
        "bottom_divergence_v2_candidate_version": version,
        "bottom_divergence_v2_near_zone_lower": 37.46,
        "bottom_divergence_v2_major_zone_lower": 40.61,
        "bottom_divergence_v2_candidate_records": [{
            "candidate_version": version,
            "early_reversal": {"date": "2026-07-22"},
            "near_zone_events": {
                "cleared_confirmed": {"date": "2026-07-23"},
            },
            "major_zone_breakout": {
                "date": signal_date.isoformat(),
                "bar_index": 164,
                "confirmed": confirmed,
            },
        }],
    }
    candidate = __import__("types").SimpleNamespace(
        code="001337",
        factor_snapshot=factor,
        trade_stage="add_on_strength",
        trade_plan_json=json.dumps({"initial_position": "目标仓位100%"}),
        market_regime="balanced",
    )
    bars = [
        __import__("types").SimpleNamespace(
            date=signal_date + timedelta(days=index),
            close=44.0,
            high=45.0,
            low=42.0,
            amount=10_000_000.0,
        )
        for index in range(1, 21)
    ]
    repository = __import__("types").SimpleNamespace(
        get_range=lambda *_args, **_kwargs: (),
        get_forward_bars=lambda **_kwargs: bars,
        get_prior_bars=lambda **_kwargs: bars,
    )
    dependencies = ReplayDependencies(
        db_manager=object(),
        factor_service_factory=lambda _config: __import__(
            "types"
        ).SimpleNamespace(
            build_factor_snapshot=lambda *_args, **_kwargs: pd.DataFrame(
                [factor]
            ),
        ),
        pipeline=__import__("types").SimpleNamespace(
            run=lambda **_kwargs: __import__("types").SimpleNamespace(
                candidates=[candidate]
            ),
        ),
        screener_factory=lambda _version: (object(), object()),
        market_context_provider=lambda *_args: (object(), object()),
        stock_repository=repository,
    )

    batch = replay_historical_dates(
        strategy_version="v2",
        config=Config(bottom_divergence_v2_enabled=True),
        trade_dates=(signal_date,),
        universe=pd.DataFrame([{"code": "001337"}]),
        dependencies=dependencies,
    )

    assert len(batch.samples) == expected_samples
    assert batch.event_evidence[0].major_breakout_event_date == expected_event


def test_checkpoint_is_atomic_resumable_and_rejects_identity_mismatch(
    tmp_path,
):
    path = tmp_path / "checkpoint.json"
    store = CanonicalCheckpointStore(
        path,
        data_version="data-a",
        config_hash="config-a",
    )
    store.save_partition(
        parameter_hash="param-a",
        partition="train",
        payload={"codes": ["000002", "000001"]},
    )

    assert not path.with_suffix(".json.tmp").exists()
    assert store.completed_partitions("param-a") == ("train",)
    assert store.load_partition("param-a", "train") == {
        "codes": ["000002", "000001"]
    }
    json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(CheckpointMismatchError):
        CanonicalCheckpointStore(
            path,
            data_version="data-b",
            config_hash="config-a",
        )


def test_checkpoint_identity_covers_complete_config_and_yaml(tmp_path):
    v1_path = tmp_path / "v1.yaml"
    v2_path = tmp_path / "v2.yaml"
    v1_path.write_text("version: v1\n", encoding="utf-8")
    v2_path.write_text("version: v2\n", encoding="utf-8")
    config = Config()
    kwargs = {
        "config": config,
        "date_from": "2026-01-01",
        "date_to": "2026-06-30",
        "market": "cn",
        "trading_dates": ["2026-01-05"],
        "universe_identity": {"codes": ["000001"], "count": 1},
        "data_version": "data-a",
        "costs": {
            "buy_cost_bps": 1.0,
            "sell_cost_bps": 1.0,
            "slippage_bps": 1.0,
        },
        "parameter_snapshots": {"hash-a": {"cluster_pct": 0.01}},
        "v1_strategy_path": v1_path,
        "v2_strategy_path": v2_path,
    }
    expected = validation_checkpoint_config_hash(**kwargs)
    assert validation_checkpoint_config_hash(**kwargs) == expected
    for field_name, value in (
        ("bottom_divergence_v2_sync_window", 4),
        ("bottom_divergence_v2_retention_bars", 21),
        ("bottom_divergence_v2_r1_weights", (0.1, 0.2, 0.7)),
        ("bottom_divergence_v2_r2_weights", (0.2, 0.3, 0.5)),
        ("bottom_divergence_v2_zone_score_min", 0.51),
    ):
        changed = dict(kwargs)
        changed["config"] = replace(config, **{field_name: value})
        assert validation_checkpoint_config_hash(**changed) != expected
    v2_path.write_text("version: v2-changed\n", encoding="utf-8")
    assert validation_checkpoint_config_hash(**kwargs) != expected
    changed = dict(kwargs, data_version="data-b")
    assert validation_checkpoint_config_hash(**changed) != expected


def test_checkpoint_recovers_valid_atomic_temp_after_main_corruption(tmp_path):
    path = tmp_path / "checkpoint.json"
    store = CanonicalCheckpointStore(
        path,
        data_version="data-a",
        config_hash="config-a",
    )
    store.save_partition(
        parameter_hash="param-a",
        partition="selection",
        payload={"samples": [1]},
    )
    valid = path.read_text(encoding="utf-8")
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(valid, encoding="utf-8")
    path.write_text("{corrupted", encoding="utf-8")

    recovered = CanonicalCheckpointStore(
        path,
        data_version="data-a",
        config_hash="config-a",
    )
    assert recovered.load_partition("param-a", "selection") == {
        "samples": [1]
    }
    assert not temporary.exists()
    json.loads(path.read_text(encoding="utf-8"))

    path.write_text("{corrupted", encoding="utf-8")
    with pytest.raises(CheckpointCorruptionError):
        CanonicalCheckpointStore(
            path,
            data_version="data-a",
            config_hash="config-a",
        )
