# Bottom Divergence Resistance Zones Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, point-in-time-safe bottom-divergence v2 strategy with frozen R1/R2 resistance zones and staged early/R1/R2 entries while preserving all legacy v1 behavior.

**Architecture:** Keep `BottomDivergenceBreakoutDetector` and `bottom_divergence_double_breakout` unchanged as `legacy_v1`. Add a pure resistance-zone calculator and a separate `CausalBottomDivergenceDetector` that freezes candidate evidence at B+1, returns deterministic candidate/version records, and exposes flattened `bottom_divergence_v2_*` factors. Integrate v2 through a separate setup type, YAML strategy, decision path, AI evidence, Web presentation, and version-separated backtest evidence.

**Tech Stack:** Python 3.12, pandas/numpy, SQLAlchemy snapshots, pytest/unittest, YAML screening rules, React/TypeScript, Vitest, existing five-layer backtest pipeline.

**Design spec:** `docs/superpowers/specs/2026-08-06-bottom-divergence-resistance-zones-design.md`

**Repository constraint:** Commit checkpoints below are optional. Do not run `git commit`, `git push`, or create a PR unless the user explicitly authorizes it. If commits are authorized, use the listed English messages without `Co-Authored-By`.

**Final implementation reconciliation:**

- The fixture is
  `tests/fixtures/001337_bottom_divergence_20251201_20260805.csv`; it starts on
  2025-12-01 to provide sufficient indicator warm-up.
- The frozen golden zones are R1=`37.46–39.20` and R2=`40.61–42.90`.
- 001337 replays as 2026-07-22 early, 2026-07-23 R1, and 2026-08-05 R2
  historical. Unknown adjustment provenance keeps early/R1 as observation-only,
  projects an R2 breakout as `major_unverified`, and blocks all execution.
  Only trusted `tushare_native` / `akshare_qfq_div_raw` provenance is executable.
- v1 remains unchanged. v2 remains disabled unless
  `BOTTOM_DIVERGENCE_V2_ENABLED=true` is explicitly configured.
- Task 8's pure validation core is split into five modules:
  `bottom_divergence_v2_models.py`, `bottom_divergence_v2_metrics.py`,
  `bottom_divergence_v2_selection.py`, `bottom_divergence_v2_validation.py`, and
  `bottom_divergence_v2_report.py`. Dataset/replay/CLI orchestration lives in
  `bottom_divergence_v2_dataset.py`, `bottom_divergence_v2_replay.py`, and
  `bottom_divergence_v2_cli_service.py`; the Task 8 five-module/CLI focused suite
  contains 113 tests. Task 1 replay and end-to-end replay tests are additional.
- Production AI review uses
  `CandidateAnalysisService → ScreeningAiReviewService → ScreeningAiReviewGuard`
  with `screening_ai_review_prompt_builder.py`; this is the only production
  review path for the feature.

**Task 10 verification status (2026-08-07):**

- Backend full-gate coverage is green through stable shards: the complete suite
  has 0 failures, expected skips remain intentional, and the subsequently added
  configuration/API cases pass separately. Exact collection totals and command
  output belong in delivery evidence rather than this long-lived plan.
- All added/modified production Python files pass `python -m py_compile`;
  flake8 critical checks and the commands equivalent to `test.sh` also pass.
- The complete Web gate is green: full lint and production build both pass.
- On Windows, invoking the checked-out CRLF `scripts/ci_gate.sh` directly
  through Bash still encounters `\r`; this is only a local shell/line-ending
  invocation difference. The gate's equivalent commands are green and no
  backend or frontend test failure remains.
- The only release blocker is intentionally outside the code gates: the
  sample-out CLI still uses the default zero-cost model, exits 1 with canonical
  `ZERO_COST_MODEL`, and has not received a user-approved non-zero cost model
  or explicit run range. Until both are supplied and the sample-out gates pass,
  `BOTTOM_DIVERGENCE_V2_ENABLED` remains `false` and v2 must not be rolled out.

---

## File Map

### New backend files

- `src/indicators/resistance_zone_detector.py`  
  Pure ATR, candidate-touch extraction, deterministic clustering, R1/R2 scoring, canonical version hashing, and zone breakout events.
- `src/indicators/causal_bottom_divergence_detector.py`  
  Separate v2 A/B lifecycle, frozen MACD/trendline evidence, candidate-record reconstruction, primary-candidate ranking, and staged output.
- `src/indicators/causal_bottom_divergence_events.py` and
  `src/indicators/causal_bottom_divergence_support.py`  
  Point-in-time event replay and shared deterministic support helpers.
- `src/strategies/bottom_divergence_layered_entry.py`  
  Direct-call wrapper for the v2 detector; keeps `entry_strategies.py` legacy class unchanged.
- `strategies/bottom_divergence_layered_entry_v2.yaml`  
  Opt-in quantitative screening definition for early/R1/R2 stages.
- `src/services/bottom_divergence_v2_trade_support.py`  
  Shared stage buy-point and stop-loss resolution for decision and AI review guards.
- `src/backtest/services/bottom_divergence_v2_{models,metrics,selection,validation,report}.py`  
  Five-module pure sample-out validation core.
- `src/backtest/services/bottom_divergence_v2_{dataset,replay,cli_service}.py`  
  Isolated local-dataset copy, chronological replay, and CLI orchestration.

### New test and fixture files

- `tests/fixtures/001337_bottom_divergence_20251201_20260805.csv`
- `tests/test_resistance_zone_detector.py`
- `tests/test_causal_bottom_divergence_detector.py`
- `tests/test_entry_strategy_e_v2.py`
- `tests/test_bottom_divergence_v2_replay.py`
- `tests/test_bottom_divergence_v2_e2e_replay.py`
- `src/backtest/services/bottom_divergence_v2_validation.py`
- `tests/test_bottom_divergence_v2_validation.py`
- `tests/test_bottom_divergence_v2_validation_cli.py`
- `tests/test_bottom_divergence_v2_validation_inputs.py`
- `tests/test_bottom_divergence_v2_replay_service.py`
- `scripts/validate_bottom_divergence_v2.py`

### Existing backend files to modify

- `src/config.py`
- `.env.example`
- `src/services/factor_service.py`
- `src/schemas/trading_types.py`
- `src/services/setup_freshness_assessor.py`
- `src/services/entry_maturity_assessor.py`
- `src/services/trade_plan_builder.py`
- `src/services/five_layer_pipeline.py`
- `src/services/candidate_analysis_service.py`
- `src/services/screening_ai_review_prompt_builder.py`
- `src/services/screening_ai_review_service.py`
- `src/services/screening_ai_review_guard.py`
- `tests/test_config_validate_structured.py`
- `tests/test_factor_service_bottom_divergence.py`
- `tests/test_decision_modules.py`
- `tests/test_trade_plan_builder.py`
- `tests/test_setup_resolver.py`
- `tests/test_screening_ai_review_prompt_builder.py`
- `tests/test_screening_ai_review_service.py`
- `tests/test_screening_ai_review_guard.py`
- `tests/test_candidate_analysis_service.py`
- `tests/test_five_layer_pipeline.py`
- `tests/test_five_layer_backtest_pipeline.py`
- `tests/test_screening_api.py`
- `tests/test_screening_api_schema.py`

### Existing Web files to modify

- `apps/dsa-web/src/types/screening.ts`
- `apps/dsa-web/src/components/screening/TechnicalPatternCards.tsx`
- `apps/dsa-web/src/components/screening/__tests__/TechnicalPatternCards.test.tsx`
- `apps/dsa-web/src/pages/BacktestPage.tsx`
- `apps/dsa-web/src/pages/__tests__/BacktestPage.test.tsx`

