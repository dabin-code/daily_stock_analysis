# K 线完整性治理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 A 股日线数据建立“同步、审计、补偿、豁免、定时治理”的完整闭环，保证非豁免股票在审计窗口内 K 线完整，且长期失败股票不会卡住主流程。

**Architecture:** 先在存储层建立 `kline_audit_runs / kline_audit_trade_dates / kline_audit_gaps / kline_audit_events / kline_skip_registry` 这组真相源，再在服务层新增 `kline_audit_service / kline_repair_service / kline_governance_schedule_service`。现有 `market_data_sync_service`、`screening_task_service`、`factor_service` 改为围绕审计结果协作，而不是依赖覆盖率阈值做隐式放行。

**Tech Stack:** Python 3.12, SQLAlchemy/SQLite, pytest, schedule, existing `src/config.py` / `src/storage.py` / `main.py` service patterns.

---

## File Structure

### New Files

- `src/services/kline_audit_service.py`
  - 负责构造应有集合、识别 `market_day_gap` / `symbol_range_gap`、写入审计真相源。
- `src/services/kline_repair_service.py`
  - 负责消费 gap 记录、执行补偿、复审、状态迁移。
- `src/services/kline_governance_schedule_service.py`
  - 负责 17:00 调度入口、目标交易日解析、调用审计/补偿服务。
- `scripts/audit_kline_completeness.py`
  - 手工审计与一次性存量修复入口。
- `scripts/approve_kline_skip.py`
  - 数据库/脚本方式确认 `candidate_skip -> approved_skip`。
- `tests/test_kline_audit_service.py`
  - 审计集合、gap 拆分、`pass_status` 合同测试。
- `tests/test_kline_repair_service.py`
  - gap 重试、候选豁免、恢复事件测试。
- `tests/test_kline_governance_schedule_service.py`
  - 17:00 任务、交易日历不可用、非交易日跳过测试。
- `tests/test_approve_kline_skip_script.py`
  - 候选审批脚本行为测试。
- `tests/test_audit_kline_completeness_script.py`
  - 手工审计/修复脚本参数与行为测试。

### Modify Existing Files

- `src/storage.py`
  - 新增审计/豁免表模型、迁移逻辑、读写接口。
- `src/config.py`
  - 新增 K 线治理配置项与默认值。
- `src/core/config_registry.py`
  - 注册新增配置项描述。
- `src/services/market_data_sync_service.py`
  - 去掉“80% 覆盖率即放过”的逻辑，改为输出更明确的失败分类。
- `src/services/screening_task_service.py`
  - ingest 阶段改为接收审计/补偿结果，不再把 `fetch_failed` 当普通可跳过结果。
- `src/services/factor_service.py`
  - `get_latest_trade_date()` 改为只读取审计通过的 `trade_date`。
- `src/scheduler.py`
  - 注册新的 K 线治理任务。
- `main.py`
  - 接入 `KlineGovernanceScheduleService`。
- `.env.example`
  - 暴露新增配置项。
- `README.md`
  - 补充 K 线治理任务与 17:00 定时行为说明。
- `docs/CHANGELOG.md`
  - 记录用户可见/运维可见能力变更。

### Existing Tests To Extend

- `tests/test_market_data_sync_service.py`
- `tests/test_screening_task_service.py`
- `tests/test_factor_service.py`
- `tests/test_scheduler.py`
- `tests/test_config_env_compat.py`
- `tests/test_board_sync_schedule_service.py`
- `tests/test_main_screening_schedule.py`

### Repo Policy Note

- 本仓库规则要求：**除非用户明确要求，不执行 git commit**。
- 下面各任务的最后一步统一使用“检查点（Checkpoint）”替代提交动作。

---

## Task 1: 建立审计与豁免真相源

**Files:**
- Create: `tests/test_kline_audit_storage.py`
- Modify: `src/storage.py`
- Modify: `src/config.py`
- Modify: `src/core/config_registry.py`
- Modify: `.env.example`
- Test: `tests/test_kline_audit_storage.py`
- Test: `tests/test_config_env_compat.py`

- [ ] **Step 1: Write the failing storage tests**

