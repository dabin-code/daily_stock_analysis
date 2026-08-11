# -*- coding: utf-8 -*-
"""TUSHARE_QFQ_ENABLED 硬开关测试。

引信语义：默认关闭时 _apply_qfq_adjustment 必须原样返回，且绝不调用 adj_factor。
这条断言比「返回值相同」更重要——买入积分后 adj_factor 会变得可用，
若仍被调用，生产写入语义就会从「不复权」静默翻转为「前复权」。
"""
import os
import unittest
from unittest.mock import MagicMock

import pandas as pd

from src.config import Config


def _make_fetcher():
    """绕过 __init__ 构造，只装配 _apply_qfq_adjustment 实际用到的三个属性。

    走 __new__ 是为了不依赖 Tushare token 与配置初始化。
    """
    from data_provider.tushare_fetcher import TushareFetcher

    fetcher = TushareFetcher.__new__(TushareFetcher)
    fetcher._api = MagicMock()
    fetcher._adj_factor_cooldown_until = 0.0
    fetcher._check_rate_limit = lambda: None
    return fetcher


def _sample_df():
    return pd.DataFrame([
        {"trade_date": "20260810", "open": 10.0, "high": 11.0,
         "low": 9.5, "close": 10.5, "pre_close": 10.0, "pct_chg": 5.0},
    ])


class TushareQfqSwitchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("TUSHARE_QFQ_ENABLED", None)
        Config.reset_instance()

    def tearDown(self) -> None:
        os.environ.pop("TUSHARE_QFQ_ENABLED", None)
        Config.reset_instance()

    def test_disabled_by_default_returns_df_untouched(self) -> None:
        fetcher = _make_fetcher()
        df = _sample_df()

        result = fetcher._apply_qfq_adjustment(df, "000001.SZ", "20260101", "20260810")

        pd.testing.assert_frame_equal(result, df)

    def test_disabled_by_default_never_calls_adj_factor(self) -> None:
        """反例测试：这是引信的核心。adj_factor 一旦被调用，语义就已翻转。"""
        fetcher = _make_fetcher()

        fetcher._apply_qfq_adjustment(_sample_df(), "000001.SZ", "20260101", "20260810")

        fetcher._api.adj_factor.assert_not_called()

    def test_explicitly_enabled_calls_adj_factor(self) -> None:
        os.environ["TUSHARE_QFQ_ENABLED"] = "true"
        Config.reset_instance()
        fetcher = _make_fetcher()
        fetcher._api.adj_factor.return_value = pd.DataFrame([
            {"trade_date": "20260810", "adj_factor": 1.0},
        ])

        fetcher._apply_qfq_adjustment(_sample_df(), "000001.SZ", "20260101", "20260810")

        fetcher._api.adj_factor.assert_called_once()


if __name__ == "__main__":
    unittest.main()
