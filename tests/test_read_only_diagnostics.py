from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from meetily_memory.cli.app import app
from meetily_memory.db.schema_family import INDEX_SCHEMA_USER_VERSION, STATE_SCHEMA_USER_VERSION
from meetily_memory.tagging import TagRepository
from tests.index_helpers import publish_fresh_index

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import Result


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


def invoke_json(index_path: Path, *command: str) -> tuple[Result, dict[str, Any]]:
    result = CliRunner().invoke(
        app,
        ["--index", str(index_path), *command, "--json"],
        env={"HOME": str(index_path.parent / "home")},
    )
    return result, json.loads(result.stdout)


@pytest.mark.parametrize("command", [("doctor",), ("status",), ("db", "status")])
def test_read_only_diagnostics_leave_empty_directory_absent(
    tmp_path: Path,
    command: tuple[str, ...],
) -> None:
    index_path = tmp_path / "data" / "index.sqlite"
    result, payload = invoke_json(index_path, *command)

    assert result.exit_code == 0
    assert payload["index_database"]["status"] == "missing"
    assert payload["state_database"]["status"] == "missing"
    assert not index_path.parent.exists()


def test_doctor_status_and_db_status_preserve_current_files(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "data" / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    before = tree_snapshot(tmp_path)

    for command in (("doctor",), ("status",), ("db", "status")):
        result, payload = invoke_json(index_path, *command)
        assert result.exit_code == 0
        assert payload["index_database"]["status"] == "current"
        assert payload["state_database"]["status"] == "current"
        assert payload["meetings"] == 2 if "meetings" in payload else True
        assert tree_snapshot(tmp_path) == before


def test_diagnostics_fail_closed_on_wrong_stored_language_type(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    with sqlite3.connect(index_path) as connection:
        connection.execute(
            "UPDATE meetings SET language = ?",
            (sqlite3.Binary(b"not-text"),),
        )
        connection.commit()

    result, payload = invoke_json(index_path, "doctor")

    assert result.exit_code == 0
    assert payload["index_database"]["status"] == "incompatible"
    assert "meetings.language must be TEXT, got BLOB" in payload["index_database"]["error"]


def test_db_status_counts_real_orphaned_tags_without_migration_reports(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    result = publish_fresh_index(index_path, meetily_db)
    TagRepository(index_path.with_name("state.sqlite")).assign(
        result.source.source_uuid,
        ("missing-meeting",),
        ("diagnostic",),
        now="2026-08-31T00:00:00Z",
    )
    before = tree_snapshot(tmp_path)

    command, payload = invoke_json(index_path, "db", "status")

    assert command.exit_code == 0
    assert payload["schema_version"] == INDEX_SCHEMA_USER_VERSION
    assert payload["state_schema_version"] == STATE_SCHEMA_USER_VERSION
    assert payload["orphaned_tag_assignments"] == 1
    assert "migration_status" not in payload
    assert "user_state_migration" not in payload
    assert tree_snapshot(tmp_path) == before


def test_unsupported_index_is_incompatible_and_only_offers_rebuild(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    with sqlite3.connect(index_path) as conn:
        conn.execute("CREATE TABLE old_projection (id INTEGER PRIMARY KEY)")
        conn.execute("PRAGMA user_version=7")
        conn.commit()
    before = tree_snapshot(tmp_path)

    result, payload = invoke_json(index_path, "doctor")

    assert result.exit_code == 0
    diagnostic = payload["index_database"]
    assert diagnostic["status"] == "incompatible"
    assert "Delete the disposable `index.sqlite`" in diagnostic["error"]
    assert "state_transfer" not in diagnostic["error"]
    assert "migration" not in diagnostic["error"].casefold()
    assert tree_snapshot(tmp_path) == before


def test_unsupported_state_warns_that_deletion_loses_tags_and_settings(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    state_path = index_path.with_name("state.sqlite")
    with sqlite3.connect(state_path) as conn:
        conn.execute("PRAGMA user_version=1")
        conn.commit()
    before = tree_snapshot(tmp_path)

    result, payload = invoke_json(index_path, "db", "status")

    assert result.exit_code == 0
    diagnostic = payload["state_database"]
    assert diagnostic["status"] == "incompatible"
    assert (
        "Deleting state permanently loses manual tags and application settings"
        in diagnostic["error"]
    )
    assert "state_transfer" not in diagnostic["error"]
    assert payload["orphaned_tag_assignments"] is None
    assert tree_snapshot(tmp_path) == before


def test_corrupt_index_is_reported_without_replacement(tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    index_path.write_bytes(b"not a sqlite database")
    before = tree_snapshot(tmp_path)

    result, payload = invoke_json(index_path, "doctor")

    assert result.exit_code == 0
    assert payload["index_database"]["status"] == "incompatible"
    assert tree_snapshot(tmp_path) == before


def test_active_wal_is_reported_without_touching_sidecars(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    with sqlite3.connect(index_path) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE index_meta SET meeting_count=meeting_count + 1")
        writer.commit()
        before = tree_snapshot(tmp_path)

        result, payload = invoke_json(index_path, "doctor")

        assert result.exit_code == 0
        assert payload["index_database"]["status"] == "incompatible"
        assert "active WAL sidecar" in payload["index_database"]["error"]
        assert tree_snapshot(tmp_path) == before


def test_diagnostics_follow_parent_and_child_symlinks_without_writes(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    physical = tmp_path / "physical"
    index_path = physical / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(physical, target_is_directory=True)
    child_dir = tmp_path / "children"
    child_dir.mkdir()
    child_index = child_dir / "index.sqlite"
    child_state = child_dir / "state.sqlite"
    child_index.symlink_to(index_path)
    child_state.symlink_to(index_path.with_name("state.sqlite"))

    for logical_index in (parent_link / "index.sqlite", child_index):
        before = tree_snapshot(tmp_path)
        result, payload = invoke_json(logical_index, "db", "status")
        assert result.exit_code == 0
        assert payload["schema_status"] == "current"
        assert payload["state_schema_status"] == "current"
        assert tree_snapshot(tmp_path) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions are required")
def test_diagnostics_work_with_read_only_current_files(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    paths = (index_path, index_path.with_name("state.sqlite"), meetily_db)
    for path in paths:
        path.chmod(0o444)
    data_dir.chmod(0o555)
    before = tree_snapshot(tmp_path)
    try:
        result, payload = invoke_json(index_path, "doctor")
        assert result.exit_code == 0
        assert payload["index_database"]["status"] == "current"
        assert payload["state_database"]["status"] == "current"
        assert tree_snapshot(tmp_path) == before
    finally:
        data_dir.chmod(0o755)
        for path in paths:
            path.chmod(0o644)
