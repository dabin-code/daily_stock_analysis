# -*- coding: utf-8 -*-
"""spec 10.5 不可变数据链路：copy -> hash -> export -> run_data_manifest。

`copy` 由 `bottom_divergence_v2_dataset.isolated_replay_database` 完成，本模块
只补上其余三段：对快照逐表算摘要、把摘要与版本号落进 `run_data_manifest`、
以及按 manifest 逐字段判定两次运行是否可比（spec 验收 #33）。
"""
from __future__ import annotations

import json
import math
import re
import shutil
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .bottom_divergence_v2_dataset import _iso
from .bottom_divergence_v2_report import canonical_json_dumps


HASH_BATCH_SIZE = 2000

# 快照里参与摘要的表。前五张是 `isolated_replay_database` 实际复制的读取面；
# 后三张目前这套部署没有，登记在这里是为了让 manifest 明确记下「这次运行
# 没有读到它们」——不登记的话，等这些表将来落地，旧 manifest 与新 manifest
# 会因为字段集不同而无法比对。
MANIFEST_TABLES: tuple[str, ...] = (
    "instrument_master",
    "board_master",
    "instrument_board_membership",
    "daily_sector_heat",
    "stock_daily",
    "stock_daily_adj",
    "corporate_actions",
    "instrument_status_history",
)

# 版本位取自对应表的内容摘要。`corporate_actions` 将来会自带 `version` 列
# （数据基础设施 7.6f），那时该列才是版本真源；在它落地之前，用内容摘要当
# 版本是能自证的做法，不需要凭空造一个版本号。
_VERSION_SOURCE_TABLES: Dict[str, str] = {
    "adj_table_version": "stock_daily_adj",
    "corporate_actions_version": "corporate_actions",
    "st_industry_version": "instrument_status_history",
}

# 摘要前缀。加前缀是为了让「表存在但没有行」（仍是一个真摘要）与
# `VERSION_ABSENT`（这套部署里根本没有这张表）在字符串层面就分得开。
DIGEST_PREFIX = "sha256:"

# 不参与内容摘要的审计时间戳列。**逐个点名，不做任何模式匹配。**
#
# 起因是实测：生产库 stock_daily 共 962 万行，`created_at` 与 `updated_at`
# 完全同步，只有 2092 个不同取值，其中 9,621,460 行都是同一次 staging 提升
# 写下的 2026-08-12。整表的审计时间戳会被下一次提升或重建整体改写，而价格
# 一个字节都不变。把它们算进内容摘要，等于每次例行维护后所有历史 manifest
# 一起判为不可比——一个逢维护必红的判定会被当噪音略过，比没有判定更糟。
# 这不削弱检测：值真变了，值本身就不同，内容摘要照样抓得到。
#
# 之所以只能点名：这几张表上 `list_date`、`delist_date`、`trade_date`、
# `date`、`adj_anchor_date`（复权锚点）全是业务字段，`*_date` 会把它们一次
# 吃光；`*_at` 今天恰好只命中这两列，但下一个 `suspended_at`、`announced_at`
# 就会被误伤。新增名字前必须确认该列除了「这行何时被写入」之外不携带任何
# 业务语义。
AUDIT_TIMESTAMP_COLUMNS = frozenset({"created_at", "updated_at"})

# 版本位上的三态：
#   VERSION_ABSENT —— 这套部署没有这张表，无从判定版本；
#   DIGEST_PREFIX 开头的值 —— 表存在，值是它当前内容的摘要（空表也有摘要）；
#   SQL NULL —— 这一行是本次改动之前写的，当时没有记录该版本位。
# 三者必须区分：把「没有这张表」和「有表但是空的」并成同一个值，manifest 就
# 无法回答「两次运行读到的是不是同一个世界」，可比性判定随之失效。
VERSION_ABSENT = "absent"

# 全窗口快照基本等于整张 stock_daily（生产库 6.85 GB / 962 万行），单份按
# 数 GB 计，不能无限累积。默认留 3 份是能支撑实际比对动作的最小值：当前这次、
# 用来对照的基线、外加一份更早的参照——只有三者同时在，才能回答「摘要变了，
# 是这次变的还是基线早就变了」。留 2 份每遇到一次三方比对就得重切一遍全窗口；
# 留 5 份在同时放着生产库与 staging 的盘上会先撑爆磁盘。
DEFAULT_SNAPSHOT_RETENTION_COUNT = 3

