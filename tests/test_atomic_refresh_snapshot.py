from __future__ import annotations

import multiprocessing
import os
import shutil
import sqlite3
import threading
from pathlib import Path
from stat import S_ISDIR
from time import monotonic, sleep
from typing import Protocol

import pytest
from typer.testing import CliRunner

from meetily_memory.cli.app import app
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.db.schema import IndexReadError, existing_index_connection
from meetily_memory.durable_files import fsync_directory
from meetily_memory.json_codec import loads_json
from meetily_memory.refresh import (
    IndexRemovalDurabilityAmbiguousError,
    PublicationDurabilityAmbiguousError,
    SourceSelection,
    StaleSourceSelectionError,
    publish_index_candidate_locked,
    refresh_index,
    relocate_selected_source_locked,
)
from meetily_memory.refresh_lock import RefreshLock, RefreshLockBusyError
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.scanner.fresh_index import build_fresh_index
from meetily_memory.user_state import UserStateRepository


class EventLike(Protocol):
    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


def _hold_lock(index_path: Path, ready: EventLike, release: EventLike) -> None:
    with RefreshLock(index_path):
        ready.set()
        release.wait(timeout=10)


def _selected_state(index_path: Path, source_path: Path) -> tuple[UserStateRepository, str]:
    state = UserStateRepository(index_path.with_name("state.sqlite"))
    source_uuid = state.resolve_source(
        "meetily_sqlite",
        source_path,
        now="2026-08-31T10:00:00Z",
    )
    state.select_source(source_uuid)
    return state, source_uuid


def _selection(state: UserStateRepository, source_uuid: str) -> SourceSelection:
    binding = state.get_source_binding(source_uuid)
    assert binding is not None
    return SourceSelection(
        source_uuid=source_uuid,
        source_path=Path(str(binding["current_path"])),
        source_revision=int(binding["revision"]),
    )


def _sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal"))


def _connection_content(conn: sqlite3.Connection) -> tuple[str, str]:
    title = str(
        conn.execute("SELECT title FROM meetings WHERE external_id='meeting-1'").fetchone()[0]
    )
    text = str(
        conn.execute("SELECT text FROM chunks WHERE external_id='transcript-1'").fetchone()[0]
    )
    return title, text


def _coherent_content(path: Path) -> tuple[str, str]:
    with sqlite3.connect(f"{path.resolve(strict=True).as_uri()}?mode=ro", uri=True) as conn:
        conn.execute("BEGIN")
        content = _connection_content(conn)
        conn.rollback()
    return content


