# base 因子缓存键可信化 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 base 因子缓存键如实反映「因子实际是用哪些参数算出来的」，消除一处会静默返回错误因子的脱钩，并让 base 快照能在不同策略之间共享。

**Architecture:** 两步。先让 base 因子只读传入的 `config`（当前有两个检测器绕过它去读全局单例，缓存键因此与实际计算脱钩）；再为 base 快照**新增**一个 8 字段白名单键，v2 冻结证据与已评估因子两层的键保持原样不动。

**Tech Stack:** Python 3.10+、pandas、pytest、`dataclasses.fields` / `replace`、`hashlib`。

---

## 为什么这份计划排在「重放引擎四处接缝」之前

`docs/superpowers/specs/2026-08-12-replay-engine-parameterization-notes.md` 把阶段 1 的核心设计问题定为「如何让策略私有的配置字段不污染 base 缓存键」，并把风险描述为**变慢十倍且不报错**。后续取证推翻了其中两条假设，风险等级要上调：

1. **base 因子不是中立的通用技术因子。** `FactorService._compute_extended_factors`（`src/services/factor_service.py:460-549`）在 base pass 里跑的是整套检测器：MA100、跳空涨停、MACD 背离、趋势线、123 形态、v1 底背离双突破、缩量回踩、MA100+low123、MA100+60min。它是**策略族输出的超集**，不是 MA/RSI/MACD 这种通用层。新策略若复用已有检测器输出则免费，若需要新检测器则不在覆盖范围内。

2. **base 因子读取的配置只有 8 个字段**，不是 232 个（`len(fields(Config))` 实测 232）。这让白名单在规模上完全可维护。

3. **存在一处比"变慢"严重得多的脱钩（本计划的首要目标）。** 其中 5 个字段是从全局单例 `get_config()` 读的，而缓存键哈希的是**传入的 `config`**：

```783:791:src/services/factor_service.py
        # Joint detector — thresholds are configurable via env/config so they
        # can be tuned without code changes / image rebuilds.
        _cfg = get_config()
        joint = Low123TrendlineDetector.detect(
            group,
            max_p1_p2_bars=_cfg.low123_max_p1_p2_bars,
            max_breakout_gap=_cfg.low123_max_breakout_gap,
            break_tolerance=_cfg.low123_break_tolerance,
        )
```

```861:866:src/services/factor_service.py
        _cfg = get_config()
        result = BottomDivergenceBreakoutDetector.detect(
            group,
            max_breakout_gap=_cfg.bottom_divergence_max_breakout_gap,
            break_tolerance=_cfg.bottom_divergence_break_tolerance,
        )
```

两个方向都会出问题：

- 只改传入 config → 缓存键变了、重算了，但检测器仍用单例的旧值 → **白算，且算出来的不是你要的参数对应的因子**。
- 只改单例（例如通过环境变量）→ 缓存键不变 → **直接命中旧缓存，静默返回用另一套参数算出的因子**。

今天它只是潜伏：重放 CLI 各 leg 传的 config 一致，单例也无人改动。**阶段 1 要跑任意策略集，正是激活它的场景。** 在缓存键不可信的前提下改造那四处接缝，等于在流沙上盖楼——所以先做这一份。

## 设计决策记录

### 决策一：白名单，而非策略参数命名空间

笔记留了两个候选方向，取证后结论如下。

**否决「策略声明自己的参数命名空间」**：仓库里不存在这个机制，且代价过大。YAML 策略定义（`strategies/*.yaml`，由 `src/agent/skills/base.py:62-117` 的 `load_skill_from_yaml` 加载）只有 `name` / `display_name` / `description` / `instructions` 四个必填项，加可选的 `screening.filters` / `screening.scoring`。filters 里只能写**引用因子快照已有列名的字面量阈值**（`{field, op, value}`），这些阈值由筛选引擎消费，**从不流入 `Config`，也从不参与因子计算**。要让 YAML 声明能影响因子层的参数命名空间，需要新造一套 schema、校验、以及从 YAML 到 `Config` 的注入通道——远超阶段 1 范围，且违反 AGENTS.md「不新增平行实现」。

**采纳「白名单」**：base 路径真正读取的配置字段只有 8 个，全部可枚举：

| # | 字段 | 读取位置 | 作用 |
| --- | --- | --- | --- |
| 1 | `screening_factor_lookback_days` | `bottom_divergence_v2_performance.py:438`、`factor_service.py:76` | 取窗长度 |
| 2 | `screening_min_list_days` | `factor_service.py:81`，用于 `:452` | 次新股风险标记 |
| 3 | `screening_breakout_lookback_days` | `factor_service.py:86`，用于 `:191` | 突破比 |
| 4 | `low123_max_p1_p2_bars` | `factor_service.py:788` | 123 形态检测 |
| 5 | `low123_max_breakout_gap` | `factor_service.py:789` | 同上 |
| 6 | `low123_break_tolerance` | `factor_service.py:790` | 同上 |
| 7 | `bottom_divergence_max_breakout_gap` | `factor_service.py:864` | v1 底背离检测 |
| 8 | `bottom_divergence_break_tolerance` | `factor_service.py:865` | 同上 |

该清单已经过独立复核：从 `build_factor_snapshot_from_groups` 可达的全部 config 读取点仅 `:71/76/81/86`、`:785`、`:861`，以及在 `:1032` 因 v2 关闭而短路的 v2 块；沿途的 indicator 与 `LeaderScoreCalculator` / `ExtremeStrengthScorer` 都不读 `get_config()`。

**白名单的失败模式比黑名单危险，必须配覆盖性测试。** 黑名单漏加字段 → 过度失效 → 变慢但正确；白名单漏加字段 → 过度复用 → **算错**。Task 3 用枚举 `Config` 全部字段的变异测试兜底。

