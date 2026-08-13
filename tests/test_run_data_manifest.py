# -*- coding: utf-8 -*-
"""spec 10.5 不可变数据链路：copy -> hash -> export -> run_data_manifest。"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from src.backtest.services.bottom_divergence_v2_dataset import (
    isolated_replay_database,
)
from src.backtest.services.run_data_manifest_service import (
    AUDIT_TIMESTAMP_COLUMNS,
    DEFAULT_SNAPSHOT_RETENTION_COUNT,
    DIGEST_PREFIX,
    PINNED_MARKER_FILENAME,
    VERDICT_COMPARABLE,
    VERDICT_IDENTICAL,
    VERDICT_NOT_COMPARABLE,
    VERSION_ABSENT,
    build_manifest_payload,
    compare_manifests,
    count_snapshot_table,
    hash_snapshot_table,
    load_run_data_manifest,
    partition_audit_columns,
    prune_retained_snapshots,
    table_columns,
    write_run_data_manifest,
)
from src.storage import DatabaseManager


def _manager(tmp_path, name: str = "snapshot.db") -> DatabaseManager:
    """建一个绕开单例的私有库。

    必须用 object.__new__：DatabaseManager.__new__ 是单例，直接构造会拿到
    进程级实例，测试之间互相污染，也会绕过 conftest 的生产库护栏。
    """
    manager = object.__new__(DatabaseManager)
    DatabaseManager.__init__(manager, f"sqlite:///{(tmp_path / name).as_posix()}")
    return manager


def _create_bars_table(manager: DatabaseManager) -> None:
    with manager._engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE bars (code TEXT, date TEXT, close REAL)"
        )


def _insert_bars(manager: DatabaseManager, rows) -> None:
    with manager._engine.begin() as conn:
        for code, day, close in rows:
            conn.execute(
                text("INSERT INTO bars VALUES (:code, :date, :close)"),
                {"code": code, "date": day, "close": close},
            )


@pytest.mark.unit
def test_table_digest_changes_when_a_single_cell_changes(tmp_path) -> None:
    left = _manager(tmp_path, "left.db")
    right = _manager(tmp_path, "right.db")
    try:
        for manager in (left, right):
            _create_bars_table(manager)
        _insert_bars(left, [("000001", "2024-01-02", 10.0)])
        _insert_bars(right, [("000001", "2024-01-02", 10.01)])

        with left.get_session() as session:
            left_digest = hash_snapshot_table(session, "bars")
        with right.get_session() as session:
            right_digest = hash_snapshot_table(session, "bars")

        assert left_digest is not None
        assert left_digest.content != right_digest.content
    finally:
        left._engine.dispose()
        right._engine.dispose()


@pytest.mark.unit
def test_table_digest_ignores_row_insertion_order(tmp_path) -> None:
    """摘要必须对行序不敏感，否则同一份数据换个插入顺序就判为不可比。"""
    left = _manager(tmp_path, "left.db")
    right = _manager(tmp_path, "right.db")
    try:
        for manager in (left, right):
            _create_bars_table(manager)
        rows = [
            ("000001", "2024-01-02", 10.0),
            ("000002", "2024-01-02", 20.0),
            ("000003", "2024-01-03", 30.0),
        ]
        _insert_bars(left, rows)
        _insert_bars(right, list(reversed(rows)))

        with left.get_session() as session:
            left_digest = hash_snapshot_table(session, "bars")
        with right.get_session() as session:
            right_digest = hash_snapshot_table(session, "bars")

        assert left_digest.content == right_digest.content
    finally:
        left._engine.dispose()
        right._engine.dispose()


@pytest.mark.unit
def test_absent_table_is_distinguishable_from_present_but_empty(tmp_path) -> None:
    """缺表与空表必须给出不同的摘要与行数。

    两者合并成同一个值，manifest 就无法回答「两次运行读到的是不是同一个
    世界」——一次运行根本没有 corporate_actions，另一次有表但没同步到数据，
    这是两种截然不同的处境。
    """
    manager = _manager(tmp_path)
    try:
        with manager._engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE present_empty (code TEXT)")

        with manager.get_session() as session:
            absent_digest = hash_snapshot_table(session, "never_created")
            absent_count = count_snapshot_table(session, "never_created")
            empty_digest = hash_snapshot_table(session, "present_empty")
            empty_count = count_snapshot_table(session, "present_empty")

        assert absent_digest is None
        assert absent_count is None
        assert empty_digest is not None
        assert empty_digest.content.startswith(DIGEST_PREFIX)
        assert empty_count == 0
    finally:
        manager._engine.dispose()


@pytest.mark.unit
def test_audit_exclusion_is_an_exact_name_set_that_spares_business_columns(
    tmp_path,
) -> None:
    """排除规则必须是逐个点名，不能是任何形态的名字模式。

    这张表上 `list_date` / `delist_date` / `trade_date` / `date` /
    `adj_anchor_date` 都是业务字段，`*_date` 一类的模式会把它们全吃掉；
    `*_at` 今天恰好只命中审计列，但下一个 `suspended_at`、`announced_at`
    就会被误伤。这里同时钉住「集合是哪两个名字」和「后缀不参与判断」。
    """
    assert AUDIT_TIMESTAMP_COLUMNS == frozenset({"created_at", "updated_at"})

    business_at = partition_audit_columns(("suspended_at", "created_at"))
    assert business_at.audit == ("created_at",)
    assert "suspended_at" in business_at.content

    business_date = partition_audit_columns(("adj_anchor_date", "updated_at"))
    assert business_date.audit == ("updated_at",)
    assert "adj_anchor_date" in business_date.content

    manager = _manager(tmp_path)
    try:
        with manager.get_session() as session:
            bars = partition_audit_columns(table_columns(session, "stock_daily"))
            instruments = partition_audit_columns(
                table_columns(session, "instrument_master")
            )
            heat = partition_audit_columns(
                table_columns(session, "daily_sector_heat")
            )

        assert bars.audit == ("created_at", "updated_at")
        for column in (
            "date",
            "adj_anchor_date",
            "adj_convention",
            "adj_factor_source",
            "close",
            "pre_close",
            "data_source",
        ):
            assert column in bars.content

        # 每张表的审计列不尽相同：instrument_master 只有 updated_at，
        # daily_sector_heat 只有 created_at。取交集而不是硬套同一组名字。
        assert instruments.audit == ("updated_at",)
        assert {"list_date", "delist_date"} <= set(instruments.content)
        assert heat.audit == ("created_at",)
        assert "trade_date" in heat.content
    finally:
        manager._engine.dispose()


@pytest.mark.unit
def test_manifest_marks_missing_upstream_tables_absent(tmp_path) -> None:
    """本部署没有 stock_daily_adj / corporate_actions /
    instrument_status_history，版本位应记显式缺失标记，而不是编一个值。
    """
    manager = _manager(tmp_path)
    try:
        payload = build_manifest_payload(
            backtest_run_id="run-a",
            snapshot_db=manager,
            date_from=date(2024, 1, 1),
            date_to=date(2024, 3, 1),
        )
        assert payload["adj_table_version"] == VERSION_ABSENT
        assert payload["corporate_actions_version"] == VERSION_ABSENT
        assert payload["st_industry_version"] == VERSION_ABSENT
        assert payload["table_hashes"]["corporate_actions"] is None
        assert payload["row_counts"]["corporate_actions"] is None
        assert payload["row_rewrite_hashes"]["corporate_actions"] is None
        # stock_daily 由 create_all 建出来，属于「存在但为空」。
        assert payload["table_hashes"]["stock_daily"].startswith(DIGEST_PREFIX)
        assert payload["row_rewrite_hashes"]["stock_daily"].startswith(
            DIGEST_PREFIX
        )
        assert payload["row_counts"]["stock_daily"] == 0
        assert payload["date_range"] == {
            "from": "2024-01-01",
            "to": "2024-03-01",
        }
    finally:
        manager._engine.dispose()


@pytest.mark.unit
def test_present_but_empty_upstream_table_gets_a_digest_version(tmp_path) -> None:
    """同一张上游表，建出来（哪怕是空的）就必须拿到摘要版本而非缺失标记。"""
    manager = _manager(tmp_path)
    try:
        with manager._engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE corporate_actions (code TEXT, version TEXT)"
            )
        payload = build_manifest_payload(
            backtest_run_id="run-a",
            snapshot_db=manager,
            date_from=date(2024, 1, 1),
            date_to=date(2024, 3, 1),
        )
        assert payload["corporate_actions_version"] != VERSION_ABSENT
        assert payload["corporate_actions_version"].startswith(DIGEST_PREFIX)
        assert payload["row_counts"]["corporate_actions"] == 0
    finally:
        manager._engine.dispose()


@pytest.mark.unit
def test_manifest_round_trips_and_is_one_per_run(tmp_path) -> None:
    from sqlalchemy.exc import IntegrityError

    manager = _manager(tmp_path)
    try:
        payload = build_manifest_payload(
            backtest_run_id="run-a",
            snapshot_db=manager,
            date_from=date(2024, 1, 1),
            date_to=date(2024, 3, 1),
            snapshot_path="snapshots/run-a/validation.db",
            code_revision="abc1234",
            config_hash="cfg-1",
        )
        write_run_data_manifest(manager, payload)

        loaded = load_run_data_manifest(manager, "run-a")
        assert loaded is not None
        assert loaded["snapshot_path"] == "snapshots/run-a/validation.db"
        assert loaded["code_revision"] == "abc1234"
        assert loaded["config_hash"] == "cfg-1"
        assert loaded["date_range"] == {
            "from": "2024-01-01",
            "to": "2024-03-01",
        }
        assert loaded["table_hashes"] == payload["table_hashes"]
        assert loaded["row_counts"] == payload["row_counts"]
        assert loaded["row_rewrite_hashes"] == payload["row_rewrite_hashes"]
        assert loaded["corporate_actions_version"] == VERSION_ABSENT

        # 一次运行只能有一份 manifest，否则「这次运行读了什么」有两个答案。
        with pytest.raises(IntegrityError):
            write_run_data_manifest(manager, payload)

        assert load_run_data_manifest(manager, "run-missing") is None
    finally:
        manager._engine.dispose()


@pytest.mark.unit
def test_absent_and_empty_survive_the_round_trip(tmp_path) -> None:
    """缺失/空的区分必须活过一次落库与读回，否则判定只在内存里成立。"""
    manager = _manager(tmp_path)
    try:
        payload = build_manifest_payload(
            backtest_run_id="run-a",
            snapshot_db=manager,
            date_from=date(2024, 1, 1),
            date_to=date(2024, 3, 1),
        )
        write_run_data_manifest(manager, payload)
        loaded = load_run_data_manifest(manager, "run-a")

        assert loaded["table_hashes"]["corporate_actions"] is None
        assert loaded["row_counts"]["corporate_actions"] is None
        assert loaded["row_counts"]["stock_daily"] == 0
        assert loaded["table_hashes"]["stock_daily"].startswith(DIGEST_PREFIX)
    finally:
        manager._engine.dispose()


@pytest.mark.unit
def test_inline_migration_backfills_index_when_only_the_table_exists(
    tmp_path,
) -> None:
    """列已在而索引缺失是可达状态，迁移必须独立补索引。

    把 CREATE INDEX 挂在「本次新增了列」分支里，这种库就永远补不上索引：
    create_all 对已存在的表整表跳过，没有别的路径会走到。
    """
    database = tmp_path / "legacy.db"
    manager = _manager(tmp_path, "legacy.db")
    manager._engine.dispose()

    legacy = object.__new__(DatabaseManager)
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE run_data_manifest")
        conn.exec_driver_sql(
            "CREATE TABLE run_data_manifest ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "backtest_run_id VARCHAR(64) NOT NULL, "
            "snapshot_path TEXT)"
        )

        def index_names() -> set:
            return {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA index_list(run_data_manifest)"
                ).fetchall()
            }

        assert "ix_run_data_manifest_backtest_run_id" not in index_names()
    engine.dispose()

    legacy._engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        legacy._apply_inline_migrations()
        # 幂等：同一套迁移跑两遍不得报错，也不得产生第二个索引。
        legacy._apply_inline_migrations()
        with legacy._engine.begin() as conn:
            columns = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(run_data_manifest)"
                ).fetchall()
            }
            indexes = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA index_list(run_data_manifest)"
                ).fetchall()
            }
        assert "ix_run_data_manifest_backtest_run_id" in indexes
        assert {
            "table_hashes_json",
            "row_counts_json",
            "row_rewrite_hashes_json",
            "date_range_from",
            "date_range_to",
            "adj_table_version",
            "corporate_actions_version",
            "st_industry_version",
            "code_revision",
            "config_hash",
        } <= columns
    finally:
        legacy._engine.dispose()


def _seed_replay_source(tmp_path) -> DatabaseManager:
    from datetime import timedelta

    from src.storage import InstrumentMaster, StockDaily

    source = _manager(tmp_path, "source.db")
    start = date(2024, 1, 1)
    with source.get_session() as session:
        session.add(
            InstrumentMaster(
                code="000001",
                name="测试",
                market="cn",
                listing_status="active",
                is_st=False,
            )
        )
        session.add_all([
            StockDaily(
                code="000001",
                date=start + timedelta(days=index),
                open=100.0,
                high=102.0,
                low=99.0,
                close=101.0,
                volume=1_000.0,
                amount=1_000_000.0,
                pct_chg=1.0,
            )
            for index in range(80)
        ])
        session.commit()
    return source


def _replay_window():
    from datetime import timedelta

    start = date(2024, 1, 1)
    return start + timedelta(days=30), start + timedelta(days=50)


@pytest.mark.unit
def test_default_isolated_replay_database_still_discards_the_snapshot(
    tmp_path,
) -> None:
    """默认行为必须与改动前完全一致：快照用完即删。"""
    import pandas as pd

    source = _seed_replay_source(tmp_path)
    date_from, date_to = _replay_window()
    try:
        with isolated_replay_database(
            source_db=source,
            universe=pd.DataFrame([{"code": "000001"}]),
            date_from=date_from,
            date_to=date_to,
            market="cn",
            market_guard_index="sh000001",
        ) as temporary:
            snapshot_path = Path(temporary.snapshot_path)
            assert snapshot_path.exists()
            assert temporary.snapshot_retained is False
        assert snapshot_path.exists() is False
    finally:
        source._engine.dispose()


@pytest.mark.unit
def test_retained_snapshot_outlives_the_run_and_still_hashes(tmp_path) -> None:
    """留存模式下快照要能在运行结束后重新打开并复算摘要。

    这是整条链路成立的前提：运行当时算出的摘要若无法在事后复算，manifest
    就只是一串无从核对的字符串。
    """
    import pandas as pd

    source = _seed_replay_source(tmp_path)
    date_from, date_to = _replay_window()
    snapshot_dir = tmp_path / "snapshots" / "run-a"
    try:
        with isolated_replay_database(
            source_db=source,
            universe=pd.DataFrame([{"code": "000001"}]),
            date_from=date_from,
            date_to=date_to,
            market="cn",
            market_guard_index="sh000001",
            snapshot_dir=snapshot_dir,
        ) as temporary:
            assert temporary.snapshot_retained is True
            snapshot_path = Path(temporary.snapshot_path)
            during_run = build_manifest_payload(
                backtest_run_id="run-a",
                snapshot_db=temporary,
                date_from=date_from,
                date_to=date_to,
            )
        assert snapshot_path.exists()
        assert snapshot_path.parent == snapshot_dir

        reopened = object.__new__(DatabaseManager)
        DatabaseManager.__init__(
            reopened,
            f"sqlite:///{snapshot_path.as_posix()}",
        )
        try:
            after_run = build_manifest_payload(
                backtest_run_id="run-a",
                snapshot_db=reopened,
                date_from=date_from,
                date_to=date_to,
            )
        finally:
            reopened._engine.dispose()

        assert after_run["table_hashes"] == during_run["table_hashes"]
        assert during_run["row_counts"]["stock_daily"] > 0
        assert during_run["snapshot_path"] == str(snapshot_path)
    finally:
        source._engine.dispose()


def _make_snapshot_dir(root: Path, name: str, mtime: float) -> Path:
    import os

    directory = root / name
    directory.mkdir(parents=True)
    (directory / "validation.db").write_bytes(b"x")
    os.utime(directory, (mtime, mtime))
    return directory


@pytest.mark.unit
def test_prune_retained_snapshots_keeps_the_most_recent_n(tmp_path) -> None:
    root = tmp_path / "snapshots"
    root.mkdir()
    for index in range(5):
        _make_snapshot_dir(root, f"run-{index}", 1_700_000_000 + index * 60)

    removed = prune_retained_snapshots(root, keep=2)

    assert sorted(removed) == ["run-0", "run-1", "run-2"]
    assert sorted(path.name for path in root.iterdir()) == ["run-3", "run-4"]


@pytest.mark.unit
def test_prune_retained_snapshots_never_drops_a_pinned_snapshot(tmp_path) -> None:
    """按运行标记保留是 spec 给的第二条保留口径，不能被「最近 N 份」挤掉。"""
    root = tmp_path / "snapshots"
    root.mkdir()
    for index in range(4):
        directory = _make_snapshot_dir(
            root,
            f"run-{index}",
            1_700_000_000 + index * 60,
        )
        if index == 0:
            (directory / PINNED_MARKER_FILENAME).write_text("keep", encoding="utf-8")

    removed = prune_retained_snapshots(root, keep=1)

    assert sorted(removed) == ["run-1", "run-2"]
    assert sorted(path.name for path in root.iterdir()) == ["run-0", "run-3"]


@pytest.mark.unit
def test_snapshot_retention_default_is_explicit_and_configurable(tmp_path) -> None:
    """默认值必须是个能被调用方覆盖的具体数字，而不是「无限保留」。"""
    assert DEFAULT_SNAPSHOT_RETENTION_COUNT == 3

    root = tmp_path / "snapshots"
    root.mkdir()
    for index in range(5):
        _make_snapshot_dir(root, f"run-{index}", 1_700_000_000 + index * 60)

    removed = prune_retained_snapshots(root)

    assert sorted(removed) == ["run-0", "run-1"]
    assert prune_retained_snapshots(tmp_path / "never-created") == ()

    with pytest.raises(ValueError):
        prune_retained_snapshots(root, keep=-1)


_FIXED_INGEST_TIME = datetime(2024, 1, 3, 9, 30, 0)


def _seed_one_bar(
    manager: DatabaseManager,
    close: float,
    *,
    ingested_at: datetime = _FIXED_INGEST_TIME,
    anchor: date = date(2024, 1, 2),
) -> None:
    """写一根 bar。

    `created_at` / `updated_at` 显式给值：它们默认取 `datetime.now`，而重写
    摘要正是算在这两列上，留默认值会让两个本该相同的夹具永远对不上。
    """
    from src.storage import StockDaily

    with manager.get_session() as session:
        session.add(StockDaily(
            code="000001",
            date=date(2024, 1, 2),
            open=100.0,
            high=102.0,
            low=99.0,
            close=close,
            volume=1_000.0,
            amount=1_000_000.0,
            pct_chg=1.0,
            adj_anchor_date=anchor,
            created_at=ingested_at,
            updated_at=ingested_at,
        ))
        session.commit()


def _payload(manager: DatabaseManager, run_id: str, **overrides):
    payload = build_manifest_payload(
        backtest_run_id=run_id,
        snapshot_db=manager,
        date_from=date(2024, 1, 1),
        date_to=date(2024, 3, 1),
        snapshot_path=f"snapshots/{run_id}/validation.db",
        code_revision="abc1234",
        config_hash="cfg-1",
    )
    payload.update(overrides)
    return payload


@pytest.mark.unit
def test_identical_manifests_compare_equal(tmp_path) -> None:
    left = _manager(tmp_path, "left.db")
    right = _manager(tmp_path, "right.db")
    try:
        _seed_one_bar(left, 101.0)
        _seed_one_bar(right, 101.0)
        payload = _payload(left, "run-a")
        comparison = compare_manifests(
            payload,
            _payload(right, "run-a", snapshot_path=payload["snapshot_path"]),
        )

        assert comparison.verdict == VERDICT_IDENTICAL
        assert comparison.differences == ()
        assert comparison.is_comparable is True
    finally:
        left._engine.dispose()
        right._engine.dispose()


@pytest.mark.unit
def test_single_changed_cell_yields_not_comparable_naming_the_table(
    tmp_path,
) -> None:
    """一个单元格变化就必须判红，并指名是哪张表——这是 spec 验收 #33 的原话。"""
    left = _manager(tmp_path, "left.db")
    right = _manager(tmp_path, "right.db")
    try:
        _seed_one_bar(left, 101.0)
        _seed_one_bar(right, 101.01)

        comparison = compare_manifests(_payload(left, "run-a"), _payload(right, "run-b"))

        assert comparison.verdict == VERDICT_NOT_COMPARABLE
        assert comparison.is_comparable is False
        assert "table_hashes.stock_daily" in comparison.blocking_fields
        assert "table_hashes.instrument_master" not in comparison.blocking_fields
        changed = [
            difference
            for difference in comparison.differences
            if difference.field == "table_hashes.stock_daily"
        ]
        assert len(changed) == 1
        assert changed[0].blocking is True
        assert changed[0].left != changed[0].right
    finally:
        left._engine.dispose()
        right._engine.dispose()


