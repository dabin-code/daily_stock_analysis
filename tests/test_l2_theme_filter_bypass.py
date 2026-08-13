# -*- coding: utf-8 -*-
"""信号研究测量模式：跳过 L2 主线题材收窄，且不与部署模式混淆。

测量模式与部署模式回答的是两个问题——「信号本身有没有预测力」和「部署口径
今天会买什么」——它们的样本量与期望没有可比性。因此这里钉的不只是「开关能
关掉那一层过滤」，还有更重要的一半：**两种模式在任何持久化或报告结果的层面
都必须能被分辨，且证据层缓存不能互相供货**。
"""
from __future__ import annotations

from argparse import Namespace
from dataclasses import fields, replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from scripts import validate_bottom_divergence_v2 as command
from src.backtest.services.bottom_divergence_v2_checkpoint import (
    validation_checkpoint_config_hash,
)
from src.backtest.services.bottom_divergence_v2_cli_service import (
    _apply_signal_research_mode,
)
from src.backtest.services.bottom_divergence_v2_performance import (
    BASE_FACTOR_CONFIG_FIELDS,
    ValidationFactorCache,
)
from src.backtest.services.bottom_divergence_v2_replay import (
    ReplayDependencies,
    replay_historical_dates,
)
from src.backtest.services.bottom_divergence_v2_report import (
    PIPELINE_MODE_DEPLOYED,
    PIPELINE_MODE_SIGNAL_MEASUREMENT,
    _enrich_report,
)
from src.config import Config
from src.schemas.trading_types import (
    MarketEnvironment,
    MarketRegime,
    RiskLevel,
)
from src.services.five_layer_pipeline import (
    L2_FILTER_MODE_MEASUREMENT,
    MIN_THEME_CANDIDATES,
    FiveLayerPipeline,
)
from tests.test_bottom_divergence_v2_performance import _bars

SWITCH = "signal_research_bypass_l2_theme_filter"
TRADE_DATE = date(2026, 3, 2)

# 主线板块成员必须 >= MIN_THEME_CANDIDATES，否则 L2 会走
# `theme_fallback_insufficient_candidates` 而不是真正的收窄，对照组就退化成
# 「两边都是全 universe」，开关的效果无从观察。
_UNIVERSE_SIZE = 30
_THEME_MEMBER_COUNT = MIN_THEME_CANDIDATES + 2


def _codes(count: int) -> list[str]:
    return [f"{600000 + index}" for index in range(count)]


def _run_l2(*, bypass: bool, lock_universe: bool = False) -> dict:
    """跑到 L2 为止：选股返回空，流水线在 L3 之前带着 stats 返回。"""
    snapshot_df = pd.DataFrame([
        {"code": code, "name": f"N{code}"}
        for code in _codes(_UNIVERSE_SIZE)
    ])
    screener = MagicMock()
    screener._context = SimpleNamespace(run_id="l2-bypass-test")
    screener.evaluate.return_value = SimpleNamespace(selected=[], rejected=[])
    db = MagicMock()
    db.batch_get_board_member_codes.return_value = {
        "主线板块": _codes(_THEME_MEMBER_COUNT),
    }
    theme_resolver = MagicMock()
    theme_resolver.identified_themes = []
    theme_resolver.get_main_theme_boards.return_value = {"主线板块"}
    sector_engine = MagicMock()
    sector_engine.compute_all_sectors.return_value = []
    registry = MagicMock()
    registry.is_empty = False
    aggregation = MagicMock()
    aggregation.aggregate.return_value = []

    with (
        patch(
            "src.services.sector_heat_engine.SectorHeatEngine",
            return_value=sector_engine,
        ),
        patch(
            "src.services.theme_mapping_registry.ThemeMappingRegistry",
            return_value=registry,
        ),
        patch(
            "src.services.theme_aggregation_service.ThemeAggregationService",
            return_value=aggregation,
        ),
        patch(
            "src.services.theme_position_resolver.ThemePositionResolver",
            return_value=theme_resolver,
        ),
    ):
        result = FiveLayerPipeline().run(
            snapshot_df=snapshot_df,
            trade_date=TRADE_DATE,
            market_env=MarketEnvironment(
                regime=MarketRegime.BALANCED,
                risk_level=RiskLevel.MEDIUM,
                is_safe=True,
            ),
            guard_result=SimpleNamespace(is_safe=True, message="ok"),
            screener_service=screener,
            candidate_limit=100,
            db_manager=db,
            skill_manager=None,
            lock_universe=lock_universe,
            bypass_theme_filter=bypass,
        )
    stats = dict(result.pipeline_stats)
    # 真正决定选股面的是喂给 screener 的那个 DataFrame，而不是 stats 里的
    # 计数；两者分开断言，stats 写错时不会跟着一起错。
    stats["rows_reaching_screener"] = len(screener.evaluate.call_args[0][0])
    return stats


