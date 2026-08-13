# -*- coding: utf-8 -*-
"""TushareFetcher.get_stock_lifecycle 测试：退市样本的抓取入口。"""

import importlib.util
import sys
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

try:
    json_repair_available = importlib.util.find_spec("json_repair") is not None
except ValueError:
    json_repair_available = "json_repair" in sys.modules

if not json_repair_available and "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.tushare_fetcher import TushareFetcher


class TushareStockLifecycleTestCase(unittest.TestCase):
    @staticmethod
    def _make_fetcher() -> TushareFetcher:
        with patch.object(TushareFetcher, "_init_api", return_value=None):
            fetcher = TushareFetcher()
        fetcher._api = MagicMock()
        fetcher.priority = 2
        return fetcher

    @staticmethod
    def _frame() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "ts_code": ["000033.SZ"],
                "symbol": ["000033"],
                "name": ["新都退"],
                "list_date": ["19940103"],
                "delist_date": ["20160526"],
                "list_status": ["D"],
                "market": ["主板"],
            }
        )

    def test_lifecycle_goes_through_rate_limited_wrapper(self) -> None:
        """必须走 _call_api_with_rate_limit。

        自建 ts.pro_api() 会绕过 _patch_api_endpoint 的端点重定向，落回
        api.waditu.com 的 503 与静默空 DataFrame；限频计数也会一起失效。
        """
        fetcher = self._make_fetcher()
        expected = self._frame()

        with patch.object(
            fetcher, "_call_api_with_rate_limit", return_value=expected
        ) as call_mock:
            df = fetcher.get_stock_lifecycle(list_status="D")

        call_mock.assert_called_once()
        args, kwargs = call_mock.call_args
        self.assertEqual(args[0], "stock_basic")
        self.assertEqual(kwargs["exchange"], "")
        self.assertEqual(kwargs["list_status"], "D")
        for field in ("ts_code", "symbol", "name", "list_date", "delist_date",
                      "list_status", "market"):
            self.assertIn(field, kwargs["fields"])
        pd.testing.assert_frame_equal(df, expected)

    def test_lifecycle_defaults_to_listed_status(self) -> None:
        fetcher = self._make_fetcher()

        with patch.object(
            fetcher, "_call_api_with_rate_limit", return_value=self._frame()
        ) as call_mock:
            fetcher.get_stock_lifecycle()

        self.assertEqual(call_mock.call_args.kwargs["list_status"], "L")

    def test_lifecycle_returns_none_without_token(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api = None

        self.assertIsNone(fetcher.get_stock_lifecycle())

    def test_lifecycle_returns_none_on_api_error(self) -> None:
        """限频/无权限时返回 None，由调用方决定是否 fail-closed。

        tushare 客户端对限频是**抛异常**（`抱歉，您访问接口(stock_basic)频率超限`
        / `您每小时最多访问该接口1次`），所以限频落在这条分支里，仍然是 None。
        """
        fetcher = self._make_fetcher()

        with patch.object(
            fetcher,
            "_call_api_with_rate_limit",
            side_effect=RuntimeError("抱歉，您每小时最多访问该接口1次"),
        ):
            self.assertIsNone(fetcher.get_stock_lifecycle(list_status="D"))

    def test_lifecycle_returns_empty_frame_instead_of_none_when_source_has_no_rows(self) -> None:
        """空结果必须原样返回空 DataFrame，不能折叠成 None。

        折叠成 None 之后，调用方再也分不清「抓取失败」和「该状态确实没有证券」。
        'P'（暂停上市）在现行退市规则下已基本不再使用，全市场返回 0 行是合法结果；
        把它当失败会让已经抓成功的 L / D 一起作废，而 stock_basic 免费额度约每
        小时 1 次，等于白烧三个额度窗口。
        """
        fetcher = self._make_fetcher()
        empty = self._frame().iloc[0:0]

        with patch.object(
            fetcher, "_call_api_with_rate_limit", return_value=empty
        ):
            df = fetcher.get_stock_lifecycle(list_status="P")

        self.assertIsNotNone(df, "空结果不能返回 None，否则与抓取失败无法区分")
        self.assertTrue(df.empty)


if __name__ == "__main__":
    unittest.main()