@pytest.mark.unit
def test_pure_rewrite_is_comparable_and_reported_as_non_blocking(tmp_path) -> None:
    """纯重写（价格逐字节相同，只有审计时间戳变了）判定为可比，但要留痕。

    生产库 962 万行 stock_daily 的 `created_at` 只有 2092 个不同取值，其中
    9,621,460 行是同一次 staging 提升写下的。也就是说下一次提升会把全表的
    审计时间戳重写一遍，而价格一个字节都没动。若这也判红，每次例行维护后
    所有历史 manifest 一起失效，判定就会被当成噪音略过——那比没有判定更糟。

    信息不能丢：重写本身仍然出现在差异清单里，只是不阻断。
    """
    left = _manager(tmp_path, "left.db")
    right = _manager(tmp_path, "right.db")
    try:
        _seed_one_bar(left, 101.0)
        _seed_one_bar(right, 101.0, ingested_at=datetime(2026, 8, 12, 3, 0, 0))

        comparison = compare_manifests(
            _payload(left, "run-a"),
            _payload(right, "run-b"),
        )

        assert comparison.verdict == VERDICT_COMPARABLE
        assert comparison.is_comparable is True
        assert comparison.blocking_fields == ()
        rewrites = [
            difference
            for difference in comparison.differences
            if difference.field == "row_rewrite_hashes.stock_daily"
        ]
        assert len(rewrites) == 1
        assert rewrites[0].blocking is False
        assert rewrites[0].left != rewrites[0].right
        # 内容摘要必须纹丝不动，否则拆分没有生效。
        assert "table_hashes.stock_daily" not in {
            difference.field for difference in comparison.differences
        }
    finally:
        left._engine.dispose()
        right._engine.dispose()


