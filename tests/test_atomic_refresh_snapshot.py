from __future__ import annotations

import hashlib
import multiprocessing
import os
import shutil
import sqlite3
import threading
import time
import traceback
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

    from meetily_memory.domain import MeetingSearchFilters, SearchHit, SearchResults

import pytest
from typer.testing import CliRunner

import meetily_memory.cli.lifecycle_commands as lifecycle_module
import meetily_memory.db.schema as schema_module
import meetily_memory.retrieval as retrieval_module
import meetily_memory.scanner.meetily_sqlite as scanner_module
from meetily_memory.cli.app import app
from meetily_memory.config.settings import (
    ObsidianSettings,
    load_app_settings,
    update_app_settings,
)
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.db.repository import IndexRepository
from meetily_memory.domain import MeetingRef, RetrievalSource
from meetily_memory.integrations import ObsidianSyncResult
from meetily_memory.json_codec import loads_json
from meetily_memory.refresh_lock import RefreshLock, RefreshLockBusyError
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.tagging import TagService
from meetily_memory.user_state import UserStateRepository

CRASH_EXIT_CODE = 91
PROJECTION_TABLES = (
    "sources",
    "meetings",
    "chunks",
    "chunks_fts",
    "people",
    "meeting_people",
    "artifacts",
    "decisions",
    "action_items",
    "risks",
    "open_questions",
    "knowledge_nodes",
    "knowledge_edges",
    "topic_aliases",
    "index_generation",
)
PRE_PUBLISH_FAULTS = (
    pytest.param(
        ("before_meeting:2", "source_scan", "keep"),
        id="before-second-meeting",
    ),
    pytest.param(
        ("before_reconciliation", "reconciliation", "delete"),
        id="before-reconciliation",
    ),
    pytest.param(("before_publish", "publishing", "delete"), id="before-publish"),
)


def _crash_scan_at_checkpoint(index_path: str, source_path: str, checkpoint: str) -> None:
    def exit_at_checkpoint(_name: str) -> None:
        if _name == checkpoint:
            os._exit(CRASH_EXIT_CODE)

    setattr(scanner_module, "_scan_checkpoint", exit_at_checkpoint)  # noqa: B010
    MeetilySQLiteScanner(Path(index_path)).scan(Path(source_path))
    os._exit(2)


@contextmanager
def _readonly_connection(path: Path) -> Generator[sqlite3.Connection, None, None]:
    uri = f"{path.resolve(strict=True).as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        yield conn


def _projection_snapshot(index_path: Path) -> dict[str, tuple[tuple[Any, ...], ...]]:
    with _readonly_connection(index_path) as conn:
        conn.execute("BEGIN")
        snapshot = {
            table: tuple(
                tuple(row)
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")  # noqa: S608
            )
            for table in PROJECTION_TABLES
        }
        conn.rollback()
    return snapshot


