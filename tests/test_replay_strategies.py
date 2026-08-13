# -*- coding: utf-8 -*-
"""重放策略描述符与注册表的契约。

这些用例钉的是「加一条策略要填什么、填不出来会怎样」，而不是 v1/v2 的具体数值：
后者由 `test_bottom_divergence_v2_validation.py` 与整趟重放的逐字节比对覆盖。
"""
from __future__ import annotations

import pickle

import pytest

from src.backtest.services import replay_strategies
from src.backtest.services.replay_strategies import (
    BOTTOM_DIVERGENCE_V1,
    BOTTOM_DIVERGENCE_V2,
    EvidenceHooks,
    ReplayStrategy,
    evidence_strategy_for,
    resolve_strategy,
)
from src.backtest.services.bottom_divergence_v2_replay import (
    build_isolated_config,
)
from src.config import Config


@pytest.fixture
def temporary_strategy():
    """注册一条只在单个用例里存在的策略。

    注册表是模块级单例，留下残留会让后续用例看到一条不存在的策略。
    """
    registered: list[str] = []

    def register(strategy: ReplayStrategy) -> ReplayStrategy:
        replay_strategies.register_strategy(strategy)
        registered.append(strategy.name)
        return strategy

    try:
        yield register
    finally:
        for name in registered:
            replay_strategies._REGISTRY.pop(name, None)


@pytest.mark.unit
def test_an_unknown_strategy_name_fails_instead_of_falling_back() -> None:
    """名字对不上时必须报错，不能落到某条策略上。

    改造前这里是 `V1_STRATEGY_PATH if version == "v1" else V2_STRATEGY_PATH`：
    任何拼错的名字都会**静默**跑成 v2，报告照常产出，只是跑的不是要跑的策略。
    """
    with pytest.raises(ValueError) as excinfo:
        resolve_strategy("v2 ")

    assert "v2 " in str(excinfo.value)


@pytest.mark.unit
def test_v1_declares_no_grid_no_flag_and_no_evidence_layer() -> None:
    """v1 在三个层次上都没有对应物，描述符必须如实留空。

    给它补一个「v1 版」的网格或证据层不会让引擎更通用，只会让下一条策略
    照着抄一份不存在的东西。
    """
    assert BOTTOM_DIVERGENCE_V1.grid_fields == {}
    assert BOTTOM_DIVERGENCE_V1.enabled_field is None
    assert BOTTOM_DIVERGENCE_V1.evidence is None
    assert BOTTOM_DIVERGENCE_V1.matures_events is False


@pytest.mark.unit
def test_isolated_config_only_touches_the_declared_grid() -> None:
    """网格覆盖面来自描述符：不参与网格的策略一个字段都不该被改。"""
    base = Config(
        bottom_divergence_v2_enabled=False,
        bottom_divergence_v2_cluster_pct=0.015,
    )

    isolated = build_isolated_config(
        base,
        {"cluster_pct": 0.02, "atr_gap_multiplier": 0.75, "zone_score_min": 0.5},
        BOTTOM_DIVERGENCE_V1,
    )

    assert isolated == base
    assert isolated.bottom_divergence_v2_enabled is False
    assert isolated.bottom_divergence_v2_cluster_pct == 0.015


@pytest.mark.unit
def test_evidence_layer_is_chosen_by_the_strategy_that_claims_the_config(
) -> None:
    v2_config = Config(bottom_divergence_v2_enabled=True)
    v1_config = Config(bottom_divergence_v2_enabled=False)

    assert evidence_strategy_for(v2_config) is BOTTOM_DIVERGENCE_V2
    assert evidence_strategy_for(v1_config) is None


@pytest.mark.unit
def test_two_enabled_evidence_layers_refuse_to_guess(
    temporary_strategy,
) -> None:
    """两条带证据层的策略同时打开时必须报错。

    因子缓存只能从配置反查该跑哪层证据。猜一条出来的后果不是跑错策略而已：
    证据会按参数哈希落进缓存，之后每条 leg 都会读到另一条策略算出的证据。
    """
    temporary_strategy(ReplayStrategy(
        name="test-shadow-v2",
        strategy_path=BOTTOM_DIVERGENCE_V2.strategy_path,
        event_context=BOTTOM_DIVERGENCE_V2.event_context,
        breakout_floor=BOTTOM_DIVERGENCE_V2.breakout_floor,
        enabled_field="bottom_divergence_v2_enabled",
        evidence=BOTTOM_DIVERGENCE_V2.evidence,
    ))

    with pytest.raises(ValueError) as excinfo:
        evidence_strategy_for(Config(bottom_divergence_v2_enabled=True))

    assert "test-shadow-v2" in str(excinfo.value)


@pytest.mark.unit
def test_evidence_hooks_survive_the_trip_to_a_worker_process() -> None:
    """证据回调要被 pickle 送进 spawn 出来的 worker。

    写成 lambda 或闭包在单进程（`workers=1`）下完全正常，只有并行那条路会炸，
    而并行才是实际跑法。
    """
    hooks = BOTTOM_DIVERGENCE_V2.evidence
    restored = pickle.loads(pickle.dumps(hooks))

    assert isinstance(restored, EvidenceHooks)
    assert restored.freeze is hooks.freeze
    assert restored.compute is hooks.compute


@pytest.mark.unit
def test_major_zone_floor_follows_the_declared_stage_mapping() -> None:
    """突破下沿取哪个区，由阶段到事件键的映射决定，不另写一份阶段清单。"""
    factor = {
        "bottom_divergence_v2_near_zone_lower": 37.46,
        "bottom_divergence_v2_major_zone_lower": 40.61,
    }
    major_stages = sorted(
        label
        for label, event_key in BOTTOM_DIVERGENCE_V2.stage_events.items()
        if event_key == replay_strategies.EVENT_R2
    )

    assert major_stages == ["major", "major_actionable", "r2"]
    for stage in major_stages:
        assert BOTTOM_DIVERGENCE_V2.breakout_floor(
            BOTTOM_DIVERGENCE_V2, factor, stage
        ) == 40.61
    for stage in ("early", "near", "near_cleared", "r1", "Major"):
        assert BOTTOM_DIVERGENCE_V2.breakout_floor(
            BOTTOM_DIVERGENCE_V2, factor, stage
        ) == 37.46
