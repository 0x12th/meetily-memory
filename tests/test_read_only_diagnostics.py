from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from meetily_memory.cli.app import app
from meetily_memory.db.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS
from meetily_memory.db.repository import IndexRepository
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


def test_doctor_reports_valid_legacy_index_without_upgrading(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    legacy_version = CURRENT_SCHEMA_VERSION - 1
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
    legacy_version = CURRENT_SCHEMA_VERSION - 1
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

        assert doctor.exit_code == 0
        assert status.exit_code == 0
        assert json.loads(doctor.stdout)["source_schema_valid"] is True
        assert json.loads(status.stdout)["state_database"]["schema_version"] == (
            CURRENT_USER_STATE_SCHEMA_VERSION
        )
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