```python
def test_kline_audit_tables_store_gap_and_trade_date_status():
    # run -> trade_date -> gap/event linkage
    ...


def test_skip_registry_supports_symbol_and_date_range_scope():
    # code + date_from/date_to + status persisted correctly
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest tests/test_kline_audit_storage.py tests/test_config_env_compat.py -v
```

Expected:

- `tests/test_kline_audit_storage.py` fails because new models/APIs do not exist yet.

- [ ] **Step 3: Add minimal storage models and APIs**

Implement in `src/storage.py`:

- SQLAlchemy models for:
  - `KlineAuditRun`
  - `KlineAuditTradeDate`
  - `KlineAuditGap`
  - `KlineAuditEvent`
  - `KlineSkipRegistry`
- APIs for:
  - create/list audit runs
  - upsert trade date audit result
  - upsert/list gaps by `gap_key`
  - append audit events
  - create/update/list skip registry entries

Lock the minimum contract here instead of deferring it:

- `kline_audit_trade_dates` required fields:
  - `window_start`
  - `window_end`
  - `rule_version`
  - `source_run_id`
  - `passed_at`
- deterministic `gap_key` rules:
  - `market_day_gap = market + trade_date + gap_scope`
  - `symbol_range_gap = market + code + missing_date_from + missing_date_to + gap_scope`
- `kline_skip_registry` recovery fields:
  - `success_streak`
  - `last_success_at`
  - `last_recovered_at`

Add config keys in `src/config.py` / `src/core/config_registry.py` / `.env.example`:

- `KLINE_GOVERNANCE_ENABLED`
- `KLINE_GOVERNANCE_SCHEDULE_TIME`
- `KLINE_GOVERNANCE_RUN_IMMEDIATELY`
- `KLINE_AUDIT_LOOKBACK_DAYS`
- `KLINE_DEEP_AUDIT_LOOKBACK_DAYS`
- `KLINE_DEEP_AUDIT_SCHEDULE_ENABLED`
- `KLINE_DEEP_AUDIT_SCHEDULE_TIME`
- `KLINE_RETRY_MAX_ATTEMPTS`
- `KLINE_SKIP_CANDIDATE_FAILURE_THRESHOLD`

- [ ] **Step 4: Run targeted tests**

Run:

```bash
python -m pytest tests/test_kline_audit_storage.py tests/test_config_env_compat.py -v
python -m py_compile src/storage.py src/config.py src/core/config_registry.py
```

Expected:

- New storage/config tests PASS.
- `py_compile` succeeds.

- [ ] **Step 5: Checkpoint**

Validate:

- New truth-source tables and config entries exist.
- `.env.example` contains the new environment variables.

---

## Task 2: 实现 K 线审计服务

**Files:**
- Create: `src/services/kline_audit_service.py`
- Create: `tests/test_kline_audit_service.py`
- Modify: `src/storage.py`
- Test: `tests/test_kline_audit_service.py`
- Test: `tests/test_factor_service.py`

- [ ] **Step 1: Write the failing audit service tests**

```python
def test_audit_service_builds_market_day_gap_for_sparse_trade_date():
    ...


def test_audit_service_splits_non_contiguous_symbol_gaps_into_ranges():
    ...


def test_audit_service_marks_trade_date_not_passed_when_run_degraded():
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest tests/test_kline_audit_service.py tests/test_factor_service.py -v
```

Expected:

- Audit service tests fail because service and trade-date truth-source wiring do not exist.

- [ ] **Step 3: Implement the minimal audit service**

Implement in `src/services/kline_audit_service.py`:

- resolve expected stock universe from `instrument_master`
- exclude:
  - pre-listing
  - delisted / non-active listings
  - approved symbol/date-range skips
  - approved suspended ranges
- build gaps:
  - `market_day_gap`
  - `symbol_range_gap`
- persist:
  - `kline_audit_runs`
  - `kline_audit_trade_dates`
  - `kline_audit_gaps`
  - `kline_audit_events`
- set:
  - `run_result`
  - `pass_status`
  - `window_start/window_end/rule_version/source_run_id/passed_at`

- [ ] **Step 4: Wire `FactorService` to the audit truth source**

Change `src/services/factor_service.py` so `get_latest_trade_date()`:

- reads the latest `pass_status='passed'` trade date
- validates the stored window range covers the requested lookback
- returns `None` with a clear warning when no passed trade date exists

