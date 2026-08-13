# -*- coding: utf-8 -*-
"""逐日收益引擎的行为测试（设计稿 8.2c）。

这里测的不是「能不能跑通」，而是几件算错了也不会报错的事：

1. 三段公式连乘必须恰好等于 ``open(T+1+k)/open(T+1) - 1``。这个恒等式是
   8.2c 指定的自检条件，它同时钉住三段公式与成交约定自洽。
2. 首日若误用 ``close(T+1)/close(T) - 1``，恒等式必须**当场破掉**。
   这是设计稿验收项 30：那段跳空在下单时点不可得，属未来信息。
   恒等式测试如果在这种改写下仍然为真，说明它根本没承重。
3. 逐日收益必须在复权后的价格上算。原始价上 ``close(d)/close(d-1)``
   会在每个除权日凭空造出一段收益。
4. 复权守卫拒绝窗口时不许退回原始价，必须显式失败。
5. 基准腿的三条 ``U(d)`` 条件缺一不可，且停牌代理与主数据缺失都按剔除处理。
"""

import numpy as np
import pandas as pd
import pytest

from src.backtest.services.daily_return_engine import (
    DEFAULT_MIN_LISTED_TRADING_DAYS,
    UnadjustableWindowError,
    compute_holding_daily_returns,
    compute_market_median_benchmark,
    compute_portfolio_daily_returns,
)
from src.services.adjustment_chain import RAW_CONVENTION

BASE_DATE = pd.Timestamp("2026-01-05")


def _dates(count, start=BASE_DATE):
    return [start + pd.Timedelta(days=index) for index in range(count)]


def _holding_window(
    opens,
    closes,
    pre_closes,
    *,
    volumes=None,
    code="600000",
    conventions=None,
):
    """造一个单只股票、按日期升序的持仓窗口，首行即入场日 T+1。"""
    count = len(closes)
    if volumes is None:
        volumes = [1_000_000.0] * count
    if conventions is None:
        conventions = [RAW_CONVENTION] * count
    return pd.DataFrame(
        {
            "code": [code] * count,
            "date": _dates(count),
            "open": opens,
            "close": closes,
            "pre_close": pre_closes,
            "volume": volumes,
            "adj_convention": conventions,
        }
    )


def _random_paths(seed, *, count, holding_days):
    """生成若干条随机价格路径。

    每条路径含 T 日收盘价与 T+1 .. T+1+k 的开收盘价。隔夜跳空幅度刻意不小于
    0.5%：首日公式的负向测试正是靠这段跳空把恒等式顶破，跳空为 0 时两种写法
    数值相同，测试会退化成一句空话。
    """
    rng = np.random.default_rng(seed)
    for _ in range(count):
        rows = holding_days + 1
        close_t = float(rng.uniform(5.0, 50.0))
        opens = []
        closes = []
        previous_close = close_t
        for _index in range(rows):
            gap = float(rng.uniform(0.005, 0.03)) * float(rng.choice([-1.0, 1.0]))
            intraday = float(rng.normal(0.0, 0.015))
            open_price = previous_close * (1.0 + gap)
            close_price = open_price * (1.0 + intraday)
            opens.append(open_price)
            closes.append(close_price)
            previous_close = close_price
        yield close_t, opens, closes


def _chain(returns):
    chained = 1.0
    for value in returns:
        chained *= 1.0 + value
    return chained - 1.0


# ── 个股逐日收益：自检恒等式 ────────────────────────────────────────────


@pytest.mark.parametrize("holding_days", [1, 2, 5, 10])
def test_segment_formulas_chain_to_open_to_open_return(holding_days):
    """三段连乘 == open(T+1+k)/open(T+1) - 1。

    路径无公司行为，因此复权因子恒为 1，可以直接拿输入的原始开盘价做对照，
    恒等式不依赖被测模块自己算出来的任何中间量。
    """
    for close_t, opens, closes in _random_paths(
        20260813, count=25, holding_days=holding_days
    ):
        pre_closes = [close_t] + closes[:-1]
        window = _holding_window(opens, closes, pre_closes)

        result = compute_holding_daily_returns(window, holding_days=holding_days)

        assert len(result.returns) == holding_days + 1
        expected = opens[-1] / opens[0] - 1.0
        assert _chain(result.returns) == pytest.approx(expected, rel=1e-12)