### Documentation files to modify

- `README.md`
- `strategies/README.md`
- `docs/CHANGELOG.md`

---

### Task 1: Freeze a reproducible 001337 fixture

**Files:**

- Create: `tests/fixtures/001337_bottom_divergence_20251201_20260805.csv`
- Create: `tests/test_bottom_divergence_v2_replay.py`
- Reference: `.claude/reviews/001337_kline_through_2026-08-05.csv`

- [ ] **Step 1: Export only the required fixture columns**

Create a UTF-8 CSV sorted by date with:

```text
date,open,high,low,close,volume,pct_chg,data_source,adj_factor_source
```

Limit rows to `2025-12-01..2026-08-05`. The longer prefix is required for
MACD/ATR warm-up and point-in-time replay. Normalize volume to the same unit
already persisted in `stock_daily`; do not mix provider units.

- [ ] **Step 2: Write a failing fixture-contract test**

```python
def test_001337_fixture_is_sorted_unique_and_point_in_time_safe():
    frame = load_fixture()
    assert frame["date"].is_monotonic_increasing
    assert frame["date"].is_unique
    assert frame.iloc[-1]["date"] == "2026-08-05"
    assert frame.loc[frame["date"] == "2026-07-21", "low"].item() == 31.68
    assert frame.loc[frame["date"] == "2026-07-22", "close"].item() == 36.60
    assert frame.loc[frame["date"] == "2026-07-23", "close"].item() == 40.26
    assert frame.loc[frame["date"] == "2026-08-05", "close"].item() == 43.27
```

- [ ] **Step 3: Run the fixture test**

Run:

```bash
python -m pytest tests/test_bottom_divergence_v2_replay.py::test_001337_fixture_is_sorted_unique_and_point_in_time_safe -v
```

Expected: PASS after the fixture is present.

- [ ] **Step 4: Add a legacy replay guard**

Assert the fixture still reproduces:

```python
assert replay["2026-07-22"]["state"] == "divergence_only"
assert replay["2026-08-05"]["state"] == "confirmed"
```

This protects the v1 baseline while v2 is developed.

- [ ] **Step 5: Run the complete replay test file**

Run:

```bash
python -m pytest tests/test_bottom_divergence_v2_replay.py -v
```

Expected: fixture and legacy baseline tests PASS.

- [ ] **Step 6: Optional commit checkpoint**

If explicitly authorized:

```bash
git add tests/fixtures/001337_bottom_divergence_20251201_20260805.csv tests/test_bottom_divergence_v2_replay.py
git commit -m "test: freeze 001337 divergence replay fixture"
```

---

### Task 2: Implement the deterministic resistance-zone calculator

**Files:**

- Create: `src/indicators/resistance_zone_detector.py`
- Create: `tests/test_resistance_zone_detector.py`

- [ ] **Step 1: Write failing tests for ATR and candidate extraction**

Cover:

```python
def test_true_range_atr14_uses_only_b_and_prior_rows(): ...
def test_extracts_swing_highs_and_rejection_bars_without_duplicate_dates(): ...
def test_missing_open_falls_back_to_close_and_records_degradation(): ...
def test_post_b_rows_do_not_change_touch_candidates(): ...
```

The calculator API should be:

```python
result = ResistanceZoneDetector.calculate(
    df,
    a_idx=a_idx,
    b_idx=b_idx,
    candidate_version="...",
    params=ResistanceZoneParams(),
    metadata=ResistanceZoneMetadata(
        data_source="TushareFetcher(bulk)",
        adj_factor_source="legacy_assume_one",
    ),
)
```

- [ ] **Step 2: Run the extraction tests and verify RED**

Run:

```bash
python -m pytest tests/test_resistance_zone_detector.py -k "atr or extracts or missing_open or post_b" -v
```

Expected: FAIL because the module/API does not exist.

- [ ] **Step 3: Add immutable parameter and result models**

Implement frozen dataclasses:

```python
@dataclass(frozen=True)
class ResistanceZoneParams:
    cluster_pct: float = 0.015
    atr_gap_multiplier: float = 0.5
    long_wick_ratio: float = 0.5
    rejection_wick_ratio: float = 0.35
    rejection_atr_ratio: float = 0.5
    score_min: float = 0.45
    overlap_ratio: float = 0.60
    breakout_buffer_pct: float = 0.003
    sync_window: int = 3
    invalidated_retention_bars: int = 20
    r1_touch_weight: float = 0.30
    r1_recency_weight: float = 0.25
    r1_volume_weight: float = 0.15
    r1_rejection_weight: float = 0.15
    r1_tightness_weight: float = 0.10
    r1_distance_weight: float = 0.05
    r2_touch_weight: float = 0.35
    r2_recency_weight: float = 0.15
    r2_volume_weight: float = 0.15
    r2_rejection_weight: float = 0.15
    r2_tightness_weight: float = 0.10
    r2_height_weight: float = 0.10

@dataclass(frozen=True)
class ResistanceZoneMetadata:
    data_source: str | None = None
    adj_factor_source: str | None = None
```

Use plain dict serialization at module boundaries so existing factor snapshots remain JSON-compatible.
Validate every weight as non-negative and require each R1/R2 weight group to sum to 1 within
`1e-9`. Include all weights in `parameter_snapshot` and `zone_version`.

- [ ] **Step 4: Implement ATR and touch extraction minimally**

Rules must match the spec exactly:

- Input slice ends at B.
- Long-wick anchor is capped by `0.75 * ATR14_at_B`.
- Rejection bars require both wick-ratio and ATR-rejection predicates.
- Same date contributes one touch.
- Missing volume uses weight 1.