### 决策二：只给 base 快照换键，证据层的键原样不动

**这是复核中纠正的一处严重错误，动手前必须理解。**

`base_hash` 不只用于 base 快照的 pickle 文件名。它同时被当作 v2 **冻结证据**与**已评估因子**两层缓存的 `config_hash`：

```512:531:src/backtest/services/bottom_divergence_v2_performance.py
            temporary_key = self._temporary_frozen_key(
                data_version=self.data_version,
                code=code,
                as_of_index=as_of_index,
                config_hash=base_hash,
            )
            temporary_keys[code] = temporary_key
            frozen = self._frozen_lookup.get(temporary_key)
            if frozen is not None:
                evaluation_keys = tuple(
                    FrozenEvidenceCacheKey(
                        data_version=self.data_version,
                        code=code,
                        candidate_version=candidate_version,
                        as_of_index=as_of_index,
                        algorithm_version=(
                            FROZEN_EVIDENCE_ALGORITHM_VERSION
                        ),
                        config_hash=base_hash,
                        parameter_hash=parameter_hash,
                    )
```

而 `_parameter_hash`（`:398-406`）只覆盖三个网格字段。于是 `bottom_divergence_v2_sync_window` / `_retention_bars` / `_breakout_buffer_pct` / `_r1_weights` / `_r2_weights` **目前只由 `base_hash` 这一处覆盖**，它们确实决定 v2 输出（`factor_service.py:1232-1265` 的 `_bottom_divergence_v2_zone_params` 把五个字段全部装进 `ResistanceZoneParams`；`causal_bottom_divergence_detector.py:1174` 用 `sync_window` 判 `major.confirmed`；`:679` 在冻结阶段就把 `retention_bars` 烘进证据）。

`base_hash` 这个局部变量在 `build_factor_snapshot` 里共 **4 个使用点**，实施时必须逐个分清归属：

| 位置 | 归属 | 处置 |
| --- | --- | --- |
| `:443` `self._base_path(trade_date, base_hash)` | base 快照层 | 换成新的白名单键 |
| `:516` `_temporary_frozen_key(config_hash=...)` | 证据层 | 保持旧口径 |
| `:530` `FrozenEvidenceCacheKey(config_hash=...)` | 证据层 | 保持旧口径 |
| `:574` `frozen_keys` 构造处 | 证据层 | 保持旧口径 |

**因此「把这些字段从缓存键里剔除」是错的**：对 base pickle 而言它们确实无效（base pass 强制 `bottom_divergence_v2_enabled=False`，见 `:447-450`），但对共享同一个键的证据两层而言，剔除等于让缓存把一套参数的证据当成另一套参数的结果返回——正是本计划要消灭的那类静默错误。

**采纳的方案**：`_config_hash` 保持现状不动，继续服务证据两层；**新增**一个只给 base 快照用的 `_base_config_hash`。收益（跨策略共享 base 因子）照拿，且不引入第二份需要独立守护完整性的白名单。

一个附带效果随之取消：`sync_window` 等五个字段仍会触发 base 重算。这是**刻意保留的既有行为**——消除这点浪费需要给证据层单独建白名单并配套完整性测试，成本远高于收益，留给需要时再做。

## 明确不在本计划范围内

- **重放引擎的四处接缝**（`screener_factory` 二选一分支、`build_isolated_config` 网格写死、事件/阶段抽取读 v2 字段、因子回调写死）。它们是下一份计划的内容，前提是本计划完成。
- **`cache_directory` 跨进程透传**。阶段 1 的前置约束明确规定**不得跑出任何用于下结论的历史候选**（`2026-08-11-signal-research-backtest-design.md` 11.-1），所以分批/断点跑全窗口的需求尚未成立，按 YAGNI 推迟到阶段 2，届时与 `data_version` 失效条件一并设计。已确认 `cache_directory` 当前无任何调用方传入，缓存目录恒为进程内临时目录，因此改键格式不会命中跨进程的陈旧产物。
- **给证据层单独建白名单**（理由见决策二）。
- **修改 `Config` 的字段结构或默认值。** 本计划只改「谁读它」和「怎么哈希它」。

## 提交约定

AGENTS.md 规定未经明确确认不执行 `git commit`。各 Task 的提交步骤请在得到操作者确认后再执行；未确认时把改动留在工作区并在交付说明里写明。commit message 用英文，不加 `Co-Authored-By`（Cursor 环境会自动追加该 trailer，忽略即可）。

## 文件结构

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `src/services/factor_service.py` | 因子计算。两个 `@staticmethod` 检测器包装函数改为接收显式 config 参数 | 修改 |
| `src/backtest/services/bottom_divergence_v2_performance.py` | `ValidationFactorCache` 与缓存键 | 修改 |
| `tests/test_factor_service_config_isolation.py` | 新增：证明 base 因子只读传入 config | 新建 |
| `tests/test_base_factor_cache_key_whitelist.py` | 新增：白名单覆盖性变异测试 | 新建 |
| `tests/test_bottom_divergence_v2_performance.py` | 既有缓存共享测试，扩若干条 | 修改 |
| `tests/test_factor_service_phase3.py`、`tests/test_factor_service_bottom_divergence.py` | 直接调用被改签名的静态方法，需同步 | 修改 |

---

## Task 1: 让 base 因子只读传入的 config

**Files:**
- Modify: `src/services/factor_service.py:460-549`（`_compute_extended_factors`）、`:742-830`（`_compute_pattern_123_factors`）、`:832-916`（`_compute_bottom_divergence_factors`）
- Modify: `tests/test_factor_service_phase3.py:57,68`、`tests/test_factor_service_bottom_divergence.py:853,925,954,979`（直接调用点）
- Test: `tests/test_factor_service_config_isolation.py`（新建）