@pytest.mark.parametrize("holding_days", [1, 2, 5, 10])
def test_lookahead_first_day_formula_breaks_the_identity(holding_days):
    """验收项 30：首日改用 ``close(T+1)/close(T)-1`` 后恒等式必须失败。

    偏差不是随便一个数，恰好等于 ``open(T+1)/close(T)``——也就是信号日收盘到
    次日开盘那段在下单时点拿不到的跳空。把它钉住，才能说明失败来自未来信息
    而不是别的数值噪声。
    """
    for close_t, opens, closes in _random_paths(
        20260814, count=25, holding_days=holding_days
    ):
        pre_closes = [close_t] + closes[:-1]
        window = _holding_window(opens, closes, pre_closes)

        result = compute_holding_daily_returns(window, holding_days=holding_days)
        identity = opens[-1] / opens[0] - 1.0
        assert _chain(result.returns) == pytest.approx(identity, rel=1e-12)

        lookahead = [closes[0] / close_t - 1.0, *result.returns[1:]]
        chained = _chain(lookahead)
        assert chained != pytest.approx(identity, rel=1e-9)
        assert (1.0 + chained) / (1.0 + identity) == pytest.approx(
            opens[0] / close_t, rel=1e-12
        )


def test_first_day_return_is_open_to_close_not_full_day():
    window = _holding_window(
        opens=[10.0, 11.0, 12.0],
        closes=[10.5, 11.5, 12.5],
        pre_closes=[9.0, 10.5, 11.5],
    )

    result = compute_holding_daily_returns(window, holding_days=2)

    assert result.returns[0] == pytest.approx(10.5 / 10.0 - 1.0, rel=1e-12)


def test_exit_day_return_is_overnight_gap_only():
    window = _holding_window(
        opens=[10.0, 11.0, 12.0],
        closes=[10.5, 11.5, 12.5],
        pre_closes=[9.0, 10.5, 11.5],
    )

    result = compute_holding_daily_returns(window, holding_days=2)

    assert result.returns[-1] == pytest.approx(12.0 / 11.5 - 1.0, rel=1e-12)


def test_continuation_day_return_is_close_to_close():
    window = _holding_window(
        opens=[10.0, 11.0, 12.0, 13.0],
        closes=[10.5, 11.5, 12.5, 13.5],
        pre_closes=[9.0, 10.5, 11.5, 12.5],
    )

    result = compute_holding_daily_returns(window, holding_days=3)

    assert result.returns[1] == pytest.approx(11.5 / 10.5 - 1.0, rel=1e-12)
    assert result.returns[2] == pytest.approx(12.5 / 11.5 - 1.0, rel=1e-12)


def test_suspended_day_return_is_zero():
    """停牌日记 0（8.2b 细节 3）。

    这里刻意让停牌日的收盘价仍在动：真实数据里停牌日价格不变，两种写法数值
    相同，测不出这条规则有没有实现。
    """
    window = _holding_window(
        opens=[10.0, 11.0, 12.0, 13.0],
        closes=[10.5, 11.5, 12.5, 13.5],
        pre_closes=[9.0, 10.5, 11.5, 12.5],
        volumes=[1_000_000.0, 0.0, 1_000_000.0, 1_000_000.0],
    )

    result = compute_holding_daily_returns(window, holding_days=3)

    assert result.returns[1] == 0.0
    assert result.suspended_days == 1


def test_missing_volume_counts_as_suspended():
    window = _holding_window(
        opens=[10.0, 11.0, 12.0],
        closes=[10.5, 11.5, 12.5],
        pre_closes=[9.0, 10.5, 11.5],
        volumes=[1_000_000.0, None, 1_000_000.0],
    )

    result = compute_holding_daily_returns(window, holding_days=2)

    assert result.returns[1] == 0.0
    assert result.suspended_days == 1


# ── 个股逐日收益：复权正确性 ────────────────────────────────────────────