# 目录里放这个标记文件即免于回收，对应 spec 10.5 的「按运行标记保留」。
# 用文件而不是配置项：保留期验证（9.5）那类快照要跨进程、跨很多天存活，
# 标记必须和快照本身待在一起，不能依赖某个进程记得它。
PINNED_MARKER_FILENAME = ".pinned"

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_identifier(name: str) -> str:
    """SQLite 标识符不能参数化，只能先校验再内联。"""
    if not _IDENTIFIER.match(name):
        raise ValueError(f"unsafe sqlite identifier: {name!r}")
    return f'"{name}"'


def table_columns(session: Any, table_name: str) -> Optional[tuple[str, ...]]:
    """返回列名元组；表不存在时返回 None（空元组是不可能的返回值）。"""
    from sqlalchemy import text

    quoted = _quote_identifier(table_name)
    rows = session.execute(text(f"PRAGMA table_info({quoted})")).fetchall()
    if not rows:
        return None
    return tuple(str(row[1]) for row in rows)


@dataclass(frozen=True)
class ColumnPartition:
    """一张表的列按「内容」与「审计时间戳」分开后的结果。"""

    content: tuple[str, ...]
    audit: tuple[str, ...]


def partition_audit_columns(columns: Sequence[str]) -> ColumnPartition:
    """按 `AUDIT_TIMESTAMP_COLUMNS` 精确点名拆分列，保持原有列序。"""
    content = tuple(
        name for name in columns if name not in AUDIT_TIMESTAMP_COLUMNS
    )
    audit = tuple(name for name in columns if name in AUDIT_TIMESTAMP_COLUMNS)
    return ColumnPartition(content=content, audit=audit)


@dataclass(frozen=True)
class TableDigests:
    """一张表的两条摘要。

    `content` 决定可比性；`row_rewrite` 只报告「这些行被重写过」，不阻断。
    表上没有审计列时 `row_rewrite` 为 None——无从判断是否被重写，就不要编。
    """

    content: str
    row_rewrite: Optional[str]


def _cell_token(value: Any) -> Any:
    """把任意 SQLite 取值折成 canonical_json_dumps 能吃的形式。

    `canonical_json_dumps` 设了 `allow_nan=False`，且不认识 bytes/Decimal，
    直接喂原值会在真实数据上抛异常。这里对这几类做带标签的转写，既保证
    哈希是全函数，又不让不同类型的值折叠成同一个 token。
    """
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return {"__nonfinite__": repr(value)}
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__bytes__": bytes(value).hex()}
    if isinstance(value, Decimal):
        return {"__decimal__": str(value)}
    return _iso(value)