def _coherent_meeting_state(index_path: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    with _readonly_connection(index_path) as conn:
        conn.execute("BEGIN")
        titles = tuple(
            str(row[0])
            for row in conn.execute("SELECT title FROM meetings ORDER BY external_id").fetchall()
        )
        transcript_texts = tuple(
            str(row[0])
            for row in conn.execute(
                """
                SELECT c.text
                FROM chunks c
                JOIN meetings m ON m.id = c.meeting_id
                WHERE c.external_id IN ('transcript-1', 'transcript-2')
                ORDER BY m.external_id, c.external_id
                """
            ).fetchall()
        )
        conn.rollback()
    return titles, transcript_texts


def _source_digest(source_path: Path) -> str:
    return hashlib.sha256(source_path.read_bytes()).hexdigest()


def _mutate_source(source_path: Path, *, delete_second: bool) -> None:
    with sqlite3.connect(source_path) as conn:
        conn.execute(
            "UPDATE meetings SET title = 'Atomic New Launch', updated_at = ? WHERE id = ?",
            ("2026-08-29T10:00:00Z", "meeting-1"),
        )
        conn.execute(
            "UPDATE transcripts SET transcript = ? WHERE id = ?",
            ("Atomic refreshed marker confirms the new snapshot.", "transcript-1"),
        )
        if delete_second:
            conn.execute("DELETE FROM transcripts WHERE meeting_id = 'meeting-2'")
            conn.execute("DELETE FROM summary_processes WHERE meeting_id = 'meeting-2'")
            conn.execute("DELETE FROM meeting_notes WHERE meeting_id = 'meeting-2'")
            conn.execute("DELETE FROM meetings WHERE id = 'meeting-2'")
        else:
            conn.execute(
                "UPDATE meetings SET title = 'Atomic New Follow-up', updated_at = ? WHERE id = ?",
                ("2026-08-29T10:00:00Z", "meeting-2"),
            )
            conn.execute(
                "UPDATE transcripts SET transcript = ? WHERE id = ?",
                ("Atomic second marker belongs to the same snapshot.", "transcript-2"),
            )
        conn.commit()


def _latest_scan_run(index_path: Path) -> dict[str, Any]:
    with _readonly_connection(index_path) as conn:
        row = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    return dict(row)


def _assert_clean_index_family(index_path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal", ".next", ".tmp"):
        assert not index_path.with_name(index_path.name + suffix).exists()
    assert not tuple(index_path.parent.glob(f".{index_path.name}.*"))
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).casefold() == "delete"


def _assert_old_snapshot_is_searchable(index_path: Path, *, deleted_on_success: bool) -> None:
    repo = IndexRepository.open_existing(index_path)
    assert repo.search("atomic refreshed marker") == []
    assert repo.search("pricing decision")
    if deleted_on_success:
        assert repo.search("migration risks")


def _assert_new_snapshot_is_searchable(index_path: Path, *, deleted_on_success: bool) -> None:
    repo = IndexRepository.open_existing(index_path)
    assert repo.search("atomic refreshed marker")
    assert repo.search("pricing decision") == []
    if deleted_on_success:
        assert repo.search("migration risks") == []


def _assert_sanitized_post_publish_exception(error: BaseException, secret: str) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is True
    assert secret not in str(error)
    assert secret not in "".join(traceback.format_exception(error))


def _assert_sanitized_persisted_run(run: dict[str, Any], secret: str) -> None:
    assert secret not in str(run["error_message"])
    assert secret not in str(run["errors_json"])


def test_pending_path_heal_is_published_with_content_or_rolled_back(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    secondary_source = tmp_path / "secondary.sqlite"
    moved_primary = tmp_path / "moved-primary.sqlite"
    moved_secondary = tmp_path / "moved-secondary.sqlite"
    shutil.copy2(meetily_db, secondary_source)
    shutil.copy2(meetily_db, moved_primary)
    shutil.copy2(meetily_db, moved_secondary)
    scanner = MeetilySQLiteScanner(index_path, state_path=state_path)
    primary = scanner.scan(meetily_db)
    secondary = scanner.scan(secondary_source)
    state = UserStateRepository(state_path)
    state.claim_source_path(
        primary.source_uuid,
        scanner.source_kind,
        moved_primary,
        now="move-primary",
    )
    state.claim_source_path(
        secondary.source_uuid,
        scanner.source_kind,
        moved_secondary,
        now="move-secondary",
    )
    previous_projection = _projection_snapshot(index_path)

    IndexRepository(index_path, state_path=state_path)

    assert _projection_snapshot(index_path) == previous_projection
    with sqlite3.connect(state_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sources WHERE pending_revision IS NOT NULL"
            ).fetchone()[0]
            == 2
        )

    def fail_after_path_projection(checkpoint: str) -> None:
        if checkpoint == "after_pending_path_projection":
            error_message = "credential=secret pending projection"
            raise RuntimeError(error_message)

    monkeypatch.setattr(scanner_module, "_scan_checkpoint", fail_after_path_projection)
    with pytest.raises(RuntimeError, match="credential=secret"):
        scanner.scan(moved_primary)

    assert _projection_snapshot(index_path) == previous_projection
    with sqlite3.connect(state_path) as conn:
        pending = conn.execute(
            """
            SELECT uuid, current_path, projected_path, pending_revision
            FROM sources
            ORDER BY uuid
            """
        ).fetchall()
    assert all(row[1] != row[2] and row[3] is not None for row in pending)
    assert _latest_scan_run(index_path)["status"] == "failed"

    monkeypatch.setattr(scanner_module, "_scan_checkpoint", lambda _checkpoint: None)
    scanner.scan(moved_primary)

    expected_paths = {
        primary.source_uuid: str(moved_primary.resolve(strict=True)),
        secondary.source_uuid: str(moved_secondary.resolve(strict=True)),
    }
    with sqlite3.connect(index_path) as conn:
        assert dict(conn.execute("SELECT source_uuid, path FROM sources")) == expected_paths
        assert set(conn.execute("SELECT DISTINCT source_path FROM meetings")) == {
            (path,) for path in expected_paths.values()
        }
    with sqlite3.connect(state_path) as conn:
        finalized = conn.execute(
            "SELECT uuid, current_path, projected_path, pending_revision FROM sources"
        ).fetchall()
    assert all(row[1] == row[2] and row[3] is None for row in finalized)
    _assert_clean_index_family(index_path)


def test_retry_resolves_source_path_finalize_for_status_and_doctor(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    moved_source = tmp_path / "moved.sqlite"
    shutil.copy2(meetily_db, moved_source)
    scanner = MeetilySQLiteScanner(index_path)
    initial = scanner.scan(meetily_db)
    state = UserStateRepository(data_dir / "state.sqlite")
    state.claim_source_path(
        initial.source_uuid,
        scanner.source_kind,
        moved_source,
        now="move-before-finalize-failure",
    )
    original_finalize = UserStateRepository.finalize_source_path_claims

    def fail_finalize(_state: UserStateRepository, _claims: object) -> bool:
        error_message = "credential=secret finalize=private"
        raise RuntimeError(error_message)

    monkeypatch.setattr(UserStateRepository, "finalize_source_path_claims", fail_finalize)
    with pytest.raises(scanner_module.SourcePathProjectionFinalizeError) as raised:
        scanner.scan(moved_source)

    _assert_sanitized_post_publish_exception(raised.value, "secret")
    failed_finalize_run = _latest_scan_run(index_path)
    assert failed_finalize_run["status"] == "completed"
    assert failed_finalize_run["phase"] == "post_publish_source_path_finalize_failed"
    _assert_sanitized_persisted_run(failed_finalize_run, "secret")
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()
    for command in ("status", "doctor"):
        unresolved = runner.invoke(
            app,
            ["--index", str(index_path), command, "--json"],
            env=env,
        )
        assert unresolved.exit_code == 0, unresolved.output
        assert (
            loads_json(unresolved.stdout)["last_post_publish_error"]["id"]
            == (failed_finalize_run["id"])
        )

    monkeypatch.setattr(
        UserStateRepository,
        "finalize_source_path_claims",
        original_finalize,
    )
    scanner.scan(moved_source)

    for command in ("status", "doctor"):
        resolved = runner.invoke(
            app,
            ["--index", str(index_path), command, "--json"],
            env=env,
        )
        assert resolved.exit_code == 0, resolved.output
        assert loads_json(resolved.stdout)["last_post_publish_error"] is None
    binding = state.get_source_binding(initial.source_uuid)
    assert binding is not None
    assert binding["pending_revision"] is None
    _assert_clean_index_family(index_path)


@pytest.mark.parametrize(
    "cleanup_function",
    [
        "_restore_delete_journal_mode",
        "_require_clean_index_sidecars",
        "_discard_transient_wal_marker",
    ],
)
def test_post_commit_cleanup_failure_stays_completed_and_retry_cleans(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_function: str,
) -> None:
    index_path = tmp_path / "index.sqlite"
    scanner = MeetilySQLiteScanner(index_path)
    initial = scanner.scan(meetily_db)
    _mutate_source(meetily_db, delete_second=False)
    original_cleanup = getattr(schema_module, cleanup_function)

    def fail_cleanup(_value: object) -> None:
        error_message = "credential=secret cleanup=private"
        raise OSError(error_message)

    monkeypatch.setattr(schema_module, cleanup_function, fail_cleanup)
    with pytest.raises(schema_module.IndexProjectionCleanupError) as raised:
        scanner.scan(meetily_db)

    _assert_sanitized_post_publish_exception(raised.value, "secret")
    _assert_new_snapshot_is_searchable(index_path, deleted_on_success=False)
    run = _latest_scan_run(index_path)
    assert run["id"] > initial.run_id
    assert run["status"] == "completed"
    assert run["phase"] == "post_publish_index_cleanup_failed"
    _assert_sanitized_persisted_run(run, "secret")
    details = loads_json(str(run["errors_json"]))
    assert details["source_uuid"] == initial.source_uuid
    assert details["source_path"] == str(meetily_db.resolve(strict=True))
    assert details["issues"][0]["retry_command"] == [
        "mm",
        "--index",
        str(index_path),
        "refresh",
        "--source",
        str(meetily_db.resolve(strict=True)),
    ]
    assert schema_module.transient_wal_marker_path(index_path).exists()
    diagnostics = IndexRepository.open_existing(index_path).scan_run_diagnostics()
    assert diagnostics["last_failed_run"] is None
    post_publish_error = diagnostics["last_post_publish_error"]
    assert post_publish_error is not None
    assert post_publish_error["id"] == run["id"]

    monkeypatch.setattr(schema_module, cleanup_function, original_cleanup)
    scanner.scan(meetily_db)

    assert (
        IndexRepository.open_existing(index_path).scan_run_diagnostics()["last_post_publish_error"]
        is None
    )
    _assert_clean_index_family(index_path)


@pytest.mark.parametrize("fault", PRE_PUBLISH_FAULTS)
def test_pre_publish_error_rolls_back_the_complete_projection(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: tuple[str, str, str],
) -> None:
    checkpoint, expected_phase, mutation = fault
    delete_second = mutation == "delete"
    index_path = tmp_path / "index.sqlite"
    scanner = MeetilySQLiteScanner(index_path)
    scanner.scan(meetily_db)
    previous_projection = _projection_snapshot(index_path)
    _mutate_source(meetily_db, delete_second=delete_second)
    source_before_scan = _source_digest(meetily_db)
    observed_running: list[dict[str, Any]] = []

    def inject_fault(name: str) -> None:
        if name == "after_running":
            observed_running.append(_latest_scan_run(index_path))
        if name == checkpoint:
            error_message = "credential=secret transcript=private"
            raise RuntimeError(error_message)

    monkeypatch.setattr(scanner_module, "_scan_checkpoint", inject_fault)

    with pytest.raises(RuntimeError, match="credential=secret"):
        scanner.scan(meetily_db)

    assert observed_running
    assert observed_running[0]["status"] == "running"
    assert _projection_snapshot(index_path) == previous_projection
    assert _source_digest(meetily_db) == source_before_scan
    failed_run = _latest_scan_run(index_path)
    assert failed_run["status"] == "failed"
    assert failed_run["phase"] == expected_phase
    assert failed_run["finished_at"] is not None
    assert "RuntimeError" in failed_run["error_message"]
    assert "secret" not in failed_run["error_message"]
    _assert_old_snapshot_is_searchable(index_path, deleted_on_success=delete_second)
    _assert_clean_index_family(index_path)


@pytest.mark.parametrize("fault", PRE_PUBLISH_FAULTS)
def test_process_death_keeps_old_snapshot_readable_and_retry_recovers(
    meetily_db: Path,
    tmp_path: Path,
    fault: tuple[str, str, str],
) -> None:
    checkpoint, _expected_phase, mutation = fault
    delete_second = mutation == "delete"
    index_path = tmp_path / "index.sqlite"
    scanner = MeetilySQLiteScanner(index_path)
    scanner.scan(meetily_db)
    previous_projection = _projection_snapshot(index_path)
    _mutate_source(meetily_db, delete_second=delete_second)
    source_before_scan = _source_digest(meetily_db)

    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_scan_at_checkpoint,
        args=(str(index_path), str(meetily_db), checkpoint),
    )
    process.start()
    process.join(timeout=20)

    assert process.exitcode == CRASH_EXIT_CODE
    assert _latest_scan_run(index_path)["status"] == "running"
    assert index_path.with_name(index_path.name + "-wal").exists()
    assert index_path.with_name(index_path.name + "-shm").exists()
    assert _projection_snapshot(index_path) == previous_projection
    assert _source_digest(meetily_db) == source_before_scan
    _assert_old_snapshot_is_searchable(index_path, deleted_on_success=delete_second)

    recovered = scanner.scan(meetily_db)

    assert recovered.meetings_seen == (1 if delete_second else 2)
    assert _source_digest(meetily_db) == source_before_scan
    _assert_new_snapshot_is_searchable(index_path, deleted_on_success=delete_second)
    with sqlite3.connect(index_path) as conn:
        statuses = conn.execute(
            "SELECT status, phase FROM scan_runs ORDER BY id DESC LIMIT 2"
        ).fetchall()
    assert statuses == [("completed", "completed"), ("failed", "interrupted")]
    _assert_clean_index_family(index_path)


def test_concurrent_readers_observe_only_complete_snapshots(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    scanner = MeetilySQLiteScanner(index_path)
    scanner.scan(meetily_db)
    old_state = _coherent_meeting_state(index_path)
    _mutate_source(meetily_db, delete_second=False)
    publish_ready = threading.Event()
    allow_publish = threading.Event()
    stop_readers = threading.Event()
    observed: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    read_errors: list[BaseException] = []
    writer_errors: list[BaseException] = []
    observed_lock = threading.Lock()

    def pause_before_publish(name: str) -> None:
        if name == "before_publish":
            publish_ready.set()
            assert allow_publish.wait(timeout=10)

    def run_writer() -> None:
        try:
            scanner.scan(meetily_db)
        except BaseException as exc:  # noqa: BLE001
            writer_errors.append(exc)

    def run_reader() -> None:
        try:
            while not stop_readers.is_set():
                state = _coherent_meeting_state(index_path)
                with observed_lock:
                    observed.append(state)
        except BaseException as exc:  # noqa: BLE001
            read_errors.append(exc)

    monkeypatch.setattr(scanner_module, "_scan_checkpoint", pause_before_publish)
    writer = threading.Thread(target=run_writer)
    writer.start()
    assert publish_ready.wait(timeout=10)
    readers = [threading.Thread(target=run_reader) for _ in range(4)]
    for reader in readers:
        reader.start()
    time.sleep(0.05)
    allow_publish.set()
    time.sleep(0.1)
    stop_readers.set()
    for reader in readers:
        reader.join(timeout=10)
    writer.join(timeout=10)

    assert not writer.is_alive()
    assert not read_errors
    assert not writer_errors
    new_state = _coherent_meeting_state(index_path)
    observed.append(new_state)
    assert old_state in observed
    assert new_state in observed
    assert set(observed) <= {old_state, new_state}
    _assert_clean_index_family(index_path)


def test_core_search_stays_on_one_snapshot_across_projection_replacement(  # noqa: PLR0915
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    scan = MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    TagService(IndexRepository(index_path, state_path=state_path)).assign(
        (MeetingRef(scan.source_uuid, "meeting-1"),),
        ("pricing decision",),
    )
    old_core = MeetilyMemoryCore(index_path, state_path=state_path)
    old_meeting = old_core.search("pricing decision", limit=1).results[0]
    assert old_meeting.match_sources == (RetrievalSource.TAG, RetrievalSource.FTS)
    assert old_meeting.matched_tags == ("pricing decision",)

    with sqlite3.connect(meetily_db) as conn:
        conn.execute(
            """
            UPDATE meetings
            SET title = ?, created_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                "Atomic Replacement Launch",
                "2026-07-03T10:00:00Z",
                "2026-08-30T10:00:00Z",
                "meeting-1",
            ),
        )
        conn.execute(
            "UPDATE transcripts SET transcript = ? WHERE id = ?",
            ("Atomic replacement marker is visible only in the new projection.", "transcript-1"),
        )
        conn.commit()

    next_index_path = tmp_path / "index.next.sqlite"
    MeetilySQLiteScanner(next_index_path, state_path=state_path).scan(meetily_db)
    with _readonly_connection(next_index_path) as conn:
        new_local_id = int(
            conn.execute("SELECT id FROM meetings WHERE external_id = 'meeting-1'").fetchone()[0]
        )
    assert old_meeting.meeting_id != new_local_id

    fts_complete = threading.Event()
    continue_search = threading.Event()
    search_results: list[SearchResults] = []
    search_errors: list[BaseException] = []
    original_search = retrieval_module.LexicalRetrievalStrategy._search_in_snapshot  # noqa: SLF001

    def pause_after_fts(  # noqa: PLR0913
        strategy: retrieval_module.LexicalRetrievalStrategy,
        query: str,
        limit: int = 10,
        *,
        operation_snapshot: sqlite3.Connection,
        prepared_query: object | None = None,
        filters: MeetingSearchFilters | None = None,
    ) -> tuple[SearchHit, ...]:
        hits = original_search(
            strategy,
            query,
            limit,
            operation_snapshot=operation_snapshot,
            prepared_query=prepared_query,
            filters=filters,
        )
        if query == "pricing decision":
            fts_complete.set()
            assert continue_search.wait(timeout=10)
        return hits

    def run_search() -> None:
        try:
            search_results.append(old_core.search("pricing decision", limit=1, context=1))
        except BaseException as exc:  # noqa: BLE001
            search_errors.append(exc)

    monkeypatch.setattr(
        retrieval_module.LexicalRetrievalStrategy,
        "_search_in_snapshot",
        pause_after_fts,
    )
    reader = threading.Thread(target=run_search)
    reader.start()
    assert fts_complete.wait(timeout=10)

    next_index_path.replace(index_path)
    continue_search.set()
    reader.join(timeout=10)

    assert not reader.is_alive()
    assert not search_errors
    old_snapshot_result = search_results[0].results[0]
    assert old_snapshot_result.meeting_id == old_meeting.meeting_id
    assert old_snapshot_result.meeting.title == "Launch Planning"
    assert set(old_snapshot_result.match_sources) == {
        RetrievalSource.FTS,
        RetrievalSource.TAG,
    }
    assert old_snapshot_result.matched_tags == ("pricing decision",)
    assert all(
        hit.meeting.id == old_meeting.meeting_id and hit.meeting.title == "Launch Planning"
        for hit in old_snapshot_result.evidence
    )
    assert any(
        not hit.is_context and "pricing decision" in hit.excerpt.text
        for hit in old_snapshot_result.evidence
    )
    assert any(hit.is_context for hit in old_snapshot_result.evidence)
    assert all(
        "atomic replacement marker" not in hit.excerpt.text.casefold()
        for hit in old_snapshot_result.evidence
    )

    new_core = MeetilyMemoryCore(index_path, state_path=state_path)
    new_tag_result = new_core.search("pricing decision", limit=1).results[0]
    assert new_tag_result.meeting_id == new_local_id
    assert new_tag_result.meeting.title == "Atomic Replacement Launch"
    assert new_tag_result.match_sources == (RetrievalSource.TAG,)
    assert new_tag_result.matched_tags == ("pricing decision",)
    assert new_tag_result.evidence == ()
    new_snapshot_result = new_core.search("atomic replacement marker", limit=1).results[0]
    assert new_snapshot_result.meeting_id == new_local_id
    assert new_snapshot_result.meeting.title == "Atomic Replacement Launch"
    assert any(
        "atomic replacement marker" in hit.excerpt.text.casefold()
        for hit in new_snapshot_result.evidence
    )


def test_obsidian_failure_is_post_publish_and_retry_clears_diagnostic(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()
    initialized = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env=env,
    )
    assert initialized.exit_code == 0
    update_app_settings(
        settings_path=settings_path,
        last_update_at="2000-01-01T00:00:00Z",
        obsidian=ObsidianSettings(
            vault_path=str(tmp_path / "vault"),
            sync_after_update=True,
        ),
    )
    _mutate_source(meetily_db, delete_second=False)

    def fail_obsidian(_index_path: Path, _vault_path: Path, _folder: str) -> ObsidianSyncResult:
        error_message = "credential=secret note=private"
        raise RuntimeError(error_message)

    monkeypatch.setattr(lifecycle_module, "sync_obsidian_vault", fail_obsidian)
    failed = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env=env,
    )

    assert failed.exit_code != 0
    assert isinstance(failed.exception, lifecycle_module.PostPublishRefreshError)
    _assert_sanitized_post_publish_exception(failed.exception, "secret")
    _assert_new_snapshot_is_searchable(index_path, deleted_on_success=False)
    run = _latest_scan_run(index_path)
    assert run["status"] == "completed"
    assert run["phase"] == "post_publish_obsidian_sync_failed"
    _assert_sanitized_persisted_run(run, "secret")
    diagnostic_payload = loads_json(str(run["errors_json"]))
    assert diagnostic_payload["index_status"] == "completed"
    assert diagnostic_payload["post_publish_status"] == "failed"
    assert diagnostic_payload["source_uuid"] == load_app_settings(settings_path).source_uuid
    assert diagnostic_payload["source_path"] == str(meetily_db.resolve(strict=True))
    assert diagnostic_payload["issues"][0]["phase"] == "obsidian_sync"
    assert diagnostic_payload["issues"][0]["retry_command"] == [
        "mm",
        "--index",
        str(index_path),
        "refresh",
        "--source",
        str(meetily_db.resolve(strict=True)),
    ]
    assert load_app_settings(settings_path).last_update_at != "2000-01-01T00:00:00Z"
    status = runner.invoke(app, ["--index", str(index_path), "status", "--json"], env=env)
    status_payload = loads_json(status.stdout)
    assert status_payload["last_failed_run"] is None
    assert status_payload["last_post_publish_error"]["id"] == run["id"]
    assert (
        status_payload["last_post_publish_error"]["post_publish"]["source_uuid"]
        == (diagnostic_payload["source_uuid"])
    )

    update_app_settings(
        settings_path=settings_path,
        obsidian=ObsidianSettings(
            vault_path=str(tmp_path / "vault"),
            sync_after_update=False,
        ),
    )
    not_attempted = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env=env,
    )
    assert not_attempted.exit_code == 0, not_attempted.output
    unresolved = runner.invoke(app, ["--index", str(index_path), "status", "--json"], env=env)
    assert loads_json(unresolved.stdout)["last_post_publish_error"]["id"] == run["id"]
    update_app_settings(
        settings_path=settings_path,
        obsidian=ObsidianSettings(
            vault_path=str(tmp_path / "vault"),
            sync_after_update=True,
        ),
    )

    def sync_while_lock_is_held(
        _index_path: Path,
        vault_path: Path,
        folder: str,
    ) -> ObsidianSyncResult:
        with pytest.raises(RefreshLockBusyError), RefreshLock(index_path):
            pass
        return ObsidianSyncResult(
            root_dir=vault_path / folder,
            files_written=0,
            files_skipped=0,
            files_removed=0,
        )

    monkeypatch.setattr(lifecycle_module, "sync_obsidian_vault", sync_while_lock_is_held)
    recovered = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env=env,
    )

    assert recovered.exit_code == 0, recovered.output
    recovered_status = runner.invoke(
        app,
        ["--index", str(index_path), "status", "--json"],
        env=env,
    )
    assert loads_json(recovered_status.stdout)["last_post_publish_error"] is None
    _assert_clean_index_family(index_path)


def test_settings_failure_is_post_publish_and_next_refresh_recovers(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()
    initialized = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env=env,
    )
    assert initialized.exit_code == 0
    update_app_settings(settings_path=settings_path, last_update_at="2000-01-01T00:00:00Z")
    _mutate_source(meetily_db, delete_second=False)
    original_update = lifecycle_module.update_app_settings

    def fail_settings(**_changes: object) -> None:
        error_message = "credential=secret settings=private"
        raise OSError(error_message)

    monkeypatch.setattr(lifecycle_module, "update_app_settings", fail_settings)
    failed = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env=env,
    )

    assert failed.exit_code != 0
    assert isinstance(failed.exception, lifecycle_module.PostPublishRefreshError)
    _assert_sanitized_post_publish_exception(failed.exception, "secret")
    _assert_new_snapshot_is_searchable(index_path, deleted_on_success=False)
    run = _latest_scan_run(index_path)
    assert run["status"] == "completed"
    assert run["phase"] == "post_publish_settings_update_failed"
    _assert_sanitized_persisted_run(run, "secret")
    diagnostic_payload = loads_json(str(run["errors_json"]))
    assert diagnostic_payload["source_path"] == str(meetily_db.resolve(strict=True))
    assert diagnostic_payload["issues"][0]["retry_command"] == [
        "mm",
        "--index",
        str(index_path),
        "refresh",
        "--source",
        str(meetily_db.resolve(strict=True)),
    ]
    assert load_app_settings(settings_path).last_update_at == "2000-01-01T00:00:00Z"
    status = runner.invoke(app, ["--index", str(index_path), "status", "--json"], env=env)
    status_payload = loads_json(status.stdout)
    assert status_payload["last_failed_run"] is None
    assert status_payload["last_post_publish_error"]["id"] == run["id"]

    monkeypatch.setattr(lifecycle_module, "update_app_settings", original_update)
    recovered = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env=env,
    )

    assert recovered.exit_code == 0, recovered.output
    assert load_app_settings(settings_path).last_update_at != "2000-01-01T00:00:00Z"
    recovered_status = runner.invoke(
        app,
        ["--index", str(index_path), "status", "--json"],
        env=env,
    )
    assert loads_json(recovered_status.stdout)["last_post_publish_error"] is None
    _assert_clean_index_family(index_path)


def test_custom_source_settings_failure_survives_unrelated_source_success(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    source_b = tmp_path / "source-b.sqlite"
    shutil.copy2(meetily_db, source_b)
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()
    source_a_refresh = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env=env,
    )
    assert source_a_refresh.exit_code == 0
    source_a_uuid = load_app_settings(settings_path).source_uuid
    original_update = lifecycle_module.update_app_settings

    def fail_settings(**_changes: object) -> None:
        error_message = "credential=secret source-b settings=private"
        raise OSError(error_message)

    monkeypatch.setattr(lifecycle_module, "update_app_settings", fail_settings)
    source_b_failure = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(source_b)],
        env=env,
    )

    assert source_b_failure.exit_code != 0
    assert isinstance(source_b_failure.exception, lifecycle_module.PostPublishRefreshError)
    _assert_sanitized_post_publish_exception(source_b_failure.exception, "secret")
    source_b_run = _latest_scan_run(index_path)
    _assert_sanitized_persisted_run(source_b_run, "secret")
    source_b_diagnostic = loads_json(str(source_b_run["errors_json"]))
    source_b_uuid = source_b_diagnostic["source_uuid"]
    retry_command = source_b_diagnostic["issues"][0]["retry_command"]
    assert source_b_uuid != source_a_uuid
    assert source_b_diagnostic["source_path"] == str(source_b.resolve(strict=True))
    assert retry_command == [
        "mm",
        "--index",
        str(index_path),
        "refresh",
        "--source",
        str(source_b.resolve(strict=True)),
    ]

    monkeypatch.setattr(lifecycle_module, "update_app_settings", original_update)
    unrelated_success = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env=env,
    )
    assert unrelated_success.exit_code == 0, unrelated_success.output
    assert load_app_settings(settings_path).source_uuid == source_a_uuid
    unresolved = runner.invoke(app, ["--index", str(index_path), "status", "--json"], env=env)
    unresolved_payload = loads_json(unresolved.stdout)
    assert unresolved_payload["last_completed_run"]["id"] > source_b_run["id"]
    assert unresolved_payload["last_post_publish_error"]["id"] == source_b_run["id"]
    assert unresolved_payload["last_post_publish_error"]["post_publish"]["source_uuid"] == (
        source_b_uuid
    )

    retried = runner.invoke(
        app,
        retry_command[1:],
        env=env,
    )

    assert retried.exit_code == 0, retried.output
    assert load_app_settings(settings_path).source_uuid == source_b_uuid
    resolved = runner.invoke(app, ["--index", str(index_path), "status", "--json"], env=env)
    assert loads_json(resolved.stdout)["last_post_publish_error"] is None
    _assert_clean_index_family(index_path)


def test_init_settings_failure_is_sanitized_and_has_exact_retry(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()
    original_update = lifecycle_module.update_app_settings

    def fail_settings(**_changes: object) -> None:
        error_message = "credential=secret init-settings=private"
        raise OSError(error_message)

    monkeypatch.setattr(lifecycle_module, "update_app_settings", fail_settings)
    failed = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "init",
            "--source",
            str(meetily_db),
        ],
        env=env,
    )

    assert failed.exit_code != 0
    assert isinstance(failed.exception, lifecycle_module.PostPublishRefreshError)
    _assert_sanitized_post_publish_exception(failed.exception, "secret")
    assert IndexRepository.open_existing(index_path).search("pricing decision")
    run = _latest_scan_run(index_path)
    assert run["status"] == "completed"
    assert run["phase"] == "post_publish_settings_update_failed"
    _assert_sanitized_persisted_run(run, "secret")
    diagnostic = loads_json(str(run["errors_json"]))
    retry_command = diagnostic["issues"][0]["retry_command"]
    assert diagnostic["source_path"] == str(meetily_db.resolve(strict=True))
    assert retry_command == [
        "mm",
        "--index",
        str(index_path),
        "init",
        "--source",
        str(meetily_db.resolve(strict=True)),
    ]

    monkeypatch.setattr(lifecycle_module, "update_app_settings", original_update)
    retried = runner.invoke(
        app,
        retry_command[1:],
        env=env,
    )

    assert retried.exit_code == 0, retried.output
    status = runner.invoke(app, ["--index", str(index_path), "status", "--json"], env=env)
    assert loads_json(status.stdout)["last_post_publish_error"] is None
    _assert_clean_index_family(index_path)