- [ ] **Step 5: Run targeted tests**

Run:

```bash
python -m pytest tests/test_kline_audit_service.py tests/test_factor_service.py -v
python -m py_compile src/services/kline_audit_service.py src/services/factor_service.py
```

Expected:

- Audit service tests PASS.
- FactorService tests PASS or only fail where contract changed and are updated accordingly.

- [ ] **Step 6: Checkpoint**

Validate:

- A trade date is only consumable when `pass_status='passed'`.
- Gaps are persisted as stable entities, not ad hoc logs.

---

## Task 3: 先锁同步结果分类契约

**Files:**
- Modify: `src/services/market_data_sync_service.py`
- Modify: `tests/test_market_data_sync_service.py`
- Test: `tests/test_market_data_sync_service.py`

- [ ] **Step 1: Write the failing sync-classification tests**

```python
def test_sync_trade_date_returns_retryable_vs_blocking_vs_skip_eligible_metadata():
    ...


def test_sync_trade_date_does_not_treat_80_percent_coverage_as_healthy():
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest tests/test_market_data_sync_service.py -v
```

Expected:

- Tests fail because current sync service still uses implicit coverage short-circuiting and lacks stable failure categories.

- [ ] **Step 3: Implement the minimal sync result contract**

Update `src/services/market_data_sync_service.py`:

- remove “available_ratio/min_available_ratio reached -> stop caring” semantics
- classify failures into:
  - `blocking`
  - `retryable`
  - `skip_eligible`
- return enough metadata for downstream audit/repair to reason on:
  - source attempts
  - target trade date
  - failure detail
  - candidate-skip eligibility signals

- [ ] **Step 4: Run targeted tests**

Run:

```bash
python -m pytest tests/test_market_data_sync_service.py -v
python -m py_compile src/services/market_data_sync_service.py
```

Expected:

- Sync contract tests PASS.

- [ ] **Step 5: Checkpoint**

Validate:

- Repair service can depend on a stable sync classification contract instead of temporary heuristics.

---

## Task 4: 实现补偿服务、自动恢复与候选豁免审批脚本

**Files:**
- Create: `src/services/kline_repair_service.py`
- Create: `scripts/approve_kline_skip.py`
- Create: `tests/test_kline_repair_service.py`
- Create: `tests/test_approve_kline_skip_script.py`
- Modify: `src/storage.py`
- Test: `tests/test_kline_repair_service.py`
- Test: `tests/test_approve_kline_skip_script.py`

- [ ] **Step 1: Write the failing repair, recovery, and approval tests**

```python
def test_repair_service_retries_pending_retry_gap_and_records_event():
    ...


def test_repair_service_promotes_gap_to_candidate_skip_after_threshold():
    ...


def test_repair_service_recovers_candidate_skip_after_window_repaired():
    ...


def test_repair_service_recovers_approved_skip_only_after_three_consecutive_successes():
    ...


def test_approve_skip_script_marks_candidate_as_approved_skip():
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest tests/test_kline_repair_service.py tests/test_approve_kline_skip_script.py -v
```

Expected:

- Tests fail because repair orchestration and approval script do not exist.

- [ ] **Step 3: Implement the minimal repair service**

Implement in `src/services/kline_repair_service.py`:

- consume unresolved gaps from storage
- retry `pending_retry` gaps using:
  - market-day batch repair for `market_day_gap`
  - symbol/date-range repair for `symbol_range_gap`
- promote to `candidate_skip` only when:
  - threshold reached
  - multiple sources tried
  - no recent success
- reuse the sync classification contract from `market_data_sync_service` instead of inventing a second failure taxonomy
- automatically recover:
  - `candidate_skip -> healthy`
  - `approved_skip -> healthy` only after 3 consecutive successful governance runs
  - append `recovered` event when gaps are fully repaired
- emit audit events for each transition

- [ ] **Step 4: Implement the approval script**

Implement `scripts/approve_kline_skip.py` to:

- list candidate gaps/stocks
- approve by code and optional date range
- record `approved_by`, `approved_at`, `reason_type`, `notes`
- support dry-run mode

- [ ] **Step 5: Run targeted tests**

Run:

```bash
python -m pytest tests/test_kline_repair_service.py tests/test_approve_kline_skip_script.py -v
python -m py_compile src/services/kline_repair_service.py scripts/approve_kline_skip.py
```