两个目标函数都是 `@staticmethod`，拿不到 `self.config`，所以要显式加参数。调用方 `_compute_extended_factors` 是实例方法，持有 `self.config`（`factor_service.py:71`，缺省 `get_config()`）。

**测试用探针而不是构造能触发差异的行情数据**：断言检测器收到的 kwarg 等于传入 config 的值，结果确定、与行情形态无关。若改为断言因子数值差异，测试会依赖精心构造的 K 线，脆且难懂。已确认两个检测器在 `factor_service.py:20-23` 模块级导入（patch 目标有效），且阈值均以关键字传参（`.call_args.kwargs` 有效）。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_factor_service_config_isolation.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

```
python -m pytest tests/test_factor_service_config_isolation.py -v
```

预期：两个参数化用例都 FAIL，报「检测器拿到 30（默认值），而传入 config 要求 7（或 13）」。

**若报的是 `KeyError` 或其他异常而不是这条断言**，说明夹具还缺列，先修夹具——用错误的理由变红不算 RED。

- [ ] **Step 3: 最小实现**

`src/services/factor_service.py`，把两个静态方法改为接收显式 config：

```python
    @staticmethod
    def _compute_pattern_123_factors(
        group: pd.DataFrame, config: Config
    ) -> tuple[dict, dict]:
```

函数体内删掉 `_cfg = get_config()`，改用参数：

```python
        # 阈值取自传入的 config：base 因子缓存键哈希的正是这个对象，
        # 这里若回落到全局单例，缓存键就不再代表因子实际的计算参数。
        joint = Low123TrendlineDetector.detect(
            group,
            max_p1_p2_bars=config.low123_max_p1_p2_bars,
            max_breakout_gap=config.low123_max_breakout_gap,
            break_tolerance=config.low123_break_tolerance,
        )
```

`_compute_bottom_divergence_factors` 同样处理：

```python
    @staticmethod
    def _compute_bottom_divergence_factors(
        group: pd.DataFrame, config: Config
    ) -> dict:
```

```python
        result = BottomDivergenceBreakoutDetector.detect(
            group,
            max_breakout_gap=config.bottom_divergence_max_breakout_gap,
            break_tolerance=config.bottom_divergence_break_tolerance,
        )
```

在 `_compute_extended_factors`（`:460-549`）里把 `self.config` 传下去。**改之前先枚举全部调用点**：

```
rg -n "_compute_pattern_123_factors|_compute_bottom_divergence_factors" --glob "*.py"
```

已知的测试侧调用点有 6 处：`tests/test_factor_service_phase3.py:57,68`、`tests/test_factor_service_bottom_divergence.py:853,925,954,979`。它们都以单参数形式直接调用静态方法，签名变更后会 `TypeError`，必须同步传入 `Config()` 或用例自己的 config。

- [ ] **Step 4: 运行测试确认通过**

```
python -m pytest tests/test_factor_service_config_isolation.py -v
python -m pytest tests/test_factor_service.py tests/test_factor_service_phase3.py tests/test_factor_service_bottom_divergence.py tests/test_bottom_divergence_v2_performance.py -q
```

预期：新测试 2 passed；四个既有文件全绿。**这四个文件必须一起跑**——只跑前两个的话，签名变更打破的调用点要拖到 Step 6 才暴露。

- [ ] **Step 5: 变异检查**

把其中一处改回 `get_config()`，确认对应的参数化用例转红，然后还原：

```
python -m pytest tests/test_factor_service_config_isolation.py -v
```

预期：1 failed。还原后 2 passed。**若变异后仍然全绿，说明测试没有覆盖到真实路径，必须先修测试。**

- [ ] **Step 6: 全量离线回归**

```
python -m pytest -m "not network" -q -n 8
```

预期：除既有失败 `test_env.py::test_notification`（未配置企微 webhook 时对 `None` 取 `len()`，与本改动无关）外全绿。

AGENTS.md §6 要求 `src/` 改动优先跑 `./scripts/ci_gate.sh`。它是 bash 脚本，Windows 本机不可直接执行；**若在 Windows 上实施，用它内含的三项等价替代**（`python -m py_compile <changed>`、`python -m flake8 . --count --select=E9,F63,F7,F82`、上面的离线 pytest），并在交付说明里写明是替代执行而非原样执行。

- [ ] **Step 7: 提交（需操作者确认）**

```
git add src/services/factor_service.py tests/test_factor_service_config_isolation.py tests/test_factor_service_phase3.py tests/test_factor_service_bottom_divergence.py
git commit -m "fix: read base factor thresholds from the passed config instead of the global singleton"
```

---

## Task 2: 给 base 快照新增白名单键

**Files:**
- Modify: `src/backtest/services/bottom_divergence_v2_performance.py:383-396`、`:442`
- Test: `tests/test_bottom_divergence_v2_performance.py`

依赖 Task 1：Task 1 未完成时，白名单里的 `low123_*` / `bottom_divergence_*` 五个字段并不真正决定因子取值（检测器读单例），此时切白名单会把脱钩藏得更深。

**注意改动边界**：`_config_hash` 保持不变，继续服务证据两层（理由见决策二）。本 Task 只新增函数，并把 base 快照那一个使用点换掉；`:516` / `:530` / `:574` 三处证据键必须继续来自旧口径。四个使用点的归属表见决策二，动手前先对照代码逐个核实一遍。