def test_refresh_replaces_reopens_and_searches_stable_evidence(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state, source_uuid = _selected_state(index_path, meetily_db)

    published = refresh_index(index_path)
    repository = IndexRepository.open_existing(index_path)
    hit = repository.search_hits("pricing decision", limit=1)[0]

    assert published.source.source_uuid == source_uuid
    assert published.source.source_revision == 0
    assert hit.meeting.ref.source_uuid == source_uuid
    assert hit.id
    assert repository.get_search_hit(hit.id) == hit
    assert IndexRepository.open_existing(index_path).search_hits("pricing decision", limit=1) == (
        hit,
    )
    meeting_result = MeetilyMemoryCore(index_path).search("pricing decision", limit=1).results[0]
    assert meeting_result.meeting.ref == hit.meeting.ref
    assert meeting_result.evidence[0].id == hit.id
    assert state.source_binding_is_current(source_uuid, "meetily_sqlite", str(meetily_db), 0)
    assert all(not sidecar.exists() for sidecar in _sidecars(index_path))


def test_refresh_noops_when_source_fingerprint_is_unchanged(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    _selected_state(index_path, meetily_db)

    first = refresh_index(index_path)
    before = (index_path.stat().st_ino, index_path.stat().st_mtime_ns, index_path.read_bytes())
    second = refresh_index(index_path)

    assert first.changed is True
    assert second.changed is False
    after = (index_path.stat().st_ino, index_path.stat().st_mtime_ns, index_path.read_bytes())
    assert after == before


def test_forced_refresh_rebuilds_an_unchanged_source(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    _selected_state(index_path, meetily_db)
    refresh_index(index_path)
    before_inode = index_path.stat().st_ino

    forced = refresh_index(index_path, force=True)

    assert forced.changed is True
    assert index_path.stat().st_ino != before_inode


def test_wal_change_invalidates_source_fingerprint(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    _selected_state(index_path, meetily_db)
    refresh_index(index_path)

    with sqlite3.connect(meetily_db) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE meetings SET title='WAL title' WHERE id='meeting-1'")
        writer.commit()
        assert meetily_db.with_name(meetily_db.name + "-wal").exists()

        refreshed = refresh_index(index_path)

    assert refreshed.changed is True
    assert _coherent_content(index_path)[0] == "WAL title"


def test_cli_refresh_search_status_and_doctor_use_disposable_snapshot(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    environment = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()

    refreshed = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db), "--json"],
        env=environment,
    )
    searched = runner.invoke(
        app,
        ["--index", str(index_path), "s", "pricing decision", "--json"],
        env=environment,
    )
    status = runner.invoke(
        app,
        ["--index", str(index_path), "status", "--json"],
        env=environment,
    )
    doctor = runner.invoke(
        app,
        ["--index", str(index_path), "doctor", "--json"],
        env=environment,
    )

    assert refreshed.exit_code == 0, refreshed.output
    assert searched.exit_code == 0, searched.output
    assert status.exit_code == 0, status.output
    assert doctor.exit_code == 0, doctor.output
    refresh_payload = loads_json(refreshed.stdout)
    assert refresh_payload["source_revision"] == 0
    assert refresh_payload["meetings_seen"] == 2
    assert loads_json(searched.stdout)[0]["evidence"][0]["id"]
    for diagnostic in (loads_json(status.stdout), loads_json(doctor.stdout)):
        assert diagnostic["index_database"]["status"] == "current"
        assert diagnostic["meetings"] == 2
        assert diagnostic["chunks"] == 6
        assert "decisions" not in diagnostic
        assert "action_items" not in diagnostic
        assert "risks" not in diagnostic
        assert "open_questions" not in diagnostic


def test_concurrent_reader_observes_only_old_or_new_complete_snapshot(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    _selected_state(index_path, meetily_db)
    refresh_index(index_path)
    reader_uri = f"{index_path.resolve(strict=True).as_uri()}?mode=ro"

    with sqlite3.connect(reader_uri, uri=True) as pinned_reader:
        pinned_reader.execute("BEGIN")
        old = _connection_content(pinned_reader)
        with sqlite3.connect(meetily_db) as source:
            source.execute("UPDATE meetings SET title='Published New Title' WHERE id='meeting-1'")
            source.execute(
                "UPDATE transcripts SET transcript='published new evidence' WHERE id='transcript-1'"
            )
            source.commit()

        refresh_index(index_path)
        new = _coherent_content(index_path)

        assert old != new
        assert _connection_content(pinned_reader) == old
        assert new == ("Published New Title", "published new evidence")
        pinned_reader.rollback()


def test_refresh_lock_is_process_safe_for_fresh_publication(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    _selected_state(index_path, meetily_db)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(target=_hold_lock, args=(index_path, ready, release))
    process.start()
    assert ready.wait(timeout=10)
    try:
        with pytest.raises(RefreshLockBusyError, match="already running"):
            refresh_index(index_path)
        assert not index_path.exists()
    finally:
        release.set()
        process.join(timeout=10)
    assert process.exitcode == 0


def test_stale_state_revision_prevents_publish_and_preserves_canonical(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state, source_uuid = _selected_state(index_path, meetily_db)
    refresh_index(index_path)
    before = index_path.read_bytes()
    selected = _selection(state, source_uuid)
    candidate = build_fresh_index(
        selected_source_uuid=source_uuid,
        selected_source_path=meetily_db,
        selected_source_revision=selected.source_revision,
        destination_directory=tmp_path,
    )
    state.update_source_path(source_uuid, str(meetily_db), now="revision-changed")

    with RefreshLock(index_path), pytest.raises(StaleSourceSelectionError, match="changed"):
        publish_index_candidate_locked(index_path, state, selected, candidate)

    assert index_path.read_bytes() == before
    assert not candidate.candidate_path.exists()


def test_pre_replace_failure_preserves_canonical_bytes_and_cleans_candidate(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state, source_uuid = _selected_state(index_path, meetily_db)
    refresh_index(index_path)
    before = index_path.read_bytes()
    selected = _selection(state, source_uuid)
    candidate = build_fresh_index(
        selected_source_uuid=source_uuid,
        selected_source_path=meetily_db,
        selected_source_revision=selected.source_revision,
        destination_directory=tmp_path,
    )

    replace_fault = "replace fault"

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError(replace_fault)

    with RefreshLock(index_path):
        monkeypatch.setattr(os, "replace", fail_replace)
        with pytest.raises(OSError, match=replace_fault):
            publish_index_candidate_locked(index_path, state, selected, candidate)

    assert index_path.read_bytes() == before
    assert not candidate.candidate_path.exists()


def test_post_replace_directory_fsync_failure_is_ambiguous_without_rollback(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state, source_uuid = _selected_state(index_path, meetily_db)
    refresh_index(index_path)
    with sqlite3.connect(meetily_db) as source:
        source.execute("UPDATE meetings SET title='Ambiguous Published Title' WHERE id='meeting-1'")
        source.commit()
    selected = _selection(state, source_uuid)
    candidate = build_fresh_index(
        selected_source_uuid=source_uuid,
        selected_source_path=meetily_db,
        selected_source_revision=selected.source_revision,
        destination_directory=tmp_path,
    )

    fsync_fault = "directory fsync fault"

    def fail_fsync(_descriptor: int) -> None:
        raise OSError(fsync_fault)

    with RefreshLock(index_path):
        monkeypatch.setattr(os, "fsync", fail_fsync)
        with pytest.raises(
            PublicationDurabilityAmbiguousError,
            match="Do not restore or roll back",
        ):
            publish_index_candidate_locked(index_path, state, selected, candidate)

    assert not candidate.candidate_path.exists()
    assert _coherent_content(index_path)[0] == "Ambiguous Published Title"
    assert IndexRepository.open_existing(index_path).search_hits("pricing decision", limit=1)


def test_malformed_candidate_is_rejected_before_replace(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state, source_uuid = _selected_state(index_path, meetily_db)
    refresh_index(index_path)
    before = index_path.read_bytes()
    selected = _selection(state, source_uuid)
    candidate = build_fresh_index(
        selected_source_uuid=source_uuid,
        selected_source_path=meetily_db,
        selected_source_revision=selected.source_revision,
        destination_directory=tmp_path,
    )
    with sqlite3.connect(candidate.candidate_path) as conn:
        conn.execute("UPDATE index_meta SET chunk_count=chunk_count + 1")
        conn.commit()

    with RefreshLock(index_path), pytest.raises(RuntimeError, match="counts do not match"):
        publish_index_candidate_locked(index_path, state, selected, candidate)

    assert index_path.read_bytes() == before
    assert not candidate.candidate_path.exists()


def test_legacy_index_is_rejected_with_actionable_rebuild_error(tmp_path: Path) -> None:
    index_path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(index_path) as conn:
        conn.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY, source_uuid TEXT)")
        conn.execute("PRAGMA user_version=7")
        conn.commit()
    before = index_path.read_bytes()

    with (
        pytest.raises(IndexReadError, match=r"refresh.*in-place migration is not supported"),
        existing_index_connection(index_path),
    ):
        pass

    assert index_path.read_bytes() == before
    assert all(not sidecar.exists() for sidecar in _sidecars(index_path))


def test_foreign_and_corrupt_indexes_are_read_only_rebuild_errors(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    foreign_path = tmp_path / "foreign.sqlite"
    with sqlite3.connect(foreign_path) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        conn.commit()

    corrupt = build_fresh_index(
        selected_source_uuid="corrupt-reader-source",
        selected_source_path=meetily_db,
        destination_directory=tmp_path,
    ).candidate_path
    with sqlite3.connect(corrupt) as conn:
        conn.execute("UPDATE index_meta SET chunk_count=chunk_count + 1")
        conn.commit()

    for rejected in (foreign_path, corrupt):
        before = (rejected.read_bytes(), rejected.stat().st_mtime_ns)
        with (
            pytest.raises(IndexReadError, match=r"refresh.*in-place migration is not supported"),
            existing_index_connection(rejected),
        ):
            pass
        assert (rejected.read_bytes(), rejected.stat().st_mtime_ns) == before
        assert all(not sidecar.exists() for sidecar in _sidecars(rejected))


def test_successful_relocation_publishes_only_new_path_and_revision(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state, source_uuid = _selected_state(index_path, meetily_db)
    refresh_index(index_path)
    moved_source = tmp_path / "moved-success.sqlite"
    shutil.copy2(meetily_db, moved_source)

    with RefreshLock(index_path):
        relocated = relocate_selected_source_locked(
            index_path,
            state,
            source_uuid,
            moved_source,
            now="2026-08-31T10:30:00Z",
        )

    assert relocated.previous_path == meetily_db.resolve()
    assert relocated.published.source.source_path == moved_source.resolve()
    assert relocated.published.source.source_revision == 1
    binding = state.get_selected_source_binding()
    assert binding is not None
    assert binding["current_path"] == str(moved_source.resolve())
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("SELECT source_path, source_revision FROM index_meta").fetchone() == (
            str(moved_source.resolve()),
            1,
        )
    assert IndexRepository.open_existing(index_path).search_hits("pricing decision", limit=1)
    assert all(not sidecar.exists() for sidecar in _sidecars(index_path))


def test_relocation_removes_old_index_then_changes_state_before_fresh_rebuild(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state, source_uuid = _selected_state(index_path, meetily_db)
    refresh_index(index_path)
    moved_source = tmp_path / "moved-blocked.sqlite"
    shutil.copy2(meetily_db, moved_source)
    errors: list[BaseException] = []
    results: list[object] = []

    def relocate() -> None:
        try:
            with RefreshLock(index_path):
                results.append(
                    relocate_selected_source_locked(
                        index_path,
                        state,
                        source_uuid,
                        moved_source,
                        now="2026-08-31T10:45:00Z",
                    )
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with sqlite3.connect(moved_source) as source_blocker:
        source_blocker.execute("BEGIN EXCLUSIVE")
        worker = threading.Thread(target=relocate)
        worker.start()
        try:
            deadline = monotonic() + 3
            while monotonic() < deadline:
                binding = state.get_source_binding(source_uuid)
                if (
                    binding is not None
                    and binding["current_path"] == str(moved_source.resolve())
                    and binding["revision"] == 1
                    and not index_path.exists()
                ):
                    break
                sleep(0.01)
            else:
                failure = "Relocation did not durably remove the old index and commit state"
                pytest.fail(f"{failure} before waiting on the fresh source snapshot")
        finally:
            source_blocker.rollback()
            worker.join(timeout=10)

    assert not worker.is_alive()
    assert not errors
    assert len(results) == 1
    assert index_path.is_file()
    assert IndexRepository.open_existing(index_path).search_hits("pricing decision", limit=1)
    assert all(not sidecar.exists() for sidecar in _sidecars(index_path))


def test_relocation_removes_old_index_before_state_commit_on_directory_fsync_failure(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state, source_uuid = _selected_state(index_path, meetily_db)
    refresh_index(index_path)
    moved_source = tmp_path / "moved.sqlite"
    shutil.copy2(meetily_db, moved_source)
    before_binding = state.get_source_binding(source_uuid)
    assert before_binding is not None

    real_fsync = os.fsync
    directory_failed = False

    unlink_fsync_fault = "unlink directory fsync fault"

    def fail_first_directory_fsync(descriptor: int) -> None:
        nonlocal directory_failed
        if S_ISDIR(os.fstat(descriptor).st_mode) and not directory_failed:
            directory_failed = True
            raise OSError(unlink_fsync_fault)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_first_directory_fsync)
    with (
        RefreshLock(index_path),
        pytest.raises(
            IndexRemovalDurabilityAmbiguousError,
            match="state was not changed",
        ),
    ):
        relocate_selected_source_locked(
            index_path,
            state,
            source_uuid,
            moved_source,
            now="2026-08-31T11:00:00Z",
        )

    after_binding = state.get_source_binding(source_uuid)
    assert after_binding == before_binding
    assert not index_path.exists()
    assert all(not sidecar.exists() for sidecar in _sidecars(index_path))
    fsync_directory(tmp_path)