@pytest.mark.unit
def test_content_digest_is_independent_of_audit_driven_row_order(
    tmp_path,
) -> None:
    """审计时间戳不得影响内容摘要的行序。

    排序键若让审计列参与在前（把审计列排在前面，或直接沿用表的列序而该表
    恰好把 `created_at` 声明在前），这两份内容完全相同的快照就会因为时间戳
    颠倒而吐出不同的行序，内容摘要随之不同——拆分看起来做了，实际仍然逢重写
    必红。

    这里刻意用一张把 `created_at` 声明在首列的表：现有五张表的审计列都在
    末尾，沿用列序恰好不出错，这个隐患要等到某张表的列序变了才会暴露。
    """
    early = "2020-01-01 00:00:00"
    late = "2026-08-12 03:00:00"

    def build(name: str, cheap_at: str, dear_at: str):
        manager = _manager(tmp_path, name)
        with manager._engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE audit_first "
                "(created_at DATETIME, code TEXT, close REAL)"
            )
            for stamp, code, close in (
                (cheap_at, "000001", 100.0),
                (dear_at, "000002", 200.0),
            ):
                conn.execute(
                    text(
                        "INSERT INTO audit_first VALUES "
                        "(:created_at, :code, :close)"
                    ),
                    {"created_at": stamp, "code": code, "close": close},
                )
        return manager

    left = build("order-left.db", late, early)
    right = build("order-right.db", early, late)
    try:
        with left.get_session() as session:
            left_digests = hash_snapshot_table(session, "audit_first")
        with right.get_session() as session:
            right_digests = hash_snapshot_table(session, "audit_first")

        assert left_digests.content == right_digests.content
        # 时间戳确实不同，所以重写摘要必须报出差异，否则这个用例是空转。
        assert left_digests.row_rewrite != right_digests.row_rewrite
    finally:
        left._engine.dispose()
        right._engine.dispose()