- [ ] **Step 5: Run extraction tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_resistance_zone_detector.py -k "atr or extracts or missing_open or post_b" -v
```

Expected: PASS.

- [ ] **Step 6: Write failing deterministic clustering tests**

Add exact golden tests for:

```python
def test_connected_components_cluster_by_anchor_price(): ...
def test_weighted_quantile_uses_first_cumulative_weight_without_interpolation(): ...
def test_single_long_wick_cannot_be_r1(): ...
def test_high_volume_single_rejection_can_be_low_confidence_r2(): ...
def test_overlapping_zones_merge_at_sixty_percent(): ...
def test_r2_can_exist_when_r1_is_none(): ...
```

- [ ] **Step 7: Run clustering tests and verify RED**

Run:

```bash
python -m pytest tests/test_resistance_zone_detector.py -k "cluster or quantile or long_wick or overlapping or r2_can" -v
```

Expected: FAIL on unimplemented clustering/scoring.

- [ ] **Step 8: Implement clustering, boundaries, scores, and selection**

Implement:

- deterministic connected components over sorted anchors;
- weighted 25/50/75/90 quantiles;
- R1/R2 normalized score formulas;
- independent R1/R2 candidate sets;
- exact tie-breakers ending in `zone_id`;
- `zone_count` semantics for no-zone, R1-only, R2-only, and R1+R2.

Do not import or modify the legacy detector.

- [ ] **Step 9: Add canonical version hashing tests**

```python
def test_zone_version_is_stable_for_canonical_equivalent_inputs(): ...
def test_zone_version_changes_when_params_or_prefix_changes(): ...
def test_zone_version_ignores_post_b_rows_and_as_of_index(): ...
def test_weight_change_updates_zone_version_without_changing_candidate_version(): ...
```

- [ ] **Step 10: Run version tests and verify RED**

Run:

```bash
python -m pytest tests/test_resistance_zone_detector.py -k "zone_version or weight_change" -v
```

Expected: FAIL until canonical hashing is implemented.

- [ ] **Step 11: Implement canonical JSON hashing**

Canonicalize:

- sorted keys;
- dates as `YYYY-MM-DD`;
- floats rounded to 6 decimals;
- non-finite values as `null`;
- candidate version, OHLCV prefix, parameters, data-source metadata in `zone_version`;
- touch dates in `zone_id`.

- [ ] **Step 12: Run the complete calculator suite**

Run:

```bash
python -m pytest tests/test_resistance_zone_detector.py -v
```

Expected: all resistance-zone tests PASS.

- [ ] **Step 13: Optional commit checkpoint**

If explicitly authorized:

```bash
git add src/indicators/resistance_zone_detector.py tests/test_resistance_zone_detector.py
git commit -m "feat: add deterministic resistance zone calculator"
```

---

### Task 3: Implement the causal v2 detector and candidate lifecycle

**Files:**

- Create: `src/indicators/causal_bottom_divergence_detector.py`
- Create: `tests/test_causal_bottom_divergence_detector.py`
- Modify: `tests/test_bottom_divergence_v2_replay.py`
- Reference only: `src/indicators/bottom_divergence_breakout_detector.py`
- Reuse: `src/indicators/divergence_detector.py`

- [ ] **Step 1: Write failing as-of boundary tests**

```python
def test_detect_slices_every_component_at_as_of_index(): ...
def test_mutating_rows_after_as_of_does_not_change_output(): ...
def test_zone_and_trendline_use_no_rows_after_b(): ...
def test_legacy_detector_output_is_unchanged(): ...
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
python -m pytest tests/test_causal_bottom_divergence_detector.py -k "as_of or mutating or after_b or legacy" -v
```

Expected: FAIL because `CausalBottomDivergenceDetector` is missing.

- [ ] **Step 3: Implement a separate v2 entry point**

Public API:

```python
result = CausalBottomDivergenceDetector.detect(
    df,
    as_of_index=len(df) - 1,
    zone_params=ResistanceZoneParams(),
    max_ab_gap=60,
    break_tolerance=0.0,
)
```

Do not change `BottomDivergenceBreakoutDetector.detect`.

- [ ] **Step 4: Implement B+1 frozen evidence**

At the first provisional point:

- freeze A/B dates/prices;
- freeze matched DIF/DEA indices, dates, and values;
- freeze pattern relations/code;
- calculate frozen trendline using rows through B only;
- call `ResistanceZoneDetector` on rows through B;
- generate `candidate_version` and `zone_version`.

Later prefixes may update only lifecycle, breakout events, actionability, and ranking.

- [ ] **Step 5: Run as-of tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_causal_bottom_divergence_detector.py -k "as_of or mutating or after_b or legacy" -v
```

Expected: PASS.

- [ ] **Step 6: Write failing candidate-record lifecycle tests**

Cover:

```python
def test_b_plus_one_creates_provisional_record(): ...
def test_provisional_to_confirmed_keeps_versions_and_frozen_macd(): ...
def test_lower_low_invalidates_without_rewriting_history(): ...
def test_invalidated_record_is_retained_for_twenty_bars(): ...
def test_candidate_records_reconstruct_from_prefix_replay(): ...
def test_primary_candidate_uses_documented_stable_sort(): ...
def test_no_active_candidate_returns_empty_top_level_projection(): ...
def test_candidate_version_includes_a_and_b_trade_dates(): ...
```

- [ ] **Step 7: Run lifecycle/version tests and verify RED**

Run:

```bash
python -m pytest tests/test_causal_bottom_divergence_detector.py -k "provisional or invalidates or retained or reconstruct or primary or trade_dates" -v
```

Expected: FAIL on missing lifecycle reconstruction and candidate hashing.

- [ ] **Step 8: Implement reconstruction and primary projection**

Rebuild recent records by replaying each historical B at B+1. Return:

```python
{
    "candidate_records": [...],
    "primary_candidate_version": str | None,
    # flattened primary projection follows
}
```

Keep invalidated records out of primary selection.

- [ ] **Step 9: Write failing staged-event tests**

Cover:

- early strength formula and missing-open/volume weight renormalization;
- R1 entered/accepted/crossed/cleared-confirmed distinction;
- no-volume cross confirmed only on a later hold;
- irreversible major breakout event;
- current `major_zone_actionable_entry`;
- `0 <= extended_pct <= 10`, confirmation days, structure floor, and latest close above R2;
- frozen trendline/R2 sync window of 3.
- `adjustment_unknown` preserves early observation but forces
  `major_zone_actionable_entry=False` with an explicit actionability status.

- [ ] **Step 10: Run staged-event tests and verify RED**

Run:

```bash
python -m pytest tests/test_causal_bottom_divergence_detector.py -k "early or near_zone or major_zone or extended or adjustment_unknown" -v
```

Expected: FAIL until staged event and actionability logic exists.

- [ ] **Step 11: Implement staged event calculations**

Expose event fields without mutating historical bar indices. A stale or broken setup must preserve `major_zone_breakout.confirmed=True` while actionability becomes false.

- [ ] **Step 12: Add the 001337 golden replay**

Assert:

```python
assert replay["2026-07-22"]["early_reversal"]["triggered"] is True
assert replay["2026-07-22"]["early_reversal"]["strength"] == pytest.approx(0.8314, abs=1e-4)
assert replay["2026-07-23"]["near_zone_events"]["crossed_bar_index"] is not None
assert replay["2026-07-23"]["near_zone_events"]["cleared_confirmed_bar_index"] is not None
assert replay["2026-08-05"]["major_zone_breakout"]["confirmed"] is True
assert replay["2026-08-05"]["major_zone_actionable_entry"] is True
```

Also assert R1 touch dates are all `<= 2026-07-21`.

- [ ] **Step 13: Run detector and replay suites**

Run:

```bash
python -m pytest tests/test_causal_bottom_divergence_detector.py tests/test_bottom_divergence_v2_replay.py -v
```

Expected: all v2 causal/replay tests PASS and v1 replay guard remains PASS.

- [ ] **Step 14: Optional commit checkpoint**

If explicitly authorized:

```bash
git add src/indicators/causal_bottom_divergence_detector.py tests/test_causal_bottom_divergence_detector.py tests/test_bottom_divergence_v2_replay.py
git commit -m "feat: add causal bottom divergence detector"
```

---

### Task 4: Add opt-in configuration and FactorService v2 factors

**Files:**

- Modify: `src/config.py:494-560`
- Modify: `src/config.py:1090-1110`
- Modify: `src/config.py:1650-1720`
- Modify: `.env.example`
- Modify: `src/services/factor_service.py:80-126`
- Modify: `src/services/factor_service.py:478-480`
- Modify: `src/services/factor_service.py:796-946`
- Modify: `tests/test_factor_service_bottom_divergence.py`
- Modify: `tests/test_config_validate_structured.py`

- [ ] **Step 1: Write failing config parse and structured-validation tests**

Add tests for these environment-backed values:

```text
BOTTOM_DIVERGENCE_V2_ENABLED=false
BOTTOM_DIVERGENCE_V2_CLUSTER_PCT=0.015
BOTTOM_DIVERGENCE_V2_ATR_GAP_MULTIPLIER=0.5
BOTTOM_DIVERGENCE_V2_ZONE_SCORE_MIN=0.45
BOTTOM_DIVERGENCE_V2_BREAKOUT_BUFFER_PCT=0.003
BOTTOM_DIVERGENCE_V2_SYNC_WINDOW=3
BOTTOM_DIVERGENCE_V2_RETENTION_BARS=20
BOTTOM_DIVERGENCE_V2_R1_WEIGHTS=0.30,0.25,0.15,0.15,0.10,0.05
BOTTOM_DIVERGENCE_V2_R2_WEIGHTS=0.35,0.15,0.15,0.15,0.10,0.10
BACKTEST_BUY_COST_BPS=0.0
BACKTEST_SELL_COST_BPS=0.0
BACKTEST_SLIPPAGE_BPS=0.0
```

Expected default: v2 disabled, so production selection remains unchanged.
Also assert invalid score, negative multiplier, zero sync window, and invalid weight sums produce
structured config issues rather than silently running.
Cost/slippage defaults remain zero for backward compatibility, but the v2 release validator must
mark its report ineligible until an explicit non-zero cost model is configured.

- [ ] **Step 2: Run config tests and verify RED**

Run:

```bash
python -m pytest tests/test_config_validate_structured.py -k "bottom_divergence_v2" -v
```

Expected: FAIL on missing config attributes.

- [ ] **Step 3: Add Config fields and `.env.example` documentation**

Add dataclass fields, environment parsing, and `validate_structured()` checks. Validate ranges:

- percentages/multipliers non-negative;
- score in `[0,1]`;
- sync window and retention bars positive integers.

- [ ] **Step 4: Write failing FactorService metadata tests**

Assert `build_factor_snapshot` passes through:

```python
{
    "data_source": row.data_source,
    "adj_factor": row.adj_factor,
    "adj_factor_source": row.adj_factor_source,
}
```

and does not claim known adjustment when metadata is absent.

Also add:

```python
def test_factor_service_accepts_isolated_config_without_mutating_global_singleton():
    isolated = copy.deepcopy(get_config())
    isolated.bottom_divergence_v2_enabled = True
    service = FactorService(config=isolated, db_manager=db)
    service.build_factor_snapshot(...)
    assert get_config().bottom_divergence_v2_enabled is False
```

- [ ] **Step 5: Add metadata to the StockDaily query projection**

Extend `all_bar_dicts` in `FactorService.build_factor_snapshot` without changing v1 OHLCV semantics.
Add an optional `config` constructor argument:

```python
def __init__(..., config: Config | None = None):
    self.config = config or get_config()
```

Derive existing constructor settings from `self.config`. Make v2 factor computation an instance
method that reads `self.config`; keep v1 static behavior unchanged. This is the injection point used
by validation and tests.

- [ ] **Step 6: Write failing v2 factor-flattening tests**

Define exact public fields:

```text
bottom_divergence_v2_candidate
bottom_divergence_v2_stage
bottom_divergence_v2_pattern_code
bottom_divergence_v2_early_reversal
bottom_divergence_v2_early_strength
bottom_divergence_v2_near_zone_lower
bottom_divergence_v2_near_zone_upper
bottom_divergence_v2_near_zone_score
bottom_divergence_v2_near_crossed
bottom_divergence_v2_near_cleared
bottom_divergence_v2_major_zone_lower
bottom_divergence_v2_major_zone_upper
bottom_divergence_v2_major_zone_score
bottom_divergence_v2_major_breakout
bottom_divergence_v2_major_actionable_entry
bottom_divergence_v2_actionability_status
bottom_divergence_v2_confirmation_days
bottom_divergence_v2_extended_pct
bottom_divergence_v2_stop_loss_price
bottom_divergence_v2_candidate_version
bottom_divergence_v2_zone_version
bottom_divergence_v2_candidate_records
bottom_divergence_v2_layered_buy_points
bottom_divergence_v2_degradation_reasons
```

When disabled or insufficient, every field must have a stable empty/default value.

- [ ] **Step 7: Run v2 factor tests and verify RED**

Run:

```bash
python -m pytest tests/test_factor_service_bottom_divergence.py -k "v2" -v
```

Expected: FAIL because v2 factor computation/flattening is absent.

- [ ] **Step 8: Implement `_compute_bottom_divergence_v2_factors`**

Call the v2 detector with `as_of_index=len(group)-1` and config-derived parameters. Keep `_compute_bottom_divergence_factors` untouched.

Stage priority:

```text
major_actionable > near_cleared > early > forming > invalidated/rejected
```

Set `bottom_divergence_v2_candidate=True` only for `early`, `near_cleared`, or `major_actionable`.
When `adjustment_unknown` is present, keep early/R1 evidence as observation-only.
If R2 has historically broken, project the stage as `major_unverified`; always force:

```text
bottom_divergence_v2_candidate=false
bottom_divergence_v2_major_actionable_entry=false
bottom_divergence_v2_actionability_status=adjustment_unknown
```

Execution is allowed only when every row has finite positive adjustment factors
and `adj_factor_source` is one of `tushare_native` or
`akshare_qfq_div_raw`.

- [ ] **Step 9: Run FactorService tests**

Run:

```bash
python -m pytest tests/test_factor_service_bottom_divergence.py -v
```

Expected: all legacy and v2 factor tests PASS.

- [ ] **Step 10: Optional commit checkpoint**

If explicitly authorized:

```bash
git add src/config.py .env.example src/services/factor_service.py tests/test_factor_service_bottom_divergence.py tests/test_config_validate_structured.py
git commit -m "feat: expose bottom divergence v2 factors"
```

---

### Task 5: Add the v2 strategy and five-layer decision semantics

**Files:**

- Create: `src/strategies/bottom_divergence_layered_entry.py`
- Create: `strategies/bottom_divergence_layered_entry_v2.yaml`
- Create: `tests/test_entry_strategy_e_v2.py`
- Modify: `src/schemas/trading_types.py:54-62`
- Modify: `src/services/setup_freshness_assessor.py:28-47`
- Modify: `src/services/entry_maturity_assessor.py:21-36`
- Modify: `src/services/entry_maturity_assessor.py:144-151`
- Modify: `src/services/trade_plan_builder.py:23-66`
- Modify: `src/services/trade_plan_builder.py:202-210`
- Modify: `src/services/trade_plan_builder.py:279-306`
- Modify: `src/services/five_layer_pipeline.py:513-549`
- Modify: `tests/test_decision_modules.py`
- Modify: `tests/test_trade_plan_builder.py`
- Modify: `tests/test_setup_resolver.py`
- Modify: `tests/test_five_layer_pipeline.py`

- [ ] **Step 1: Write failing setup-type and resolver tests**

Add:

```python
assert SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY.value == "bottom_divergence_layered_entry"
```

Verify a YAML rule with `setup_type: bottom_divergence_layered_entry` resolves without falling back to `NONE`.

- [ ] **Step 2: Add the new enum without aliasing v1**

Do not change `SetupType.BOTTOM_DIVERGENCE_BREAKOUT`.

- [ ] **Step 3: Write failing direct strategy tests**

Expected behavior:

- early triggers a probe result with 20% target-position guidance;
- R1 cleared triggers add-on guidance toward 50%;
- R2 actionable triggers full confirmation guidance;
- stale/extended R2 does not trigger;
- legacy `EntryStrategyE` output is unchanged.

- [ ] **Step 4: Run direct strategy tests and verify RED**

Run:

```bash
python -m pytest tests/test_entry_strategy_e_v2.py -v
```

Expected: FAIL because the v2 wrapper is absent.

- [ ] **Step 5: Implement the focused v2 strategy wrapper**

Return:

```python
{
    "triggered": bool,
    "stage": str,
    "actionable_entry": bool,
    "candidate_version": str | None,
    "zone_version": str | None,
    "entry_price": float | None,
    "stop_loss_price": float | None,
    "score": int,
    "reason": str,
}
```