本 Task 还要顺带补上 base 快照键的另一个缺口：**universe 不在键里**。`_base_path`（`:290-293`）只由 `trade_date` 与哈希构成，读回时（`:444-445`）不校验 universe。今天两条 leg 只有在 config 几乎完全相同时才会撞同一份 base pickle，风险低；但键收窄到 8 个字段后，撞键的 config 对大幅增多，「不同 universe 复用同一份 base 快照」从理论问题变成可达问题——而阶段 1 的多策略场景恰恰可能给不同策略配不同的预筛 universe。base 快照的内容按 code 逐行构成，universe 是它不折不扣的输入，按本计划「键必须如实反映结果依赖什么」的立场，它必须进键。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_bottom_divergence_v2_performance.py`（`replace` / `Config` / `ValidationFactorCache` 已在 `:4`/`:41`/`:19` 导入，只需新增 `BASE_FACTOR_CONFIG_FIELDS`）：

```python
def test_base_snapshot_key_ignores_fields_the_base_pass_never_reads():
    """与 base 快照无关的字段不该进入 base 快照键。

    base pass 强制关闭 v2（`bottom_divergence_v2_performance.py:447-450`），
    所以 v2 的这些参数不可能改变 base 快照的取值。
    """
    left = Config(bottom_divergence_v2_sync_window=3)
    right = Config(bottom_divergence_v2_sync_window=9)

    assert ValidationFactorCache._base_config_hash(
        left
    ) == ValidationFactorCache._base_config_hash(right)


def test_evidence_key_still_separates_v2_evidence_parameters():
    """证据层的键必须继续区分 v2 参数——这层不能跟着收窄。

    `sync_window` 决定 `major.confirmed`
    （`causal_bottom_divergence_detector.py:1174`），`retention_bars` 在冻结阶段
    就被烘进证据（`:679`）。它们只由这个键覆盖，`_parameter_hash` 不含它们；
    一旦收窄，缓存会把一套参数的证据当成另一套参数的结果返回。
    """
    left = Config(bottom_divergence_v2_sync_window=3)
    right = Config(bottom_divergence_v2_sync_window=9)

    assert ValidationFactorCache._config_hash(
        left, include_grid=False
    ) != ValidationFactorCache._config_hash(right, include_grid=False)


def test_base_snapshot_key_tracks_every_field_the_base_pass_reads():
    """base 路径真正读取的字段必须改变 base 快照键。"""
    baseline = Config()
    for field_name in BASE_FACTOR_CONFIG_FIELDS:
        current = getattr(baseline, field_name)
        mutated = replace(baseline, **{field_name: current + 1})
        assert ValidationFactorCache._base_config_hash(
            baseline
        ) != ValidationFactorCache._base_config_hash(mutated), (
            f"{field_name} 影响 base 因子，却没有进入 base 快照键"
        )


def test_base_snapshot_path_separates_different_universes():
    """universe 是 base 快照的输入，不同 universe 不得复用同一份缓存文件。

    base 快照按 code 逐行构成。键里不含 universe 时，两条 leg 若配了不同的
    预筛 universe 却撞上同一个 config 哈希，第二条会读回第一条的行集合。
    """
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(date(2024, 3, 1),),
        bar_groups={},
    )
    left = cache._base_path(
        date(2024, 3, 1),
        "same-hash",
        universe=pd.DataFrame([{"code": "000001", "name": "A"}]),
    )
    right = cache._base_path(
        date(2024, 3, 1),
        "same-hash",
        universe=pd.DataFrame([
            {"code": "000001", "name": "A"},
            {"code": "000002", "name": "B"},
        ]),
    )
    assert left != right
```

（8 个字段全为 int/float，`+ 1` 安全。）

- [ ] **Step 2: 运行测试确认失败**

```
python -m pytest tests/test_bottom_divergence_v2_performance.py -k "key" -v
```

预期：**收集阶段就报 `ImportError`**，因为 `BASE_FACTOR_CONFIG_FIELDS` 还不存在，整个文件的用例全部 error——这是正常的 RED，不要误判成写错了。

若想看到更细的失败信息，把 `BASE_FACTOR_CONFIG_FIELDS` 的 import 挪进用到它的那一个用例里，此时预期为：两条 `_base_config_hash` 用例报 `AttributeError`，`test_base_snapshot_path_separates_different_universes` 报 `TypeError`（`_base_path` 还不接受 `universe`），`test_evidence_key_still_separates...` 直接 PASS（它钉的是现状，作用是防止后人顺手收窄证据键）。

- [ ] **Step 3: 最小实现**

模块级新增常量：

```python
# base 快照路径真正读取的全部配置字段。这是**白名单**而不是黑名单：
# `Config` 有 232 个字段，其中绝大多数（LLM、通知、调度等）与因子计算无关，
# 把它们纳入键只会让无关改动触发全市场重算，跨策略共享因此无法成立。
#
# 白名单的失败模式比黑名单危险：漏登记一个真正影响 base 的字段会导致**过度复用**，
# 也就是拿旧参数的因子当新参数的结果用——算错，不是算慢。
# `tests/test_base_factor_cache_key_whitelist.py` 通过逐字段变异枚举守住这一点。
#
# 只用于 base 快照。证据两层继续走 `_config_hash`：那两层的输出受
# `bottom_divergence_v2_sync_window` 等字段影响，收窄会静默算错。
BASE_FACTOR_CONFIG_FIELDS = frozenset({
    "screening_factor_lookback_days",
    "screening_min_list_days",
    "screening_breakout_lookback_days",
    "low123_max_p1_p2_bars",
    "low123_max_breakout_gap",
    "low123_break_tolerance",
    "bottom_divergence_max_breakout_gap",
    "bottom_divergence_break_tolerance",
})
```

新增方法（`_config_hash` 原样保留）：

```python
    @staticmethod
    def _base_config_hash(config: Any) -> str:
        payload = asdict(config)
        base_payload = {
            name: payload[name]
            for name in sorted(BASE_FACTOR_CONFIG_FIELDS)
            if name in payload
        }
        return hashlib.sha256(
            canonical_json_dumps(base_payload).encode("utf-8")
        ).hexdigest()