def hash_snapshot_table(
    session: Any,
    table_name: str,
    *,
    batch_size: int = HASH_BATCH_SIZE,
) -> Optional[TableDigests]:
    """一次扫描算出内容摘要与重写摘要；表不存在返回 None。

    行序无关是靠 `ORDER BY` 做到的，不是靠在内存里排序：全窗口快照的
    `stock_daily` 是千万行量级，把每行摘要都留在内存里排会吃掉数 GB。交给
    SQLite 排序则走它的外部归并，内存有界。

    排序键是「先全部内容列、再全部审计列」，这个顺序不能反。内容列在前，
    内容摘要的取值就与审计时间戳无关：内容相同的行无论时间戳怎么排，吐出的
    内容载荷序列都一样。审计列垫底则保证并列行之间仍有确定次序，重写摘要
    不会因为 SQLite 的返回顺序而漂移。

    `id` 计入内容摘要。`_copy_batches` 的每条复制语句都带确定的 ORDER BY，
    同一份生产数据切两次拿到的自增 id 相同，所以纳入 id 不会把内容相同的两份
    快照判成不同；反过来，排除 id 会让 board_id 这类被外键引用的值改变时摘要
    不动。
    """
    from sqlalchemy import text

    columns = table_columns(session, table_name)
    if columns is None:
        return None

    partition = partition_audit_columns(columns)
    quoted_table = _quote_identifier(table_name)
    selected = ", ".join(_quote_identifier(name) for name in columns)
    ordered = ", ".join(
        _quote_identifier(name)
        for name in (*partition.content, *partition.audit)
    )

    content_digest = sha256()
    content_digest.update(
        canonical_json_dumps(
            {"table": table_name, "columns": list(partition.content)}
        ).encode("utf-8")
    )
    content_digest.update(b"\n")
    rewrite_digest = sha256() if partition.audit else None
    if rewrite_digest is not None:
        rewrite_digest.update(
            canonical_json_dumps(
                {"table": table_name, "columns": list(partition.audit)}
            ).encode("utf-8")
        )
        rewrite_digest.update(b"\n")

    statement = text(
        f"SELECT {selected} FROM {quoted_table} ORDER BY {ordered}"
    ).execution_options(stream_results=True, yield_per=batch_size)
    result = session.execute(statement).mappings()
    for batch in result.partitions(batch_size):
        for row in batch:
            payload = {
                name: _cell_token(row[name]) for name in partition.content
            }
            content_digest.update(
                canonical_json_dumps(payload).encode("utf-8")
            )
            content_digest.update(b"\n")
            if rewrite_digest is not None:
                audit_payload = {
                    name: _cell_token(row[name]) for name in partition.audit
                }
                rewrite_digest.update(
                    canonical_json_dumps(audit_payload).encode("utf-8")
                )
                rewrite_digest.update(b"\n")

    return TableDigests(
        content=f"{DIGEST_PREFIX}{content_digest.hexdigest()}",
        row_rewrite=(
            None
            if rewrite_digest is None
            else f"{DIGEST_PREFIX}{rewrite_digest.hexdigest()}"
        ),
    )


def count_snapshot_table(session: Any, table_name: str) -> Optional[int]:
    """返回行数；表不存在返回 None，与「存在但 0 行」区分开。"""
    from sqlalchemy import text

    if table_columns(session, table_name) is None:
        return None
    quoted = _quote_identifier(table_name)
    return int(
        session.execute(text(f"SELECT COUNT(*) FROM {quoted}")).scalar_one()
    )


def build_manifest_payload(
    *,
    backtest_run_id: str,
    snapshot_db: Any,
    date_from: date,
    date_to: date,
    snapshot_path: Optional[str] = None,
    code_revision: Optional[str] = None,
    config_hash: Optional[str] = None,
    tables: Sequence[str] = MANIFEST_TABLES,
) -> Dict[str, Any]:
    """对只读快照算出一份 manifest 载荷。

    摘要取自快照而不是生产库：manifest 描述的是「这次运行读到了什么」，
    而运行只读快照。某张上游表没有被复制进来，对这次运行来说它就是不存在的，
    如实记 `VERSION_ABSENT` 比去生产库补一个运行根本没读过的版本号更可靠。
    """
    table_hashes: Dict[str, Optional[str]] = {}
    row_rewrite_hashes: Dict[str, Optional[str]] = {}
    row_counts: Dict[str, Optional[int]] = {}
    with snapshot_db.get_session() as session:
        for table_name in tables:
            digests = hash_snapshot_table(session, table_name)
            table_hashes[table_name] = (
                None if digests is None else digests.content
            )
            row_rewrite_hashes[table_name] = (
                None if digests is None else digests.row_rewrite
            )
            row_counts[table_name] = count_snapshot_table(session, table_name)

    versions = {
        column: (
            VERSION_ABSENT
            if table_hashes.get(source_table) is None
            else table_hashes[source_table]
        )
        for column, source_table in _VERSION_SOURCE_TABLES.items()
    }
    if snapshot_path is None:
        snapshot_path = getattr(snapshot_db, "snapshot_path", None)
    return {
        "backtest_run_id": backtest_run_id,
        "snapshot_path": snapshot_path,
        "table_hashes": table_hashes,
        "row_rewrite_hashes": row_rewrite_hashes,
        "row_counts": row_counts,
        "date_range": {"from": _iso(date_from), "to": _iso(date_to)},
        "code_revision": code_revision,
        "config_hash": config_hash,
        **versions,
    }


