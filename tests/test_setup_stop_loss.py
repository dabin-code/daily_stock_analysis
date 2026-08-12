# -*- coding: utf-8 -*-
"""setup 级止损解析的单一实现测试。

这是回测链路的根因修复：has_stop_loss 从未写入快照，导致除
BOTTOM_DIVERGENCE_LAYERED_ENTRY 外的所有 setup 永远拿不到止损，
L5 永远停在 focus，交易计划恒为空。
"""
import unittest

from src.schemas.trading_types import SetupType
from src.services.setup_stop_loss import resolve_setup_stop_loss


class SetupStopLossTestCase(unittest.TestCase):
    def test_low123_breakout_reads_pattern_stop(self) -> None:
        snapshot = {"close": 50.0, "pattern_123_stop_loss": 45.5}
        self.assertAlmostEqual(
            resolve_setup_stop_loss(SetupType.LOW123_BREAKOUT, snapshot), 45.5
        )

    def test_bottom_divergence_breakout_falls_back_to_exit_plan(self) -> None:
        snapshot = {
            "close": 50.0,
            "bottom_divergence_stop_loss": None,
            "bottom_divergence_exit_plan": {"initial_stop_loss": 44.0},
        }
        self.assertAlmostEqual(
            resolve_setup_stop_loss(SetupType.BOTTOM_DIVERGENCE_BREAKOUT, snapshot), 44.0
        )

    def test_trend_pullback_reads_shrink_pullback_stop(self) -> None:
        snapshot = {"close": 50.0, "shrink_pullback_stop_loss_price": 47.2}
        self.assertAlmostEqual(
            resolve_setup_stop_loss(SetupType.TREND_PULLBACK, snapshot), 47.2
        )

    def test_unknown_setup_falls_back_to_risk_params(self) -> None:
        snapshot = {"close": 50.0, "risk_params": {"stop_loss": 46.0}}
        self.assertAlmostEqual(
            resolve_setup_stop_loss(SetupType.TREND_BREAKOUT, snapshot), 46.0
        )

    def test_layered_entry_reads_v2_current_stage_stop(self) -> None:
        """v2 分层买点只认当前阶段的止损，且不吃 risk_params 兜底。"""
        snapshot = {
            "close": 50.0,
            "bottom_divergence_v2_stage": "near_cleared",
            "bottom_divergence_v2_stop_loss_price": 41.5,
            "risk_params": {"stop_loss": 46.0},
        }
        self.assertAlmostEqual(
            resolve_setup_stop_loss(
                SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY, snapshot
            ),
            41.5,
        )

    def test_layered_entry_does_not_borrow_risk_params(self) -> None:
        """反例：v2 没解析出止损时不能靠 risk_params 蒙过关。"""
        snapshot = {
            "close": 50.0,
            "bottom_divergence_v2_stage": "near_cleared",
            "risk_params": {"stop_loss": 46.0},
        }
        self.assertIsNone(
            resolve_setup_stop_loss(
                SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY, snapshot
            )
        )

    def test_returns_none_when_no_stop_available(self) -> None:
        """反例：真的没有止损时必须返回 None，不能编一个出来。

        L5 用 None 与否决定能不能进可执行阶段；返回一个兜底值
        等于让所有候选都拿到止损，把这道闸门废掉。
        """
        self.assertIsNone(
            resolve_setup_stop_loss(SetupType.TREND_BREAKOUT, {"close": 50.0})
        )

    def test_rejects_non_positive_and_non_finite(self) -> None:
        # True 必须被拒：bool 是 int 子类，float(True) == 1.0 会变成 1 元止损价
        for bad in (0.0, -1.0, float("nan"), float("inf"), "abc", None, True):
            with self.subTest(bad=bad):
                self.assertIsNone(
                    resolve_setup_stop_loss(
                        SetupType.LOW123_BREAKOUT,
                        {"close": 50.0, "pattern_123_stop_loss": bad},
                    )
                )


if __name__ == "__main__":
    unittest.main()