```

> `if name in payload` 这层保护是为了让 `Config` 字段重命名不至于炸掉整条重放链路；静默漏字段的风险由 Task 3 的覆盖性测试承担。

在 `build_factor_snapshot`（`:424` 起）里把 base 快照文件的键换成新口径，**证据层保持旧口径**，并把 universe 纳入 base 快照路径：

```python
        base_snapshot_hash = self._base_config_hash(config)
        base_hash = self._config_hash(config, include_grid=False)
        base_path = self._base_path(
            trade_date, base_snapshot_hash, universe=universe
        )
```

`_base_path`（`:290-293`）增加一个必填的 keyword-only `universe` 参数，把 universe
指纹与 `data_version` 一起混进文件名。（属性名是 `_cache_directory`，不是
`_cache_dir`；`config_hash[:16]` 的截断是既有行为，照抄别改。）

**只哈希 `code` 列表是不够的**（本步骤的原始写法就是这样，已订正）：universe 还提供
`list_date`（`factor_service.py:183-188` → `days_since_listed` → `:207-212` 风险标记
→ `:232`）、`is_st`（`:208`、`:231`）、`circ_mv`（`:230`）、`name`（`:219`），它们
全都会改变 base 快照的内容。同一组代码配不同的上市日期或 ST 状态会产出不同快照却
撞同一个缓存文件——正是本计划要消灭的复用。因此指纹覆盖
`BASE_SNAPSHOT_UNIVERSE_COLUMNS = ("code", "name", "list_date", "is_st", "circ_mv")`
这五列，并同时纳入 `data_version`（证据层的键都带它，`_base_path` 没带）。

实施时踩到的两个坑，都已实测确认：

1. **`canonical_json_dumps` 不能直接吃 universe 里的值。** 它以 `allow_nan=False`
   运行，`NaN` 抛 `ValueError`；`np.int64` / `np.bool_` / `Timestamp` / `NaT` /
   `datetime.date` 一律抛 `TypeError`（只有 `np.float64` 例外，它是 `float` 子类）。
   单元格必须先渲染成带类型标签的文本，标签用于防止 `1` / `1.0` / `"1"` 与
   `None` / `NaN` / `False` 互相折叠。
2. **缺列与「有这列但为空」不能撞同一个指纹。** 实测结论比计划原先的表述更细：
   缺 `is_st` 列与 `is_st=False` 输出**完全相同**（都走 `False`），但缺列与
   `is_st=NaN` **确实不同**——`bool(nan)` 是 `True`，还会多一个 `st` 风险标记。
   `name` / `circ_mv` 的 `NaN` 同样与缺列不同；`list_date=NaN` 更会直接抛
   `TypeError`。所以指纹分开记录「缺哪些列」与「各列的值」，代价是缺列与
   `None`（生产侧等价）会多算一次，方向安全。

参数设为 keyword-only 且必填，是为了让任何遗漏的调用点直接 `TypeError`，而不是悄悄退回旧行为。改完用 `rg -n "_base_path" --glob "*.py"` 确认全部调用点都已更新。

`base_hash` 变量继续供 `:516` / `:530` / `:574` 的证据键使用，除新增变量外不做其他改动。

- [ ] **Step 4: 运行测试确认通过**

```
python -m pytest tests/test_bottom_divergence_v2_performance.py -q
```

预期：全绿，尤其这两条既有测试必须仍然通过——
- `test_v1_and_v2_legs_share_one_base_factor_pass`（`base_snapshot_builds == 1`）
- `test_factor_cache_matches_uncached_results_and_isolates_parameter_hash`（`base_snapshot_builds == 1`、`frozen_evidence_builds == 2`、`parameter_evaluations == 4`）

后者的 `frozen_evidence_builds == 2` 是证据层未被误伤的证据。**若它变成 4 或更多，说明证据键被连带收窄或改坏了，立即回退重做。**

- [ ] **Step 5: 补一条能察觉证据层被误伤的测试**

**这条测试是必写的，不是备选。** 已确认既有测试全都察觉不到「把证据层的 `base_hash` 换成 `base_snapshot_hash`」这个变异：`test_factor_cache_matches_uncached_results_and_isolates_parameter_hash`（`:64`）与 `test_cache_keys_isolate_...`（`:155`）两两只差 `cluster_pct` / `zone_score_min`，这两个字段既被 `include_grid=False` 弹掉、也不在白名单里，两个哈希对它们都不敏感。而 `test_evidence_key_still_separates_v2_evidence_parameters` 直接调 `_config_hash`、不经过 `build_factor_snapshot`，**不能用作参照**。

补这一条（两个只差 `sync_window` 的 config 共用一个 cache）：

```python
def test_evidence_layer_is_not_collapsed_by_the_narrowed_base_key():
    """证据层若跟着 base 键收窄，两套 v2 参数会共用同一份冻结证据。

    未误伤时两次调用的临时冻结键不同，各冻结一次；
    误伤后第二次会命中第一次的冻结证据，计数掉到 1。
    """
    groups = {"000001": _bars("000001")}
    trade_date = groups["000001"].iloc[-2]["date"]
    universe = pd.DataFrame([{"code": "000001", "name": "A"}])
    cache = ValidationFactorCache.from_groups(
        data_version="data-a",
        trade_dates=(trade_date,),
        bar_groups=groups,
    )

    for sync_window in (3, 9):
        cache.build_factor_snapshot(
            config=Config(
                bottom_divergence_v2_enabled=True,
                bottom_divergence_v2_sync_window=sync_window,
            ),
            universe=universe,
            trade_date=trade_date,
        )

    assert cache.stats["frozen_evidence_builds"] == 2, (
        "两套 v2 参数共用了同一份冻结证据，证据层缓存键被收窄了"
    )
