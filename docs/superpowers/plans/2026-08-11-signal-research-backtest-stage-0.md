# 信号研究回测 阶段 0 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复回测链路的四个纯代码根因（止损解析、规则分被覆盖、版本字段空缺、指标混族），并清空离线测试套件里候选与 L2 相关的 5 个失败，使后续历史重放的任何异常结果都能归因到结论本身而非实现缺陷。

**Architecture:** 全部改动不依赖历史数据，不触碰数据库既有行，也不启动任何重放。修复方式一律优先「追加字段 / 提取共享函数」，避免改变现有列语义与下游消费方。

**Tech Stack:** Python 3.12、SQLAlchemy ORM、pytest（marker 定义在 `setup.cfg`）、unittest 风格测试。

**Spec:** `docs/superpowers/specs/2026-08-11-signal-research-backtest-design.md`（v7），范围见其 11.-1 节。

---

## 范围与边界

**本计划做**：spec 11.-1 判定为「可开工」的「回测阶段 0 的纯代码与黄金测试」，共 8 个任务。

**明确不做**：历史候选生成、因子全量计算、任何重放、消融、对照组、`run_data_manifest` 的完整实现。这些依赖数据基础设施阶段 J，spec 11.0 已列为前置。

**为什么先清测试**：实测审计给出 3073 passed / 6 failed。带着红测试开工，后续任何反常结果都无法区分是「结论异常」还是「实现有 bug」，而这个项目的全部价值就在于结论可信。

---

## 5 个失败测试的实测归因

已逐条复现并定位根因。**它们不是同一类问题**，修法完全不同：

| 测试 | 单独跑 | 根因 | 性质 |
| --- | --- | --- | --- |
| `test_e2e_five_layer_local.py::TestL2SectorHeatEngine::test_compute_returns_non_empty_results` | **通过** | 测试污染 | 全局单例泄漏 |
| `test_e2e_five_layer_local.py::TestSectorHeatPersistence::test_compute_and_persist` | **通过** | 同上 | 同上 |
| `test_data_gap_fixes.py::BoardDataAvailabilityTestCase::test_sector_heat_engine_warns_on_empty_boards` | 失败 | 代码只写 debug 日志不告警 | 真实缺陷 |
| `test_theme_services.py::ThemePositionResolverBasicTestCase::test_warm_expand_fallback_caps_theme_count` | 失败 | 主路径无题材数上限 | 真实缺陷 |
| `golden_cases/test_candidate_decision_golden.py::...::test_pipeline_golden_case_builds_complete_candidate_decision` | 失败 | 断言未跟上涨停准入规则 | 断言过期 |

**污染源已定位**：`tests/test_data_health_service.py:9-11`

```python
def _build_db(tmp_path):
    DatabaseManager.reset_instance()
    return DatabaseManager(f"sqlite:///{tmp_path / 'data_health.db'}")
```

`DatabaseManager` 是单例（`src/storage.py:1189` 的 `__new__`），构造即把临时库**装成全局实例**，而该文件**没有任何 teardown 还原**。此后所有调用 `DatabaseManager.get_instance()` 的测试都拿到这个临时库。

验证证据：

```text
pytest tests/test_data_health_service.py <target>   →  1 failed
pytest <target>                                     →  1 passed
```

> 第 6 个失败 `test_env.py::test_notification` 属通知配置问题，与候选/L2 无关，**不在本计划范围**。

---

## 文件结构

**新建：**

| 文件 | 职责 |
| --- | --- |
| `src/services/setup_stop_loss.py` | setup 级止损解析的单一实现，供 L5 与交易计划构建共用 |
| `tests/test_setup_stop_loss.py` | 止损解析的分 setup 用例与反例 |
| `tests/conftest.py` 或既有 conftest 增补 | 全局单例的测试隔离 |

**修改：**

| 文件 | 改动 |
| --- | --- |
| `tests/test_data_health_service.py` | 加 teardown 还原 `DatabaseManager` 单例 |
| `src/services/sector_heat_engine.py:85-96` | 空板块时发 `logger.warning` |
| `src/services/theme_position_resolver.py:128-143` | 主路径应用题材数上限 |
| `tests/golden_cases/test_candidate_decision_golden.py` | `factor_snapshot` 加 `is_limit_up`、补非涨停反例；Task 5 还会再改一次它的 `has_stop_loss` |
| `src/services/screener_service.py:14-23` | `ScreeningCandidateRecord` 加 `raw_rule_score` |
| `src/schemas/trading_types.py:244,252-258` | `CandidateDecision` 加字段，`from_record` 补一行 |
| `src/services/trade_plan_builder.py:257-341` | 改调用共享止损解析 |
| `src/services/five_layer_pipeline.py:563-571` | `has_stop_loss` 改用共享解析 |
| `src/services/five_layer_pipeline.py:659-668` | 保留 `raw_rule_score` |
| `src/storage.py:849` | `screening_candidates` 新增 `raw_rule_score` 列 |
| `src/backtest/models/backtest_models.py:29-67` | 运行表新增 `code_revision`、`config_hash` |
| `src/backtest/repositories/run_repo.py:54-95` | `update_run_status` 打通版本字段 |
| `src/backtest/aggregators/group_summary_aggregator.py:203` | 按分组是否单族选择族过滤，混族行只算 entry |

---

## Task 1: 修复测试污染（全局单例泄漏）

**Files:**

- Modify: `tests/test_data_health_service.py:9-11`
- Test: 复现命令即验证手段

**为什么这个任务排第一**：它不只是让两个测试变绿。同一个 `DatabaseManager` 单例会被历史重放复用，而重放要跑 20+ 小时并写 `run_data_manifest`——单例在运行中被换掉，产出的数据指纹就对不上它实际读的库。测试里暴露出来的正是这个隐患。

> **新增实证（基础设施 Task 4 期间发现）：离线测试套件会写生产库文件。**
> 跑一次 `pytest -m "not network"` 前后对比 `data/stock_analysis.db` 的 mtime，
> 发现它在套件运行期间被推进（实测 10:56 → 11:10）。也就是说至少有一条用例
> 打开的是 `DATABASE_PATH` 默认指向的生产库，而不是自己的临时库。
>
> 这把本任务的范围从「两个测试互相污染」抬到了「测试污染生产数据」：在阶段 C
> 的长时回补期间如果有人跑测试，两边会同时写同一个库文件。
>
> 因此 Step 1 的复现之外，还需要定位**是谁**打开了默认库。可行的定位手段：在
> `DatabaseManager.get_instance()` 里临时对 `database_path` 命中生产库路径的调用
> 打印堆栈，然后跑全量，把命中者揪出来。修法优先考虑给套件加一个统一的
> `conftest.py`（仓库目前没有这个文件）把 `DATABASE_PATH` 默认指向临时库，
> 让「不显式指定就落到生产库」这件事在测试环境下不可能发生。

- [ ] **Step 1: 复现污染**

```bash
python -m pytest tests/test_data_health_service.py "tests/test_e2e_five_layer_local.py::TestL2SectorHeatEngine::test_compute_returns_non_empty_results" -q --tb=line -p no:randomly
```

预期：`1 failed, 3 passed`，失败信息为 `0 not greater than 0 : 应至少有 1 个板块热度结果`。

对照组：

```bash
python -m pytest "tests/test_e2e_five_layer_local.py::TestL2SectorHeatEngine::test_compute_returns_non_empty_results" -q -p no:randomly
```

预期：`1 passed`。两者差异即污染证据。

- [ ] **Step 2: 加还原逻辑**

`tests/test_data_health_service.py`，把模块级 `_build_db` 改为带清理的 fixture：