def test_default_still_shrinks_the_universe_to_the_main_theme() -> None:
    stats = _run_l2(bypass=False)

    assert stats["l2_filter_mode"] == "theme_shrink"
    assert stats["universe_before"] == _UNIVERSE_SIZE
    assert stats["universe_after_l2"] == _THEME_MEMBER_COUNT
    assert stats["rows_reaching_screener"] == _THEME_MEMBER_COUNT


def test_bypass_sends_the_whole_universe_to_the_screener() -> None:
    stats = _run_l2(bypass=True)

    assert stats["universe_after_l2"] == _UNIVERSE_SIZE
    assert stats["rows_reaching_screener"] == _UNIVERSE_SIZE


def test_bypass_mode_label_is_distinct_from_every_deployed_label() -> None:
    """测量模式的 `l2_filter_mode` 不能与任何部署侧取值重合。

    尤其不能复用 `theme_universe_locked`：那个值声称的是「universe 已在上游
    锁定到指定板块」，是部署侧的正常形态。两者同值时，运行自己的统计就会对
    「universe 为什么这么宽」给出错误解释，而这两种口径的数字不可比。
    """
    deployed_labels = {
        "full_universe",
        "theme_shrink",
        "theme_universe_locked",
        "theme_fallback_insufficient_candidates",
        "theme_fallback_no_members",
    }

    assert L2_FILTER_MODE_MEASUREMENT not in deployed_labels
    assert _run_l2(bypass=True)["l2_filter_mode"] == L2_FILTER_MODE_MEASUREMENT


def test_measurement_label_wins_when_the_universe_is_also_locked() -> None:
    """两个开关同时为真时必须报测量模式。

    部署侧的取值一旦出现在测量运行上，这份数字就会被当成可比的；反过来把
    测量运行标成 locked 只是少了一条信息，不会让人误判。所以优先级只能是
    测量模式在前。
    """
    stats = _run_l2(bypass=True, lock_universe=True)

    assert stats["l2_filter_mode"] == L2_FILTER_MODE_MEASUREMENT


def test_replay_forwards_the_switch_from_the_config() -> None:
    """CLI 的开关要真的走到流水线，而不是只停在 config 上。"""
    observed: list[dict] = []
    dependencies = ReplayDependencies(
        db_manager=object(),
        factor_service_factory=lambda _config: SimpleNamespace(
            build_factor_snapshot=lambda *_a, **_k: pd.DataFrame(
                [{"code": "000001", "close": 10.0}]
            ),
        ),
        pipeline=SimpleNamespace(
            run=lambda **kwargs: (
                observed.append(kwargs),
                SimpleNamespace(candidates=[]),
            )[1],
        ),
        screener_factory=lambda _version: (object(), object()),
        market_context_provider=lambda *_args: (object(), object()),
        stock_repository=SimpleNamespace(),
    )

    for bypass in (False, True):
        replay_historical_dates(
            strategy_version="v2",
            config=Config(**{SWITCH: bypass}),
            trade_dates=[TRADE_DATE],
            universe=pd.DataFrame([{"code": "000001"}]),
            dependencies=dependencies,
        )

    assert [item["bypass_theme_filter"] for item in observed] == [False, True]
    assert [item["lock_universe"] for item in observed] == [False, False]