```

写完后做变异检查：把 `:516` 的 `config_hash=base_hash` 改成 `config_hash=base_snapshot_hash`，确认这条测试转红，然后还原。

**转红的形态取决于误伤范围，两种都算捕获成功**：只改 `:516` 时，第二次调用会复用 `sync_window=3` 冻结的证据，随后在 `evaluate_frozen_evidence` 抛 `ValueError("frozen causal evidence parameter mismatch")`（`causal_bottom_divergence_detector.py:783`）；`:516` / `:530` / `:574` 三处全改才会安静地退化成 `frozen_evidence_builds == 1`。上面测试的 docstring 按后者写，实施时按实际观察到的形态订正它。

- [ ] **Step 6: 提交（需操作者确认）**

```
git add src/backtest/services/bottom_divergence_v2_performance.py tests/test_bottom_divergence_v2_performance.py
git commit -m "refactor: key the base factor snapshot on an explicit whitelist of base-affecting config fields"
```

---

## Task 3: 白名单覆盖性变异测试

**Files:**
- Test: `tests/test_base_factor_cache_key_whitelist.py`（新建）

这是整份计划的承重墙。没有它，白名单只是一句注释；有了它，漏登记会在提交前变红。

**两个必须先理解的坑（复核实测得出）：**

1. **必须复现缓存层的 v2 强制关闭。** 缓存在 `:447-450` 用 `replace(config, bottom_divergence_v2_enabled=False)` 构造 base config。测试若直接把原 config 交给 `FactorService`，把 `bottom_divergence_v2_enabled` 从 `False` 变异成 `True` 会打开整条 v2 路径、输出必然变化，于是它被误报成「漏登记字段」。**照着误报把它加进白名单，会直接打破 Task 2 要求全绿的 `test_v1_and_v2_legs_share_one_base_factor_pass`**——那条测试的全部意义就是 v1/v2 落在同一个 base 分区上。

2. **单调斜坡夹具对 8 个字段全部没有约束力。** 实测用等差上升的 K 线变异这 8 个字段，输出无一变化：`high` 严格递增让滚动最高价与窗口长度无关；universe 无 `list_date` 时 `days_since_listed=9999` 使次新股判断恒假；单调行情下两个检测器一律 `rejected`，五个阈值字段无从体现。**夹具必须能真正触发检测器**，否则这条测试是绿色的安慰剂。

- [ ] **Step 1: 先解决夹具，再写测试**

不要凭空造 K 线，也**不要**去 `tests/test_factor_service_bottom_divergence.py` 或 `tests/test_factor_service_phase3.py` 找形态——已确认那两个文件里只有单调斜坡（`test_factor_service_bottom_divergence.py:26-40` 的 `_make_test_df`）与随机游走（`test_factor_service_phase3.py:16-28` 的 `_make_group_df`），其中所有非 `rejected` 状态都是 `@patch` 出来的返回值，不是真实行情触发的。

可用的真实夹具有两处，需要**组一个多股 universe**（单只股票在结构上不可能同时满足 123 与底背离两组形态）：

| 来源（均在 `tests/test_low_123_trendline_detector.py`，除末行外） | 状态 | 已实测的翻转点 |
| --- | --- | --- |
| `_downtrend_then_breakout_ready_low123`（`:121`）、`_late_breakout`（`:177`） | `breakout_ready` | 对 `+7` 量级的放松不敏感 |
| `_deep_retrace_breakout_ready`（`:528`） | `breakout_ready` | `low123_max_breakout_gap` 收到 6 → `watching` |
| `_long_span_p1_to_p2`（`:686`） | `rejected` | `low123_max_p1_p2_bars` 放到 ≥45 → 翻转 |
| `_stale_late_p2_breakout`（`:747`） | `watching` | `low123_max_breakout_gap` 放到 ≥50 → 翻转 |
| `tests/fixtures/001337_bottom_divergence_20251201_20260805.csv`（165 行） | v1 底背离 `confirmed` | `bottom_divergence_max_breakout_gap` 收到 10 → `structure_ready`，收到 1 → `divergence_only` |

五份都要进 universe。**单向放松的变异探不到大多数翻转点**——上表里三个翻转分别需要「收紧到 6」「放松到 45」「收紧到 10」。这就是 Step 2 的 `_mutations` 要双向返回两个候选值的原因。实测双向能把覆盖从 2/8 提到约 5/8；只放松则只有 `screening_min_list_days` 与 `screening_breakout_lookback_days` 两个字段有效。

**三处夹具的列都不全，必须补齐后再用**：`_make_df`（`test_low_123_trendline_detector.py:27-43`）只产 open/high/low/close/volume，缺 `date` / `amount` / `pct_chg`；`001337` 的 CSV 缺 `amount`。消费点分别在 `factor_service.py:174`、`:181`、`:229`。

**补 `date` 列时必须让所有股票的末根 bar 落在同一个 `trade_date` 上。** `factor_service.py:174-176` 会把末根日期不等于 `trade_date` 的 group **静默丢弃**——没有报错、没有日志，夹具直接失效，正是 B1 那一类失败。写完夹具先断言 `len(baseline) == len(groups)`，确认没有股票被丢，再开始变异。

universe 必须带 `list_date`，取值让 `days_since_listed` 落在 `screening_min_list_days` 默认值（120）附近，否则该字段恒无约束力。

**不要设"至少 N 个字段可察觉"的硬门槛**——实测表明达不到。已知的天花板：

- `bottom_divergence_break_tolerance` 在 0.0 / 0.005 / 0.02 / 0.05 上输出**全部相同**，实测无敏感度；`low123_break_tolerance` 默认同为 `0.0`，大概率同样无约束力。
- `screening_factor_lookback_days` **结构上不可能**被这条测试覆盖（见 Step 3 第 4 条）。

正确做法是：夹具就位后逐个字段实测一遍，**如实记录哪些字段有约束力、哪些没有**，把结果写进交付说明。这条测试的价值在于拦住「新增字段影响 base 却没登记」，它对已有 8 个字段的覆盖率是附带产物，不是验收标准。伪造一个好看的覆盖率比承认缺口更有害。

- [ ] **Step 2: 写测试**

```python
# -*- coding: utf-8 -*-
"""白名单必须覆盖所有影响 base 因子输出的配置字段。

base 快照键改用白名单后，漏登记一个真正影响 base 的字段不会报错，
只会让缓存把旧参数的因子当成新参数的结果返回——静默算错。
本测试逐字段改值重算，凡是能改变输出的字段都必须已登记。
"""
import inspect
from dataclasses import fields, replace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.backtest.services.bottom_divergence_v2_performance import (
    BASE_FACTOR_CONFIG_FIELDS,
    ValidationFactorCache,
)
from src.config import Config
from src.services.factor_service import FactorService