def _ex_dividend_window():
    """第 2 行是除权日：``pre_close`` 比前一日收盘低 1 元（每股派息 1 元）。

    原始价上 ``close/close(-1) - 1 = 9/10 - 1 = -10%``，是纯粹的除权假跌；
    真实收益是 ``close/pre_close - 1 = 9/9 - 1 = 0``。
    """
    return _holding_window(
        opens=[10.0, 9.0, 9.2],
        closes=[10.0, 9.0, 9.4],
        pre_closes=[10.0, 9.0, 9.0],
    )


def test_ex_dividend_day_has_no_artificial_drop():
    window = _ex_dividend_window()
    raw_close = window["close"].to_numpy(dtype=float)
    # 前提自检：这确实是一个除权窗口，而不是一段普通行情。
    assert abs(window["pre_close"].iloc[1] - raw_close[0]) > 1e-6

    result = compute_holding_daily_returns(window, holding_days=2)

    assert result.returns[1] == pytest.approx(0.0, abs=1e-12)
    assert raw_close[1] / raw_close[0] - 1.0 == pytest.approx(-0.1, rel=1e-12)


def test_identity_still_holds_across_an_ex_dividend_date():
    """除权窗口上的恒等式要拿复权后的两端开盘价对照。

    原始 ``open(T+1+k)/open(T+1)`` 少掉的正是那 1 元分红，本来就不该相等。
    """
    result = compute_holding_daily_returns(_ex_dividend_window(), holding_days=2)

    assert _chain(result.returns) == pytest.approx(
        result.exit_open / result.entry_open - 1.0, rel=1e-12
    )
    assert result.entry_open == pytest.approx(10.0, rel=1e-12)
    assert result.exit_open == pytest.approx(9.2 * 10.0 / 9.0, rel=1e-12)


# ── 个股逐日收益：fail-closed ───────────────────────────────────────────


def test_non_raw_convention_window_is_rejected():
    window = _holding_window(
        opens=[10.0, 9.0, 9.2],
        closes=[10.0, 9.0, 9.4],
        pre_closes=[10.0, 9.0, 9.0],
        conventions=[RAW_CONVENTION, "qfq", RAW_CONVENTION],
    )

    with pytest.raises(UnadjustableWindowError):
        compute_holding_daily_returns(window, holding_days=2)


def test_missing_adj_convention_column_is_rejected():
    window = _holding_window(
        opens=[10.0, 9.0, 9.2],
        closes=[10.0, 9.0, 9.4],
        pre_closes=[10.0, 9.0, 9.0],
    ).drop(columns=["adj_convention"])

    with pytest.raises(UnadjustableWindowError):
        compute_holding_daily_returns(window, holding_days=2)


def test_missing_pre_close_column_is_rejected():
    window = _holding_window(
        opens=[10.0, 9.0, 9.2],
        closes=[10.0, 9.0, 9.4],
        pre_closes=[10.0, 9.0, 9.0],
    ).drop(columns=["pre_close"])

    with pytest.raises(UnadjustableWindowError):
        compute_holding_daily_returns(window, holding_days=2)


def test_missing_pre_close_value_is_rejected():
    """锚在首行时断链会作废包含锚在内的整个前缀，整窗必须拒绝。"""
    window = _holding_window(
        opens=[10.0, 9.0, 9.2],
        closes=[10.0, 9.0, 9.4],
        pre_closes=[10.0, None, 9.0],
    )

    with pytest.raises(UnadjustableWindowError):
        compute_holding_daily_returns(window, holding_days=2)


def test_window_length_must_match_holding_days():
    window = _holding_window(
        opens=[10.0, 11.0, 12.0],
        closes=[10.5, 11.5, 12.5],
        pre_closes=[9.0, 10.5, 11.5],
    )

    with pytest.raises(ValueError):
        compute_holding_daily_returns(window, holding_days=5)


def test_descending_window_is_rejected():
    window = _holding_window(
        opens=[10.0, 11.0, 12.0],
        closes=[10.5, 11.5, 12.5],
        pre_closes=[9.0, 10.5, 11.5],
    ).iloc[::-1]

    with pytest.raises(ValueError):
        compute_holding_daily_returns(window, holding_days=2)


# ── 组合逐日收益 ────────────────────────────────────────────────────────


