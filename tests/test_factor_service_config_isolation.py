# -*- coding: utf-8 -*-
"""base 因子必须只读传入的 config，不得回落到全局单例。

缓存键哈希的是传入的 config（`bottom_divergence_v2_performance.py:385`），
检测器却曾经读 `get_config()`。两者脱钩时，改单例不会让缓存键变化，
缓存会静默返回用另一套参数算出的因子——错的结论，不是慢的结论。
"""
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from src.config import Config
from src.services.factor_service import FactorService


def _bars(code: str, rows: int = 240) -> pd.DataFrame:
    start = date(2024, 1, 1)
    return pd.DataFrame([
        {
            "code": code,
            "date": pd.Timestamp(start + timedelta(days=index)),
            "open": 10.0 + index * 0.01,
            "high": 10.5 + index * 0.01,
            "low": 9.5 + index * 0.01,
            "close": 10.2 + index * 0.01,
            "volume": 1_000_000 + index * 100,
            "amount": 10_000_000.0 + index * 1000,
            # 必须有：build_factor_snapshot_from_groups 在 :229 下标访问它，
            # 缺列直接 KeyError，测试拿不到断言就 error 掉。
            "pct_chg": 0.0,
        }
        for index in range(rows)
    ])


@pytest.mark.parametrize(
    "detector_path, config_field, kwarg_name, probe_value",
    [
        (
            "src.services.factor_service.BottomDivergenceBreakoutDetector",
            "bottom_divergence_max_breakout_gap",
            "max_breakout_gap",
            7,
        ),
        (
            "src.services.factor_service.Low123TrendlineDetector",
            "low123_max_p1_p2_bars",
            "max_p1_p2_bars",
            13,
        ),
    ],
)
def test_base_factor_detectors_read_the_passed_config(
    detector_path, config_field, kwarg_name, probe_value
):
    """探针值只出现在传入的 config 上，全局单例保持默认。

    检测器若回落到单例，收到的就是默认值而非探针值。
    """
    base_config = Config()
    assert getattr(base_config, config_field) != probe_value, (
        "探针值必须与默认值不同，否则这条测试无法区分两个来源"
    )
    probed_config = replace(base_config, **{config_field: probe_value})

    # persist 默认 False，self.db 只在 :246-247 被触碰，因此传哑对象即可；
    # 生产代码 bottom_divergence_v2_performance.py:50,66 本来就传 object()。
    service = FactorService(db_manager=object(), config=probed_config)
    groups = {"000001": _bars("000001")}
    universe = pd.DataFrame([{"code": "000001", "name": "A"}])
    trade_date = groups["000001"].iloc[-1]["date"].date()

    with patch(detector_path) as detector:
        detector.detect.return_value = {"state": "rejected"}
        service.build_factor_snapshot_from_groups(
            universe, groups, trade_date=trade_date
        )

    assert detector.detect.called, "检测器未被调用，测试没有覆盖到目标路径"
    observed = detector.detect.call_args.kwargs.get(kwarg_name)
    assert observed == probe_value, (
        f"检测器拿到 {observed}，而传入 config 要求 {probe_value}；"
        f"说明它读的是全局单例"
    )