def test_evidence_cache_refuses_to_serve_one_mode_to_the_other() -> None:
    """两种模式的证据层缓存键必须不同，且实测不会互相复用。

    这里不满足于比较两个哈希：哈希不同却仍被复用（例如某个查表路径根本没
    用上 config_hash）会让部署模式的证据被端给测量模式的运行——一个看起来
    正常、实际错掉的答案。所以按同一个 code、同一天连跑两种模式，要求证据
    确实被重算了两次。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-2]["date"]
    universe = pd.DataFrame([{"code": "000001", "name": "A"}])
    deployed = Config(bottom_divergence_v2_enabled=True)
    measurement = replace(deployed, **{SWITCH: True})
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
    )

    assert (
        ValidationFactorCache._config_hash(deployed)
        != ValidationFactorCache._config_hash(measurement)
    )

    for config in (deployed, measurement):
        cache.build_factor_snapshot(
            config=config,
            universe=universe,
            trade_date=trade_date,
        )

    assert cache.stats["frozen_evidence_builds"] == 2
    assert cache.stats["parameter_evaluations"] == 2


def test_base_snapshot_is_shared_because_l2_runs_after_it() -> None:
    """base 快照必须**照旧共享**：L2 在它之后，改不到它。

    这条与上一条方向相反且同样重要。把开关登记进 `BASE_FACTOR_CONFIG_FIELDS`
    会让两种模式各算一遍全市场基础因子——纯粹的算力浪费，还会让
    `tests/test_base_factor_cache_key_whitelist.py` 的白名单语义（「登记的都是
    真正影响 base 的字段」）失真。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-2]["date"]
    universe = pd.DataFrame([{"code": "000001", "name": "A"}])
    deployed = Config(bottom_divergence_v2_enabled=True)
    measurement = replace(deployed, **{SWITCH: True})
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
    )

    assert SWITCH not in BASE_FACTOR_CONFIG_FIELDS
    assert (
        ValidationFactorCache._base_config_hash(deployed)
        == ValidationFactorCache._base_config_hash(measurement)
    )

    for config in (deployed, measurement):
        cache.build_factor_snapshot(
            config=config,
            universe=universe,
            trade_date=trade_date,
        )

    assert cache.stats["base_snapshot_builds"] == 1


def test_checkpoint_identity_separates_the_two_modes() -> None:
    """断点续跑不能跨模式接上。

    `--resume` 会把已保存的 selection 分区直接当本次结果读回；身份哈希不区分
    模式的话，一次部署模式的中断运行可以被一次测量模式的运行「续」完，产出
    一份两种口径拼起来的报告。
    """
    common = {
        "date_from": date(2026, 1, 1),
        "date_to": date(2026, 2, 1),
        "market": "cn",
        "trading_dates": [date(2026, 1, 5)],
        "universe_identity": {"count": 1, "codes_sha256": "abc"},
        "data_version": "dv",
        "costs": {"buy_cost_bps": 2.5},
        "parameter_snapshots": {},
    }
    deployed = Config()
    measurement = replace(deployed, **{SWITCH: True})

    assert (
        validation_checkpoint_config_hash(config=deployed, **common)
        != validation_checkpoint_config_hash(config=measurement, **common)
    )


def _report(config: Config) -> dict:
    return _enrich_report(
        {"eligible": True, "passed": True},
        args=Namespace(
            date_from=date(2026, 1, 1),
            date_to=date(2026, 2, 1),
            market="cn",
        ),
        replay_service=SimpleNamespace(
            data_version=lambda: "dv",
            universe_identity=lambda _universe: {"count": 1},
        ),
        universe=pd.DataFrame([{"code": "000001"}]),
        parameter_snapshots={},
        config=config,
    )


def test_report_states_which_question_the_run_answers() -> None:
    deployed = _report(Config())
    measurement = _report(Config(**{SWITCH: True}))

    assert deployed["pipeline_mode"] == PIPELINE_MODE_DEPLOYED
    assert measurement["pipeline_mode"] == PIPELINE_MODE_SIGNAL_MEASUREMENT


def test_cli_flag_turns_the_switch_on_and_defaults_to_deployed() -> None:
    base = [
        "--date-from", "2026-01-01",
        "--date-to", "2026-02-01",
        "--market", "cn",
        "--output", "report.json",
    ]
    parser = command.build_parser()

    assert parser.parse_args(base).bypass_l2_theme_filter is False
    enabled = parser.parse_args([*base, "--bypass-l2-theme-filter"])
    assert enabled.bypass_l2_theme_filter is True

    off = _apply_signal_research_mode(parser.parse_args(base), Config())
    on = _apply_signal_research_mode(enabled, Config())
    assert getattr(off, SWITCH) is False
    assert getattr(on, SWITCH) is True


def test_absent_flag_never_downgrades_an_env_enabled_config() -> None:
    """flag 只加不减。

    `--bypass-l2-theme-filter` 缺席时把开关按回 False，会让通过环境变量开启
    测量模式的运行静默退回部署口径，而报告仍照实写 deployed——数字与标签一致，
    但操作者拿到的不是他要的那份测量。
    """
    args = Namespace()
    enabled = Config(**{SWITCH: True})

    assert getattr(_apply_signal_research_mode(args, enabled), SWITCH) is True


def test_switch_defaults_to_the_deployed_behaviour() -> None:
    assert getattr(Config(), SWITCH) is False
    assert {item.name for item in fields(Config)} >= {SWITCH}