def test_portfolio_return_is_equal_weight_mean():
    days = _dates(1)
    holdings = {days[0]: ["600000", "600001", "600002"]}
    stock_returns = {days[0]: {"600000": 0.01, "600001": 0.03, "600002": -0.02}}

    result = compute_portfolio_daily_returns(holdings, stock_returns)

    assert result.returns[0] == pytest.approx((0.01 + 0.03 - 0.02) / 3.0, rel=1e-12)
    assert result.position_counts[0] == 3


def test_repeated_signal_on_the_same_stock_counts_once():
    """`H(d,k)` 是集合：同一只票在窗口内多次入选不等于加杠杆。"""
    days = _dates(1)
    holdings = {days[0]: ["600000", "600000", "600000", "600001"]}
    stock_returns = {days[0]: {"600000": 0.10, "600001": 0.00}}

    result = compute_portfolio_daily_returns(holdings, stock_returns)

    assert result.position_counts[0] == 2
    assert result.returns[0] == pytest.approx(0.05, rel=1e-12)


def test_empty_position_day_is_kept_as_zero_and_reported():
    days = _dates(4)
    holdings = {
        days[0]: ["600000"],
        days[1]: [],
        days[2]: [],
        days[3]: ["600000"],
    }
    stock_returns = {days[0]: {"600000": 0.02}, days[3]: {"600000": -0.02}}

    result = compute_portfolio_daily_returns(holdings, stock_returns)

    assert result.dates == tuple(days)
    assert result.returns == (
        pytest.approx(0.02),
        0.0,
        0.0,
        pytest.approx(-0.02),
    )
    assert result.empty_days == 2
    assert result.empty_day_share == pytest.approx(0.5, rel=1e-12)


def test_explicit_date_axis_keeps_days_without_any_holding_record():
    days = _dates(3)
    holdings = {days[1]: ["600000"]}
    stock_returns = {days[1]: {"600000": 0.05}}

    result = compute_portfolio_daily_returns(holdings, stock_returns, dates=days)

    assert result.dates == tuple(days)
    assert result.returns[0] == 0.0
    assert result.returns[2] == 0.0
    assert result.empty_days == 2


def test_missing_daily_return_for_a_held_stock_is_rejected():
    """持仓缺收益不能静默丢掉：丢一只等于当天把权重悄悄重分配给其余持仓。"""
    days = _dates(1)
    holdings = {days[0]: ["600000", "600001"]}
    stock_returns = {days[0]: {"600000": 0.01}}

    with pytest.raises(ValueError):
        compute_portfolio_daily_returns(holdings, stock_returns)


# ── 市场中位数基准 ──────────────────────────────────────────────────────


def _benchmark_prices(rows):
    """rows: (code, date, close, pre_close, volume) 五元组序列。"""
    return pd.DataFrame(
        {
            "code": [row[0] for row in rows],
            "date": [row[1] for row in rows],
            "close": [row[2] for row in rows],
            "pre_close": [row[3] for row in rows],
            "volume": [row[4] for row in rows],
            "adj_convention": [RAW_CONVENTION] * len(rows),
        }
    )


def _listings(entries):
    """entries: (code, list_date, delist_date) 三元组序列。"""
    return pd.DataFrame(
        {
            "code": [entry[0] for entry in entries],
            "list_date": [entry[1] for entry in entries],
            "delist_date": [entry[2] for entry in entries],
        }
    )


def _two_day_market():
    days = _dates(2)
    prices = _benchmark_prices(
        [
            ("600000", days[0], 10.0, 9.9, 1_000_000.0),
            ("600000", days[1], 10.1, 10.0, 1_000_000.0),
            ("600001", days[0], 20.0, 19.9, 1_000_000.0),
            ("600001", days[1], 20.6, 20.0, 1_000_000.0),
            ("600002", days[0], 5.0, 4.9, 1_000_000.0),
            ("600002", days[1], 5.35, 5.0, 1_000_000.0),
        ]
    )
    listings = _listings(
        [
            ("600000", pd.Timestamp("2020-01-02"), None),
            ("600001", pd.Timestamp("2020-01-02"), None),
            ("600002", pd.Timestamp("2020-01-02"), None),
        ]
    )
    return days, prices, listings