```python
import pytest

from src.storage import DatabaseManager


@pytest.fixture
def db(tmp_path):
    """构造隔离的 DatabaseManager 并在用例结束后还原全局单例。

    DatabaseManager 是单例，直接构造会把临时库装成全局实例。
    不还原的话，后续所有 get_instance() 的调用者都会读到这个
    已被删除的临时库——这正是 test_e2e_five_layer_local 的板块热度
    用例在全量套件里失败、单独跑却通过的原因。
    """
    DatabaseManager.reset_instance()
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'data_health.db'}")
    try:
        yield manager
    finally:
        DatabaseManager.reset_instance()
```

把该文件所有 `_build_db(tmp_path)` 的调用改为使用 `db` fixture 参数。

- [ ] **Step 3: 验证污染消失**

```bash
python -m pytest tests/test_data_health_service.py "tests/test_e2e_five_layer_local.py::TestL2SectorHeatEngine::test_compute_returns_non_empty_results" "tests/test_e2e_five_layer_local.py::TestSectorHeatPersistence::test_compute_and_persist" -q --tb=line -p no:randomly
```

预期：全部 passed。

- [ ] **Step 4: 先量爆炸半径，再决定是否加护栏**

仓库目前**没有任何 `conftest.py`**，约 50 个测试文件会构造 `DatabaseManager(...)`
或调 `reset_instance()`。直接加 autouse 护栏很可能一次性弄红一大片，
所以**先测量，后决定**。

护栏必须比较 **engine URL 而非对象身份**。原因在 `src/storage.py:1189-1194`：
`__new__` 在 `_instance` 非空时返回同一个对象，所以「不先 reset 就换库」的用例
只是把 engine 换掉、对象没换，比对象身份会静默漏掉。

先写一个只报告不失败的版本，跑一遍统计：

```python
# tests/conftest.py
import pytest

from src.storage import DatabaseManager

_LEAKS: list[str] = []


def _current_url():
    instance = getattr(DatabaseManager, "_instance", None)
    engine = getattr(instance, "_engine", None)
    return str(getattr(engine, "url", None)) if engine is not None else None


@pytest.fixture(autouse=True)
def _observe_database_singleton_leak(request):
    """记录哪些用例换掉了全局单例指向的库但没还原。

    单例泄漏的症状是跨用例的隐性失败，排查成本极高——本次定位
    test_e2e_five_layer_local 的两个失败花了四轮二分。
    """
    before = _current_url()
    yield
    after = _current_url()
    if before != after:
        _LEAKS.append(f"{request.node.nodeid}: {before} -> {after}")


def pytest_sessionfinish(session, exitstatus):
    if _LEAKS:
        print(f"\n[singleton-leak] {len(_LEAKS)} test(s) left a different database:")
        for line in _LEAKS:
            print(f"  {line}")
```

```bash
python -m pytest -m "not network" -q --tb=no 2>&1 | Select-String "singleton-leak" -Context 0,30
```

**决策规则**：

- 泄漏数 ≤ 5：逐个修成 fixture 还原，然后把 `_LEAKS.append` 改成 `pytest.fail`，本任务收口。
- 泄漏数 > 5：**保留观察版护栏，不改成 fail**，把清单记入本任务交付说明，
  停下来向人类汇报后再决定是否单开一个任务清理。不要在本计划里硬扛。

无论走哪条路，`test_data_health_service.py` 这一处（Step 2 已修）必须清零。

### 实测结论（Task 1 执行时测得，已定案）

**泄漏 40 处，远超阈值 5 → 按规则保留观察版护栏，未改成 fail。**

**「谁在写生产库」已定位：13 个用例**，通过 `DatabaseManager.get_instance()`
打开 `DATABASE_PATH` 缺省指向的生产库：

```
tests/test_agent_executor.py::TestAgentExecutor::test_max_steps_exceeded
tests/test_bottom_divergence_v2_performance.py::test_factor_cache_matches_uncached_results_and_isolates_parameter_hash
tests/test_database_matched_strategies.py::TestDatabaseMatchedStrategies::test_database_stores_only_filtered_strategies
tests/test_factor_service_bottom_divergence.py::TestFactorServiceBottomDivergence::test_bottom_divergence_factors_types
tests/test_five_layer_phase4_api.py::TestGetRunDetail::test_get_run_ok
tests/test_llm_usage.py::TestPersistUsageHelper::test_persist_usage_never_raises
tests/test_multi_agent.py::TestStrategyAggregator::test_mixed_signals_produce_hold
tests/test_screening_api.py::ScreeningApiTestCase::test_create_run_accepts_ai_top_k_at_balanced_mode_limit
tests/test_screening_notification_service.py::ScreeningNotificationServiceTestCase::test_build_run_notification_contains_frontend_like_candidate_details
tests/test_screening_strategy_router.py::TestScreeningTaskServiceRegimeSelection::test_resolve_active_strategies_with_explicit_strategies
tests/test_screening_task_service_hot_theme.py::ScreeningTaskServiceHotThemeIntegrationTestCase::test_build_run_config_snapshot_includes_theme_context
tests/test_storage.py::TestStorage::test_get_instance_recovers_after_failed_initialization
tests/test_strategy_names_filtering.py::TestStrategyNamesFiltering::test_screening_task_service_passes_strategy_names_to_screener
```

**会话级 `DATABASE_PATH` 兜底已实测，可行但不可直接采用，原因如下——这是留给
人类的决策点，不要擅自实施：**

1. 单在 `pytest_configure` 里设一次**不够**。多个用例用
   `os.environ.pop("DATABASE_PATH", None)` 做清理
   （如 `tests/test_screening_task_service.py:3122,3193,3269`），会把兜底一并删除，
   此后所有 `get_instance()` 又落回生产库。必须配一个每用例后重设的 autouse fixture。
2. 补上重设后，生产库确实**完全不再被写**（实测全量前后 mtime 一致），
   但 `tests/test_e2e_five_layer_local.py` 的两条 L2 用例随即转红。
3. 根因是**该文件本就设计为跑真实本地数据**：`FiveLayerLocalTestBase.setUpClass`
   （`:96-107`）直接检查生产库是否存在、有无因子快照，没有就 skip。它此前能通过，
   靠的是重定向被别的用例 `pop` 掉、恰好又落回生产库——**是巧合，不是隔离**。

因此完整修复必须先回答：`test_e2e_five_layer_local.py`（以及上面 13 个里其他
有意读真实数据的用例）应当改造成自带 fixture 数据，还是显式标记为需要真实库的
一类、从默认离线套件里分离出去。这是测试策略决策，超出本任务范围。

**当前状态**：护栏为观察版（只报告不失败），生产库在跑全量测试时仍会被写入。
阶段 C 的数小时回补期间**不要跑测试**。

- [ ] **Step 5: 全量回归**

```bash
python -m pytest -m "not network" -q --tb=no
```

预期：失败数从 6 降到 4（剩 3 个候选/L2 真实缺陷 + `test_env.py::test_notification`）。

- [ ] **Step 6: 提交**

```bash
git add tests/test_data_health_service.py tests/conftest.py
git commit -m "fix: restore DatabaseManager singleton after data health tests

The singleton leaked a deleted temp database into every later test that
called get_instance(), which silently emptied L2 sector heat results."
```

---

## Task 2: L2 板块热度在无板块数据时必须告警

**Files:**

- Modify: `src/services/sector_heat_engine.py:85-96`
- Test: `tests/test_data_gap_fixes.py:158-177`（已存在，无需改）

**根因**：`compute_all_sectors` 在拿不到板块时只调 `write_debug_log` 写文件，从不调 `logger.warning`，然后静默 `return []`。对回测的影响是 L2 整层无声降级——历史重放会产出一堆空板块热度而没有任何信号。

- [ ] **Step 1: 确认测试失败**

```bash
python -m pytest "tests/test_data_gap_fixes.py::BoardDataAvailabilityTestCase::test_sector_heat_engine_warns_on_empty_boards" -v -p no:randomly
```