# 无法机械变异的字段类型：字符串多为密钥/URL/模型名，改动可能触发校验；
# 容器与 None 无通用的"改一点点"语义。base 因子只消费数值与布尔，
# 因此跳过它们是安全的——但跳过集合本身要被断言，防止将来新增
# 一个影响 base 的非数值字段后被静默略过。
_UNMUTABLE_TYPES = (str, type(None), dict, list, tuple)


def _snapshot(config: Config) -> pd.DataFrame:
    # 复现缓存层构造 base config 的方式（`bottom_divergence_v2_performance.py:447-450`），
    # 否则测的就不是 base pass。这行必须与生产侧保持同步：生产侧若不再强制关闭 v2，
    # 这里也要跟着改，否则本测试会对 v2 字段失去判断力。
    base_config = replace(config, bottom_divergence_v2_enabled=False)
    service = FactorService(db_manager=MagicMock(), config=base_config)
    groups = _fixture_groups()          # Step 1 产出的、能触发检测器的夹具
    universe = _fixture_universe()      # 必须带 list_date
    # 所有 group 的末根 bar 必须同日，否则 `factor_service.py:174-176`
    # 会静默丢弃对不上的股票
    snapshot = service.build_factor_snapshot_from_groups(
        universe, groups, trade_date=_FIXTURE_TRADE_DATE
    )
    assert len(snapshot) == len(groups), (
        f"夹具有股票被丢弃：期望 {len(groups)} 行，实得 {len(snapshot)} 行；"
        f"检查各 group 的末根日期是否都等于 {_FIXTURE_TRADE_DATE}"
    )
    return snapshot


