import sqlite3
from pathlib import Path

from meetily_memory.db.migrations import migrate_to_v1, migrate_to_v2, migrate_to_v3
from meetily_memory.db.repository import IndexRepository
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.user_state import UserStateRepository


def test_legacy_task_status_migrates_to_persistent_state_before_index_schema(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _create_v3_index_with_task_status(index_path)

    repo = IndexRepository(index_path, state_path=state_path)
    tasks = repo.list_structured_entity_details("action_items")
    report = UserStateRepository(state_path).latest_migration_report()

    assert tasks[0]["status"] == "done"
    assert tasks[0]["status_note"] == "verified by user"
    assert report == {"migrated": 1, "orphaned": 0}
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert (
            conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'task_status_overrides'
                """
            ).fetchone()
            is None
        )


def test_task_status_survives_disposable_index_rebuild(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    repo = IndexRepository(index_path, state_path=state_path)
    task = repo.list_structured_entity_details("action_items")[0]
    repo.set_task_status(task["id"], "done", note="keep me")

    index_path.unlink()
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    rebuilt = IndexRepository(index_path, state_path=state_path)
    matching = [
        row
        for row in rebuilt.list_structured_entity_details("action_items", limit=100)
        if row["text"] == task["text"]
    ]

    assert matching[0]["status"] == "done"
    assert matching[0]["status_note"] == "keep me"


def test_unmatched_legacy_status_is_preserved_as_orphan(tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _create_v3_index_with_task_status(index_path, chunk_external_id=None)

    IndexRepository(index_path, state_path=state_path)
    state = UserStateRepository(state_path)

    assert state.latest_migration_report() == {"migrated": 0, "orphaned": 1}
    assert state.list_orphans()[0]["status"] == "done"


def test_source_uuid_survives_explicit_path_update(tmp_path: Path) -> None:
    state = UserStateRepository(tmp_path / "state.sqlite")
    source_uuid = state.get_or_create_source("meetily_sqlite", "/old/source.sqlite", now="1")

    state.update_source_path(source_uuid, "/new/source.sqlite", now="2")

    assert (
        state.get_or_create_source("meetily_sqlite", "/new/source.sqlite", now="3") == source_uuid
    )


def _create_v3_index_with_task_status(
    index_path: Path, *, chunk_external_id: str | None = "chunk-1"
) -> None:
    with sqlite3.connect(index_path) as conn:
        migrate_to_v1(conn)
        migrate_to_v2(conn)
        migrate_to_v3(conn)
        conn.execute("PRAGMA user_version = 3")
        conn.execute(
            """
            INSERT INTO sources (id, kind, path, created_at, updated_at)
            VALUES (1, 'meetily_sqlite', '/tmp/source.sqlite', 'now', 'now')
            """
        )
        conn.execute(
            """
            INSERT INTO meetings (
              id, source_id, external_id, title, fingerprint, indexed_at
            ) VALUES (1, 1, 'meeting-1', 'Meeting', 'meeting-fp', 'now')
            """
        )
        conn.execute(
            """
            INSERT INTO chunks (
              id, meeting_id, external_id, kind, ordinal, text, fingerprint
            ) VALUES (1, 1, ?, 'transcript', 0, 'Ship migration plan.', 'chunk-fp')
            """,
            (chunk_external_id,),
        )
        conn.execute(
            """
            INSERT INTO action_items (
              id, meeting_id, source_chunk_id, ordinal, text, source, confidence,
              fingerprint, created_at, updated_at
            ) VALUES (
              1, 1, 1, 0, 'Ship migration plan.', 'heuristic', 0.55,
              'entity-fp', 'now', 'now'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO task_status_overrides (
              action_item_id, status, note, source, created_at, updated_at
            ) VALUES (1, 'done', 'verified by user', 'manual', 'now', 'now')
            """
        )
        conn.commit()