预期：`AssertionError: no logs of level WARNING or higher triggered on src.services.sector_heat_engine`。

- [ ] **Step 2: 加告警**

`src/services/sector_heat_engine.py`，在第 85 行 `if not boards:` 的分支里，`write_debug_log(...)` 之后、`return []` 之前插入：

```python
            logger.warning(
                "L2 板块热度无可用板块（min_member_count=%s，快照 %s 行），本日 L2 整层降级为空结果",
                MIN_SECTOR_STOCK_COUNT,
                len(snapshot_df),
            )
```

同样在第 68 行的空快照分支加一条 `logger.warning`——两条降级路径都必须可见，否则历史重放里分不清是「没有板块」还是「没有快照」。

> 实施注意：确认该模块已有 `logger = logging.getLogger(__name__)`。若没有，按仓库既有约定在文件顶部补上。

- [ ] **Step 3: 确认通过**

```bash
python -m pytest tests/test_data_gap_fixes.py -v -p no:randomly
```

预期：7 passed。

- [ ] **Step 4: 提交**

```bash
git add src/services/sector_heat_engine.py
git commit -m "fix: warn instead of silently returning empty L2 sector heat

Both degradation paths (empty snapshot, no active boards) previously only
wrote a debug file, making a whole-layer downgrade invisible in replay."
```

---

## Task 3: L2 题材数上限在主路径生效

**Files:**

- Modify: `src/services/theme_position_resolver.py:128-143`（无 registry 分支）与 `:104-126`（有 registry 分支）
- Test: `tests/test_theme_services.py:176-200`（已存在，无需改）

**根因**：`_position_from_sector`（`:259-261`）把 `warm + expand` 判为 `SECONDARY_THEME`，于是 15 个 warm+expand 板块全部进入 `raw_themes`，`raw_themes` 非空 → 带 `[:MAX_FALLBACK_THEMES]` 上限的 `_build_warm_expand_fallback_themes`（`:145-161`）**根本不会被调用**。而主路径（`:104-143`）**没有任何上限**。

对回测的影响是 L2 过宽：题材层不做限制，L3 候选池的输入就被放大，整条五层链路的筛选力度失真。

- [ ] **Step 1: 确认测试失败**

```bash
python -m pytest "tests/test_theme_services.py::ThemePositionResolverBasicTestCase::test_warm_expand_fallback_caps_theme_count" -v -p no:randomly
```

预期：`AssertionError: 15 not less than or equal to 10`。

- [ ] **Step 2: 把上限提到主路径**

`src/services/theme_position_resolver.py`，把第 33 行的常量重命名并明确语义：

```python
# 单日识别的主线/次线题材数量上限。
# 这是 L2 的宽度闸门：题材层不设限会把 L3 的候选输入整体放大，
# 让五层筛选形同虚设。fallback 与主路径必须共用同一个上限。
MAX_IDENTIFIED_THEMES = 10
MAX_FALLBACK_THEMES = MAX_IDENTIFIED_THEMES  # 向后兼容既有引用
```

在**两个** return 之前应用上限。有 registry 的分支（第 125-126 行）：

```python
            themes.sort(key=self._theme_sort_key, reverse=True)
            return themes[:MAX_IDENTIFIED_THEMES]
```

无 registry 的分支（第 142-143 行）：

```python
        themes.sort(key=self._theme_sort_key, reverse=True)
        return themes[:MAX_IDENTIFIED_THEMES]
```

上限在 `sort` **之后**应用，保证保留的是分数最高的 10 个——测试的第二条断言 `identified_themes[0].name == "题材00"` 正是在钉这个顺序。

- [ ] **Step 3: 确认通过**

```bash
python -m pytest tests/test_theme_services.py -v -p no:randomly
```

预期：全部 passed，尤其原有的主线/次线判定用例不受影响。

- [ ] **Step 4: 提交**

```bash
git add src/services/theme_position_resolver.py
git commit -m "fix: apply theme count cap on the main identification path

warm+expand sectors are classified as SECONDARY_THEME directly, so the
capped fallback path never ran and L2 width was unbounded."
```

---

## Task 4: 黄金用例对齐涨停准入规则

**Files:**

- Modify: `tests/golden_cases/test_candidate_decision_golden.py:153-218`
- Test: 同一文件

**根因**：`CandidatePoolClassifier.classify`（`src/services/candidate_pool_classifier.py:52`）要求 `is_limit_up=True` 才能进 `LEADER_POOL`，这是该模块 docstring 第 5-7 行明确声明的规则（「Only main/secondary theme limit-up stocks can enter leader_pool」）。黄金用例的 fixture 是 `pct_chg: 5.0`（非涨停），走到 `:56-57` 得到 `FOCUS_LIST`，而断言仍写着 `leader_pool`。

**这是断言过期，不是代码缺陷。** 因此修测试而非改分类器。

- [ ] **Step 1: 确认失败原因**

```bash
python -m pytest "tests/golden_cases/test_candidate_decision_golden.py::CandidateDecisionGoldenCase::test_pipeline_golden_case_builds_complete_candidate_decision" -v -p no:randomly
```

预期：`AssertionError: 'focus_list' != 'leader_pool'`。

- [ ] **Step 2: 把用例改成真涨停**

黄金用例的意图是「走通完整链路并产出完整决策」，因此应让 fixture 满足进入
`leader_pool` 的条件，而不是把断言降级。

**改的位置是 `factor_snapshot`，不是 `snapshot_df`。** L3 的涨停标记来自
`five_layer_pipeline.py:551` 的 `fs.get("is_limit_up", False)`，而 `fs` 是
`candidate.factor_snapshot`（`:501`）。该用例用 `_StubScreenerService` 返回硬编码的
`selected` 列表，`snapshot_df` 根本不参与 `factor_snapshot` 的构造——
改 `snapshot_df` 报错会一模一样。

改 `tests/golden_cases/test_candidate_decision_golden.py:146-154` 的
`ScreeningCandidateRecord(...)`：

```python
                factor_snapshot={
                    "close": 100.0,
                    "ma20": 94.0,
                    "ma100": 88.0,
                    "ma100_breakout_days": 2,
                    "leader_score": 85.0,
                    "extreme_strength_score": 62.0,
                    "has_stop_loss": True,
                    "is_limit_up": True,     # 主板涨停，L3 进 leader_pool 的准入条件
                },
```

> **同一个 `fs` 也会喂给 `entry_maturity_assessor.py:154` 与 `setup_resolver.py:217`。**
> 加 `is_limit_up` 可能改变 `entry_maturity` 或 `setup_type`，从而波及该文件里
> 现有的 `setup_freshness == 0.9` 与 `execution_note` 断言。跑完看实际结果再判断：
> 若这些断言变化，说明涨停确实改变了成熟度判定，**应该更新断言并在 commit 里说明**，
> 而不是回头去绕开判定逻辑。

- [ ] **Step 3: 补一条非涨停反例**

单改一条断言只能证明「涨停能进 leader_pool」，证不了「非涨停不能进」。追加：

```python
    def test_non_limit_up_main_theme_stays_in_focus_list(self) -> None:
        """反例：主线题材但未涨停，只能到 focus_list。

        这条钉住的是 CandidatePoolClassifier 的核心准入规则。
        没有它，分类器把涨停条件删掉也不会有任何测试变红。
        """
```

用例主体复用上一条的构造，把 `factor_snapshot` 里的 `is_limit_up` 设为 `False`
（其余字段不变），断言 `payload["candidate_pool_level"] == "focus_list"`。

- [ ] **Step 4: 确认通过**

```bash
python -m pytest tests/golden_cases/ -v -p no:randomly
```

预期：全部 passed。

- [ ] **Step 5: 全量回归**