def test_benchmark_is_the_cross_sectional_median_of_daily_returns():
    days, prices, listings = _two_day_market()

    result = compute_market_median_benchmark(
        prices,
        listings,
        trading_days=days,
        min_listed_trading_days=1,
    )

    assert result.dates == tuple(days)
    # 首日没有可比的前一交易日，`U(d)` 为空，收益未定义而不是 0。
    assert result.returns[0] is None
    assert result.universe_sizes[0] == 0
    assert result.returns[1] == pytest.approx(0.03, rel=1e-12)
    assert result.universe_sizes[1] == 3


def test_benchmark_excludes_zero_volume_rows_as_suspended():
    days, prices, listings = _two_day_market()
    prices.loc[
        (prices["code"] == "600002") & (prices["date"] == days[1]), "volume"
    ] = 0.0

    result = compute_market_median_benchmark(
        prices,
        listings,
        trading_days=days,
        min_listed_trading_days=1,
    )

    assert result.universe_sizes[1] == 2
    assert result.returns[1] == pytest.approx((0.01 + 0.03) / 2.0, rel=1e-12)


def test_benchmark_excludes_codes_absent_from_instrument_master():
    days, prices, listings = _two_day_market()
    listings = listings[listings["code"] != "600002"].reset_index(drop=True)

    result = compute_market_median_benchmark(
        prices,
        listings,
        trading_days=days,
        min_listed_trading_days=1,
    )

    assert result.excluded_unmastered_codes == frozenset({"600002"})
    assert result.universe_sizes[1] == 2
    assert result.returns[1] == pytest.approx((0.01 + 0.03) / 2.0, rel=1e-12)


def test_benchmark_excludes_stocks_not_listed_long_enough():
    days, prices, listings = _two_day_market()
    calendar = _dates(70, start=pd.Timestamp("2025-09-01")) + days
    listings = _listings(
        [
            ("600000", calendar[0], None),
            ("600001", calendar[0], None),
            # 上市至今仅 22 个交易日，默认 N=60 时应被剔除。
            ("600002", calendar[50], None),
        ]
    )

    result = compute_market_median_benchmark(prices, listings, trading_days=calendar)

    assert DEFAULT_MIN_LISTED_TRADING_DAYS == 60
    assert result.universe_sizes[1] == 2
    assert result.returns[1] == pytest.approx((0.01 + 0.03) / 2.0, rel=1e-12)


def test_benchmark_excludes_not_yet_listed_and_delisted_stocks():
    days, prices, listings = _two_day_market()
    listings = _listings(
        [
            # 退市日当天不再在市（`d < delist_date` 不成立）。
            ("600000", pd.Timestamp("2020-01-02"), days[1]),
            ("600001", pd.Timestamp("2020-01-02"), None),
            # 上市日晚于 d。
            ("600002", days[1] + pd.Timedelta(days=1), None),
        ]
    )

    result = compute_market_median_benchmark(
        prices,
        listings,
        trading_days=days,
        min_listed_trading_days=1,
    )

    assert result.universe_sizes[1] == 1
    assert result.returns[1] == pytest.approx(0.03, rel=1e-12)


def test_benchmark_uses_adjusted_prices_across_an_ex_dividend_date():
    days = _dates(2)
    prices = _benchmark_prices(
        [
            ("600000", days[0], 10.0, 10.0, 1_000_000.0),
            ("600000", days[1], 9.0, 9.0, 1_000_000.0),
        ]
    )
    listings = _listings([("600000", pd.Timestamp("2020-01-02"), None)])

    result = compute_market_median_benchmark(
        prices,
        listings,
        trading_days=days,
        min_listed_trading_days=1,
    )

    assert result.universe_sizes[1] == 1
    assert result.returns[1] == pytest.approx(0.0, abs=1e-12)


def test_benchmark_drops_rows_the_adjustment_guard_rejects():
    days, prices, listings = _two_day_market()
    prices.loc[prices["code"] == "600002", "adj_convention"] = "qfq"

    result = compute_market_median_benchmark(
        prices,
        listings,
        trading_days=days,
        min_listed_trading_days=1,
    )

    assert result.untrusted_rows == 2
    assert result.universe_sizes[1] == 2
    assert result.returns[1] == pytest.approx((0.01 + 0.03) / 2.0, rel=1e-12)