def _parse_date(value: Any) -> Optional[date]:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def write_run_data_manifest(db_manager: Any, payload: Mapping[str, Any]) -> None:
    """把 manifest 落库。同一个 backtest_run_id 重复写会撞唯一索引。"""
    from src.backtest.models.backtest_models import RunDataManifest

    date_range = payload.get("date_range") or {}
    with db_manager.get_session() as session:
        session.add(RunDataManifest(
            backtest_run_id=payload["backtest_run_id"],
            snapshot_path=payload.get("snapshot_path"),
            table_hashes_json=canonical_json_dumps(
                payload.get("table_hashes") or {}
            ),
            row_counts_json=canonical_json_dumps(payload.get("row_counts") or {}),
            row_rewrite_hashes_json=canonical_json_dumps(
                payload.get("row_rewrite_hashes") or {}
            ),
            date_range_from=_parse_date(date_range.get("from")),
            date_range_to=_parse_date(date_range.get("to")),
            adj_table_version=payload.get("adj_table_version"),
            corporate_actions_version=payload.get("corporate_actions_version"),
            st_industry_version=payload.get("st_industry_version"),
            code_revision=payload.get("code_revision"),
            config_hash=payload.get("config_hash"),
        ))
        session.commit()


def load_run_data_manifest(
    db_manager: Any,
    backtest_run_id: str,
) -> Optional[Dict[str, Any]]:
    """读回一份归一化 manifest；没有这次运行的记录时返回 None。"""
    from sqlalchemy import select

    from src.backtest.models.backtest_models import RunDataManifest

    with db_manager.get_session() as session:
        row = session.execute(
            select(RunDataManifest).where(
                RunDataManifest.backtest_run_id == backtest_run_id
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return normalise_manifest(row.to_dict())


def normalise_manifest(manifest: Any) -> Dict[str, Any]:
    """把 ORM 行、`to_dict()` 结果、`build_manifest_payload()` 载荷折成同一形状。

    比对必须能吃这三种输入：新算出来的载荷还没落库，历史 manifest 只在库里。
    要求调用方先手工对齐形状，等于把「忘了解析 JSON 就直接比字符串」这种
    静默误判留在每个调用点上。
    """
    if hasattr(manifest, "to_dict"):
        manifest = manifest.to_dict()
    source: Mapping[str, Any] = manifest

    if "table_hashes" in source:
        table_hashes = source.get("table_hashes") or {}
        row_counts = source.get("row_counts") or {}
        row_rewrite_hashes = source.get("row_rewrite_hashes") or {}
    else:
        table_hashes = _load_json_object(source.get("table_hashes_json"))
        row_counts = _load_json_object(source.get("row_counts_json"))
        row_rewrite_hashes = _load_json_object(
            source.get("row_rewrite_hashes_json")
        )

    if "date_range" in source:
        date_range = dict(source.get("date_range") or {})
    else:
        date_range = {
            "from": _iso(source.get("date_range_from")),
            "to": _iso(source.get("date_range_to")),
        }

    return {
        "backtest_run_id": source.get("backtest_run_id"),
        "snapshot_path": source.get("snapshot_path"),
        "table_hashes": dict(table_hashes),
        "row_rewrite_hashes": dict(row_rewrite_hashes),
        "row_counts": dict(row_counts),
        "date_range": {
            "from": _iso(date_range.get("from")),
            "to": _iso(date_range.get("to")),
        },
        "adj_table_version": source.get("adj_table_version"),
        "corporate_actions_version": source.get("corporate_actions_version"),
        "st_industry_version": source.get("st_industry_version"),
        "code_revision": source.get("code_revision"),
        "config_hash": source.get("config_hash"),
    }


def _load_json_object(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    loaded = json.loads(raw)
    return dict(loaded) if isinstance(loaded, dict) else {}


VERDICT_IDENTICAL = "identical"
VERDICT_COMPARABLE = "comparable"
VERDICT_NOT_COMPARABLE = "not_comparable"

# 差异存在但不影响可比性的字段。
#   snapshot_path —— 两次运行的快照必然落在不同目录，参与判定就没有任何两次
#     运行可比；
#   code_revision —— spec 5.3 的既有结论，也写在 FiveLayerBacktestRun 的注释里：
#     git commit 会因与回测无关的改动而变化，用它当键会把本可对比的两次运行
#     误判为不可比；
#   row_rewrite_hashes —— 只说明这些行被重写过，不说明数据变了，理由见
#     `AUDIT_TIMESTAMP_COLUMNS`。信息保留在差异清单里，但不参与判定。
_NON_BLOCKING_FIELDS = frozenset({"snapshot_path", "code_revision"})

_SCALAR_FIELDS = (
    "snapshot_path",
    "adj_table_version",
    "corporate_actions_version",
    "st_industry_version",
    "code_revision",
    "config_hash",
)


@dataclass(frozen=True)
class ManifestDifference:
    """一处逐字段差异。`field` 用点号定位到表名，便于直接读出是哪张表变了。"""

    field: str
    left: Any
    right: Any
    blocking: bool


@dataclass(frozen=True)
class ManifestComparison:
    verdict: str
    differences: tuple[ManifestDifference, ...]

    @property
    def blocking_fields(self) -> tuple[str, ...]:
        return tuple(
            difference.field
            for difference in self.differences
            if difference.blocking
        )

    @property
    def is_comparable(self) -> bool:
        return self.verdict != VERDICT_NOT_COMPARABLE


def _diff_mapping(
    prefix: str,
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    blocking: bool = True,
) -> list[ManifestDifference]:
    differences = []
    for key in sorted(set(left) | set(right)):
        left_value = left.get(key)
        right_value = right.get(key)
        if left_value != right_value:
            differences.append(ManifestDifference(
                field=f"{prefix}.{key}",
                left=left_value,
                right=right_value,
                blocking=blocking,
            ))
    return differences


def compare_manifests(left: Any, right: Any) -> ManifestComparison:
    """逐字段比对两份 manifest 并给出可比性判定（spec 验收 #33）。

    判定由代码产出，不由人回忆：只要有一张表的摘要对不上，两次运行读的就不是
    同一份数据，结论直接判为不可比。`backtest_run_id` 与 `created_at` 不参与
    比对——两次运行的这两项必然不同，比它们等于永远判红。
    """
    left_manifest = normalise_manifest(left)
    right_manifest = normalise_manifest(right)

    differences: list[ManifestDifference] = []
    for field_name in _SCALAR_FIELDS:
        left_value = left_manifest.get(field_name)
        right_value = right_manifest.get(field_name)
        if left_value != right_value:
            differences.append(ManifestDifference(
                field=field_name,
                left=left_value,
                right=right_value,
                blocking=field_name not in _NON_BLOCKING_FIELDS,
            ))
    differences.extend(_diff_mapping(
        "date_range",
        left_manifest["date_range"],
        right_manifest["date_range"],
    ))
    differences.extend(_diff_mapping(
        "table_hashes",
        left_manifest["table_hashes"],
        right_manifest["table_hashes"],
    ))
    differences.extend(_diff_mapping(
        "row_counts",
        left_manifest["row_counts"],
        right_manifest["row_counts"],
    ))
    differences.extend(_diff_mapping(
        "row_rewrite_hashes",
        left_manifest["row_rewrite_hashes"],
        right_manifest["row_rewrite_hashes"],
        blocking=False,
    ))

    if not differences:
        verdict = VERDICT_IDENTICAL
    elif any(difference.blocking for difference in differences):
        verdict = VERDICT_NOT_COMPARABLE
    else:
        verdict = VERDICT_COMPARABLE
    return ManifestComparison(verdict=verdict, differences=tuple(differences))


def prune_retained_snapshots(
    root: Any,
    *,
    keep: int = DEFAULT_SNAPSHOT_RETENTION_COUNT,
) -> tuple[str, ...]:
    """回收留存快照，返回被删掉的目录名。

    带 `PINNED_MARKER_FILENAME` 的目录不参与排序也不占 `keep` 名额——
    「最近 N 份」与「按运行标记保留」是并列的两条保留口径，若让标记目录占用
    名额，钉住 N 份就会把其余全部挤掉，等于取消了前一条。
    """
    if keep < 0:
        raise ValueError("keep must be >= 0")
    directory = Path(root)
    if not directory.is_dir():
        return ()

    candidates = []
    for child in directory.iterdir():
        if not child.is_dir():
            continue
        if (child / PINNED_MARKER_FILENAME).exists():
            continue
        candidates.append(child)

    candidates.sort(key=lambda path: (path.stat().st_mtime, path.name))
    doomed = candidates[:-keep] if keep else candidates
    removed = []
    for child in doomed:
        shutil.rmtree(child)
        removed.append(child.name)
    return tuple(removed)