```bash
python -m pytest -m "not network" -q --tb=no
```

预期：仅剩 `test_env.py::test_notification` 一个失败（不在本计划范围）。

> 这是**阶段性**达成，不是 spec 验收 #36 的最终达成点：Task 5 会再次弄红
> `golden_cases`（它的 fixture 依赖 `has_stop_loss` 键），Task 8 会弄红聚合相关用例。
> 验收 #36 在 Task 8 Step 6 收口。

- [ ] **Step 6: 提交**

```bash
git add tests/golden_cases/test_candidate_decision_golden.py
git commit -m "test: align golden candidate case with limit-up leader_pool rule

Adds a negative case so removing the limit-up condition would fail a test."
```

---

## Task 5: setup 级止损统一解析（核心根因）

**Files:**

- Create: `src/services/setup_stop_loss.py`
- Modify: `src/services/trade_plan_builder.py:257-341`
- Modify: `src/services/five_layer_pipeline.py:563-571`
- Test: `tests/test_setup_stop_loss.py`（新建）

**根因链**（已实测，不是推断）：

1. `five_layer_pipeline.py:570` 对**非** `BOTTOM_DIVERGENCE_LAYERED_ENTRY` 的 setup 读 `fs.get("has_stop_loss", False)`。
2. 该键**从未被任何生产代码写入** `factor_snapshot`（`factor_service.py:216-239` 的字典里没有它），恒为 `False`。
3. `trade_stage_judge.py:62` 遇 `has_stop_loss=False` 直接返回 `FOCUS`，进不了 `PROBE_ENTRY` / `ADD_ON_STRENGTH`。
4. `TradePlanBuilder.build`（`:356`）要求 `trade_stage` 属于这两者才产出计划，于是 `trade_plan_json` 恒为 None。
5. 实测：652 条候选中带 `trade_plan_json` 的为 **0**，含 `has_stop_loss` 键的为 **0 / 652**。

**为什么不能直接在 L5 里调 `TradePlanBuilder`**：时序上 `stage_judge.judge` 在 `:572`，`plan_builder.build` 在 `:588`，且 `build` 依赖 `trade_stage` 的结果——直接互调构成环。解法是把止损解析从 `_build_structured_prices` 中**提取为独立模块**，两边都依赖它，谁也不依赖谁。