@pytest.mark.unit
def test_value_change_is_still_blocking_even_with_identical_timestamps(
    tmp_path,
) -> None:
    """拆分不得削弱检测：值变了就必须判红，审计时间戳一样也不例外。"""
    left = _manager(tmp_path, "left.db")
    right = _manager(tmp_path, "right.db")
    try:
        _seed_one_bar(left, 101.0)
        _seed_one_bar(right, 101.01)

        comparison = compare_manifests(
            _payload(left, "run-a"),
            _payload(right, "run-b"),
        )

        assert comparison.verdict == VERDICT_NOT_COMPARABLE
        assert "table_hashes.stock_daily" in comparison.blocking_fields
        # 审计时间戳没变，重写摘要就不该报差异。
        assert "row_rewrite_hashes.stock_daily" not in {
            difference.field for difference in comparison.differences
        }
        # 行数没变，摘要是唯一的发现路径。
        assert "row_counts.stock_daily" not in comparison.blocking_fields
    finally:
        left._engine.dispose()
        right._engine.dispose()


@pytest.mark.unit
def test_date_like_business_column_change_is_still_blocking(tmp_path) -> None:
    """`adj_anchor_date` 是复权锚点，是业务数据，不是审计时间戳。

    它长得像时间列，正是排除规则一旦放宽成名字模式最先被误吃的那个。
    """
    left = _manager(tmp_path, "left.db")
    right = _manager(tmp_path, "right.db")
    try:
        _seed_one_bar(left, 101.0, anchor=date(2024, 1, 2))
        _seed_one_bar(right, 101.0, anchor=date(2024, 5, 6))

        comparison = compare_manifests(
            _payload(left, "run-a"),
            _payload(right, "run-b"),
        )

        assert comparison.verdict == VERDICT_NOT_COMPARABLE
        assert "table_hashes.stock_daily" in comparison.blocking_fields
    finally:
        left._engine.dispose()
        right._engine.dispose()


