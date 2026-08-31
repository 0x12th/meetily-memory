from __future__ import annotations

import multiprocessing
import shutil
import sqlite3
from typing import TYPE_CHECKING, Protocol

import pytest
from typer.testing import CliRunner

from meetily_memory.cli.app import app
from meetily_memory.config.settings import load_app_settings
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.db.index_snapshot import INDEX_APPLICATION_TABLES
from meetily_memory.json_codec import loads_json
from meetily_memory.refresh_lock import RefreshLock
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.scanner.fresh_index import DuplicateEvidenceIdentityError
from meetily_memory.tagging import TagService
from tests.index_helpers import publish_fresh_index

if TYPE_CHECKING:
    from pathlib import Path


class EventLike(Protocol):
    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


def hold_refresh_lock(index_path: Path, ready: EventLike, release: EventLike) -> None:
    with RefreshLock(index_path):
        ready.set()
        release.wait(timeout=10)


def index_tables(index_path: Path) -> set[str]:
    with sqlite3.connect(index_path) as conn:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }


def test_public_refresh_bootstraps_exact_state_and_single_source_index(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    runner = CliRunner()
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}

    refresh = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db), "--json"],
        env=env,
    )

    assert refresh.exit_code == 0, refresh.output
    payload = loads_json(refresh.stdout)
    assert payload["meetings_seen"] == 2
    settings = load_app_settings(data_dir / "settings.json")
    assert settings.source_uuid == payload["source_uuid"]
    assert settings.source_path is None
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("SELECT source_uuid, meeting_count FROM index_meta").fetchone() == (
            payload["source_uuid"],
            2,
        )
        assert conn.execute("SELECT COUNT(DISTINCT source_uuid) FROM meetings").fetchone() == (1,)
    assert index_tables(index_path) >= INDEX_APPLICATION_TABLES
    assert "scan_runs" not in index_tables(index_path)
    assert "index_generation" not in index_tables(index_path)


def test_explicit_rebind_preserves_identity_evidence_and_manual_tags(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    moved_db = tmp_path / "moved.sqlite"
    shutil.copyfile(meetily_db, moved_db)
    runner = CliRunner()
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    init = runner.invoke(
        app,
        ["--index", str(index_path), "init", "--source", str(meetily_db)],
        env=env,
    )
    assert init.exit_code == 0, init.output

    before = MeetilyMemoryCore(index_path).search("migration risks", limit=1).results[0]
    TagService(IndexRepository(index_path)).assign((before.meeting.ref,), ("Сбер",))
    original_uuid = before.meeting.ref.source_uuid

    rebind = runner.invoke(
        app,
        ["--index", str(index_path), "config", "source", str(moved_db), "--rebind"],
        env=env,
    )

    assert rebind.exit_code == 0, rebind.output
    assert f"old source path: {meetily_db}" in rebind.stdout
    assert f"new source path: {moved_db}" in rebind.stdout
    after = MeetilyMemoryCore(index_path).search("migration risks", limit=1).results[0]
    assert after.meeting.ref == before.meeting.ref
    assert after.evidence[0].id == before.evidence[0].id
    assert load_app_settings(data_dir / "settings.json").source_uuid == original_uuid
    assert [
        tag.display_name
        for tag in TagService(IndexRepository(index_path)).list_for_meeting(before.meeting.ref)
    ] == ["Сбер"]
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("SELECT source_uuid, source_path FROM index_meta").fetchone() == (
            original_uuid,
            str(moved_db.resolve()),
        )


def test_selecting_another_source_creates_distinct_state_uuid_and_replaces_active_index(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    other_db = tmp_path / "other.sqlite"
    shutil.copyfile(meetily_db, other_db)
    runner = CliRunner()
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    first = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db), "--json"],
        env=env,
    )
    first_uuid = loads_json(first.stdout)["source_uuid"]

    selected = runner.invoke(
        app,
        ["--index", str(index_path), "config", "source", str(other_db), "--json"],
        env=env,
    )

    assert selected.exit_code == 0, selected.output
    second_uuid = loads_json(selected.stdout)["source_uuid"]
    assert second_uuid != first_uuid
    with sqlite3.connect(data_dir / "state.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone() == (2,)
        assert conn.execute("SELECT source_uuid FROM app_settings").fetchone() == (second_uuid,)
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("SELECT source_uuid FROM index_meta").fetchone() == (second_uuid,)
        assert conn.execute("SELECT DISTINCT source_uuid FROM meetings").fetchall() == [
            (second_uuid,)
        ]


def test_refresh_lock_blocks_second_writer_without_creating_state(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(target=hold_refresh_lock, args=(index_path, ready, release))
    process.start()
    assert ready.wait(timeout=10)
    try:
        blocked = CliRunner().invoke(
            app,
            ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
            env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
        )
        assert blocked.exit_code != 0
        assert "refresh is already running" in blocked.output.lower()
        assert not index_path.exists()
        assert not (data_dir / "state.sqlite").exists()
    finally:
        process.terminate()
        process.join(timeout=10)


def test_failed_refresh_preserves_published_index_and_has_no_scan_run_ledger(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    before = index_path.read_bytes()
    with sqlite3.connect(meetily_db) as conn:
        conn.execute("UPDATE transcripts SET id = 'summary:meeting-1' WHERE id = 'transcript-1'")
        conn.commit()

    with pytest.raises(DuplicateEvidenceIdentityError):
        publish_fresh_index(index_path, meetily_db)

    assert index_path.read_bytes() == before
    assert "scan_runs" not in index_tables(index_path)
    assert IndexRepository.open_existing(index_path).search("pricing decision")


def test_fresh_refresh_reconciles_deleted_meeting_while_state_keeps_tag(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    repository = IndexRepository(index_path)
    meeting_ref = repository.meeting_ref_for_local_id(2)
    assert meeting_ref is not None
    service = TagService(repository)
    service.assign((meeting_ref,), ("Сбер",))

    with sqlite3.connect(meetily_db) as conn:
        meeting = conn.execute("SELECT * FROM meetings WHERE id='meeting-2'").fetchone()
        assert meeting is not None
        conn.execute("DELETE FROM meetings WHERE id='meeting-2'")
        conn.commit()
    publish_fresh_index(index_path, meetily_db)

    assert MeetilyMemoryCore(index_path).get_meeting_by_ref(meeting_ref) is None
    assert service.orphaned_assignment_count() == 1

    with sqlite3.connect(meetily_db) as conn:
        placeholders = ", ".join("?" for _ in meeting)
        conn.execute(f"INSERT INTO meetings VALUES ({placeholders})", meeting)  # noqa: S608
        conn.commit()
    publish_fresh_index(index_path, meetily_db)

    reopened_service = TagService(IndexRepository(index_path))
    assert [tag.display_name for tag in reopened_service.list_for_meeting(meeting_ref)] == ["Сбер"]
    assert reopened_service.orphaned_assignment_count() == 0
