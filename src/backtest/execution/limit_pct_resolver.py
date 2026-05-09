# -*- coding: utf-8 -*-
"""LimitPctResolver: per-board (科创/创业/北交所/主板/ST) daily price-limit lookup.

Background (B1):
  ``ExecutionModelResolver`` previously hard-coded ±9.9 % as the limit-up /
  limit-down threshold (``_LIMIT_UP_PCT = 9.9``). This is only correct for
  the Shanghai/Shenzhen 主板 — 科创板 (688/689) and 创业板注册制 (300/301)
  enforce ±20 %, 北交所 (43/83/87/88/92) enforces ±30 %, and ST/*ST
  enforces ±5 %. The hard-coded threshold meant the conservative model
  silently treated 创业板 +18 % bars as "tradable next-open" instead of
  "limit-blocked", systematically over-stating buy-point quality on the
  high-volatility cohort that the audit report flagged ("涨停入选 27/80
  = 34 %").

This module returns the right ``(limit_up_pct, limit_down_pct)`` pair for
an A-share instrument and ``(None, None)`` for non-A markets (HKEX / US
have no daily price limits, callers must skip limit-up / limit-down
detection in that case).

The pct values are returned in **whole-number percent** (e.g. 20.0, not
0.20) to match the existing ``StockDaily.pct_chg`` and
``ExecutionModelResolver`` semantics.
"""
from __future__ import annotations

from typing import Optional, Tuple

# Tolerance for "close enough to limit" checks. Real exchange prints can be
# 9.99 / 19.97 / 29.93 etc. depending on tick rounding; 0.1 % is wide enough
# to capture every legitimate hit and tight enough to reject ordinary days.
LIMIT_PCT_TOLERANCE = 0.1

# Defaults used when a caller cannot resolve board info (rare; should only
# happen for legacy data without InstrumentMaster). The 主板 ±10 % default is
# the safest assumption — it errs on the side of *blocking* rather than
# letting a non-trivial pct_chg through unrestricted.
DEFAULT_A_SHARE_LIMIT_UP_PCT = 10.0
DEFAULT_A_SHARE_LIMIT_DOWN_PCT = -10.0


def resolve_limit_pct(
    code: str,
    is_st: bool = False,
    market: str = "cn",
    exchange: Optional[str] = None,
) -> Tuple[Optional[float], Optional[float]]:
    """Resolve daily price-limit thresholds for the given instrument.

    Args:
        code:     Stock code (without exchange prefix, e.g. "600519",
                  "300750", "688981", "835174").
        is_st:    True if the instrument is currently ST or *ST.
                  WARNING: this is read from ``InstrumentMaster.is_st``
                  which reflects the *current* status — historical ST
                  changes are not back-projected.
        market:   "cn" / "hk" / "us". Non-cn markets return (None, None).
        exchange: Optional exchange code (e.g. "SSE", "SZSE", "BJSE").
                  Preferred over code-prefix detection when available
                  because 4xx / 8xx prefixes overlap between B-shares,
                  legacy 老三板, and 北交所.

    Returns:
        ``(limit_up_pct, limit_down_pct)`` in whole-number percent (e.g.
        ``(20.0, -20.0)`` for 科创板) or ``(None, None)`` when the market
        has no daily price limit.

    Rules (A-share, effective 2024+):
        - ST / *ST                          → ±5 %
        - 北交所 (BJSE)                     → ±30 %
        - 科创板 (688, 689)                  → ±20 %
        - 创业板注册制后 (300, 301)          → ±20 %
        - 主板 (60xxxx, 000/001/002/003)     → ±10 %

    NOTE on 创业板 history:
        创业板 only moved to ±20 % on 2020-08-24. Pre-cutover bars were
        ±10 %. This resolver does not currently take ``trade_date`` so it
        always returns ±20 % for 300/301; callers backtesting pre-2020-08
        data must handle this themselves. Tracked for follow-up in the
        backtest defect list.
    """
    if market != "cn":
        return None, None

    # ST takes precedence over board — 戴帽 stocks always run at ±5 %
    # regardless of which exchange they list on.
    if is_st:
        return 5.0, -5.0

    # Prefer exchange field over code-prefix when available. 4xx / 8xx
    # prefixes overlap between B-shares (legacy), 老三板, and 北交所;
    # exchange="BJSE" is unambiguous.
    if exchange and exchange.upper() in {"BJSE", "BJ", "BSE"}:
        return 30.0, -30.0

    # Fallback to code prefix detection.
    if code.startswith(("43", "83", "87", "88", "92")):
        # 北交所 (43xxxx 原 NEEQ 转板, 83/87/88/92 北交所主)
        return 30.0, -30.0

    if code.startswith(("688", "689")):
        # 科创板
        return 20.0, -20.0

    if code.startswith(("300", "301")):
        # 创业板（注册制后）
        return 20.0, -20.0

    # 主板 (60xxxx 沪市主板, 000/001/002/003 深市主板/中小板)
    return DEFAULT_A_SHARE_LIMIT_UP_PCT, DEFAULT_A_SHARE_LIMIT_DOWN_PCT


def is_at_or_near_limit_up(pct_chg: Optional[float], limit_up_pct: Optional[float]) -> bool:
    """Return True if ``pct_chg`` is within ``LIMIT_PCT_TOLERANCE`` of
    ``limit_up_pct``. False when either value is None (e.g. non-cn market
    or missing pct_chg).
    """
    if pct_chg is None or limit_up_pct is None:
        return False
    return pct_chg >= limit_up_pct - LIMIT_PCT_TOLERANCE


def is_at_or_near_limit_down(pct_chg: Optional[float], limit_down_pct: Optional[float]) -> bool:
    """Mirror of :func:`is_at_or_near_limit_up` for the down side."""
    if pct_chg is None or limit_down_pct is None:
        return False
    return pct_chg <= limit_down_pct + LIMIT_PCT_TOLERANCE