@pytest.mark.unit
def test_rewrite_marker_survives_persistence(tmp_path) -> None:
    """非阻断的重写信号必须落库，否则读回来的 manifest 就把它丢了。"""
    left = _manager(tmp_path, "left.db")
    right = _manager(tmp_path, "right.db")
    store = _manager(tmp_path, "store.db")
    try:
        _seed_one_bar(left, 101.0)
        _seed_one_bar(right, 101.0, ingested_at=datetime(2026, 8, 12, 3, 0, 0))
        write_run_data_manifest(store, _payload(left, "run-a"))
        write_run_data_manifest(store, _payload(right, "run-b"))

        comparison = compare_manifests(
            load_run_data_manifest(store, "run-a"),
            load_run_data_manifest(store, "run-b"),
        )

        assert comparison.verdict == VERDICT_COMPARABLE
        assert "row_rewrite_hashes.stock_daily" in {
            difference.field for difference in comparison.differences
        }
    finally:
        left._engine.dispose()
        right._engine.dispose()
        store._engine.dispose()


@pytest.mark.unit
def test_snapshot_path_and_code_revision_never_block_comparability(
    tmp_path,
) -> None:
    """两次运行的快照路径必然不同；code_revision 按 spec 5.3 只作记录。

    任一项参与判定，都会把本可对比的两次运行判红。
    """
    manager = _manager(tmp_path)
    try:
        _seed_one_bar(manager, 101.0)
        left = _payload(manager, "run-a")
        right = _payload(manager, "run-b", code_revision="def5678")

        comparison = compare_manifests(left, right)

        assert comparison.verdict == VERDICT_COMPARABLE
        assert comparison.is_comparable is True
        assert comparison.blocking_fields == ()
        assert {difference.field for difference in comparison.differences} == {
            "snapshot_path",
            "code_revision",
        }
    finally:
        manager._engine.dispose()