- [ ] **Step 6: Write the v2 YAML**

Use:

```yaml
name: bottom_divergence_layered_entry_v2
display_name: 底背离分层入场 v2
category: reversal
system_role: entry_core
strategy_family: reversal
setup_type: bottom_divergence_layered_entry
screening:
  filters:
    - field: bottom_divergence_v2_candidate
      op: "=="
      value: true
  scoring:
    - field: bottom_divergence_v2_early_strength
      weight: 30
      cap: 1.0
    - field: bottom_divergence_v2_near_zone_score
      weight: 30
      cap: 1.0
    - field: bottom_divergence_v2_major_zone_score
      weight: 40
      cap: 1.0
```

Keep it opt-in; do not add it to default selected strategies until sample-out validation passes.

- [ ] **Step 7: Write failing maturity/freshness tests**

Expected mapping:

```text
forming/invalidated/stale/extended -> LOW
early -> MEDIUM
near_cleared -> HIGH
major_actionable -> HIGH
```

Freshness uses the active stage event bar, not a v1 confirmation bar.

- [ ] **Step 8: Run maturity/freshness tests and verify RED**

Run:

```bash
python -m pytest tests/test_decision_modules.py -k "layered_divergence" -v
```

Expected: FAIL until the new setup handler exists.

- [ ] **Step 9: Implement assessors**

Add a separate handler for the new setup type. Do not insert v2 keys into the v1 branch.

- [ ] **Step 10: Write failing trade-plan tests**

Assert:

- early: `initial_position="目标仓位20%"`, add rule references R1;
- R1: `initial_position="目标仓位50%"`, add rule references R2;
- R2 actionable: `initial_position="目标仓位100%"`;
- stop loss references `min(A,B)` buffer;
- invalidated/stale returns no executable add-on plan.

- [ ] **Step 11: Run trade-plan tests and verify RED**

Run:

```bash
python -m pytest tests/test_trade_plan_builder.py -k "layered_divergence" -v
```

Expected: FAIL until v2 trade-plan handling exists.

- [ ] **Step 12: Implement trade-plan templates and structured prices**

Consume `bottom_divergence_v2_layered_buy_points`; do not reuse legacy `bottom_divergence_buy_points`.

- [ ] **Step 13: Write a failing real five-layer execution test**

Use the actual `FiveLayerPipeline` with deterministic market/theme collaborators. Assert:

- early/R1/R2 snapshots have a stop-loss anchor derived from
  `bottom_divergence_v2_layered_buy_points`;
- early reaches `PROBE_ENTRY` when environment gates allow it;
- R1/R2 can reach the appropriate probe/add stage;
- a missing v2 stop-loss still caps the result at `FOCUS`;
- legacy setup behavior is unchanged.

- [ ] **Step 14: Run the five-layer test and verify RED**

Run:

```bash
python -m pytest tests/test_five_layer_pipeline.py -k "layered_divergence_stop_loss" -v
```

Expected: FAIL because `five_layer_pipeline.py` only reads the generic `has_stop_loss` key.

- [ ] **Step 15: Add a v2-only stop-loss resolver**

In `five_layer_pipeline.py`, derive `has_stop` from the v2 stop-loss price/buy-point evidence only
when `st == SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY`. Keep the existing
`fs.get("has_stop_loss", False)` behavior for every legacy setup.

- [ ] **Step 16: Run strategy/decision/five-layer tests**

Run:

```bash
python -m pytest tests/test_entry_strategy_e_v2.py tests/test_decision_modules.py tests/test_trade_plan_builder.py tests/test_setup_resolver.py tests/test_five_layer_pipeline.py -v
```

Expected: all v1 and v2 tests PASS.

- [ ] **Step 17: Optional commit checkpoint**

If explicitly authorized:

```bash
git add src/strategies/bottom_divergence_layered_entry.py strategies/bottom_divergence_layered_entry_v2.yaml src/schemas/trading_types.py src/services/setup_freshness_assessor.py src/services/entry_maturity_assessor.py src/services/trade_plan_builder.py src/services/five_layer_pipeline.py tests/test_entry_strategy_e_v2.py tests/test_decision_modules.py tests/test_trade_plan_builder.py tests/test_setup_resolver.py tests/test_five_layer_pipeline.py
git commit -m "feat: add layered divergence entry strategy"
```

---

### Task 6: Integrate AI evidence without weakening hard gates

**Files:**

- Modify: `src/services/candidate_analysis_service.py:25-50`
- Modify: `src/services/screening_ai_review_prompt_builder.py:46-58`
- Modify: `src/services/screening_ai_review_service.py`
- Modify: `src/services/screening_ai_review_guard.py`
- Modify: `tests/test_candidate_analysis_service.py`
- Modify: `tests/test_screening_ai_review_service.py`
- Modify: `tests/test_screening_ai_review_guard.py`
- Modify: `tests/test_screening_ai_review_prompt_builder.py`

- [ ] **Step 1: Write failing production prompt tests**

Build a real `CandidateDecision` with the v2 setup and assert the serialized production prompt
contains:

- stage and pattern code;
- only the available R1/R2 ranges for the current stage;
- touch dates/scores;
- candidate/zone versions;
- degradation reasons;
- historical major breakout and current actionability as separate values.

- [ ] **Step 2: Run and verify RED**

Run:

```bash
python -m pytest tests/test_screening_ai_review_prompt_builder.py -k "layered_divergence" -v
```

Expected: FAIL until the setup type/output contract is extended.

- [ ] **Step 3: Update the actual review prompt contract**

Add `bottom_divergence_layered_entry` to the allowed setup types. Keep `_compact_factor_snapshot`
generic, but add a compact structured v2 evidence block so early-stage evidence is not treated as
missing merely because R2 is absent. Increment `SCREENING_AI_REVIEW_PROMPT_VERSION` and update exact
prompt assertions.

- [ ] **Step 4: Write failing production service/guard tests**

Use `CandidateAnalysisService → ScreeningAiReviewService` with a fake LLM client. Assert:

- the v2 prompt reaches the client;
- early/R1/R2 stages remain distinguishable;
- missing adjustment metadata downgrades R2;
- AI cannot upgrade `FOCUS/WATCH` beyond environment/theme hard gates;
- stale/invalidated/extended signals cannot become actionable.

- [ ] **Step 5: Run service/guard tests and verify RED**

Run:

```bash
python -m pytest tests/test_candidate_analysis_service.py tests/test_screening_ai_review_service.py tests/test_screening_ai_review_guard.py -k "layered_divergence" -v
```

Expected: FAIL until the production review path understands v2 evidence/actionability.

- [ ] **Step 6: Implement the minimum production-path changes**

Implement through the production path
`CandidateAnalysisService → ScreeningAiReviewService → ScreeningAiReviewGuard`.
`screening_ai_review_prompt_builder.py` serializes the compact v2 evidence;
the guard enforces environment, adjustment provenance, stale/invalidated state,
and executable trade-plan constraints.

- [ ] **Step 7: Run AI production-path tests**

Run:

```bash
python -m pytest tests/test_candidate_analysis_service.py tests/test_screening_ai_review_service.py tests/test_screening_ai_review_guard.py tests/test_screening_ai_review_prompt_builder.py -v
```

Expected: PASS.

- [ ] **Step 8: Optional commit checkpoint**

If explicitly authorized:

```bash
git add src/services/candidate_analysis_service.py src/services/screening_ai_review_prompt_builder.py src/services/screening_ai_review_service.py src/services/screening_ai_review_guard.py tests/test_candidate_analysis_service.py tests/test_screening_ai_review_service.py tests/test_screening_ai_review_guard.py tests/test_screening_ai_review_prompt_builder.py
git commit -m "feat: add layered divergence AI evidence"
```

---

### Task 7: Persist versioned v2 evidence through the five-layer/backtest pipeline

**Files:**

- Modify: `tests/test_five_layer_pipeline.py`
- Modify: `tests/test_five_layer_backtest_pipeline.py`
- Create: `tests/test_bottom_divergence_v2_e2e_replay.py`
- Modify: `tests/test_screening_api.py`
- Modify: `tests/test_screening_api_schema.py`
- Modify only if tests prove necessary: `api/v1/schemas/screening.py`
- Modify only if tests prove necessary: `api/v1/endpoints/screening.py`
- Modify only if tests prove necessary: `src/backtest/services/backtest_service.py`
- Modify only if tests prove necessary: `src/backtest/aggregators/group_summary_aggregator.py`

- [ ] **Step 1: Write a failing five-layer persistence test**

Create a v2 candidate with:

```python
matched_strategies=["bottom_divergence_layered_entry_v2"]
setup_type="bottom_divergence_layered_entry"
factor_snapshot={
    "bottom_divergence_v2_stage": "early",
    "bottom_divergence_v2_candidate_version": "candidate-v2",
    "bottom_divergence_v2_zone_version": "zone-v2",
    "bottom_divergence_v2_candidate_records": [...],
}
```

Assert saved candidate decision and factor snapshot preserve all values.

- [ ] **Step 2: Run the persistence test and verify current behavior**

Run:

```bash
python -m pytest tests/test_five_layer_pipeline.py -k "layered_divergence" -v
```

Expected:

- PASS if generic JSON persistence already supports v2; make no production change.
- FAIL only if a hard-coded allowlist drops fields; then change the narrowest layer.

- [ ] **Step 3: Write a failing backtest evidence test**

Assert:

- strategy remains `bottom_divergence_layered_entry_v2`;
- snapshot setup type remains `bottom_divergence_layered_entry`;
- candidate/zone version survive into evidence;
- v1 and v2 are grouped separately.

- [ ] **Step 4: Run and implement the minimum required compatibility**

Run:

```bash
python -m pytest tests/test_five_layer_backtest_pipeline.py -k "layered_divergence" -v
```

Only modify production backtest files if the new test fails for a real hard-coded assumption.

- [ ] **Step 5: Write a failing full-chain point-in-time replay**

Seed a temporary SQLite database with the 001337 fixture. For each target date, run the actual:

```text
FactorService.build_factor_snapshot
→ YAML StrategyScreeningEngine
→ SetupResolver / EntryMaturityAssessor / SetupFreshnessAssessor
→ FiveLayerPipeline / TradePlanBuilder
→ CandidateAnalysisService production prompt path with a fake AI client
→ ScreeningCandidateItem API schema serialization
```

Run once with data through 07-22, 07-23, and 08-05. Then append/mutate later rows and rerun the
earlier trade dates. Assert:

- candidate/zone versions are unchanged for earlier dates;
- R1/R2 bounds and historical event dates do not repaint;
- trade plans and setup stages do not change for earlier dates;
- serialized API factor snapshot exactly preserves v2 fields;
- AI prompt sees the same frozen evidence;
- v1 output remains unchanged.

- [ ] **Step 6: Run the full-chain replay and verify RED**

Run:

```bash
python -m pytest tests/test_bottom_divergence_v2_e2e_replay.py -v
```

Expected: FAIL until all production integrations and API schema paths preserve the contract.

- [ ] **Step 7: Implement only proven integration gaps**

Keep `factor_snapshot: Dict[str, Any]` passthrough if it already works. Add explicit API/schema
fields only where serialization drops or rejects data. Do not create a database migration.

- [ ] **Step 8: Run API and full-chain tests**

Run:

```bash
python -m pytest tests/test_bottom_divergence_v2_e2e_replay.py tests/test_screening_api.py tests/test_screening_api_schema.py tests/test_five_layer_pipeline.py tests/test_five_layer_backtest_pipeline.py -v
```

Expected: PASS.

- [ ] **Step 9: Optional commit checkpoint**

If explicitly authorized:

```bash
git add tests/test_five_layer_pipeline.py tests/test_five_layer_backtest_pipeline.py tests/test_bottom_divergence_v2_e2e_replay.py tests/test_screening_api.py tests/test_screening_api_schema.py api/v1/schemas/screening.py api/v1/endpoints/screening.py src/backtest/services/backtest_service.py src/backtest/aggregators/group_summary_aggregator.py
git commit -m "test: preserve layered divergence backtest evidence"
```

Only add production files that actually changed.

---

### Task 8: Implement reproducible sample-out validation

**Files:**

- Create: `src/backtest/services/bottom_divergence_v2_models.py`
- Create: `src/backtest/services/bottom_divergence_v2_metrics.py`
- Create: `src/backtest/services/bottom_divergence_v2_selection.py`
- Create: `src/backtest/services/bottom_divergence_v2_validation.py`
- Create: `src/backtest/services/bottom_divergence_v2_report.py`
- Create: `src/backtest/services/bottom_divergence_v2_dataset.py`
- Create: `src/backtest/services/bottom_divergence_v2_replay.py`
- Create: `src/backtest/services/bottom_divergence_v2_cli_service.py`
- Create: `tests/test_bottom_divergence_v2_validation.py`
- Create: `tests/test_bottom_divergence_v2_validation_cli.py`
- Create: `tests/test_bottom_divergence_v2_validation_inputs.py`
- Create: `tests/test_bottom_divergence_v2_replay_service.py`
- Create: `scripts/validate_bottom_divergence_v2.py`
- Reuse: `src/backtest/services/backtest_service.py`
- Reuse: `src/config.py` shared buy/sell/slippage basis-point settings

- [ ] **Step 1: Write failing metric-definition tests**

Cover exact formulas for:

- 5/10/20-day gross and net returns after shared configured costs/slippage;
- MAE/MFE;
- equity-curve maximum drawdown;
- early→R1 and R1→R2 conversion rates;
- false breakout: close below zone lower within 3 sessions before 3% maximum close profit;
- Wilson 95% lower bound.

- [ ] **Step 2: Run metric tests and verify RED**

Run:

```bash
python -m pytest tests/test_bottom_divergence_v2_validation.py -k "returns or mae or mfe or drawdown or false_breakout or wilson" -v
```

Expected: FAIL because the validation service is missing.

- [ ] **Step 3: Implement `ValidationSample` and pure metric functions**

Do not add `forward_return_20d` to the ORM. Create an in-memory/dataclass sample containing:

```python
@dataclass(frozen=True)
class ValidationSample:
    code: str
    signal_date: date
    candidate_version: str
    strategy_version: str
    stage: str
    entry_close: float
    near_zone_lower: float | None
    major_zone_lower: float | None
    early_event_date: date | None
    near_cleared_event_date: date | None
    major_breakout_event_date: date | None
    close_5d: float | None
    close_10d: float | None
    close_20d: float | None
    future_closes_20d: tuple[float, ...]
    future_highs_20d: tuple[float, ...]
    future_lows_20d: tuple[float, ...]
    max_high_20d: float | None
    min_low_20d: float | None
    market_regime: str
    volatility: float
    liquidity: float
```

Build it from point-in-time factor/candidate evidence plus `stock_daily` future bars. Reuse existing
backtest return conventions for 5/10 days, extend the same pure formula to 20 days, and apply:

```text
round_trip_cost_bps = buy_cost_bps + sell_cost_bps + 2*slippage_bps
net_return = gross_return - round_trip_cost_bps/100
```

Do not persist this sample or change the ORM schema.
Use `future_closes_20d[:3]` with the stage-appropriate zone lower to calculate false breakouts.
Group the frozen event dates by `candidate_version` to calculate early→R1→R2 conversion without
joining unrelated structures from the same stock.

- [ ] **Step 4: Write failing chronological split/group tests**

Assert:

- chronological 60/20/20 split uses unique signal dates and never shuffles;
- volatility/liquidity tertile boundaries are learned from training only and frozen;
- test-period boundaries are not read during tuning;
- stages with fewer than 100 test signals fail eligibility;
- groups with fewer than 30 signals are excluded;
- v1/v2 strategy keys never mix.
- an all-zero cost model makes the release report ineligible.

- [ ] **Step 5: Run split/group tests and verify RED**

Run:

```bash
python -m pytest tests/test_bottom_divergence_v2_validation.py -k "split or tertile or sample or strategy_version" -v
```

Expected: FAIL until split/group logic exists.

- [ ] **Step 6: Implement split, rolling selection, and frozen test evaluation**

Pre-register this finite parameter grid in the test/validator:

```text
cluster_pct: [0.010, 0.015, 0.020]
atr_gap_multiplier: [0.50, 0.75]
zone_score_min: [0.40, 0.45, 0.50]
```

Expose:

```python
report = BottomDivergenceV2Validator.evaluate(
    v1_samples=v1_samples,
    v2_samples_by_parameter_hash=v2_samples_by_parameter_hash,
    train_ratio=0.60,
    validation_ratio=0.20,
    test_ratio=0.20,
)
```

For every grid candidate, generate/replay train and validation samples. Select the highest validation
10-day net expectancy among candidates that pass training/validation risk constraints; break ties by
parameter hash. Lock the selected hash, then generate/evaluate its test samples exactly once.

- [ ] **Step 7: Write failing non-inferiority decision tests**

Cover:

```text
R2 expectancy >= 98% of positive v1 baseline
if v1 expectancy <= 0, v2 expectancy >= v1 expectancy
maximum drawdown degradation <= 2 percentage points
false-breakout degradation <= 3 percentage points
R1 expectancy > 0 and MAE median degradation <= 1 percentage point
early expectancy > 0 and conversion >= frozen Wilson lower bound
>= 70% eligible regime/volatility/liquidity groups positive
report also includes signal coverage, win rate, and payoff ratio for every stage/version
```

Any sample-size failure must produce `eligible=false`, not an accidental pass.

- [ ] **Step 8: Run decision tests and verify RED**

Run:

```bash
python -m pytest tests/test_bottom_divergence_v2_validation.py -k "noninferiority or eligible" -v
```

Expected: FAIL until report gating exists.

- [ ] **Step 9: Implement the validation report and CLI**

`scripts/validate_bottom_divergence_v2.py` must:

1. enumerate historical trade dates and build point-in-time factor snapshots with
   `FactorService.build_factor_snapshot(..., trade_date=T, persist=False)`;
2. create an isolated v2 runtime/config object with `enabled=True` for the validation process only,
   and inject it through `FactorService(config=isolated_config)`; do not mutate process-wide
   environment variables, singleton production config, or `.env`;
3. for each pre-registered parameter hash, evaluate v1 and v2 YAML rules against the same universe/date without reading previously saved
   screening candidates;
4. feed generated candidates into existing five-layer/backtest evaluation components in an
   isolated temporary database/run;
5. build in-memory `ValidationSample` rows with enough future bars for 20-day metrics;
6. select parameters using train/validation only, replay the locked parameter once on test, and run
   the frozen validator;
7. write canonical JSON with data version, universe identity, cost model, parameter grid/selected
   hash, split dates, signal coverage, win rate, payoff ratio, metrics,
   and pass/fail reasons;
8. exit 0 only when eligible and all gates pass.

The CLI is orchestration only: it must reuse `FactorService`, `StrategyScreeningEngine`,
`FiveLayerPipeline`, and existing backtest evaluation conventions rather than duplicating detector or
screening logic. The only new return logic is the in-memory 20-day extension and shared configured
cost adjustment described above.

Final implementation note: the focused Task 8 suite contains 113 tests across
`test_bottom_divergence_v2_validation.py`,
`test_bottom_divergence_v2_validation_cli.py`,
`test_bottom_divergence_v2_validation_inputs.py`, and
`test_bottom_divergence_v2_replay_service.py`. Task 1 replay and end-to-end
replay files are verified separately.

- [ ] **Step 10: Run the validation unit suite**

Run:

```bash
python -m pytest tests/test_bottom_divergence_v2_validation.py -v
```

Expected: PASS.

- [ ] **Step 11: Run a real validation command**

Run:

```bash
python scripts/validate_bottom_divergence_v2.py --date-from 2024-01-01 --date-to 2026-07-31 --market cn --output .claude/reviews/bottom-divergence-v2-validation.json
```

Expected: canonical report written. A non-zero exit means v2 remains disabled; it is evidence, not
a reason to loosen thresholds.

- [ ] **Step 12: Optional commit checkpoint**

If explicitly authorized:

```bash
git add src/backtest/services/bottom_divergence_v2_validation.py tests/test_bottom_divergence_v2_validation.py scripts/validate_bottom_divergence_v2.py
git commit -m "feat: add divergence v2 sample-out validation"
```

---

### Task 9: Add Web stage and resistance-zone presentation

**Files:**

- Modify: `apps/dsa-web/src/types/screening.ts:114-121`
- Modify: `apps/dsa-web/src/types/screening.ts:399-504`
- Modify: `apps/dsa-web/src/types/screening.ts:555-562`
- Modify: `apps/dsa-web/src/components/screening/TechnicalPatternCards.tsx:26-67`
- Modify: `apps/dsa-web/src/components/screening/__tests__/TechnicalPatternCards.test.tsx`
- Modify: `apps/dsa-web/src/pages/BacktestPage.tsx:251-308`
- Modify: `apps/dsa-web/src/pages/__tests__/BacktestPage.test.tsx`

- [ ] **Step 1: Write failing TypeScript component tests**

Cases:

- early card displays “早期反转·试仓” and 20%;
- R1 card displays the R1 range and “近端突破·加仓”;
- R2 actionable card displays R2 range and “主要阻力确认”;
- stale/invalidated card never displays actionable wording;
- v1 card rendering remains unchanged;
- degradation reasons and version IDs appear in details, not the compact title.

- [ ] **Step 2: Run the focused Web test and verify RED**

Run:

```bash
cd apps/dsa-web
npm test -- --run src/components/screening/__tests__/TechnicalPatternCards.test.tsx
```

Expected: FAIL until v2 types/extractor exist.

- [ ] **Step 3: Extend Web types**

Add:

```ts
export type SetupType =
  | 'bottom_divergence_breakout'
  | 'bottom_divergence_layered_entry'
  // existing values...
```

Add exact `bottom_divergence_v2_*` fields to `ScreeningFactorSnapshot`.

- [ ] **Step 4: Implement a separate v2 pattern extractor**

Run it before the v1 extractor. Do not require
`bottom_divergence_double_breakout` for early/R1 cards.

Use range formatting:

```text
R1 37.46–39.20
R2 40.61–42.90
```

- [ ] **Step 5: Add backtest strategy labels**

Map:

```text
bottom_divergence_layered_entry -> 底背离分层入场
bottom_divergence_layered_entry_v2 -> 底背离分层入场 v2
```

