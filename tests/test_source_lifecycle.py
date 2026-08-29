import json
import multiprocessing
import shutil
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

import pytest
from typer.testing import CliRunner

import meetily_memory.scanner.meetily_sqlite as scanner_module
from meetily_memory.cli.app import app
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.db.repository import IndexRepository
from meetily_memory.json_codec import loads_json
from meetily_memory.refresh_lock import RefreshLock
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.structure_analyzer import StructureAnalyzer
from meetily_memory.tagging import TagService
from meetily_memory.user_state import UserStateRepository


class EventLike(Protocol):
    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


def hold_refresh_lock(index_path: Path, ready: EventLike, release: EventLike) -> None:
    with RefreshLock(index_path):
        ready.set()
        release.wait(timeout=10)


def test_legacy_source_path_is_read_only_in_status_and_migrates_on_refresh(
    meetily_db: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    source_uuid = UserStateRepository(data_dir / "state.sqlite").get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(meetily_db),
        now="legacy-exact-state-binding",
    )
    legacy_settings = {"source_path": str(meetily_db)}
    settings_path.write_text(json.dumps(legacy_settings) + "\n")
    runner = CliRunner()
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}

    first = runner.invoke(app, ["--index", str(index_path), "status", "--json"], env=env)
    second = runner.invoke(app, ["--index", str(index_path), "status", "--json"], env=env)

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert loads_json(settings_path.read_text()) == legacy_settings
    assert json.loads(second.stdout)["source_path"] == str(meetily_db)
    assert not index_path.exists()
    assert (data_dir / "state.sqlite").exists()

    refresh = runner.invoke(app, ["--index", str(index_path), "refresh"], env=env)
    migrated_settings = loads_json(settings_path.read_text())

    assert refresh.exit_code == 0
    assert migrated_settings["source_uuid"] == source_uuid
    assert "source_path" not in migrated_settings
    with sqlite3.connect(data_dir / "state.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1


def test_explicit_index_keeps_source_settings_out_of_user_data(
    meetily_db: Path, tmp_path: Path
) -> None:
    fake_home = tmp_path / "home"
    user_data_dir = fake_home / "Library" / "Application Support" / "meetily-memory"
    user_data_dir.mkdir(parents=True)
    user_settings = user_data_dir / "settings.json"
    user_settings.write_text(json.dumps({"ui_language": "ru"}) + "\n")
    workspace = tmp_path / "workspace"
    index_path = workspace / "index.sqlite"
    runner = CliRunner()

    refresh = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env={"HOME": str(fake_home), "MEETILY_MEMORY_DATA_DIR": ""},
    )

    assert refresh.exit_code == 0
    assert loads_json(user_settings.read_text()) == {"ui_language": "ru"}
    workspace_settings = loads_json((workspace / "settings.json").read_text())
    assert workspace_settings["source_uuid"]
    assert workspace_settings["last_update_at"]


def test_explicit_rebind_preserves_identity_evidence_and_task_state(
    meetily_db: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    moved_db = tmp_path / "moved" / "meeting_minutes.sqlite"
    moved_db.parent.mkdir()
    shutil.copyfile(meetily_db, moved_db)
    runner = CliRunner()
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}

    init = runner.invoke(
        app,
        ["--index", str(index_path), "init", "--source", str(meetily_db), "--no-autosync"],
        env=env,
    )
    assert init.exit_code == 0
    StructureAnalyzer(IndexRepository(index_path)).analyze_all()
    before = MeetilyMemoryCore(index_path)
    before_result = before.search("migration risks", limit=1).results[0]
    meeting_ref = before_result.meeting.ref
    evidence_id = before_result.evidence[0].id
    task = before.structured_entities("action_items").entities[0]
    before.set_task_status(task.id, "done", note="survives move")
    TagService(IndexRepository(index_path)).assign(("1",), ("Сбер",))
    original_uuid = loads_json((data_dir / "settings.json").read_text())["source_uuid"]

    rebind = runner.invoke(
        app,
        ["--index", str(index_path), "config", "source", str(moved_db), "--rebind"],
        env=env,
    )
    scan = runner.invoke(app, ["--index", str(index_path), "scan"], env=env)

    assert rebind.exit_code == 0
    assert f"old source path: {meetily_db}" in rebind.stdout
    assert f"new source path: {moved_db}" in rebind.stdout
    assert "matching meetings: 2" in rebind.stdout
    assert scan.exit_code == 0
    settings = loads_json((data_dir / "settings.json").read_text())
    assert settings["source_uuid"] == original_uuid
    assert "source_path" not in settings
    after = MeetilyMemoryCore(index_path)
    after_result = after.search("migration risks", limit=1).results[0]
    assert after_result.meeting.ref == meeting_ref
    assert after_result.evidence[0].id == evidence_id
    matching_tasks = [
        entity
        for entity in after.structured_entities("action_items", limit=100).entities
        if entity.text == task.text
    ]
    assert matching_tasks[0].status == "done"
    assert matching_tasks[0].status_note == "survives move"
    assert [
        tag.display_name for tag in TagService(IndexRepository(index_path)).list_for_meeting("1")
    ] == ["Сбер"]
    with sqlite3.connect(index_path) as conn:
        sources = conn.execute("SELECT path FROM sources").fetchall()
    assert sources == [(str(moved_db),)]


