# -*- coding: utf-8 -*-
"""Integration tests for gap/limit-up freshness & maturity routing.

Covers the Strategy-C alignment additions in todo 3:
- ``SetupFreshnessAssessor`` consumes ``bars_since_event`` first for
  gap/limit-up setups and returns near-max for ``retest_hold``.
- ``EntryMaturityAssessor`` routes on the new sub-semantic factors
  (``breakaway_gap_strict``, ``continuation_gap``,
  ``limitup_structure_breakout``, ``retest_hold``) before falling
  back to legacy keys.
"""

from src.services.setup_freshness_assessor import SetupFreshnessAssessor
from src.services.entry_maturity_assessor import EntryMaturityAssessor
from src.schemas.trading_types import EntryMaturity, SetupType


# ── SetupFreshnessAssessor ─────────────────────────────────────

def test_freshness_uses_bars_since_event_for_gap_breakout():
    assessor = SetupFreshnessAssessor()
    # bars_since_event=0 → freshest (event fired on the latest bar)
    assert assessor.assess(
        SetupType.GAP_BREAKOUT, {"bars_since_event": 0}
    ) == 1.0
    # bars_since_event=4 should still be quite fresh
    fresh_4 = assessor.assess(SetupType.GAP_BREAKOUT, {"bars_since_event": 4})
    assert 0.5 <= fresh_4 <= 0.9


def test_freshness_retest_hold_returns_near_max():
    assessor = SetupFreshnessAssessor()
    # Retest second-entry counts as structurally fresher than the
    # original breakout regardless of how far back the event fired.
    score = assessor.assess(
        SetupType.LIMITUP_STRUCTURE,
        {"retest_hold": True, "bars_since_event": 5},
    )
    assert score >= 0.9


def test_freshness_legacy_fallback_when_bars_missing():
    """Without ``bars_since_event`` the legacy day-counter path still works."""
    assessor = SetupFreshnessAssessor()
    # ma100_breakout_days=3 → 0.8 per existing _score_from_breakout_days
    score = assessor.assess(
        SetupType.GAP_BREAKOUT, {"ma100_breakout_days": 3}
    )
    assert score == 0.8


def test_freshness_non_gap_setup_unaffected():
    """TREND_PULLBACK must not consume bars_since_event."""
    assessor = SetupFreshnessAssessor()
    # bars_since_event is a gap/limit-up signal; pullback setups should
    # bypass the new branch and fall through to the default tier.
    score = assessor.assess(
        SetupType.TREND_PULLBACK, {"bars_since_event": 0}
    )
    assert score == 0.5


# ── EntryMaturityAssessor ──────────────────────────────────────

def test_maturity_breakaway_gap_strict_is_high_when_no_exhaustion():
    assessor = EntryMaturityAssessor()
    snapshot = {
        "breakaway_gap_strict": True,
        "gap_exhaustion_risk_level": "none",
    }
    assert assessor.assess(SetupType.GAP_BREAKOUT, snapshot) == EntryMaturity.HIGH


def test_maturity_breakaway_gap_strict_downgraded_on_warn_exhaustion():
    assessor = EntryMaturityAssessor()
    snapshot = {
        "breakaway_gap_strict": True,
        "gap_exhaustion_risk_level": "warn",
    }
    assert assessor.assess(SetupType.GAP_BREAKOUT, snapshot) == EntryMaturity.MEDIUM


def test_maturity_continuation_gap_is_medium():
    assessor = EntryMaturityAssessor()
    snapshot = {"continuation_gap": True}
    assert assessor.assess(SetupType.GAP_BREAKOUT, snapshot) == EntryMaturity.MEDIUM


def test_maturity_retest_hold_promotes_to_high():
    """Retest second-entry dominates both gap and limit-up routing."""
    assessor = EntryMaturityAssessor()
    assert assessor.assess(
        SetupType.GAP_BREAKOUT, {"retest_hold": True}
    ) == EntryMaturity.HIGH
    assert assessor.assess(
        SetupType.LIMITUP_STRUCTURE, {"retest_hold": True}
    ) == EntryMaturity.HIGH


def test_maturity_limitup_structure_high_when_no_accel_risk():
    assessor = EntryMaturityAssessor()
    snapshot = {
        "limitup_structure_breakout": True,
        "limitup_high_acceleration_risk": False,
    }
    assert assessor.assess(
        SetupType.LIMITUP_STRUCTURE, snapshot
    ) == EntryMaturity.HIGH


def test_maturity_limitup_structure_downgraded_on_accel_risk():
    """High-acceleration risk prevents a clean HIGH rating even on structure."""
    assessor = EntryMaturityAssessor()
    snapshot = {
        "limitup_structure_breakout": True,
        "limitup_high_acceleration_risk": True,
        "is_limit_up": True,
    }
    # Falls through to legacy ``is_limit_up`` → MEDIUM.
    assert assessor.assess(
        SetupType.LIMITUP_STRUCTURE, snapshot
    ) == EntryMaturity.MEDIUM


def test_maturity_legacy_gap_breakaway_still_routed():
    assessor = EntryMaturityAssessor()
    snapshot = {"gap_breakaway": True, "is_limit_up": True}
    assert assessor.assess(
        SetupType.GAP_BREAKOUT, snapshot
    ) == EntryMaturity.HIGH