def _mutations(value):
    """给出若干"确实不同"的同类型值；不可变异时返回空元组。

    必须双向：实测多数检测器阈值的翻转点在收紧方向，
    只做放松（`+7`）会让这条测试对它们全部失明。
    """
    if isinstance(value, bool):
        return (not value,)
    if isinstance(value, int):
        return (value + 7, max(value // 3, 1))
    if isinstance(value, float):
        return (value * 1.5 + 0.017, value / 3.0)
    return ()


@pytest.mark.slow
def test_whitelist_covers_every_field_that_changes_base_factors():
    baseline_config = Config()
    baseline = _snapshot(baseline_config)

    unlisted_but_influential = []
    skipped = []
    errored = []
    for field in fields(Config):
        current = getattr(baseline_config, field.name)
        candidates = (
            () if isinstance(current, _UNMUTABLE_TYPES)
            else _mutations(current)
        )
        if not candidates:
            skipped.append(field.name)
            continue

        for mutated_value in candidates:
            try:
                mutated_config = replace(
                    baseline_config, **{field.name: mutated_value}
                )
                actual = _snapshot(mutated_config)
            except Exception as exc:
                # 不能并进 skipped：算不出来的字段可能恰恰是影响 base 的那个
                errored.append(f"{field.name}={mutated_value!r} -> {exc!r}")
                continue
            if actual.equals(baseline):
                continue
            if field.name not in BASE_FACTOR_CONFIG_FIELDS:
                unlisted_but_influential.append(field.name)
            break

    assert not unlisted_but_influential, (
        "以下字段会改变 base 因子输出却不在 BASE_FACTOR_CONFIG_FIELDS 里，"
        f"缓存会把旧值当新值复用：{sorted(set(unlisted_but_influential))}"
    )
    assert not errored, (
        "以下字段变异后算不出 base 快照，无法判定它是否影响 base，"
        f"必须逐个查明而不是当作安全：{errored}"
    )
    assert skipped, "跳过集合为空说明变异逻辑失效"


def test_cache_still_forces_v2_off_when_building_the_base_pass():
    """上面那条测试依赖生产侧强制关闭 v2，这里把该前提钉住。

    生产侧若不再强制关闭，base 输出就会随 `bottom_divergence_v2_enabled` 变化，
    上面的变异测试会把它报成漏登记字段——而把它登记进白名单会直接摧毁
    v1/v2 共享同一份 base 因子的性质。所以这个前提必须有独立的守卫。
    """
    source = inspect.getsource(ValidationFactorCache.build_factor_snapshot)
    assert "bottom_divergence_v2_enabled=False" in source, (
        "缓存层不再强制关闭 v2；请同步修正 _snapshot() 与白名单的边界假设"
    )
```

> 最后这条用源码文本断言，是有意为之的下策：真正想钉的是「base pass 与 v2 无关」这个语义，但它没有可观测的对外接口。若实施时找到更好的钉法（例如让 `build_factor_snapshot` 暴露它实际使用的 base config），优先用那个，并在交付说明里写明。

- [ ] **Step 3: 跑一次并如实记录约束力**

```
python -m pytest tests/test_base_factor_cache_key_whitelist.py -v --durations=5
```

记录并写进交付说明：

1. **用时**。超过 60 秒就把夹具的 bar 数或股票数降下来。
2. **`skipped` 的实际内容**。
3. **逐字段的约束力**：对白名单里的 8 个字段各做一次「从白名单中删除 → 跑测试」，列出哪些字段能让测试变红。**这一步不是可选的**，它决定这条测试到底守住了什么。
4. `screening_factor_lookback_days` **结构上无法**被这条测试守住：`build_factor_snapshot_from_groups` 根本不读它，它只在 `factor_service.py:76` 进构造器、在 `bottom_divergence_v2_performance.py:438` 的取窗处生效。为它单写一条针对 `ValidationFactorCache.build_factor_snapshot` 取窗行为的测试（两个不同 lookback 值应产生不同的 base 快照键与不同的窗口长度），并在交付说明里点明这条分工。

- [ ] **Step 4: 注册 slow 标记**

`setup.cfg:24-27` 的 `markers` 段当前只有 `unit` / `integration` / `network`，需补：

```
    slow: tests that take more than a few seconds
```

（当前未开 `--strict-markers`，未注册只会告警不会失败，但仍应登记。）

- [ ] **Step 5: 提交（需操作者确认）**

```
git add tests/test_base_factor_cache_key_whitelist.py setup.cfg
git commit -m "test: pin that the base factor whitelist covers every base-affecting config field"
```

---

## Task 4: 处理 `include_grid=True` 死代码

**Files:**
- Modify: `src/backtest/services/bottom_divergence_v2_performance.py:383-396`
- Test: `tests/test_bottom_divergence_v2_performance.py`

- [ ] **Step 1: 确认它确实没有调用方**

```
rg -n "\b_config_hash\(" --glob "*.py"
```

预期：定义处、`:442` 一处调用（`include_grid=False`），以及 Task 2 新增的测试调用。**若发现传 `True` 的真实调用方，跳过本 Task 并在交付说明里说明。**

注意别被同名子串误导：`validation_checkpoint_config_hash`（`bottom_divergence_v2_performance.py:32`、`bottom_divergence_v2_cli_service.py:20,190`、`bottom_divergence_v2_checkpoint.py:48` 及测试 5 处）与 Task 2 新增的 `_base_config_hash` 都不是本函数的调用方。上面的 `\b` 边界写法已排除前者。

- [ ] **Step 2: 删除参数**

把 `_config_hash(config, *, include_grid)` 改为 `_config_hash(config)`，只保留今天 `include_grid=False` 的行为（全量 `asdict` 减 4 个网格字段），同步改掉 `:442` 与 Task 2 新增测试里的调用。

**不要把这个函数与 `_base_config_hash` 合并**——它们服务不同的缓存层，口径必须保持分离（决策二）。

- [ ] **Step 3: 运行测试**

```
python -m pytest tests/test_bottom_divergence_v2_performance.py tests/test_base_factor_cache_key_whitelist.py -q
python -m pytest -m "not network" -q -n 8
```

预期：除既有的 `test_env.py::test_notification` 外全绿。

- [ ] **Step 4: 提交（需操作者确认）**

```
git add src/backtest/services/bottom_divergence_v2_performance.py tests/test_bottom_divergence_v2_performance.py
git commit -m "refactor: drop the unused include_grid branch from the evidence config hash"
```

---

## 收尾

- [ ] **文档同步**

`docs/CHANGELOG.md` 的 `[Unreleased] / ### Fixed` 记录单例脱钩的修复：症状（缓存键与实际计算参数脱钩，可能返回用另一套参数算出的因子）、触发条件（改动 `low123_*` / `bottom_divergence_*` 而未同步两侧）。关于「已产出的结论是否受影响」，**必须先用 git log 与既有 run 记录核实这些参数此前是否被改动过**；核实不了就写「未能确认」，不要替读者下结论。

`### Changed` 记录 base 快照键口径变化：新增 8 字段白名单键，与因子无关的配置改动不再触发全市场 base 重算；并写明证据两层的键**未随之收窄**及其原因。

- [ ] **更新设计笔记**

在 `docs/superpowers/specs/2026-08-12-replay-engine-parameterization-notes.md` §3 补结论：设计岔路定为白名单，理由是 base 配置面只有 8 个字段而 YAML 策略层不存在参数声明机制；订正 §3 开头的表述——base 是策略族检测器输出的超集，不是通用因子层；并补记 `base_hash` 同时服务证据两层这一事实，它是「不能简单收窄缓存键」的原因。

- [ ] **交付说明**

按 AGENTS.md 第 9 节结构：改了什么、为什么这么改、验证情况、未验证项、风险点、回滚方式。风险点至少覆盖：

- 白名单漏登记的后果是算错而非算慢。
- 覆盖性测试对 8 个字段中的哪几个**实际**有约束力，逐个列出；已知 `bottom_divergence_break_tolerance` 无敏感度、`screening_factor_lookback_days` 结构上不可覆盖，这两条必须在交付说明里明写，不能省略。
- Task 1 是否改全了所有调用点。
- 证据两层的键确实未被误伤——引用 `frozen_evidence_builds == 2` 的实际输出，而不是"应该没问题"。
- base 快照键收窄后，撞键的 config 对增多；universe（含 `name` / `list_date` / `is_st` / `circ_mv` 四列元数据）与 `data_version` 均已进键，但 `data_version` 只是一个不透明字符串，它是否真的随 bar 内容变化由调用方保证，`bar_groups` 的实际内容并未直接进键，属于已知缺口。
