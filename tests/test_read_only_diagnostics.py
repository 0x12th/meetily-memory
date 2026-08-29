from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from meetily_memory import diagnostics
from meetily_memory.cli.app import app
from meetily_memory.db.migrations import (
    CURRENT_SCHEMA_VERSION,
    LATEST_IN_PLACE_SCHEMA_VERSION,
    MIGRATIONS,
)
from meetily_memory.db.repository import IndexRepository
from meetily_memory.tagging import TagRepository
from meetily_memory.user_state import CURRENT_USER_STATE_SCHEMA_VERSION

if TYPE_CHECKING:
    from pathlib import Path


def tree_snapshot(root: Path) -> dict[str, tuple[str, int]]:
    snapshot: dict[str, tuple[str, int]] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mtime_ns,
            )
    return snapshot


def create_db_status_database(
    directory: Path,
    *,
    migrated: int,
    orphaned: int,
    orphaned_tag_assignments: int,
) -> Path:
    index_path = directory / "index.sqlite"
    repo = IndexRepository(index_path)
    source_uuid = repo.user_state.get_or_create_source(
        "meetily_sqlite",
        str(directory / "source.sqlite"),
        now="2026-08-28T10:00:00Z",
    )
    TagRepository(repo.state_path).assign(
        source_uuid,
        tuple(f"missing-{index}" for index in range(orphaned_tag_assignments)),
        ("diagnostic",),
        now="2026-08-28T10:01:00Z",
    )
    with sqlite3.connect(repo.state_path) as conn:
        conn.execute(
            """
            INSERT INTO migration_reports (index_path, migrated, orphaned, created_at)
            VALUES (?, ?, ?, '2026-08-28T10:02:00Z')
            """,
            (str(index_path), migrated, orphaned),
        )
        conn.commit()
    return index_path