Expected:

- Repair and approval tests PASS.

- [ ] **Step 6: Checkpoint**

Validate:

- `candidate_skip` never directly becomes denominator-exempt without approval.
- `approved_skip` can be scoped by date range.
- successful backfill removes stale skip state and records a recovery event.
- `approved_skip` recovery respects the 3-success streak contract from the spec.

---

## Task 5: 改造筛选 ingest 口径并接入审计真相源

**Files:**
- Modify: `src/services/screening_task_service.py`
- Modify: `tests/test_screening_task_service.py`
- Test: `tests/test_screening_task_service.py`

- [ ] **Step 1: Write the failing behavior updates in existing tests**

Add/adjust tests so they assert:

```python
def test_screening_ingest_does_not_silently_accept_fetch_failed_as_success():
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest tests/test_screening_task_service.py -v
```

Expected:

- Existing ingest tests fail because screening still trusts raw sync heuristics instead of audit truth.

- [ ] **Step 3: Consume audit-derived truth in ingest**

Update `src/services/screening_task_service.py`:

- stop treating `fetch_failed` as an automatically skippable healthy outcome
- consume audit-derived truth instead of raw sync heuristics
- ensure `degraded` trade dates are not treated as passed downstream

- [ ] **Step 4: Run targeted tests**

Run:

```bash
python -m pytest tests/test_screening_task_service.py -v
python -m py_compile src/services/screening_task_service.py
```

Expected:

- Updated sync/ingest tests PASS.

- [ ] **Step 5: Checkpoint**

Validate:

- Non-exempt gaps are never silently dropped.
- Screening ingest can continue in controlled `degraded` mode without calling the trade date healthy.

---

## Task 6: 接入 17:00 每日治理任务与低频深度审计

**Files:**
- Create: `src/services/kline_governance_schedule_service.py`
- Create: `tests/test_kline_governance_schedule_service.py`
- Modify: `src/scheduler.py`
- Modify: `main.py`
- Modify: `src/config.py`
- Modify: `tests/test_scheduler.py`
- Modify: `tests/test_main_screening_schedule.py`
- Test: `tests/test_kline_governance_schedule_service.py`
- Test: `tests/test_scheduler.py`
- Test: `tests/test_main_screening_schedule.py`
- Test: `tests/test_board_sync_schedule_service.py`

- [ ] **Step 1: Write the failing schedule tests**

```python
def test_kline_governance_schedule_runs_at_1700_cn_market_only():
    ...


def test_kline_governance_schedule_fails_closed_when_calendar_unavailable():
    ...


def test_kline_governance_schedule_registers_deep_audit_as_independent_job():
    ...


def test_kline_governance_schedule_runs_sync_then_audit_then_repair_then_reaudit():
    ...


def test_deep_audit_job_scans_long_window_and_rechecks_candidate_skips():
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest tests/test_kline_governance_schedule_service.py tests/test_scheduler.py tests/test_main_screening_schedule.py tests/test_board_sync_schedule_service.py -v
```

Expected:

- New governance schedule tests fail because service and registration do not exist.

- [ ] **Step 3: Implement schedule service and hook it into runtime**

Implement `src/services/kline_governance_schedule_service.py`:

- resolve target `trade_date`
- fail closed when calendar is unavailable and no explicit `trade_date` is provided
- execute the full governance sequence:
  - incremental sync
  - audit
  - repair
  - re-audit
- return `run_result/pass_status`
- expose a separate deep-audit entry that scans the longer historical window and re-evaluates candidate skips

Update `src/scheduler.py` and `main.py`:

- register the K-line governance task
- default time `17:00`
- support config-driven enable/disable and run-immediately behavior
- register the low-frequency deep audit task as an independent job rather than overloading the daily path

- [ ] **Step 4: Run targeted tests**

Run:

```bash
python -m pytest tests/test_kline_governance_schedule_service.py tests/test_scheduler.py tests/test_main_screening_schedule.py tests/test_board_sync_schedule_service.py -v
python -m py_compile src/services/kline_governance_schedule_service.py src/scheduler.py main.py
```

Expected:

- Schedule tests PASS.

- [ ] **Step 5: Checkpoint**