- [ ] **Step 6: Run Web tests, lint, and build**

Run:

```bash
cd apps/dsa-web
npm test -- --run src/components/screening/__tests__/TechnicalPatternCards.test.tsx src/pages/__tests__/BacktestPage.test.tsx
npm run lint
npm run build
```

Expected: tests PASS, lint exits 0, production build exits 0.

- [ ] **Step 7: Optional commit checkpoint**

If explicitly authorized:

```bash
git add apps/dsa-web/src/types/screening.ts apps/dsa-web/src/components/screening/TechnicalPatternCards.tsx apps/dsa-web/src/components/screening/__tests__/TechnicalPatternCards.test.tsx apps/dsa-web/src/pages/BacktestPage.tsx apps/dsa-web/src/pages/__tests__/BacktestPage.test.tsx
git commit -m "feat: show layered divergence entry stages"
```

---

### Task 10: Document rollout and run full verification

**Files:**

- Modify: `README.md`
- Modify: `strategies/README.md`
- Modify: `docs/CHANGELOG.md`
- Verify: `.env.example`

- [ ] **Step 1: Update user-facing documentation**

Document:

- v1 remains default and unchanged;
- v2 is opt-in through `BOTTOM_DIVERGENCE_V2_ENABLED`;
- early/R1/R2 investment semantics;
- exact v2 factor fields;
- config defaults and safe rollback;
- 001337 is an explainability regression, not parameter-training evidence;
- trendline and resistance zones are frozen at B+1.

- [ ] **Step 2: Add changelog entries**

Under `[Unreleased]`, describe:

- Added causal v2 detector and resistance zones;
- Added staged entry strategy;
- Preserved legacy v1;
- Added point-in-time and sample-out test coverage.

- [ ] **Step 3: Run Python compile checks**

Run:

```bash
python -m py_compile src/indicators/resistance_zone_detector.py src/indicators/causal_bottom_divergence_support.py src/indicators/causal_bottom_divergence_events.py src/indicators/causal_bottom_divergence_detector.py src/strategies/bottom_divergence_layered_entry.py src/services/bottom_divergence_v2_trade_support.py src/services/factor_service.py src/services/setup_freshness_assessor.py src/services/entry_maturity_assessor.py src/services/trade_plan_builder.py src/services/five_layer_pipeline.py src/services/screening_ai_review_prompt_builder.py src/services/screening_ai_review_guard.py src/backtest/services/bottom_divergence_v2_models.py src/backtest/services/bottom_divergence_v2_metrics.py src/backtest/services/bottom_divergence_v2_selection.py src/backtest/services/bottom_divergence_v2_validation.py src/backtest/services/bottom_divergence_v2_report.py src/backtest/services/bottom_divergence_v2_dataset.py src/backtest/services/bottom_divergence_v2_replay.py src/backtest/services/bottom_divergence_v2_cli_service.py scripts/validate_bottom_divergence_v2.py
```

Expected: exit 0, no output.

- [ ] **Step 4: Run all focused backend tests**

Run:

```bash
python -m pytest tests/test_resistance_zone_detector.py tests/test_causal_bottom_divergence_detector.py tests/test_bottom_divergence_v2_replay.py tests/test_bottom_divergence_v2_replay_service.py tests/test_bottom_divergence_v2_e2e_replay.py tests/test_bottom_divergence_v2_validation.py tests/test_bottom_divergence_v2_validation_cli.py tests/test_bottom_divergence_v2_validation_inputs.py tests/test_config_validate_structured.py tests/test_factor_service_bottom_divergence.py tests/test_entry_strategy_e.py tests/test_entry_strategy_e_v2.py tests/test_decision_modules.py tests/test_trade_plan_builder.py tests/test_setup_resolver.py tests/test_candidate_analysis_service.py tests/test_screening_ai_review_service.py tests/test_screening_ai_review_guard.py tests/test_screening_ai_review_prompt_builder.py tests/test_five_layer_pipeline.py tests/test_five_layer_backtest_pipeline.py tests/test_five_layer_aggregator.py tests/test_screening_api.py tests/test_screening_api_schema.py -v
```

Expected: all focused tests PASS.

- [ ] **Step 5: Run repository backend gate**

Run:

```bash
./scripts/ci_gate.sh
```

Expected: backend gate exits 0.

On Windows without a compatible Bash environment, run the gate’s individual Python commands and explicitly record the platform gap.

- [ ] **Step 6: Run Web verification**

Run:

```bash
cd apps/dsa-web
npm ci
npm run lint
npm run build
```

Expected: install, lint, and build exit 0.

- [ ] **Step 7: Run point-in-time replay evidence**

Run:

```bash
python -m pytest tests/test_bottom_divergence_v2_replay.py -v
```

Expected:

```text
2026-07-22 early=True
2026-07-23 R1 cleared_confirmed=True
2026-08-05 R2 confirmed=True historical
```

With trusted adjustment provenance, the current R2 may additionally be
actionable. With unknown provenance it must be `major_unverified` and
non-executable; early/R1 remain observation evidence only.

- [ ] **Step 8: Verify backward compatibility explicitly**

Run:

```bash
python -m pytest tests/test_bottom_divergence_breakout_detector.py tests/test_entry_strategy_e.py tests/test_factor_service_bottom_divergence.py -v
```

Expected: all legacy v1 tests PASS unchanged.

- [ ] **Step 9: Run sample-out release gate**

Run:

```bash
python scripts/validate_bottom_divergence_v2.py --date-from 2024-01-01 --date-to 2026-07-31 --market cn --output .claude/reviews/bottom-divergence-v2-validation.json
```

Expected: exit 0 only when sample eligibility and every pre-registered non-inferiority gate pass.
Otherwise keep v2 disabled and report the failed gates without retuning on the test set.

- [ ] **Step 10: Review git diff and rollback path**

Confirm:

- no secrets or generated local review files are staged;
- v2 defaults disabled;
- removing the v2 YAML and setting `BOTTOM_DIVERGENCE_V2_ENABLED=false` returns runtime behavior to v1;
- no database migration is required.

- [ ] **Step 11: Optional final commit checkpoint**

If explicitly authorized:

```bash
git add README.md strategies/README.md docs/CHANGELOG.md docs/superpowers/specs/2026-08-06-bottom-divergence-resistance-zones-design.md docs/superpowers/plans/2026-08-06-bottom-divergence-resistance-zones.md
git commit -m "docs: document layered divergence rollout"
```

Do not push without separate explicit authorization.

---

## Execution Order and Checkpoints

1. Tasks 1–3 form the causal analytical core. Do not start integrations until all point-in-time invariance tests pass.
2. Task 4 exposes v2 factors but keeps them disabled by default.
3. Tasks 5–7 integrate strategy, decision, AI, and backtest semantics while preserving v1.
4. Task 8 implements the sample-out gate; v2 must remain disabled when data is ineligible or any gate fails.
5. Task 9 adds Web presentation only after the backend/API factor contract is stable.
6. Task 10 is the release gate. A failing v1 regression, point-in-time invariant, Web build, or sample-out risk non-inferiority check blocks rollout.

## Rollback

- Runtime rollback: set `BOTTOM_DIVERGENCE_V2_ENABLED=false` (the default).
- Strategy rollback: remove `bottom_divergence_layered_entry_v2` from requested strategies.
- Code rollback: revert only v2 files and additive enum/factor/UI branches; legacy detector and strategy remain untouched.
- Data rollback: no schema migration is planned; existing JSON snapshots remain readable because all changes are additive.