@pytest.mark.unit
def test_absent_upstream_table_is_not_comparable_with_present_but_empty(
    tmp_path,
) -> None:
    """缺表 vs 空表必须判红。这两种处境读到的世界不同，结论不能混着看。"""
    absent = _manager(tmp_path, "absent.db")
    present = _manager(tmp_path, "present.db")
    try:
        for manager in (absent, present):
            _seed_one_bar(manager, 101.0)
        with present._engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE corporate_actions (code TEXT, version TEXT)"
            )

        comparison = compare_manifests(
            _payload(absent, "run-a"),
            _payload(present, "run-b"),
        )

        assert comparison.verdict == VERDICT_NOT_COMPARABLE
        assert "corporate_actions_version" in comparison.blocking_fields
        assert "table_hashes.corporate_actions" in comparison.blocking_fields
        assert "row_counts.corporate_actions" in comparison.blocking_fields
    finally:
        absent._engine.dispose()
        present._engine.dispose()


@pytest.mark.unit
def test_config_and_window_differences_block_comparability(tmp_path) -> None:
    manager = _manager(tmp_path)
    try:
        _seed_one_bar(manager, 101.0)
        left = _payload(manager, "run-a")

        config_changed = compare_manifests(
            left,
            _payload(manager, "run-b", config_hash="cfg-2"),
        )
        assert config_changed.verdict == VERDICT_NOT_COMPARABLE
        assert config_changed.blocking_fields == ("config_hash",)

        window_changed = compare_manifests(
            left,
            _payload(
                manager,
                "run-b",
                date_range={"from": "2024-01-01", "to": "2024-04-01"},
            ),
        )
        assert window_changed.verdict == VERDICT_NOT_COMPARABLE
        assert window_changed.blocking_fields == ("date_range.to",)
    finally:
        manager._engine.dispose()


@pytest.mark.unit
def test_comparison_reads_persisted_manifests(tmp_path) -> None:
    """判定必须能吃库里读回来的 manifest，而不只是刚算出来的内存载荷。"""
    manager = _manager(tmp_path)
    try:
        _seed_one_bar(manager, 101.0)
        write_run_data_manifest(manager, _payload(manager, "run-a"))
        write_run_data_manifest(
            manager,
            _payload(manager, "run-b", config_hash="cfg-2"),
        )

        comparison = compare_manifests(
            load_run_data_manifest(manager, "run-a"),
            load_run_data_manifest(manager, "run-b"),
        )

        assert comparison.verdict == VERDICT_NOT_COMPARABLE
        assert comparison.blocking_fields == ("config_hash",)
    finally:
        manager._engine.dispose()