def create_current_diagnostic_state(
    index_path: Path,
    settings_path: Path,
    source_path: Path,
) -> int:
    repo = IndexRepository(index_path)
    source_uuid = repo.user_state.get_or_create_source(
        "meetily_sqlite",
        str(source_path),
        now="2026-08-28T10:00:00Z",
    )
    settings_path.write_text(json.dumps({"source_uuid": source_uuid}) + "\n", encoding="utf-8")
    with sqlite3.connect(index_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO scan_runs (started_at, status, phase)
            VALUES ('2026-08-28T10:00:00Z', 'running', 'source_scan')
            """
        )
        conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


@pytest.mark.parametrize("command", [("doctor", "--json"), ("status", "--json")])
def test_read_only_commands_leave_an_empty_data_directory_absent(
    tmp_path: Path,
    command: tuple[str, str],
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"

    result = CliRunner().invoke(
        app,
        ["--index", str(index_path), *command],
        env={"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["index_database"]["status"] == "missing"
    assert payload["state_database"]["status"] == "missing"
    assert payload["meetings"] == 0
    assert payload["last_completed_run"] is None
    assert payload["last_failed_run"] is None
    assert payload["last_running_run"] is None
    assert not data_dir.exists()


def test_doctor_and_status_resolve_legacy_source_setting_without_migrating_it(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    settings_path.write_text(json.dumps({"source_path": str(meetily_db)}) + "\n", encoding="utf-8")
    env = {"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    before = tree_snapshot(tmp_path)

    for command in (("doctor", "--json"), ("status", "--json")):
        result = CliRunner().invoke(app, ["--index", str(index_path), *command], env=env)

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["source_path"] == str(meetily_db)
        assert payload["index_database"]["status"] == "missing"
        assert payload["state_database"]["status"] == "missing"
        assert tree_snapshot(tmp_path) == before

    assert json.loads(settings_path.read_text()) == {"source_path": str(meetily_db)}
    assert not index_path.exists()
    assert not (data_dir / "state.sqlite").exists()


def test_doctor_and_status_preserve_files_and_observe_running_scan(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    running_run_id = create_current_diagnostic_state(index_path, settings_path, meetily_db)
    env = {"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    before = tree_snapshot(tmp_path)

    for command in (("doctor", "--json"), ("status", "--json")):
        result = CliRunner().invoke(app, ["--index", str(index_path), *command], env=env)

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["index_database"]["status"] == "current"
        assert payload["state_database"]["status"] == "current"
        assert payload["source_path"] == str(meetily_db)
        assert payload["last_failed_run"] is None
        assert payload["last_running_run"]["id"] == running_run_id
        assert payload["last_running_run"]["status"] == "running"
        assert tree_snapshot(tmp_path) == before

    with sqlite3.connect(index_path) as conn:
        running_row = conn.execute(
            "SELECT status, finished_at, error_message FROM scan_runs WHERE id = ?",
            (running_run_id,),
        ).fetchone()
    assert running_row == ("running", None, None)
    assert not (data_dir / "refresh.lock").exists()


@pytest.mark.parametrize("legacy_version", [1, LATEST_IN_PLACE_SCHEMA_VERSION])
def test_db_status_reports_legacy_schema_without_upgrading(
    tmp_path: Path,
    legacy_version: int,
) -> None:
    data_dir = tmp_path / f"db-status-v{legacy_version}"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    with sqlite3.connect(index_path) as conn:
        for version in range(1, legacy_version + 1):
            MIGRATIONS[version](conn)
        conn.execute(f"PRAGMA user_version = {legacy_version}")
        conn.commit()
    bytes_before = index_path.read_bytes()

    result = CliRunner().invoke(
        app,
        ["--index", str(index_path), "db", "status", "--json"],
        env={"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == legacy_version
    assert payload["schema_status"] == "legacy"
    assert payload["migration_status"] == "rebuild_required"
    assert payload["orphaned_tag_assignments"] is None
    assert index_path.read_bytes() == bytes_before
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == legacy_version
    assert not (data_dir / "state.sqlite").exists()


def test_db_status_does_not_count_all_assignments_as_orphaned_for_legacy_index(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "legacy-orphan-count"
    index_path = create_db_status_database(
        data_dir,
        migrated=1,
        orphaned=2,
        orphaned_tag_assignments=3,
    )
    with sqlite3.connect(index_path) as conn:
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION - 1}")
        conn.commit()

    result = CliRunner().invoke(
        app,
        ["--index", str(index_path), "db", "status", "--json"],
        env={"HOME": str(tmp_path / "home")},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_status"] == "legacy"
    assert payload["orphaned_tag_assignments"] is None


def test_db_status_inspects_current_schema_without_touching_files(tmp_path: Path) -> None:
    data_dir = tmp_path / "db-status-current"
    index_path = data_dir / "index.sqlite"
    IndexRepository(index_path)
    before = tree_snapshot(data_dir)

    result = CliRunner().invoke(
        app,
        ["--index", str(index_path), "db", "status", "--json"],
        env={"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == CURRENT_SCHEMA_VERSION
    assert payload["schema_status"] == "current"
    assert payload["migration_status"] == "current"
    assert tree_snapshot(data_dir) == before


@pytest.mark.parametrize(
    ("state_case", "expected_status"),
    [
        ("missing", "missing"),
        ("legacy", "legacy"),
        ("incompatible", "incompatible"),
    ],
)
def test_db_status_reports_orphan_count_unavailable_for_noncurrent_state(
    tmp_path: Path,
    state_case: str,
    expected_status: str,
) -> None:
    data_dir = tmp_path / f"noncurrent-state-{state_case}"
    index_path = create_db_status_database(
        data_dir,
        migrated=1,
        orphaned=2,
        orphaned_tag_assignments=3,
    )
    state_path = index_path.with_name("state.sqlite")
    if state_case == "missing":
        state_path.unlink()
    else:
        state_version = (
            CURRENT_USER_STATE_SCHEMA_VERSION - 1
            if state_case == "legacy"
            else CURRENT_USER_STATE_SCHEMA_VERSION + 1
        )
        with sqlite3.connect(state_path) as conn:
            conn.execute(f"PRAGMA user_version = {state_version}")
            conn.commit()
    before = tree_snapshot(data_dir)

    result = CliRunner().invoke(
        app,
        ["--index", str(index_path), "db", "status", "--json"],
        env={"HOME": str(tmp_path / "home")},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_status"] == "current"
    assert payload["state_schema_status"] == expected_status
    assert payload["orphaned_tag_assignments"] is None
    assert f"user-state database status is {expected_status}" in payload["details_error"]
    assert tree_snapshot(data_dir) == before


def test_db_status_missing_index_does_not_count_current_state_assignments_as_orphaned(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "missing-index-current-state"
    index_path = create_db_status_database(
        data_dir,
        migrated=4,
        orphaned=5,
        orphaned_tag_assignments=3,
    )
    index_path.unlink()
    before = tree_snapshot(data_dir)

    result = CliRunner().invoke(
        app,
        ["--index", str(index_path), "db", "status", "--json"],
        env={"HOME": str(tmp_path / "home")},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_status"] == "missing"
    assert payload["state_schema_status"] == "current"
    assert payload["user_state_migration"] == {"migrated": 4, "orphaned": 5}
    assert payload["orphaned_tag_assignments"] is None
    assert "index database status is missing" in payload["details_error"]
    assert tree_snapshot(data_dir) == before


def test_db_status_pins_index_and_state_before_symlink_retarget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    create_db_status_database(
        first_dir,
        migrated=11,
        orphaned=12,
        orphaned_tag_assignments=1,
    )
    second_index = create_db_status_database(
        second_dir,
        migrated=21,
        orphaned=22,
        orphaned_tag_assignments=2,
    )
    with sqlite3.connect(second_index) as conn:
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION - 1}")
        conn.commit()
    with sqlite3.connect(second_index.with_name("state.sqlite")) as conn:
        conn.execute(f"PRAGMA user_version = {CURRENT_USER_STATE_SCHEMA_VERSION - 1}")
        conn.commit()

    logical_dir = tmp_path / "current"
    logical_dir.symlink_to(first_dir, target_is_directory=True)
    checkpoints: list[str] = []

    def retarget_after_initial_inspection(name: str) -> None:
        checkpoints.append(name)
        if name == "db_status:after_initial_inspection":
            logical_dir.unlink()
            logical_dir.symlink_to(second_dir, target_is_directory=True)

    monkeypatch.setattr(diagnostics, "_diagnostic_checkpoint", retarget_after_initial_inspection)

    result = CliRunner().invoke(
        app,
        ["--index", str(logical_dir / "index.sqlite"), "db", "status", "--json"],
        env={"HOME": str(tmp_path / "home")},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert checkpoints == ["db_status:after_initial_inspection"]
    assert payload["schema_status"] == "current"
    assert payload["state_schema_status"] == "current"
    assert payload["user_state_migration"] == {"migrated": 11, "orphaned": 12}
    assert payload["orphaned_tag_assignments"] == 1
    assert payload["details_error"] is None


def test_db_status_resolves_shared_parent_once_before_pair_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_dir = tmp_path / "pair-first"
    second_dir = tmp_path / "pair-second"
    create_db_status_database(
        first_dir,
        migrated=31,
        orphaned=32,
        orphaned_tag_assignments=1,
    )
    second_index = create_db_status_database(
        second_dir,
        migrated=41,
        orphaned=42,
        orphaned_tag_assignments=2,
    )
    with sqlite3.connect(second_index) as conn:
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION - 1}")
        conn.commit()
    with sqlite3.connect(second_index.with_name("state.sqlite")) as conn:
        conn.execute(f"PRAGMA user_version = {CURRENT_USER_STATE_SCHEMA_VERSION - 1}")
        conn.commit()

    logical_dir = tmp_path / "pair-current"
    logical_dir.symlink_to(first_dir, target_is_directory=True)
    checkpoints: list[str] = []

    def retarget_between_pair_pin(name: str) -> None:
        checkpoints.append(name)
        if name == "after_shared_parent":
            logical_dir.unlink()
            logical_dir.symlink_to(second_dir, target_is_directory=True)

    monkeypatch.setattr(diagnostics, "_database_pair_checkpoint", retarget_between_pair_pin)

    result = CliRunner().invoke(
        app,
        ["--index", str(logical_dir / "index.sqlite"), "db", "status", "--json"],
        env={"HOME": str(tmp_path / "home")},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert checkpoints == ["after_shared_parent", "after_first_child"]
    assert payload["schema_status"] == "current"
    assert payload["state_schema_status"] == "current"
    assert payload["user_state_migration"] == {"migrated": 31, "orphaned": 32}
    assert payload["orphaned_tag_assignments"] == 1
    assert payload["details_error"] is None


def test_db_status_retries_file_symlink_pair_retarget_without_mixing_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_dir = tmp_path / "child-first"
    second_dir = tmp_path / "child-second"
    first_index = create_db_status_database(
        first_dir,
        migrated=51,
        orphaned=52,
        orphaned_tag_assignments=1,
    )
    second_index = create_db_status_database(
        second_dir,
        migrated=61,
        orphaned=62,
        orphaned_tag_assignments=2,
    )
    with sqlite3.connect(second_index) as conn:
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION - 1}")
        conn.commit()
    second_state = second_index.with_name("state.sqlite")
    with sqlite3.connect(second_state) as conn:
        conn.execute(f"PRAGMA user_version = {CURRENT_USER_STATE_SCHEMA_VERSION - 1}")
        conn.commit()

    logical_dir = tmp_path / "child-current"
    logical_dir.mkdir()
    logical_index = logical_dir / "index.sqlite"
    logical_state = logical_dir / "state.sqlite"
    logical_index.symlink_to(first_index)
    logical_state.symlink_to(first_index.with_name("state.sqlite"))
    retargeted = False
    checkpoints: list[str] = []

    def retarget_after_first_child(name: str) -> None:
        nonlocal retargeted
        checkpoints.append(name)
        if name != "after_first_child" or retargeted:
            return
        retargeted = True
        logical_index.unlink()
        logical_state.unlink()
        logical_index.symlink_to(second_index)
        logical_state.symlink_to(second_state)

    monkeypatch.setattr(diagnostics, "_database_pair_checkpoint", retarget_after_first_child)

    result = CliRunner().invoke(
        app,
        ["--index", str(logical_index), "db", "status", "--json"],
        env={"HOME": str(tmp_path / "home")},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert checkpoints == ["after_shared_parent", "after_first_child", "after_first_child"]
    assert payload["schema_status"] == "legacy"
    assert payload["state_schema_status"] == "legacy"
    assert payload["user_state_migration"] == {"migrated": 61, "orphaned": 62}
    assert payload["orphaned_tag_assignments"] is None
    assert "user-state database status is legacy" in payload["details_error"]


def test_db_status_rejects_continuously_retargeted_file_symlink_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_dir = tmp_path / "unstable-child-first"
    second_dir = tmp_path / "unstable-child-second"
    first_index = create_db_status_database(
        first_dir,
        migrated=71,
        orphaned=72,
        orphaned_tag_assignments=1,
    )
    second_index = create_db_status_database(
        second_dir,
        migrated=81,
        orphaned=82,
        orphaned_tag_assignments=2,
    )
    logical_dir = tmp_path / "unstable-child-current"
    logical_dir.mkdir()
    logical_index = logical_dir / "index.sqlite"
    logical_state = logical_dir / "state.sqlite"
    logical_index.symlink_to(first_index)
    logical_state.symlink_to(first_index.with_name("state.sqlite"))
    use_second = False

    def keep_retargeting_after_first_child(name: str) -> None:
        nonlocal use_second
        if name != "after_first_child":
            return
        use_second = not use_second
        target_index = second_index if use_second else first_index
        logical_index.unlink()
        logical_state.unlink()
        logical_index.symlink_to(target_index)
        logical_state.symlink_to(target_index.with_name("state.sqlite"))

    monkeypatch.setattr(
        diagnostics,
        "_database_pair_checkpoint",
        keep_retargeting_after_first_child,
    )

    result = CliRunner().invoke(
        app,
        ["--index", str(logical_index), "db", "status", "--json"],
        env={"HOME": str(tmp_path / "home")},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_status"] == "incompatible"
    assert payload["state_schema_status"] == "incompatible"
    assert "Database child path pair changed" in payload["index_database"]["error"]
    assert "Database child path pair changed" in payload["state_database"]["error"]
    assert "Database child path pair changed" in payload["details_error"]
    assert payload["orphaned_tag_assignments"] is None


def test_db_status_reports_wal_created_after_initial_inspection_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = create_db_status_database(
        tmp_path / "data",
        migrated=1,
        orphaned=2,
        orphaned_tag_assignments=1,
    )
    writers: list[sqlite3.Connection] = []

    def create_wal_after_initial_inspection(name: str) -> None:
        if name != "db_status:after_initial_inspection":
            return
        writer = sqlite3.connect(index_path)
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            """
            INSERT INTO plugin_state (plugin_name, key, value_json, updated_at)
            VALUES ('diagnostic-test', 'late-wal', '{}', '2026-08-28T10:03:00Z')
            """
        )
        writer.commit()
        assert index_path.with_name(index_path.name + "-wal").stat().st_size > 0
        writers.append(writer)

    monkeypatch.setattr(diagnostics, "_diagnostic_checkpoint", create_wal_after_initial_inspection)

    try:
        result = CliRunner().invoke(
            app,
            ["--index", str(index_path), "db", "status", "--json"],
            env={"HOME": str(tmp_path / "home")},
        )
    finally:
        for writer in writers:
            writer.close()

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_status"] == "incompatible"
    assert "active WAL sidecar" in payload["index_database"]["error"]
    assert "active WAL sidecar" in payload["details_error"]
    assert payload["user_state_migration"] is None
    assert payload["orphaned_tag_assignments"] is None


def test_db_status_reports_both_late_wals_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = create_db_status_database(
        tmp_path / "both-late-wals",
        migrated=1,
        orphaned=2,
        orphaned_tag_assignments=1,
    )
    state_path = index_path.with_name("state.sqlite")
    writers: list[sqlite3.Connection] = []

    def create_both_wals_after_initial_inspection(name: str) -> None:
        if name != "db_status:after_initial_inspection":
            return
        index_writer = sqlite3.connect(index_path)
        assert index_writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        index_writer.execute("PRAGMA wal_autocheckpoint=0")
        index_writer.execute(
            """
            INSERT INTO plugin_state (plugin_name, key, value_json, updated_at)
            VALUES ('diagnostic-test', 'both-late-wals', '{}', '2026-08-28T10:03:00Z')
            """
        )
        index_writer.commit()
        state_writer = sqlite3.connect(state_path)
        assert state_writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        state_writer.execute("PRAGMA wal_autocheckpoint=0")
        state_writer.execute(
            """
            INSERT INTO tags (normalized_name, display_name, created_at)
            VALUES ('both-late-wals', 'both-late-wals', '2026-08-28T10:03:00Z')
            """
        )
        state_writer.commit()
        assert index_path.with_name(index_path.name + "-wal").stat().st_size > 0
        assert state_path.with_name(state_path.name + "-wal").stat().st_size > 0
        writers.extend((index_writer, state_writer))

    monkeypatch.setattr(
        diagnostics,
        "_diagnostic_checkpoint",
        create_both_wals_after_initial_inspection,
    )

    try:
        result = CliRunner().invoke(
            app,
            ["--index", str(index_path), "db", "status", "--json"],
            env={"HOME": str(tmp_path / "home")},
        )
    finally:
        for writer in writers:
            writer.close()

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_status"] == "incompatible"
    assert payload["state_schema_status"] == "incompatible"
    assert "active WAL sidecar" in payload["index_database"]["error"]
    assert "active WAL sidecar" in payload["state_database"]["error"]
    assert payload["details_error"].count("active WAL sidecar") == 2
    assert payload["user_state_migration"] is None
    assert payload["orphaned_tag_assignments"] is None


def test_doctor_reports_valid_legacy_index_without_upgrading(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    legacy_version = LATEST_IN_PLACE_SCHEMA_VERSION
    with sqlite3.connect(index_path) as conn:
        for version in range(1, legacy_version + 1):
            MIGRATIONS[version](conn)
        conn.execute(f"PRAGMA user_version = {legacy_version}")
        conn.commit()
    before = tree_snapshot(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--index", str(index_path), "doctor", "--json"],
        env={"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["index_database"]["status"] == "legacy"
    assert payload["index_database"]["schema_version"] == legacy_version
    assert tree_snapshot(tmp_path) == before


def test_doctor_reports_future_or_malformed_legacy_index_as_incompatible(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env = {"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)}

    for name, version in (
        ("future.sqlite", CURRENT_SCHEMA_VERSION + 1),
        ("malformed-legacy.sqlite", CURRENT_SCHEMA_VERSION - 1),
        ("unknown-legacy.sqlite", 0),
    ):
        index_path = data_dir / name
        with sqlite3.connect(index_path) as conn:
            conn.execute("CREATE TABLE sentinel (value TEXT)")
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
        before = tree_snapshot(tmp_path)

        result = CliRunner().invoke(
            app,
            ["--index", str(index_path), "doctor", "--json"],
            env=env,
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["index_database"]["status"] == "incompatible"
        assert payload["index_database"]["schema_version"] == version
        assert payload["index_database"]["current_schema_version"] == CURRENT_SCHEMA_VERSION
        assert tree_snapshot(tmp_path) == before
        assert not index_path.with_name("state.sqlite").exists()
        assert not index_path.with_name("refresh.lock").exists()


def test_doctor_reports_corrupt_index_without_replacing_it(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    index_path.write_bytes(b"not a sqlite database")
    before = tree_snapshot(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--index", str(index_path), "doctor", "--json"],
        env={"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["index_database"]["status"] == "incompatible"
    assert payload["index_database"]["error"]
    assert tree_snapshot(tmp_path) == before
    assert not (data_dir / "state.sqlite").exists()


def test_doctor_reports_malformed_current_index_and_state_tables(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    repo = IndexRepository(index_path)
    with sqlite3.connect(index_path) as conn:
        conn.execute("DROP TABLE plugin_state")
        conn.execute("CREATE TABLE plugin_state (plugin_name TEXT, key TEXT)")
        conn.commit()
    with sqlite3.connect(repo.state_path) as conn:
        conn.execute("DROP TABLE tags")
        conn.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY)")
        conn.commit()
    before = tree_snapshot(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--index", str(index_path), "doctor", "--json"],
        env={"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["index_database"]["status"] == "incompatible"
    assert "plugin_state missing columns" in payload["index_database"]["error"]
    assert payload["state_database"]["status"] == "incompatible"
    assert "tags missing columns" in payload["state_database"]["error"]
    assert tree_snapshot(tmp_path) == before


def test_doctor_rejects_malformed_columns_in_supported_legacy_index(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    legacy_version = LATEST_IN_PLACE_SCHEMA_VERSION
    with sqlite3.connect(index_path) as conn:
        for version in range(1, legacy_version + 1):
            MIGRATIONS[version](conn)
        conn.execute(f"PRAGMA user_version = {legacy_version}")
        conn.execute("ALTER TABLE plugin_state DROP COLUMN value_json")
        conn.commit()
    before = tree_snapshot(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--index", str(index_path), "doctor", "--json"],
        env={"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["index_database"]["status"] == "incompatible"
    assert "plugin_state missing columns: value_json" in payload["index_database"]["error"]
    assert tree_snapshot(tmp_path) == before


def test_doctor_rejects_missing_operational_current_index_column(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    IndexRepository(index_path)
    with sqlite3.connect(index_path) as conn:
        conn.execute("ALTER TABLE chunks DROP COLUMN speaker")
        conn.commit()
    before = tree_snapshot(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--index", str(index_path), "doctor", "--json"],
        env={"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["index_database"]["status"] == "incompatible"
    assert "chunks missing columns: speaker" in payload["index_database"]["error"]
    assert tree_snapshot(tmp_path) == before


def test_doctor_accepts_clean_persist_journal_without_touching_it(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    IndexRepository(index_path)
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA journal_mode=PERSIST").fetchone()[0] == "persist"
        conn.execute(
            "INSERT INTO plugin_state (plugin_name, key, value_json, updated_at) "
            "VALUES ('test', 'clean', '{}', '2026-08-28T10:00:00Z')"
        )
        conn.commit()
    journal_path = index_path.with_name(index_path.name + "-journal")
    assert journal_path.read_bytes()[:8] == bytes(8)
    before = tree_snapshot(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--index", str(index_path), "doctor", "--json"],
        env={"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["index_database"]["status"] == "current"
    assert tree_snapshot(tmp_path) == before


def test_doctor_rejects_hot_rollback_journal_without_touching_it(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    IndexRepository(index_path)
    with sqlite3.connect(index_path) as writer:
        assert writer.execute("PRAGMA journal_mode=PERSIST").fetchone()[0] == "persist"
        writer.execute("PRAGMA cache_size=1")
        writer.executemany(
            "INSERT INTO plugin_state (plugin_name, key, value_json, updated_at) "
            "VALUES ('test', ?, ?, '2026-08-28T10:00:00Z')",
            [(str(index), "x" * 3000) for index in range(50)],
        )
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE plugin_state SET value_json = ?", ("y" * 3000,))
        journal_path = index_path.with_name(index_path.name + "-journal")
        assert journal_path.read_bytes()[:8] != bytes(8)
        before = tree_snapshot(tmp_path)

        result = CliRunner().invoke(
            app,
            ["--index", str(index_path), "doctor", "--json"],
            env={"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)},
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["index_database"]["status"] == "incompatible"
        assert "active rollback journal sidecar" in payload["index_database"]["error"]
        assert tree_snapshot(tmp_path) == before
        writer.rollback()


def test_doctor_rejects_active_index_wal_without_touching_sidecars(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    IndexRepository(index_path)
    with sqlite3.connect(index_path) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO scan_runs (started_at, status, phase) VALUES (?, 'running', ?)",
            ("2026-08-28T10:00:00Z", "source_scan"),
        )
        writer.commit()
        before = tree_snapshot(tmp_path)

        result = CliRunner().invoke(
            app,
            ["--index", str(index_path), "doctor", "--json"],
            env={"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)},
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["index_database"]["status"] == "incompatible"
        assert "active WAL sidecar" in payload["index_database"]["error"]
        assert tree_snapshot(tmp_path) == before


def test_doctor_resolves_symlink_before_active_wal_guard_and_immutable_open(
    tmp_path: Path,
) -> None:
    physical_path = tmp_path / "physical" / "index.sqlite"
    logical_dir = tmp_path / "logical"
    logical_dir.mkdir()
    logical_path = logical_dir / "index.sqlite"
    IndexRepository(physical_path)
    logical_path.symlink_to(physical_path)
    with sqlite3.connect(physical_path) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO scan_runs (started_at, status, phase) VALUES (?, 'running', ?)",
            ("2026-08-28T10:00:00Z", "source_scan"),
        )
        writer.commit()
        before = tree_snapshot(tmp_path)

        result = CliRunner().invoke(
            app,
            ["--index", str(logical_path), "doctor", "--json"],
            env={"HOME": str(tmp_path / "home")},
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["index_database"]["status"] == "incompatible"
        assert "active WAL sidecar" in payload["index_database"]["error"]
        assert tree_snapshot(tmp_path) == before


def test_doctor_rejects_active_source_wal_without_touching_sidecars(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    with sqlite3.connect(meetily_db) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE meetings SET title = title || ' changed' WHERE id = 'meeting-1'")
        writer.commit()
        before = tree_snapshot(tmp_path)

        result = CliRunner().invoke(
            app,
            [
                "--index",
                str(data_dir / "index.sqlite"),
                "doctor",
                "--source",
                str(meetily_db),
                "--json",
            ],
            env={"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)},
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["source_readable"] is False
        assert "active WAL sidecar" in payload["source_read_error"]
        assert tree_snapshot(tmp_path) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions are required for this check")
def test_doctor_and_status_work_with_read_only_database_files_and_directory(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    state_path = data_dir / "state.sqlite"
    settings_path = data_dir / "settings.json"
    create_current_diagnostic_state(index_path, settings_path, meetily_db)
    paths = (index_path, state_path, settings_path, meetily_db)
    for path in paths:
        path.chmod(0o444)
    data_dir.chmod(0o555)
    before = tree_snapshot(tmp_path)
    env = {"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)}

    try:
        doctor = CliRunner().invoke(
            app,
            ["--index", str(index_path), "doctor", "--json"],
            env=env,
        )
        status = CliRunner().invoke(
            app,
            ["--index", str(index_path), "status", "--json"],
            env=env,
        )
        db_status = CliRunner().invoke(
            app,
            ["--index", str(index_path), "db", "status", "--json"],
            env=env,
        )

        assert doctor.exit_code == 0
        assert status.exit_code == 0
        assert db_status.exit_code == 0
        assert json.loads(doctor.stdout)["source_schema_valid"] is True
        assert json.loads(status.stdout)["state_database"]["schema_version"] == (
            CURRENT_USER_STATE_SCHEMA_VERSION
        )
        assert json.loads(db_status.stdout)["schema_status"] == "current"
        assert tree_snapshot(tmp_path) == before
        assert not (data_dir / "refresh.lock").exists()
    finally:
        data_dir.chmod(0o755)
        for path in paths:
            path.chmod(0o644)


def test_refresh_recovers_running_scan_only_after_writer_starts(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    running_run_id = create_current_diagnostic_state(index_path, settings_path, meetily_db)
    env = {"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()

    status = runner.invoke(app, ["--index", str(index_path), "status", "--json"], env=env)
    with sqlite3.connect(index_path) as conn:
        observed_status = conn.execute(
            "SELECT status FROM scan_runs WHERE id = ?", (running_run_id,)
        ).fetchone()[0]

    assert status.exit_code == 0
    assert observed_status == "running"

    refresh = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env=env,
    )

    assert refresh.exit_code == 0
    with sqlite3.connect(index_path) as conn:
        recovered = conn.execute(
            "SELECT status, error_message FROM scan_runs WHERE id = ?",
            (running_run_id,),
        ).fetchone()
    assert recovered == ("failed", "Previous refresh ended before completion.")