def test_source_selection_without_rebind_uses_a_distinct_uuid(
    meetily_db: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    other_db = tmp_path / "other.sqlite"
    shutil.copyfile(meetily_db, other_db)
    runner = CliRunner()
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner.invoke(
        app,
        ["--index", str(index_path), "init", "--source", str(meetily_db), "--no-autosync"],
        env=env,
    )
    old_uuid = loads_json((data_dir / "settings.json").read_text())["source_uuid"]

    selected = runner.invoke(
        app,
        ["--index", str(index_path), "config", "source", str(other_db)],
        env=env,
    )

    assert selected.exit_code == 0
    new_uuid = loads_json((data_dir / "settings.json").read_text())["source_uuid"]
    assert new_uuid != old_uuid
    assert "source_path" not in loads_json((data_dir / "settings.json").read_text())


def test_explicit_rebind_allows_zero_overlap_without_creating_a_new_source(
    meetily_db: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    incompatible_db = tmp_path / "incompatible.sqlite"
    shutil.copyfile(meetily_db, incompatible_db)
    with sqlite3.connect(incompatible_db) as conn:
        conn.execute("UPDATE meetings SET id = 'other-' || id")
        conn.execute("UPDATE transcripts SET meeting_id = 'other-' || meeting_id")
        conn.execute("UPDATE summary_processes SET meeting_id = 'other-' || meeting_id")
        conn.execute("UPDATE meeting_notes SET meeting_id = 'other-' || meeting_id")
        conn.commit()
    runner = CliRunner()
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner.invoke(
        app,
        ["--index", str(index_path), "init", "--source", str(meetily_db), "--no-autosync"],
        env=env,
    )
    original_uuid = loads_json((data_dir / "settings.json").read_text())["source_uuid"]

    rebind = runner.invoke(
        app,
        ["--index", str(index_path), "config", "source", str(incompatible_db), "--rebind"],
        env=env,
    )

    assert rebind.exit_code == 0
    assert "matching meetings: 0" in rebind.stdout
    assert loads_json((data_dir / "settings.json").read_text())["source_uuid"] == original_uuid
    with sqlite3.connect(data_dir / "state.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        assert conn.execute("SELECT uuid, current_path FROM sources").fetchone() == (
            original_uuid,
            str(incompatible_db.resolve(strict=True)),
        )
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("SELECT path FROM sources").fetchone()[0] == str(
            incompatible_db.resolve(strict=True)
        )


def test_rebind_accepts_a_partial_copy_with_one_matching_meeting(
    meetily_db: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    partial_db = tmp_path / "partial.sqlite"
    shutil.copyfile(meetily_db, partial_db)
    with sqlite3.connect(partial_db) as conn:
        conn.execute("DELETE FROM transcripts WHERE meeting_id = 'meeting-2'")
        conn.execute("DELETE FROM meeting_notes WHERE meeting_id = 'meeting-2'")
        conn.execute("DELETE FROM meetings WHERE id = 'meeting-2'")
        conn.commit()
    runner = CliRunner()
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner.invoke(
        app,
        ["--index", str(index_path), "init", "--source", str(meetily_db), "--no-autosync"],
        env=env,
    )

    rebind = runner.invoke(
        app,
        ["--index", str(index_path), "config", "source", str(partial_db), "--rebind"],
        env=env,
    )

    assert rebind.exit_code == 0
    assert "matching meetings: 1" in rebind.stdout


def test_refresh_lock_blocks_a_second_writer_and_is_released_on_process_exit(
    meetily_db: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(target=hold_refresh_lock, args=(index_path, ready, release))
    process.start()
    assert ready.wait(timeout=10)

    runner = CliRunner()
    autosync = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "refresh",
            "--source",
            str(meetily_db),
            "--autosync-run",
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )
    blocked = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )
    blocked_selection = runner.invoke(
        app,
        ["--index", str(index_path), "config", "source", str(meetily_db)],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )
    blocked_rebind = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(meetily_db),
            "--rebind",
            "--source-uuid",
            "missing-while-locked",
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert autosync.exit_code == 0
    assert "autosync skipped" in autosync.output.lower()
    assert f"pid {process.pid}" in autosync.output.lower()
    assert blocked.exit_code != 0
    assert "refresh is already running" in blocked.output.lower()
    assert f"pid {process.pid}" in blocked.output.lower()
    assert "acquired at" in blocked.output.lower()
    assert blocked_selection.exit_code != 0
    assert "refresh is already running" in blocked_selection.output.lower()
    assert blocked_rebind.exit_code != 0
    assert "refresh is already running" in blocked_rebind.output.lower()
    assert not index_path.exists()
    assert not (data_dir / "state.sqlite").exists()
    assert not (data_dir / "settings.json").exists()

    process.terminate()
    process.join(timeout=10)
    assert process.exitcode is not None
    assert process.exitcode != 0
    assert (data_dir / "refresh.lock").exists()

    recovered = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert recovered.exit_code == 0
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0] == 2


def test_failed_refresh_records_phase_preserves_last_update_and_recovers(
    meetily_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()
    first = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env=env,
    )
    assert first.exit_code == 0
    settings = loads_json(settings_path.read_text())
    settings["last_update_at"] = "2020-01-01T00:00:00Z"
    settings_path.write_text(json.dumps(settings) + "\n", encoding="utf-8")

    def fail_before_second_meeting(checkpoint: str) -> None:
        if checkpoint == "before_meeting:2":
            error_message = "credential=secret transcript=private"
            raise RuntimeError(error_message)

    monkeypatch.setattr(scanner_module, "_scan_checkpoint", fail_before_second_meeting)
    failed = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env=env,
    )

    assert failed.exit_code != 0
    assert loads_json(settings_path.read_text())["last_update_at"] == "2020-01-01T00:00:00Z"
    with sqlite3.connect(index_path) as conn:
        conn.row_factory = sqlite3.Row
        failed_run = dict(
            conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        )
    assert failed_run["status"] == "failed"
    assert failed_run["phase"] == "source_scan"
    assert failed_run["finished_at"] is not None
    assert "RuntimeError" in failed_run["error_message"]
    assert "secret" not in failed_run["error_message"]
    assert failed_run["meetings_seen"] == 1

    status = runner.invoke(app, ["--index", str(index_path), "status", "--json"], env=env)
    status_payload = loads_json(status.stdout)
    assert status_payload["last_completed_run"]["status"] == "completed"
    assert status_payload["last_failed_run"]["id"] == failed_run["id"]

    monkeypatch.setattr(scanner_module, "_scan_checkpoint", lambda _checkpoint: None)
    recovered = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env=env,
    )

    assert recovered.exit_code == 0
    assert loads_json(settings_path.read_text())["last_update_at"] != "2020-01-01T00:00:00Z"
    with sqlite3.connect(index_path) as conn:
        latest_status = conn.execute(
            "SELECT status FROM scan_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert latest_status == "completed"
    recovered_status = runner.invoke(app, ["--index", str(index_path), "doctor", "--json"], env=env)
    recovered_payload = loads_json(recovered_status.stdout)
    assert recovered_payload["last_completed_run"]["status"] == "completed"
    assert recovered_payload["last_failed_run"] is None


def test_status_observes_running_scan_and_refresh_marks_it_failed(
    meetily_db: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()
    refresh = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env=env,
    )
    assert refresh.exit_code == 0
    with sqlite3.connect(index_path) as conn:
        source_id = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
        cursor = conn.execute(
            """
            INSERT INTO scan_runs (source_id, started_at, status, phase)
            VALUES (?, '2020-01-01T00:00:00Z', 'running', 'source_scan')
            """,
            (source_id,),
        )
        abandoned_run_id = cursor.lastrowid
        conn.commit()

    status = runner.invoke(app, ["--index", str(index_path), "status", "--json"], env=env)

    assert status.exit_code == 0
    payload = loads_json(status.stdout)
    assert payload["last_failed_run"] is None
    assert payload["last_running_run"]["id"] == abandoned_run_id
    with sqlite3.connect(index_path) as conn:
        observed = conn.execute(
            "SELECT status, finished_at, error_message FROM scan_runs WHERE id = ?",
            (abandoned_run_id,),
        ).fetchone()
    assert observed == ("running", None, None)

    recovered = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env=env,
    )

    assert recovered.exit_code == 0
    with sqlite3.connect(index_path) as conn:
        abandoned = conn.execute(
            "SELECT status, finished_at, error_message FROM scan_runs WHERE id = ?",
            (abandoned_run_id,),
        ).fetchone()
    assert abandoned[0] == "failed"
    assert abandoned[1] is not None
    assert abandoned[2] == "Previous refresh ended before completion."