**修复可行性已验证**：审计实测 001337 / 2026-08-05 当前停在 `focus`，模拟修复后变为 `probe_entry` 并产出 entry=43.27、stop=30.7296 的完整计划。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_setup_stop_loss.py`：

```python
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
```

- [ ] **Step 2: 运行确认失败**

```bash
python -m pytest tests/test_setup_stop_loss.py -v
```

预期：`ModuleNotFoundError: No module named 'src.services.setup_stop_loss'`。

- [ ] **Step 3: 建共享模块**

新建 `src/services/setup_stop_loss.py`。把 `_safe_positive_finite_price` 从 `trade_plan_builder.py` **移动**到这里（不是复制——DRY），并实现解析：

```python
# -*- coding: utf-8 -*-
"""setup 级止损解析的单一真源。

L5 交易阶段裁决与交易计划构建都需要「这个 setup 有没有止损位」。
二者在管线中一前一后，直接互调会成环，因此把解析逻辑提到这里，
两边都依赖本模块。
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional

from src.schemas.trading_types import SetupType
from src.services.bottom_divergence_v2_trade_support import (
    resolve_current_stage_stop_loss,
)


def safe_positive_finite_price(value: Any) -> Optional[float]:
    """只接受正的有限价格，其余一律 None。"""
    # bool 是 int 的子类，float(True) == 1.0 会被当成 1 元的止损价。
    # 这个守卫是从 trade_plan_builder 原样搬过来的，不能丢。
    if isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return price


def resolve_setup_stop_loss(
    setup_type: SetupType,
    factor_snapshot: Mapping[str, Any],
) -> Optional[float]:
    """按 setup 类型解析止损位，解析不出返回 None。

    返回 None 是有意义的信号：L5 据此把候选压在 focus，
    不得用兜底值填充。
    """
    if setup_type == SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY:
        return safe_positive_finite_price(
            resolve_current_stage_stop_loss(factor_snapshot)
        )

    if setup_type == SetupType.LOW123_BREAKOUT:
        stop = safe_positive_finite_price(factor_snapshot.get("pattern_123_stop_loss"))
    elif setup_type == SetupType.BOTTOM_DIVERGENCE_BREAKOUT:
        stop = safe_positive_finite_price(
            factor_snapshot.get("bottom_divergence_stop_loss")
        ) or safe_positive_finite_price(
            (factor_snapshot.get("bottom_divergence_exit_plan") or {}).get(
                "initial_stop_loss"
            )
        )
    elif setup_type == SetupType.TREND_PULLBACK:
        stop = safe_positive_finite_price(
            factor_snapshot.get("shrink_pullback_stop_loss_price")
        )
    else:
        stop = None

    if stop is None:
        risk_params = factor_snapshot.get("risk_params") or {}
        if isinstance(risk_params, dict):
            stop = safe_positive_finite_price(risk_params.get("stop_loss"))
    return stop
```

- [ ] **Step 4: 运行确认通过**

```bash
python -m pytest tests/test_setup_stop_loss.py -v
```

预期：6 passed。

- [ ] **Step 5: 让 `trade_plan_builder` 改用共享实现**

`src/services/trade_plan_builder.py`：

1. 顶部改为
   `from src.services.setup_stop_loss import resolve_setup_stop_loss, safe_positive_finite_price as _safe_positive_finite_price`，
   删除本地第 147-156 行的 `_safe_positive_finite_price` 定义。
   `_round_price`（`:206`）等既有调用方保持不变。
2. `_build_structured_prices`（`:257-341`）中所有 `stop_loss_price` 的分支计算替换为一次调用：

```python
    stop_loss_price = resolve_setup_stop_loss(setup_type, factor_snapshot)
```

第 315-319 行的「非 v2 时用 `risk_params` 兜底」逻辑已并入共享函数，此处删除。
`entry_price` / `entry_rule` / `take_profit_price` 的分支**保持不变**——本任务只统一止损。

迁走 `_safe_positive_finite_price` 后，确认 `trade_plan_builder.py:10` 的 `import math`
是否还有使用者；若无则删除。同理 Step 6 之后 `five_layer_pipeline.py:37` 的
`resolve_current_stage_stop_loss` 导入可能变成未使用。两处都会被 `ci_gate.sh`
的 flake8 报 F401。

- [ ] **Step 6: 让 L5 改用共享实现**

`src/services/five_layer_pipeline.py:563-571`，把 `has_stop` 的计算改为：

```python
            resolved_stop = resolve_setup_stop_loss(st, fs)
            has_stop = (
                (
                    v2_execution_allowed
                    and resolve_current_stage_buy_point(fs) is not None
                    and resolved_stop is not None
                )
                if st == SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY
                else resolved_stop is not None
            )
```

并在文件顶部加 `from src.services.setup_stop_loss import resolve_setup_stop_loss`。

**不再读 `fs.get("has_stop_loss", False)`**——该键从未写入，保留它只会让下一个人以为它有值。

- [ ] **Step 7: 全量回归，并只改真正的字典 fixture**

```bash
python -m pytest -m "not network" -q --tb=short
```

**先分清两类 `has_stop_loss=True`，它们的处置完全相反：**

| 类别 | 位置 | 处置 |
| --- | --- | --- |
| `TradeStageJudge.judge(..., has_stop_loss=True)` 的**入参** | `tests/test_decision_modules.py:375/387/401/413/439/451/475/487/511/532`、`tests/test_e2e_five_layer_local.py:440/470/500` | **不动**。`judge` 的签名本次不变，这些用例验证的是判定器本身，改了是白做还会破坏正确的单测 |
| 塞进 `factor_snapshot` 字典的**键** | `tests/test_five_layer_pipeline.py:45/455/488/527/574/617/721/754`、`:882` 的构造辅助、**`tests/golden_cases/test_candidate_decision_golden.py:153`** | **改**为提供真实止损来源字段（`pattern_123_stop_loss` 或 `risk_params.stop_loss`），走真实解析 |

**注意 `golden_cases/test_candidate_decision_golden.py:153` 也在第二类里**——
它的 `add_on_strength` 与 `trade_plan.execution_note` 断言完全依赖那个键。
Task 4 刚修好的文件会被本任务再次弄红，这是预期内的，不是回退。

**不要**为了让测试变绿而在生产代码里保留对 `has_stop_loss` 键的读取——
那等于把刚拆掉的假绿灯装回去。

另外 `tests/test_five_layer_pipeline.py:1237` 的
`test_v1_still_uses_only_legacy_has_stop_loss` 在本任务后仍会通过
（其 fixture 没有任何真实止损字段，解析结果照样是 None → FOCUS），
但用例名与意图已过期，顺手改名以免误导。

- [ ] **Step 8: 验证真实候选能产出交易计划**

这是本任务的验收核心。用审计已确认的样本对照：

```bash
python -m pytest tests/test_five_layer_pipeline.py tests/test_decision_modules.py tests/test_trade_plan_builder.py tests/golden_cases/ -v -p no:randomly
```

**spec 5.1 要求本改动带开关灰度并做切换前后分布对比。** 本计划的取舍是：
不加运行时开关（多一个开关就多一条要长期维护的分支），改为**合入前做一次
对比并留证**——用同一天的生产快照分别跑修复前后的管线，记录
`trade_stage` 分布、候选数、带交易计划的候选数三项差异，写入交付说明。
这是有意偏离 spec 字面要求，须在交付说明中写明理由；
若对比结果显示候选数变化超过一个数量级，则退回补开关的方案。

再补一条端到端断言（加入 `tests/test_five_layer_pipeline.py`）：

```python
    def test_non_v2_setup_with_real_stop_reaches_probe_entry(self) -> None:
        """反例防线：不写 has_stop_loss 键，只给真实止损字段，
        非 v2 的 setup 也必须能走到 probe_entry 并产出交易计划。

        修复前这条必然失败——正是它对应线上 652 条候选 0 计划的实测。
        """
```

- [ ] **Step 9: 提交**

```bash
git add src/services/setup_stop_loss.py src/services/trade_plan_builder.py src/services/five_layer_pipeline.py tests/
git commit -m "fix: resolve stop loss from setup type instead of unwritten snapshot key

has_stop_loss was never written to factor_snapshot, so every non-v2 setup
was capped at FOCUS and produced no trade plan (0 of 652 candidates)."
```

---

## Task 6: 保留 `raw_rule_score`

**Files:**

- Modify: `src/services/five_layer_pipeline.py:659-668`
- Modify: `src/storage.py:849`（`screening_candidates` 加列）、内联迁移
- Modify: `src/schemas/trading_types.py`（候选 dataclass 加字段）
- Test: `tests/test_five_layer_pipeline.py`（追加）

**根因**：`five_layer_pipeline.py:664` 用 `stage_p + pool_p + theme_p + c.rule_score * 0.01` **原地覆盖** `rule_score`，原始质量分被压成 1% 权重的 tie-breaker。入库（`storage.py:3165`）与所有下游看到的都是复合优先级分，原始分无处可取。

对回测的影响：Rank IC 需要一个**连续的信号强度**作为因子值。复合分由三个离散优先级主导，其取值高度离散，做不了有意义的秩相关。

**修法必须是追加而非替换**：`rule_score` 的现有语义被通知服务、二筛门控、候选选择器等多处消费（`screening_notification_service.py:444/449/508/781/789/801`、`screening_task_service.py:653/780/995`、`candidate_selector.py:149`）。改它的含义会波及一大片。

- [ ] **Step 1: 写失败测试**

该文件里**只有 `_make_candidate`（`:34`）是模块级 helper**，
不存在 `_make_service` / `_make_snapshot_df` / `_run_pipeline_with_candidate`。
唯一跑通全流水线的写法是 `BottomDivergenceV2RealPipelineTestCase._run`（`:960`
的 `FiveLayerPipeline().run(...)`，外面套四个 `patch`）。

因此本用例**作为方法追加到 `BottomDivergenceV2RealPipelineTestCase` 内**，
复用 `self._run`，不要另起炉灶：

```python
    def test_raw_rule_score_survives_priority_composition(self) -> None:
        """复合优先级分覆盖 rule_score 后，原始质量分仍须可取。

        Rank IC 需要连续的信号强度；复合分被三个离散优先级主导，
        取值离散到做不了秩相关。
        """
        candidate = self._run(stage="early", layered_points=[])

        # _run 内构造的 ScreeningCandidateRecord 是 rule_score=80.0（`:903`）。
        self.assertAlmostEqual(candidate.raw_rule_score, 80.0, places=4)
        self.assertNotAlmostEqual(candidate.rule_score, 80.0, places=4)
```

`self._run` 已断言 `len(result.candidates) == 1` 并返回 `result.candidates[0]`
（`:974-975`），所以这里直接拿到的就是候选对象。

- [ ] **Step 2: 运行确认失败**

```bash
python -m pytest tests/test_five_layer_pipeline.py -v -k raw_rule_score
```

预期：`AttributeError: 'ScreeningCandidateRecord' object has no attribute 'raw_rule_score'`
（`result.candidates` 的元素类型是 `ScreeningCandidateRecord`，不是 `CandidateDecision`）。

- [ ] **Step 3: 加字段与列（四处，缺一处就会「单测绿、库里全 NULL」）**

1. **`ScreeningCandidateRecord`**（`src/services/screener_service.py:14-23`）
   加 `raw_rule_score: float = 0.0`。**这才是 `PipelineResult.candidates` 的元素类型**，
   也是 Step 1 测试断言的对象。

   **位置必须放在 `strategy_scores`（`:23`）之后**，不能挨着 `rule_score`（`:19`）。
   `rule_hits`（`:20`）与 `factor_snapshot`（`:21`）是无默认值字段，
   在它们前面插一个带默认值的字段会让 dataclass 在导入时就
   `TypeError: non-default argument 'rule_hits' follows default argument`。

2. **`CandidateDecision`**（`src/schemas/trading_types.py:244` 的 `rule_score` 相邻）
   加同名字段。

3. **`CandidateDecision.from_record`**（`src/schemas/trading_types.py:251-258`）
   是**逐字段枚举**的，必须补一行。照邻居的写法用 `getattr` 兜底，
   以防传入的不是 `ScreeningCandidateRecord`：

```python
            raw_rule_score=float(getattr(record, "raw_rule_score", 0.0) or 0.0),
```

   漏掉这一行的后果正是本计划要消灭的那类失败：单测绿，入库全 NULL。

   `to_payload` 走通用序列化（`_serialize_value`），自动带上新字段；
   但 `from_payload`（`:377-482`）同样是逐字段枚举的，
   **也要补一行**，否则 payload 往返一圈这个字段就丢了。

**第四处** —— `src/storage.py` 的 `ScreeningCandidate`（第 849 行 `rule_score` 之后）加：

```python
    # 五层优先级排序会原地覆盖 rule_score（pipeline:664），
    # 这里保留 screener 产出的原始质量分，供 Rank IC 等连续量分析使用。
    raw_rule_score = Column(Float, nullable=True)
```

再加内联迁移 `_migrate_sqlite_screening_candidate_raw_rule_score` 与
`scripts/migrate_screening_candidate_raw_rule_score.py`
（照抄 `scripts/migrate_screening_candidate_updated_at.py`）。

**关于 `priority_score`**：spec 5.2 要的是两个字段。`rule_score` 被覆盖后的现值
**就是**优先级分，因此本任务不再新增一列存同样的数，
而是在 `to_dict` 与文档中把它显式命名为「复合优先级分」。
这是有意偏离 spec 字面要求，理由是避免同一个数存两遍产生不一致；
须写入交付说明。

- [ ] **Step 4: 在覆盖前赋值**

`src/services/five_layer_pipeline.py:659-668`：

```python
        # ── 五层优先级排序 (D6) ───────────────────────────────────────
        for c in kept:
            # 覆盖前先留存原始质量分。rule_score 的复合语义有多处下游消费方，
            # 不动它，只追加。
            c.raw_rule_score = c.rule_score
            stage_p = _STAGE_PRIORITY.get(c.trade_stage or "", 0)
            pool_p = _POOL_PRIORITY.get(c.candidate_pool_level or "", 0)
            theme_p = _THEME_PRIORITY.get(c.theme_position or "", 0)
            c.rule_score = stage_p + pool_p + theme_p + c.rule_score * 0.01
```

并在 `src/storage.py:3165` 附近的入库映射中补 `raw_rule_score=item.get("raw_rule_score")`。

- [ ] **Step 5: 确认通过并回归**

```bash
python -m pytest tests/test_five_layer_pipeline.py -v
python scripts/migrate_screening_candidate_raw_rule_score.py --db ./data/stock_analysis.db
python scripts/migrate_screening_candidate_raw_rule_score.py --db ./data/stock_analysis.db
```

预期：测试 PASS；迁移第二次全 skip。

- [ ] **Step 6: 提交**

```bash
git add src/services/screener_service.py src/schemas/trading_types.py src/storage.py src/services/five_layer_pipeline.py scripts/migrate_screening_candidate_raw_rule_score.py tests/test_five_layer_pipeline.py
git commit -m "feat: preserve raw_rule_score before priority composition overwrites it"
```

---

## Task 7: 回测运行表版本字段落地

**Files:**

- Modify: `src/backtest/models/backtest_models.py:29-67`
- Modify: `src/backtest/repositories/run_repo.py:54-95`
- Modify: `src/backtest/services/backtest_service.py:564,675`
- Test: `tests/test_five_layer_backtest_service_skeleton.py`（追加，`:44-62` 已有
  在进程内驱动 `FiveLayerBacktestService.run_backtest` 的现成模式，
  是断言运行行字段的自然位置）

**根因**：运行表定义了 5 个版本字段，但 `update_run_status`（`run_repo.py:54-95`）的参数里只有 `data_version`，其余 4 个（`market_data_version` / `theme_mapping_version` / `candidate_snapshot_version` / `rules_version`）**没有写入通道**，`create_run` 的调用点也从未传。结果是每次运行都声称自己被版本化，实际只有一个字段有值。

本任务是 spec 10.5 `run_data_manifest` 的地基，但**不实现完整 manifest**（那需要快照与哈希链路，依赖数据阶段）。

- [ ] **Step 1: 写失败测试**

追加到 `TestFiveLayerBacktestServiceSkeleton`（`:17`），
复用它既有的临时库 `setUp`/`tearDown` 与 `svc.create_run(...)` 写法（`:44-53`）：

```python
    def test_run_has_no_null_version_field(self) -> None:
        """版本字段要么有值，要么显式声明不适用，不允许静默为 NULL。

        两次运行能否比较，靠的就是这些字段；留 NULL 等于宣称
        「可复现」却拿不出任何证据。
        """
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.create_run(
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 1, 1),
            trade_date_to=date(2024, 1, 31),
        )

        for field in (
            "data_version", "market_data_version", "theme_mapping_version",
            "candidate_snapshot_version", "rules_version",
            "code_revision", "config_hash",
        ):
            with self.subTest(field=field):
                self.assertIsNotNone(getattr(run, field), f"{field} is NULL")
                self.assertNotEqual(str(getattr(run, field)).strip(), "")
```

- [ ] **Step 2: 运行确认失败**

```bash
python -m pytest tests/test_five_layer_backtest_service_skeleton.py -v -k version
```

- [ ] **Step 3: 加两列**

`src/backtest/models/backtest_models.py`，在 `rules_version` 之后：

```python
    # 代码版本与配置指纹。没有这两项，「同一份数据同一套代码」无法证明。
    # 注意：code_revision 仅作记录，**不参与可比性判定**——
    # spec 5.3 指出 git commit 会因无关改动而变化，用它当键会把
    # 本可对比的两次运行误判为不可比。可比性看 data_version 与 config_hash。
    code_revision = Column(String(64))
    config_hash = Column(String(64))
```

配套内联迁移与 `scripts/migrate_backtest_run_versions.py`。

`FiveLayerBacktestRun.to_dict`（`backtest_models.py:69-92`）是逐字段枚举的，
**必须同步补上这两个字段**，否则 API 与导出看不到。

- [ ] **Step 4: 打通写入通道**

`src/backtest/repositories/run_repo.py:54-95`，把 `update_run_status` 的签名扩展为接受全部 7 个字段（保持全部 `Optional`，未传即不更新）。

- [ ] **Step 5: 填值**

在 `src/backtest/services/backtest_service.py` 加一个构造函数：

```python
_VERSION_NOT_APPLICABLE = "n/a"


def _build_run_versions(config: Any, data_version: str) -> Dict[str, str]:
    """产出全部版本字段。不适用的显式写 n/a，绝不留 None。

    留 None 与「这一项确实不适用」在事后无法区分，
    而这正是判断两次运行可否比较时最需要分清的。
    """
    return {
        "data_version": data_version,
        "market_data_version": data_version,   # 复用重放切片的内容哈希
        "theme_mapping_version": _resolve_theme_mapping_version(),
        "candidate_snapshot_version": _resolve_candidate_snapshot_version(),
        "rules_version": _resolve_rules_version(),
        "code_revision": _resolve_code_revision(),
        "config_hash": _hash_config(config),
    }


def _resolve_code_revision() -> str:
    import subprocess
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, timeout=5
        ).decode().strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _hash_config(config: Any) -> str:
    import hashlib
    import json
    payload = json.dumps(_config_to_dict(config), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
```

`_resolve_theme_mapping_version` 等在对应数据尚不可版本化时返回 `_VERSION_NOT_APPLICABLE`——**显式声明不适用，而不是留空**。

**填值点必须是 `create_run`，不能只改 `:564` / `:675`。**
`run_backtest` 与 `run_backtest_for_screening_run` 都有 0 候选早退分支
（`:505-515`、`:616-626`），走的是 `:506` / `:617` 的 `update_run_status` 然后直接
`return`，根本到不了 `:564` / `:675`。只在那两处填值的话，0 候选运行的版本字段
全是 NULL——而 Step 1 的测试恰恰最可能落在 0 候选路径上（skeleton 测试通常
mock 空候选），会出现「改完了测试还是红」。

`create_run`（`:437-455`）本身透传 `**kwargs` 给 `run_repo.create_run`，
所以在这里注入即可，两条主路径、四个 `update_run_status` 调用点全部自动覆盖：

```python
    def create_run(self, evaluation_mode, execution_model,
                   trade_date_from, trade_date_to, market="cn", **kwargs):
        backtest_run_id = f"flbt-{uuid.uuid4().hex[:12]}"
        # 版本字段在建行时就写满，保证任何早退路径（0 候选、异常）
        # 拿到的行也不含 NULL。
        versions = _build_run_versions(self.config, data_version=_VERSION_NOT_APPLICABLE)
        versions.update({k: v for k, v in kwargs.items() if k in versions})
        kwargs.update(versions)
        return self.run_repo.create_run(...)
```

`data_version` 此刻还拿不到真值（要等重放切片算出内容哈希），先写
`_VERSION_NOT_APPLICABLE` 占位；`:547` 与 `:658` 的 `update_run_status`
在切片就绪后用真实哈希覆盖它。这样「任何时刻都非 NULL」与
「最终值真实」两个要求同时满足。

- [ ] **Step 6: 确认通过并提交**

```bash
python -m pytest tests/test_five_layer_backtest_service_skeleton.py tests/test_five_layer_backtest_repos.py -v
```

```bash
git add src/backtest/ scripts/migrate_backtest_run_versions.py tests/
git commit -m "feat: populate all backtest run version fields, add code_revision and config_hash

Four of five version columns had no write path at all, so every run
claimed reproducibility without evidence."
```

---

## Task 8: 顶层指标改为 entry 单族

**Files:**

- Modify: `src/backtest/aggregators/group_summary_aggregator.py:203,259-431`
- Modify: `tests/test_five_layer_phase3.py:222,233,247,257`、`tests/test_five_layer_aggregator.py:331`
- Test: 同上

**根因**：`aggregate_group` 在 `family_filter=None` 时把两类量纲不同的收益塞进同一个列表（`:313-318`：entry 用 `forward_return_5d`，observation 用 `risk_avoided_pct`），并把 `win` 与 `correct_wait` 合并进同一个胜率分子（`:355-356`）。该函数 docstring 第 272-278 行自己已标注 `DEPRECATED MIX`。

生产侧只有**一个**混族调用点：`:203` 的 `metrics = aggregate_group(evaluations)`。

**先厘清一件事：`_persist_group` 服务于多种 group_type。** 从 `:118/:130/:141` 可见有三类：
维度分组、`<维度>_signal_family` 拆分行、`combo`。其中**族维度的行本身就是单族**
（`signal_family` 行的 group_key 就是族名，拆分行同理）。

因此**不能在 `:203` 全局钉死 `family_filter="entry"`**——那会让 observation 族的行
经 entry 过滤后拿到 None，指标全部清空，直接打挂
`tests/test_five_layer_aggregator.py:429`（`obs.win_rate_pct == 50.0`）
与 `:479`（`split[balanced_observation].avg_return_pct == 8.0`）。

正确规则：**本行若本就单族，就用它自己的族；只有真正混族的行，顶层才只代表 entry。**

另外 `family_breakdown` 的落库通道**已经存在**（`:226-228` 写进 `extra`，
且对 `group_type == "signal_family"` 故意跳过），本任务不需要新建通道。

- [ ] **Step 1: 写失败测试**

加到 `tests/test_five_layer_aggregator.py`。**该文件没有 `_make_eval` 也没有
`_persist_and_read`**——它的既有模式是构造真实的 `FiveLayerBacktestEvaluation` ORM 行、
播一条 run、再调 `compute_all_summaries` 读回汇总。照抄该文件既有用例的播种方式，
下面片段里的 `_make_eval` / `_persist_and_read` 需要你按这个模式实现。

> 不要从 `test_five_layer_phase3.py` 导入它的 `_make_eval`：那个返回的是
> `MagicMock`，落不进 `EvaluationRepository`。

```python
    def test_mixed_group_top_level_metrics_exclude_observation(self) -> None:
        """真正混族的分组，顶层 win_rate / profit_factor 只能来自 entry 族。

        observation 的 risk_avoided_pct 与 entry 的 forward_return 量纲不同，
        且 correct_wait 并进胜率分子会把评级系统性抬高。
        """
        evals = [
            _make_eval(signal_family="entry", forward_return_5d=5.0, outcome="win"),
            _make_eval(signal_family="entry", forward_return_5d=-3.0, outcome="loss"),
            _make_eval(signal_family="observation", risk_avoided_pct=8.0, outcome="correct_wait"),
            _make_eval(signal_family="observation", risk_avoided_pct=6.0, outcome="correct_wait"),
        ]

        summary = self._persist_and_read(group_type="trade_stage", group_key="probe_entry",
                                         evaluations=evals)

        # entry 族 2 条中 1 胜 → 50%；混入两条 correct_wait 会变成 75%
        self.assertAlmostEqual(summary.win_rate_pct, 50.0, places=2)
        self.assertAlmostEqual(summary.avg_return_pct, 1.0, places=2)

    def test_single_family_group_keeps_its_own_family_metrics(self) -> None:
        """反例：observation 行不能被 entry 过滤清空。

        没有这条，「顶层只算 entry」的改法会把整类分组行的指标抹成 None。
        """
        evals = [
            _make_eval(signal_family="observation", risk_avoided_pct=8.0, outcome="correct_wait"),
            _make_eval(signal_family="observation", risk_avoided_pct=-2.0, outcome="missed"),
        ]

        summary = self._persist_and_read(group_type="signal_family", group_key="observation",
                                         evaluations=evals)

        self.assertAlmostEqual(summary.win_rate_pct, 50.0, places=2)
        self.assertIsNotNone(summary.avg_return_pct)

    def test_mixed_family_aggregation_requires_explicit_opt_in(self) -> None:
        """反例：默认不允许混族。旧口径必须显式声明才能拿到。"""
        with self.assertRaises(ValueError):
            aggregate_group([
                _make_eval(signal_family="entry", forward_return_5d=1.0),
            ])
```

- [ ] **Step 2: 运行确认失败**

```bash
python -m pytest tests/test_five_layer_aggregator.py -v -k family
```

- [ ] **Step 3: 关掉默认混族**

`src/backtest/aggregators/group_summary_aggregator.py`，把 `aggregate_group` 签名改为：

```python
def aggregate_group(
    evaluations: List[FiveLayerBacktestEvaluation],
    family_filter: Optional[str] = None,
    *,
    allow_mixed_families: bool = False,
) -> Optional[Dict[str, Any]]:
```

护栏**插在空输入检查（`:282-284` 的 `if raw_count == 0: return None`）之后**——
放在函数最前面会打挂 `tests/test_five_layer_phase3.py:236-238` 的
`test_aggregate_group_empty`，那条用例不传 `family_filter` 且期望返回 None：

```python
    if family_filter is None and not allow_mixed_families:
        raise ValueError(
            "aggregate_group without family_filter mixes entry forward returns with "
            "observation risk_avoided_pct and merges 'win' with 'correct_wait'. "
            "Pass family_filter='entry'/'observation', or allow_mixed_families=True "
            "if you explicitly want the deprecated legacy metric."
        )
```

**保留** `allow_mixed_families=True` 通道而不是删掉旧逻辑，是为了让既有历史运行结果
仍能按原口径复算做对照；但默认路径不再可能误用。

- [ ] **Step 4: 改生产调用点**

`:203` 改为：

```python
        # 本行若本就单族（signal_family 行、<维度>_signal_family 拆分行），
        # 就用它自己的族；只有真正混族的行，顶层才只代表 entry——
        # observation 的"正确观望"不是收益，并进来会把胜率系统性抬高。
        families = {e.signal_family for e in evaluations if e.signal_family}
        family_filter = next(iter(families)) if len(families) == 1 else "entry"
        metrics = aggregate_group(evaluations, family_filter=family_filter)
        if metrics is None:
            return None
```

`family_breakdown` 的写入（`:226-228`）保持原样，不动。

- [ ] **Step 4b: 让 `sample_count` 与 `sample_baseline` 仍代表整组**

`aggregate_group` 在族过滤后**重算**了 `raw_count`（`:290`）并用过滤后的列表构造
`sample_baseline`（`:292`）。不处理的话，混族分组行的样本数会缩水成 entry 数——
而「这一组一共有多少样本」和「顶层收益指标代表哪一族」是两件事，
前者缩水会让读报告的人以为样本量不足。

把这两项改为在**未过滤**的列表上计算：

```python
    raw_count = len(evaluations)
    if raw_count == 0:
        return None

    # 样本口径与指标口径分离：sample_count / sample_baseline 始终描述整组，
    # 族过滤只影响收益类指标的取数来源。
    sample_baseline = _build_sample_baseline(evaluations)

    if family_filter is None and not allow_mixed_families:
        raise ValueError(...)

    if family_filter is not None:
        evaluations = [e for e in evaluations if e.signal_family == family_filter]
        if not evaluations:
            return None
```

这样处理后，以下三条既有用例保持绿：
`tests/test_five_layer_aggregator.py:292`（`test_overall_summary` 的 sample_count 4）、
`:746-761`（`entry_sample_count` / `observation_sample_count` / `suppressed_reasons`）、
`:781-787`（`test_setup_type_summary_keeps_missing_metric_strategy_with_suppressed_reason`）。

> `tests/test_five_layer_aggregator.py:456` 的 overall `stage_accuracy` 原本就合并两族，
> 跑出来看实际结果再决定是改断言还是把 `stage_accuracy_rate` 排除在族过滤之外。

- [ ] **Step 5: 更新既有测试**

需要改的只有 `tests/test_five_layer_phase3.py:222/233/247/257` 四处不带 `family_filter`
的 `aggregate_group(evals)` 调用：显式声明意图，钉 entry 语义的传 `family_filter="entry"`，
钉旧口径的传 `allow_mixed_families=True`。
**逐条判断该用哪个，不要一律加 `allow_mixed_families`**——那等于把刚加的护栏绕过去。

不需要改的：`:236-238` 的空输入用例（护栏在空检查之后）；
`tests/test_five_layer_aggregator.py:331` 那处**已经**在 `:356` 传了
`family_filter="entry"`。

还有一条会**合理地**变红：`tests/test_five_layer_aggregator.py:595-596` 的
`test_strategy_cohort_summaries_use_primary_strategy_and_sample_bucket`。
`ps=trend_pullback|sb=boundary|...` 这个 cohort 本身是混族的
（entry −2.0 + observation 8.0），改后 `avg_return_pct` 从 3.0 变成 −2.0。
**这是本任务想要的效果**，更新断言并在 commit 里说明。

- [ ] **Step 6: 全量回归**

```bash
python -m pytest -m "not network" -q --tb=short
```

预期：仅剩 `test_env.py::test_notification`。

- [ ] **Step 7: 提交**

```bash
git add src/backtest/aggregators/group_summary_aggregator.py tests/
git commit -m "fix: make top-level backtest metrics entry-family only

Mixing observation risk_avoided_pct into win_rate and profit_factor
inflated ratings by counting 'correct_wait' as a win."
```

---

## Task 9: 同步 CHANGELOG

**Files:**

- Modify: `docs/CHANGELOG.md`

`AGENTS.md` 第 1 节把「API 行为、报告结构变化」列为必须同步 `docs/CHANGELOG.md`
的硬规则。本计划命中三处对外可见变化，不能只当内部重构提交：

- [ ] **Step 1: 记录三条变更**

1. **回测运行载荷新增 `code_revision` / `config_hash`**（Task 7）。
   `FiveLayerBacktestRun.to_dict` 是 API 与导出的出口，字段是**追加**，
   旧客户端不受影响。
2. **顶层回测指标口径收窄为 entry 单族**（Task 8）。
   这条要写清楚：`win_rate` 与 `profit_factor` 的**数值会变**，
   历史评级与新评级不可直接对比。混族口径可通过
   `allow_mixed_families=True` 显式取回。
3. **`screening_candidates` 新增 `raw_rule_score`**（Task 6）。
   `rule_score` 的既有语义（复合优先级分）**不变**，新字段是追加。

- [ ] **Step 2: 确认无需动 README**

本计划不改 CLI 参数、部署方式与通知方式，`README.md` 无需同步——
在交付说明里写明这一判断依据，而不是默认跳过。

- [ ] **Step 3: 提交**

```bash
git add docs/CHANGELOG.md
git commit -m "docs: record backtest stage-0 payload and metric convention changes"
```

---

## 验收（本计划范围内）

| # | spec 条目 | 验证方式 |
| --- | --- | --- |
| 1 | 验收 #36 阶段 0 测试全绿 | `pytest -m "not network"` 除 `test_env.py::test_notification` 外 0 failed，且无 skip/xfail 掩盖 |
| 2 | 止损解析统一 | `tests/test_setup_stop_loss.py` 6 条（含 2 条反例）+ 端到端 probe_entry 用例 |
| 3 | `raw_rule_score` 可用 | Task 6 断言原始分保留且与复合分不等 |
| 4 | 版本字段无 NULL | Task 7 的 7 字段非空断言 |
| 5 | 指标不再混族 | Task 8 的 50% vs 75% 对照 + 混族需显式 opt-in 的反例 |
| 6 | 单例泄漏可见 | 泄漏数 ≤ 5 时护栏改为 `pytest.fail` 并当场拦截；> 5 时保留观察版并交付泄漏清单。两种情况下 `test_data_health_service.py` 都必须清零 |

**未覆盖项**：spec 10.5 的完整 `run_data_manifest`（copy → hash → export）、11.2 的 free/paid 机器闸门、8.2c 的逐日收益公式实现——三者都需要历史数据或快照链路，属阶段 1 及之后。

---

## 风险与回滚

| 风险 | 影响 | 回滚方式 |
| --- | --- | --- |
| Task 5 改动 L5 判定，可能让候选数量显著上升 | 生产选股结果变化 | 这是**用户可见变更**：合入前按 Step 8 做修复前后三项分布对比并留证；候选数变化超过一个数量级则退回补运行时开关；异常则回滚该 commit |
| Task 4 与 Task 5 都改 `golden_cases`，后者会再次弄红前者 | 误以为出现回退 | 已在 Task 4 Step 5 与 Task 5 Step 7 显式说明；验收 #36 在 Task 8 收口 |
| Task 6 漏改 `CandidateDecision.from_record` | 单测绿、库里 `raw_rule_score` 全 NULL | Step 3 已列为四处改动之一；Step 5 的迁移后须抽查真实入库值非 NULL |
| Task 8 的族过滤改变 `sample_count` 语义 | 分组样本数缩水 | Step 4 已列为必须核对项 |
| Task 5 的 fixture 改造被草率处理 | 假绿灯换个位置继续存在 | Step 7 明确禁止保留 `has_stop_loss` 键读取；review 时逐个 fixture 核对 |
| Task 8 抛 `ValueError` 打断既有调用方 | 回测聚合报错 | 生产仅一个调用点已改；若有遗漏，异常信息直接指出修法 |
| Task 3 的题材上限收紧 L2 | 候选池变窄 | 同 Task 5，属用户可见变更，须记录差异 |
| `conftest.py` 护栏暴露出大量既有泄漏 | 范围膨胀 | Step 4 已设阈值：超过 5 处即停下汇报，不在本计划内硬扛 |

**整体回滚**：八个任务各自独立 commit，可单独 revert。其中 Task 5 与 Task 3 会改变选股输出，revert 后行为立即恢复；其余任务均为追加字段或测试改动，无行为影响。

---

## 与数据基础设施计划的关系

本计划**不依赖** `docs/superpowers/plans/2026-08-11-historical-data-infra-stage-a.md`，两者可并行。

但阶段 1（历史候选生成）**必须**等数据侧走完 J，理由见 spec 11.0：J 之前因子链路仍读不复权价，历史重放会把除权假断层复制到 2018–2026 全窗口。审计实测 000001 / 2024-06-14 的 raw MA20 相对正确尺度偏差 **6.7939%**，这个量级会污染所有比率型因子，而它们正是 L4 买点判定的主要输入。