Validate:

- K-line governance task is isolated from screening and board sync tasks.
- Calendar-unavailable branch is fail-closed, not fail-open.
- Daily governance and deep-audit jobs can run independently.
- Daily job performs `sync -> audit -> repair -> re-audit` in that exact order.
- Deep audit validates the longer historical window and re-evaluates candidate skips.

---

## Task 7: 文档、说明、脚本与最终验证

**Files:**
- Create: `scripts/audit_kline_completeness.py`
- Create: `tests/test_audit_kline_completeness_script.py`
- Modify: `README.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `.env.example`
- Test: `tests/test_kline_audit_service.py`
- Test: `tests/test_kline_repair_service.py`
- Test: `tests/test_audit_kline_completeness_script.py`

- [ ] **Step 1: Write the failing script/documentation expectations**

Add/adjust failing tests so they assert:

```python
def test_audit_kline_completeness_script_supports_dry_run_and_repair_modes():
    ...


def test_audit_kline_completeness_script_can_target_explicit_trade_date():
    ...
```

- [ ] **Step 2: Run the script-level checks to verify missing pieces**

Run:

```bash
python -m pytest tests/test_audit_kline_completeness_script.py -v
```

Expected:

- Script tests fail before the script exists.

- [ ] **Step 3: Implement the repair/audit script and docs**

Implement `scripts/audit_kline_completeness.py` with modes:

- `--dry-run`
- `--trade-date YYYY-MM-DD`
- `--repair`
- `--approve-suspended-range` (optional helper if kept minimal)

Update docs:

- `README.md`
- `docs/CHANGELOG.md`
- `.env.example`

Document:

- 17:00 governance task
- `candidate_skip -> approved_skip`
- how to run manual audit / repair
- what `pass_status` means for downstream consumption

- [ ] **Step 4: Run final focused verification**

Run:

```bash
python -m pytest tests/test_kline_audit_storage.py tests/test_kline_audit_service.py tests/test_kline_repair_service.py tests/test_kline_governance_schedule_service.py tests/test_approve_kline_skip_script.py tests/test_audit_kline_completeness_script.py -v
python -m py_compile scripts/audit_kline_completeness.py
```

Expected:

- All governance-focused tests PASS.

- [ ] **Step 5: Run broader regression checks**

Run:

```bash
python -m pytest tests/test_market_data_sync_service.py tests/test_screening_task_service.py tests/test_factor_service.py tests/test_scheduler.py tests/test_config_env_compat.py tests/test_board_sync_schedule_service.py -v
./scripts/ci_gate.sh
```

Expected:

- Existing critical paths still pass with the new contracts.

- [ ] **Step 6: Checkpoint**

Validate:

- Manual audit/repair entry exists.
- User-visible config and scheduling behavior are documented.
- Backend gate passes after the governance changes.

---

## Verification Matrix

- Storage/config phase:
  - `python -m pytest tests/test_kline_audit_storage.py tests/test_config_env_compat.py -v`
  - `python -m py_compile src/storage.py src/config.py src/core/config_registry.py`
- Audit/repair phase:
  - `python -m pytest tests/test_kline_audit_storage.py tests/test_kline_audit_service.py tests/test_kline_repair_service.py tests/test_approve_kline_skip_script.py tests/test_audit_kline_completeness_script.py -v`
  - `python -m py_compile src/services/kline_audit_service.py src/services/kline_repair_service.py`
- Downstream integration phase:
  - `python -m pytest tests/test_market_data_sync_service.py tests/test_screening_task_service.py tests/test_factor_service.py -v`
- Schedule/docs phase:
  - `python -m pytest tests/test_kline_governance_schedule_service.py tests/test_scheduler.py tests/test_main_screening_schedule.py tests/test_board_sync_schedule_service.py -v`
  - `python -m py_compile src/services/kline_governance_schedule_service.py scripts/audit_kline_completeness.py`
  - `./scripts/ci_gate.sh`

## Open Items For Execution

- Suspended-range truth source remains first-version minimal:
  - use approved skip registry entries with `reason_type=suspended_range`
  - do not expand to exchange-level suspension ingestion unless needed

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-16-kline-completeness-governance-implementation-plan.md`.

Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints.

Per repo policy, no commit should be created unless you explicitly request it.