def test_successful_scan_reconciles_deleted_meeting_and_restores_its_tag(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    scanner = MeetilySQLiteScanner(index_path)
    scanner.scan(meetily_db)
    repo = IndexRepository(index_path)
    deleted_meeting = repo.get_meeting("meeting-2")
    assert deleted_meeting is not None
    deleted_meeting_id = int(deleted_meeting["id"])
    tag_service = TagService(repo)
    tag_service.assign((str(deleted_meeting_id),), ("Сбер",))
    with sqlite3.connect(index_path) as conn:
        deleted_chunk_ids = {
            int(row[0])
            for row in conn.execute(
                """
                SELECT id
                FROM chunks
                WHERE meeting_id = ?
                """,
                (deleted_meeting_id,),
            )
        }
    with sqlite3.connect(meetily_db) as conn:
        source_meeting = conn.execute("SELECT * FROM meetings WHERE id = 'meeting-2'").fetchone()
        assert source_meeting is not None
        conn.execute("DELETE FROM meetings WHERE id = 'meeting-2'")
        conn.commit()

    scanner.scan(meetily_db)

    core = MeetilyMemoryCore(index_path)
    assert {meeting.external_id for meeting in core.meetings()} == {"meeting-1"}
    assert core.search("migration risks").results == ()
    assert core.search("Сбер").results == ()
    assert core.get_meeting("meeting-2") is None
    assert tag_service.orphaned_assignment_count() == 1
    assert len(tag_service.repository.list_assignments()) == 1
    with sqlite3.connect(index_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM chunks_fts WHERE meeting_id = ?", (deleted_meeting_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM knowledge_edges WHERE source_meeting_id = ?",
                (deleted_meeting_id,),
            ).fetchone()[0]
            == 0
        )
        stale_keys = {f"meeting:{deleted_meeting_id}"} | {
            f"chunk:{chunk_id}" for chunk_id in deleted_chunk_ids
        }
        assert not stale_keys.intersection(
            str(row[0]) for row in conn.execute("SELECT stable_key FROM knowledge_nodes")
        )

    with sqlite3.connect(meetily_db) as conn:
        placeholders = ", ".join("?" for _ in source_meeting)
        conn.execute(
            f"INSERT INTO meetings VALUES ({placeholders})",  # noqa: S608
            source_meeting,
        )
        conn.commit()
    scanner.scan(meetily_db)

    restored = repo.get_meeting("meeting-2")
    assert restored is not None
    assert [tag.display_name for tag in tag_service.list_for_meeting(str(restored["id"]))] == [
        "Сбер"
    ]
    assert tag_service.orphaned_assignment_count() == 0


def test_failed_source_read_does_not_reconcile_missing_meetings(
    meetily_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index_path = tmp_path / "index.sqlite"
    scanner = MeetilySQLiteScanner(index_path)
    scanner.scan(meetily_db)
    with sqlite3.connect(meetily_db) as conn:
        conn.execute("DELETE FROM meetings WHERE id = 'meeting-2'")
        conn.commit()
    original_read = MeetilySQLiteScanner._read_meetings  # noqa: SLF001
    error_message = "interrupted source read"

    def interrupted_read(
        self: MeetilySQLiteScanner, conn: sqlite3.Connection
    ) -> Iterable[dict[str, object]]:
        yield from original_read(self, conn)
        raise RuntimeError(error_message)

    monkeypatch.setattr(MeetilySQLiteScanner, "_read_meetings", interrupted_read)

    with pytest.raises(RuntimeError, match="interrupted source read"):
        scanner.scan(meetily_db)

    repo = IndexRepository(index_path)
    assert {str(row["external_id"]) for row in repo.list_meetings()} == {
        "meeting-1",
        "meeting-2",
    }
    assert repo.search("migration risks")[0]["meeting_external_id"] == "meeting-2"
