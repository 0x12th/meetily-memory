import multiprocessing
import os
import sqlite3
from collections.abc import Callable
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnProcess
from pathlib import Path

import pytest

from meetily_memory import user_state
from meetily_memory.db import migrations
from meetily_memory.db.migration_identity import canonical_database_path
from meetily_memory.db.migrations import CURRENT_SCHEMA_VERSION
from meetily_memory.db.repository import IndexRepository
from meetily_memory.user_state import (
    TAG_STATE_SCHEMA,
    USER_STATE_SCHEMA,
    UserStateRepository,
    task_identity,
)


class InjectedMigrationError(RuntimeError):
    pass


# v1-v5 are the complete compatible in-place set. The first incompatible future
# format must add side-by-side replacement tests instead of extending this list.
IN_PLACE_SCHEMA_VERSIONS = tuple(range(1, CURRENT_SCHEMA_VERSION + 1))
V4_DESTRUCTIVE_CHECKPOINTS = (
    "v4:task_status_overrides:dropped",
    "v4:decisions:renamed",
    "v4:decisions:legacy_dropped",
    "v4:action_items:renamed",
    "v4:action_items:legacy_dropped",
    "v4:risks:renamed",
    "v4:risks:legacy_dropped",
    "v4:open_questions:renamed",
    "v4:open_questions:legacy_dropped",
    "v4:user_state_migration_ready:dropped",
)
STATE_TRANSFER_CHECKPOINTS = (
    "source",
    "migrated_task",
    "orphan",
    "report",
    "state_committed",
    "state_verified",
    "index_marker",
    "index_ready",
)
STATE_PRECOMMIT_CHECKPOINTS = {"source", "migrated_task", "orphan", "report"}
ENTITY_TABLES = ("decisions", "action_items", "risks", "open_questions")
LEGACY_SOURCE_PATH = "/tmp/source.sqlite"  # noqa: S108 - synthetic legacy fixture identity.


def _fail_once_at(checkpoint: str) -> Callable[[str], None]:
    triggered = False

    def fail(name: str) -> None:
        nonlocal triggered
        if name == checkpoint and not triggered:
            triggered = True
            raise InjectedMigrationError(checkpoint)

    return fail


def _no_fault(_name: str) -> None:
    return


CHILD_CRASH_EXIT_CODE = 91


def _child_open_repository(index_path: str, state_path: str) -> None:
    IndexRepository(Path(index_path), state_path=Path(state_path))


def _child_open_repository_after_release(
    index_path: str,
    state_path: str,
    release: Connection,
) -> None:
    release.recv()
    release.close()
    _child_open_repository(index_path, state_path)


def _child_crash_during_upgrade(
    index_path: str,
    state_path: str,
    checkpoint_owner: str,
    checkpoint: str,
) -> None:
    def crash(name: str) -> None:
        if name == checkpoint:
            os._exit(CHILD_CRASH_EXIT_CODE)

    owner = migrations if checkpoint_owner == "migration" else user_state
    attribute = (
        "_migration_checkpoint" if checkpoint_owner == "migration" else "_state_transfer_checkpoint"
    )
    setattr(owner, attribute, crash)
    _child_open_repository(index_path, state_path)


def _assert_process_exit(
    process: SpawnProcess,
    expected_exit_code: int,
) -> None:
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("migration child process did not exit")
    assert process.exitcode == expected_exit_code


def _database_dump(database_path: Path) -> tuple[int, tuple[str, ...]]:
    with sqlite3.connect(database_path) as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        return version, tuple(conn.iterdump())


def _assert_database_ok(database_path: Path) -> None:
    with sqlite3.connect(database_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    query = f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - fixed test table names.
    return int(conn.execute(query).fetchone()[0])


def _create_index_at_version(index_path: Path, version: int) -> None:
    with sqlite3.connect(index_path) as conn:
        for target_version in range(1, version + 1):
            migrations.MIGRATIONS[target_version](conn)


def _create_mixed_v3_index(index_path: Path) -> None:
    _create_index_at_version(index_path, 3)
    with sqlite3.connect(index_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            INSERT INTO sources (id, kind, path, created_at, updated_at)
            VALUES (1, 'meetily_sqlite', '/tmp/source.sqlite', 'source-created', 'source-updated')
            """
        )
        conn.execute(
            """
            INSERT INTO meetings (
              id, source_id, external_id, title, fingerprint, indexed_at
            ) VALUES (1, 1, 'meeting-1', 'Migration meeting', 'meeting-fp', 'indexed')
            """
        )
        conn.execute(
            """
            INSERT INTO chunks (
              id, meeting_id, external_id, kind, ordinal, text, fingerprint
            ) VALUES (
              1, 1, 'chunk-1', 'transcript', 0, 'Migration evidence', 'chunk-fp'
            )
            """
        )
        for table in ENTITY_TABLES:
            insert_sql = f"""
                INSERT INTO {table} (
                  id, meeting_id, source_chunk_id, ordinal, text, source, confidence,
                  fingerprint, created_at, updated_at
                ) VALUES (?, 1, ?, ?, ?, 'heuristic', 0.75, ?, 'created', 'updated')
                """  # noqa: S608 - table names come from the fixed test tuple above.
            conn.executemany(
                insert_sql,
                (
                    (1, 1, 0, f"{table} with evidence", f"{table}-valid"),
                    (2, None, 1, f"{table} without evidence", f"{table}-orphan"),
                ),
            )
        conn.executemany(
            """
            INSERT INTO task_status_overrides (
              action_item_id, status, note, source, created_at, updated_at
            ) VALUES (?, ?, ?, 'manual', ?, ?)
            """,
            (
                (1, "done", "migrated state", "state-created-1", "state-updated-1"),
                (2, "blocked", "orphan state", "state-created-2", "state-updated-2"),
            ),
        )
        conn.commit()


def _create_populated_v2_state(index_path: Path, state_path: Path) -> None:
    source_uuid = "legacy-source-uuid"
    active_fingerprint = task_identity(
        source_uuid,
        "meeting-1",
        "chunk-1",
        "action_items with evidence",
    ).content_fingerprint
    orphan_fingerprint = task_identity(
        source_uuid,
        "meeting-1",
        "unused",
        "action_items without evidence",
    ).content_fingerprint
    with sqlite3.connect(state_path) as conn:
        conn.executescript(USER_STATE_SCHEMA)
        conn.executescript(TAG_STATE_SCHEMA)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            INSERT INTO sources (uuid, kind, current_path, created_at, updated_at)
            VALUES (?, 'meetily_sqlite', ?, 'source-created', 'source-updated')
            """,
            (source_uuid, LEGACY_SOURCE_PATH),
        )
        conn.execute(
            """
            INSERT INTO task_states (
              source_uuid, meeting_external_id, chunk_external_id,
              entity_kind, content_fingerprint, status, note, source,
              orphaned, orphaned_reason, legacy_action_item_id,
              created_at, updated_at
            ) VALUES (
              ?, 'meeting-1', 'chunk-1', 'task', ?, 'done', 'migrated state', 'manual',
              0, NULL, 1, 'state-updated-1', 'state-updated-1'
            )
            """,
            (source_uuid, active_fingerprint),
        )
        conn.execute(
            """
            INSERT INTO task_states (
              source_uuid, meeting_external_id, chunk_external_id,
              entity_kind, content_fingerprint, status, note, source,
              orphaned, orphaned_reason, legacy_action_item_id,
              created_at, updated_at
            ) VALUES (
              ?, 'meeting-1', NULL, 'task', ?, 'blocked', 'orphan state', 'manual',
              1, 'missing chunk_external_id', 2, 'state-created-2', 'state-updated-2'
            )
            """,
            (source_uuid, orphan_fingerprint),
        )
        conn.execute(
            """
            INSERT INTO migration_reports (
              index_path, migrated, orphaned, created_at
            ) VALUES (?, 1, 1, 'legacy-report')
            """,
            (str(index_path),),
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()


def _assert_v3_rows_and_schema_are_preserved(index_path: Path) -> None:
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM task_status_overrides").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM action_items").fetchone()[0] == 2
        statuses = conn.execute(
            "SELECT status FROM task_status_overrides ORDER BY action_item_id"
        ).fetchall()
        assert statuses == [("done",), ("blocked",)]
        for table in ENTITY_TABLES:
            columns = {
                str(row[1]): int(row[3])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            assert columns["source_chunk_id"] == 0
            assert _table_count(conn, table) == 2
            legacy_table = f"{table}_v3"
            assert (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (legacy_table,),
                ).fetchone()
                is None
            )
    _assert_database_ok(index_path)


def _assert_current_v4_rewrite(index_path: Path) -> None:
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        for removed_table in ("task_status_overrides", "user_state_migration_ready"):
            assert (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (removed_table,),
                ).fetchone()
                is None
            )
        for table in ENTITY_TABLES:
            columns = {
                str(row[1]): int(row[3])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            assert columns["source_chunk_id"] == 1
            assert _table_count(conn, table) == 1
    _assert_database_ok(index_path)


def _state_counts(state_path: Path) -> tuple[int, int, int, int, int]:
    with sqlite3.connect(state_path) as conn:
        sources = int(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0])
        active = int(
            conn.execute("SELECT COUNT(*) FROM task_states WHERE orphaned = 0").fetchone()[0]
        )
        orphaned = int(
            conn.execute("SELECT COUNT(*) FROM task_states WHERE orphaned = 1").fetchone()[0]
        )
        reports = int(conn.execute("SELECT COUNT(*) FROM migration_reports").fetchone()[0])
        total = int(conn.execute("SELECT COUNT(*) FROM task_states").fetchone()[0])
        return sources, active, orphaned, reports, total


def _assert_single_verified_state_transfer(state_path: Path) -> None:
    assert _state_counts(state_path) == (1, 1, 1, 1, 2)
    with sqlite3.connect(state_path) as conn:
        assert conn.execute(
            """
            SELECT migrated, orphaned, migration_key IS NOT NULL
            FROM migration_reports
            """
        ).fetchall() == [(1, 1, 1)]
        assert conn.execute(
            """
            SELECT
              legacy_action_item_id, outcome,
              length(legacy_intent_digest), length(task_identity_digest)
            FROM migration_report_items
            ORDER BY legacy_action_item_id
            """
        ).fetchall() == [
            (1, "active_inserted", 64, 64),
            (2, "orphan_missing_identity", 64, 64),
        ]
        assert conn.execute(
            "SELECT status, legacy_action_item_id FROM task_states WHERE orphaned = 0"
        ).fetchall() == [("done", 1)]
        assert conn.execute(
            "SELECT status, legacy_action_item_id FROM task_states WHERE orphaned = 1"
        ).fetchall() == [("blocked", 2)]
    _assert_database_ok(state_path)


def _prepare_v3_with_durable_transfer_marker(
    index_path: Path,
    state_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_mixed_v3_index(index_path)
    monkeypatch.setattr(
        user_state,
        "_state_transfer_checkpoint",
        _fail_once_at("index_ready"),
    )
    with pytest.raises(InjectedMigrationError, match="index_ready"):
        IndexRepository(index_path, state_path=state_path)
    monkeypatch.setattr(user_state, "_state_transfer_checkpoint", _no_fault)


@pytest.mark.parametrize("target_version", IN_PLACE_SCHEMA_VERSIONS)
def test_each_supported_index_migration_rolls_back_user_version_and_retries_publicly(
    target_version: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _create_index_at_version(index_path, target_version - 1)
    before = _database_dump(index_path)
    monkeypatch.setattr(
        migrations,
        "_migration_checkpoint",
        _fail_once_at(f"v{target_version}:after_user_version"),
    )

    with sqlite3.connect(index_path) as conn, pytest.raises(InjectedMigrationError):
        migrations.MIGRATIONS[target_version](conn)

    assert _database_dump(index_path) == before
    monkeypatch.setattr(migrations, "_migration_checkpoint", _no_fault)
    IndexRepository(index_path, state_path=state_path)

    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    _assert_database_ok(index_path)


@pytest.mark.parametrize("checkpoint", V4_DESTRUCTIVE_CHECKPOINTS)
def test_each_destructive_v4_step_rolls_back_then_retries_through_index_repository(
    checkpoint: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _create_mixed_v3_index(index_path)
    monkeypatch.setattr(migrations, "_migration_checkpoint", _fail_once_at(checkpoint))

    with pytest.raises(InjectedMigrationError, match=checkpoint):
        IndexRepository(index_path, state_path=state_path)

    _assert_v3_rows_and_schema_are_preserved(index_path)
    _assert_single_verified_state_transfer(state_path)
    monkeypatch.setattr(migrations, "_migration_checkpoint", _no_fault)

    IndexRepository(index_path, state_path=state_path)

    _assert_current_v4_rewrite(index_path)
    _assert_single_verified_state_transfer(state_path)


@pytest.mark.parametrize("checkpoint", STATE_TRANSFER_CHECKPOINTS)
def test_each_state_transfer_checkpoint_is_idempotent_on_public_retry(
    checkpoint: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _create_mixed_v3_index(index_path)
    monkeypatch.setattr(user_state, "_state_transfer_checkpoint", _fail_once_at(checkpoint))

    with pytest.raises(InjectedMigrationError, match=checkpoint):
        IndexRepository(index_path, state_path=state_path)

    _assert_v3_rows_and_schema_are_preserved(index_path)
    if checkpoint in STATE_PRECOMMIT_CHECKPOINTS:
        assert _state_counts(state_path) == (0, 0, 0, 0, 0)
    else:
        _assert_single_verified_state_transfer(state_path)
    with sqlite3.connect(index_path) as conn:
        marker_exists = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'user_state_migration_ready'
            """
        ).fetchone()
    assert (marker_exists is not None) is (checkpoint == "index_ready")

    monkeypatch.setattr(user_state, "_state_transfer_checkpoint", _no_fault)
    IndexRepository(index_path, state_path=state_path)

    _assert_current_v4_rewrite(index_path)
    _assert_single_verified_state_transfer(state_path)


def test_v4_revalidates_same_count_edits_under_the_destructive_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _create_mixed_v3_index(index_path)
    edited = False

    def edit_after_marker(name: str) -> None:
        nonlocal edited
        if name != "index_ready" or edited:
            return
        edited = True
        with sqlite3.connect(index_path) as conn:
            conn.execute(
                """
                UPDATE task_status_overrides
                SET status = 'in_progress', note = 'edited after marker'
                WHERE action_item_id = 1
                """
            )
            conn.commit()

    monkeypatch.setattr(user_state, "_state_transfer_checkpoint", edit_after_marker)

    with pytest.raises(RuntimeError, match="locked legacy rows"):
        IndexRepository(index_path, state_path=state_path)

    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM task_status_overrides").fetchone()[0] == 2
        assert conn.execute(
            "SELECT status, note FROM task_status_overrides WHERE action_item_id = 1"
        ).fetchone() == ("in_progress", "edited after marker")
    assert _state_counts(state_path) == (1, 1, 1, 1, 2)

    monkeypatch.setattr(user_state, "_state_transfer_checkpoint", _no_fault)
    IndexRepository(index_path, state_path=state_path)

    _assert_current_v4_rewrite(index_path)
    with sqlite3.connect(state_path) as conn:
        assert conn.execute(
            "SELECT migrated, orphaned FROM migration_reports ORDER BY id"
        ).fetchall() == [(1, 1), (0, 2)]
        assert conn.execute(
            "SELECT status, note FROM task_states WHERE orphaned = 0"
        ).fetchall() == [("done", "migrated state")]
        assert conn.execute(
            """
            SELECT status, note, orphaned_reason
            FROM task_states
            WHERE legacy_action_item_id = 1 AND orphaned = 1
            """
        ).fetchall() == [
            (
                "in_progress",
                "edited after marker",
                "legacy status conflicts with persistent state",
            )
        ]
    _assert_database_ok(state_path)


def test_preexisting_persistent_status_wins_and_legacy_conflict_is_orphaned(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _create_mixed_v3_index(index_path)
    state = UserStateRepository(state_path)
    source_uuid = state.get_or_create_source(
        "meetily_sqlite",
        LEGACY_SOURCE_PATH,
        now="persistent-source",
    )
    identity = task_identity(
        source_uuid,
        "meeting-1",
        "chunk-1",
        "action_items with evidence",
    )
    state.set_task_state(
        identity,
        "in_progress",
        note="newer persistent intent",
        source="manual",
        now="persistent-newer",
    )

    IndexRepository(index_path, state_path=state_path)

    with sqlite3.connect(state_path) as conn:
        assert conn.execute(
            "SELECT status, note, legacy_action_item_id FROM task_states WHERE orphaned = 0"
        ).fetchall() == [("in_progress", "newer persistent intent", None)]
        assert conn.execute(
            """
            SELECT legacy_action_item_id, status, orphaned_reason
            FROM task_states
            WHERE orphaned = 1
            ORDER BY legacy_action_item_id
            """
        ).fetchall() == [
            (1, "done", "legacy status conflicts with persistent state"),
            (2, "blocked", "missing chunk_external_id"),
        ]
        assert conn.execute("SELECT migrated, orphaned FROM migration_reports").fetchall() == [
            (0, 2)
        ]
        assert conn.execute(
            """
            SELECT legacy_action_item_id, outcome
            FROM migration_report_items
            ORDER BY legacy_action_item_id
            """
        ).fetchall() == [
            (1, "conflict_existing_state"),
            (2, "orphan_missing_identity"),
        ]
    _assert_database_ok(state_path)


def test_semantic_ledger_allows_post_commit_status_and_note_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _create_mixed_v3_index(index_path)
    monkeypatch.setattr(
        user_state,
        "_state_transfer_checkpoint",
        _fail_once_at("state_committed"),
    )

    with pytest.raises(InjectedMigrationError, match="state_committed"):
        IndexRepository(index_path, state_path=state_path)

    state = UserStateRepository(state_path)
    source = state.get_source_by_path("meetily_sqlite", LEGACY_SOURCE_PATH)
    assert source is not None
    identity = task_identity(
        str(source["uuid"]),
        "meeting-1",
        "chunk-1",
        "action_items with evidence",
    )
    with sqlite3.connect(state_path) as conn:
        ledger_binding = conn.execute(
            """
            SELECT legacy_intent_digest, task_identity_digest
            FROM migration_report_items
            WHERE legacy_action_item_id = 1
            """
        ).fetchone()
    state.set_task_state(
        identity,
        "in_progress",
        note="changed after durable transfer",
        source="manual",
        now="post-commit-newer",
    )

    monkeypatch.setattr(user_state, "_state_transfer_checkpoint", _no_fault)
    IndexRepository(index_path, state_path=state_path)

    with sqlite3.connect(state_path) as conn:
        assert (
            conn.execute(
                """
            SELECT legacy_intent_digest, task_identity_digest
            FROM migration_report_items
            WHERE legacy_action_item_id = 1
            """
            ).fetchone()
            == ledger_binding
        )
        assert conn.execute(
            "SELECT status, note FROM task_states WHERE orphaned = 0"
        ).fetchall() == [("in_progress", "changed after durable transfer")]
        assert conn.execute("SELECT COUNT(*) FROM migration_reports").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM task_states WHERE orphaned = 1").fetchone()[0] == 1
        )
    _assert_database_ok(state_path)


def test_collapsed_legacy_identities_keep_one_active_and_orphan_every_additional_row(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _create_mixed_v3_index(index_path)
    with sqlite3.connect(index_path) as conn:
        conn.execute(
            """
            INSERT INTO action_items (
              id, meeting_id, source_chunk_id, ordinal, text, source, confidence,
              fingerprint, created_at, updated_at
            ) VALUES (
              3, 1, 1, 3, 'action_items with evidence', 'heuristic', 0.75,
              'action_items-duplicate', 'created-3', 'updated-3'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO task_status_overrides (
              action_item_id, status, note, source, created_at, updated_at
            ) VALUES (
              3, 'blocked', 'duplicate strict identity', 'manual',
              'state-created-3', 'state-updated-3'
            )
            """
        )
        conn.commit()

    IndexRepository(index_path, state_path=state_path)

    with sqlite3.connect(state_path) as conn:
        assert conn.execute(
            "SELECT status, legacy_action_item_id FROM task_states WHERE orphaned = 0"
        ).fetchall() == [("done", 1)]
        assert conn.execute(
            """
            SELECT legacy_action_item_id, status, orphaned_reason
            FROM task_states
            WHERE orphaned = 1
            ORDER BY legacy_action_item_id
            """
        ).fetchall() == [
            (2, "blocked", "missing chunk_external_id"),
            (3, "blocked", "duplicate legacy strict identity"),
        ]
        assert conn.execute("SELECT migrated, orphaned FROM migration_reports").fetchall() == [
            (1, 2)
        ]
        assert conn.execute(
            """
            SELECT legacy_action_item_id, outcome
            FROM migration_report_items
            ORDER BY legacy_action_item_id
            """
        ).fetchall() == [
            (1, "active_inserted"),
            (2, "orphan_missing_identity"),
            (3, "conflict_duplicate_identity"),
        ]
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM action_items").fetchone()[0] == 2
    _assert_database_ok(index_path)
    _assert_database_ok(state_path)


def test_digest_keyed_report_is_reused_across_relative_absolute_and_symlink_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _create_mixed_v3_index(index_path)
    monkeypatch.chdir(tmp_path)

    attempts = [(Path("index.sqlite"), Path("state.sqlite")), (index_path, state_path)]
    for attempt_index, attempt_state in attempts:
        monkeypatch.setattr(
            migrations,
            "_migration_checkpoint",
            _fail_once_at("v4:task_status_overrides:dropped"),
        )
        with pytest.raises(InjectedMigrationError):
            IndexRepository(attempt_index, state_path=attempt_state)
        with sqlite3.connect(state_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM migration_reports").fetchone()[0] == 1

    index_alias = tmp_path / "index-alias.sqlite"
    state_alias = tmp_path / "state-alias.sqlite"
    index_alias.symlink_to(index_path)
    state_alias.symlink_to(state_path)
    monkeypatch.setattr(
        migrations,
        "_migration_checkpoint",
        _fail_once_at("v4:task_status_overrides:dropped"),
    )
    with pytest.raises(InjectedMigrationError):
        IndexRepository(index_alias, state_path=state_alias)

    with sqlite3.connect(state_path) as conn:
        reports = conn.execute("SELECT migration_key, index_path FROM migration_reports").fetchall()
        assert len(reports) == 1
        assert reports[0][0]
        assert reports[0][1] == canonical_database_path(index_path)
    with sqlite3.connect(index_path) as conn:
        marker = conn.execute(
            "SELECT index_path, state_path FROM user_state_migration_ready"
        ).fetchone()
        assert marker == (
            canonical_database_path(index_path),
            canonical_database_path(state_path),
        )

    monkeypatch.setattr(migrations, "_migration_checkpoint", _no_fault)
    IndexRepository(index_alias, state_path=state_alias)
    _assert_current_v4_rewrite(index_path)
    _assert_single_verified_state_transfer(state_path)


def test_two_process_index_repository_upgrade_is_linearizable(tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _create_mixed_v3_index(index_path)
    context = multiprocessing.get_context("spawn")
    receive_one, send_one = context.Pipe(duplex=False)
    receive_two, send_two = context.Pipe(duplex=False)
    processes = (
        context.Process(
            target=_child_open_repository_after_release,
            args=(str(index_path), str(state_path), receive_one),
        ),
        context.Process(
            target=_child_open_repository_after_release,
            args=(str(index_path), str(state_path), receive_two),
        ),
    )
    for process in processes:
        process.start()
    receive_one.close()
    receive_two.close()
    send_one.send(None)
    send_two.send(None)
    send_one.close()
    send_two.close()

    for process in processes:
        _assert_process_exit(process, 0)

    _assert_current_v4_rewrite(index_path)
    _assert_single_verified_state_transfer(state_path)


def test_child_process_exit_mid_v4_ddl_recovers_via_index_repository(tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _create_mixed_v3_index(index_path)
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_child_crash_during_upgrade,
        args=(
            str(index_path),
            str(state_path),
            "migration",
            "v4:action_items:legacy_dropped",
        ),
    )
    process.start()
    _assert_process_exit(process, CHILD_CRASH_EXIT_CODE)

    _assert_v3_rows_and_schema_are_preserved(index_path)
    _assert_single_verified_state_transfer(state_path)
    IndexRepository(index_path, state_path=state_path)
    _assert_current_v4_rewrite(index_path)
    _assert_single_verified_state_transfer(state_path)


@pytest.mark.parametrize("checkpoint", ["state_committed", "index_ready"])
def test_child_process_exit_after_committed_state_or_marker_recovers_without_duplicates(
    checkpoint: str,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _create_mixed_v3_index(index_path)
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_child_crash_during_upgrade,
        args=(str(index_path), str(state_path), "state", checkpoint),
    )
    process.start()
    _assert_process_exit(process, CHILD_CRASH_EXIT_CODE)

    _assert_v3_rows_and_schema_are_preserved(index_path)
    _assert_single_verified_state_transfer(state_path)
    with sqlite3.connect(index_path) as conn:
        marker_exists = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'user_state_migration_ready'
            """
        ).fetchone()
    assert (marker_exists is not None) is (checkpoint == "index_ready")

    IndexRepository(index_path, state_path=state_path)
    _assert_current_v4_rewrite(index_path)
    _assert_single_verified_state_transfer(state_path)


def test_v4_rejects_report_item_rebound_to_wrong_persisted_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _prepare_v3_with_durable_transfer_marker(index_path, state_path, monkeypatch)

    with sqlite3.connect(state_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        source_uuid = str(conn.execute("SELECT uuid FROM sources").fetchone()[0])
        orphan_id = int(conn.execute("SELECT id FROM task_states WHERE orphaned = 1").fetchone()[0])
        cursor = conn.execute(
            """
            INSERT INTO task_states (
              source_uuid, meeting_external_id, chunk_external_id,
              entity_kind, content_fingerprint, status, note, source,
              orphaned, orphaned_reason, legacy_action_item_id,
              created_at, updated_at
            ) VALUES (
              ?, 'meeting-other', NULL, 'task', 'unrelated-fingerprint',
              'blocked', 'different orphan', 'manual', 1,
              'different orphan identity', 999, 'other-created', 'other-updated'
            )
            """,
            (source_uuid,),
        )
        wrong_task_state_id = cursor.lastrowid
        assert wrong_task_state_id is not None
        conn.execute(
            """
            UPDATE migration_report_items
            SET task_state_id = ?
            WHERE legacy_action_item_id = 2
            """,
            (wrong_task_state_id,),
        )
        conn.execute("DELETE FROM task_states WHERE id = ?", (orphan_id,))
        conn.commit()
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    with (
        sqlite3.connect(index_path) as conn,
        pytest.raises(
            RuntimeError,
            match="report does not match",
        ),
    ):
        migrations.migrate_to_v4(conn)

    _assert_v3_rows_and_schema_are_preserved(index_path)


def test_migration_report_rejects_reused_task_state_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _prepare_v3_with_durable_transfer_marker(index_path, state_path, monkeypatch)

    with sqlite3.connect(state_path) as conn:
        active_id = int(conn.execute("SELECT id FROM task_states WHERE orphaned = 0").fetchone()[0])
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            conn.execute(
                """
                UPDATE migration_report_items
                SET task_state_id = ?
                WHERE legacy_action_item_id = 2
                """,
                (active_id,),
            )
        conn.rollback()

    IndexRepository(index_path, state_path=state_path)
    _assert_current_v4_rewrite(index_path)
    _assert_single_verified_state_transfer(state_path)


@pytest.mark.parametrize("tamper", ["report_id", "state_path"])
def test_v4_requires_marker_binding_to_the_durable_report_and_state_path(
    tamper: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _prepare_v3_with_durable_transfer_marker(index_path, state_path, monkeypatch)

    with sqlite3.connect(index_path) as conn:
        if tamper == "report_id":
            conn.execute("UPDATE user_state_migration_ready SET report_id = 999")
            expected_error = "report does not match"
        else:
            conn.execute(
                "UPDATE user_state_migration_ready SET state_path = ?",
                (canonical_database_path(tmp_path / "missing-state.sqlite"),),
            )
            expected_error = "state database.*missing"
        conn.commit()

    with sqlite3.connect(index_path) as conn, pytest.raises(RuntimeError, match=expected_error):
        migrations.migrate_to_v4(conn)

    _assert_v3_rows_and_schema_are_preserved(index_path)
    IndexRepository(index_path, state_path=state_path)
    _assert_current_v4_rewrite(index_path)
    _assert_single_verified_state_transfer(state_path)


def test_populated_v2_report_is_backfilled_atomically_after_child_crash_retry(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _create_mixed_v3_index(index_path)
    _create_populated_v2_state(index_path, state_path)
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_child_crash_during_upgrade,
        args=(str(index_path), str(state_path), "state", "report"),
    )
    process.start()
    _assert_process_exit(process, CHILD_CRASH_EXIT_CODE)

    with sqlite3.connect(state_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute(
            "SELECT migration_key, migrated, orphaned FROM migration_reports"
        ).fetchall() == [(None, 1, 1)]
        assert conn.execute("SELECT COUNT(*) FROM migration_report_items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_states").fetchone()[0] == 2
    _assert_v3_rows_and_schema_are_preserved(index_path)
    _assert_database_ok(state_path)

    IndexRepository(index_path, state_path=state_path)

    _assert_current_v4_rewrite(index_path)
    with sqlite3.connect(state_path) as conn:
        reports = conn.execute(
            "SELECT migration_key, index_path, migrated, orphaned FROM migration_reports"
        ).fetchall()
        assert len(reports) == 1
        migration_key, report_index_path, migrated, orphaned = reports[0]
        assert migration_key
        assert (report_index_path, migrated, orphaned) == (
            canonical_database_path(index_path),
            1,
            1,
        )
        assert conn.execute(
            """
            SELECT
              legacy_action_item_id, outcome,
              length(legacy_intent_digest), length(task_identity_digest)
            FROM migration_report_items
            ORDER BY legacy_action_item_id
            """
        ).fetchall() == [
            (1, "active_existing", 64, 64),
            (2, "orphan_missing_identity", 64, 64),
        ]
        assert conn.execute("SELECT COUNT(*) FROM task_states").fetchone()[0] == 2
    _assert_database_ok(state_path)


@pytest.mark.parametrize("historical_state", ["ambiguous_report", "newer_state"])
def test_populated_v2_report_is_not_adopted_when_history_is_ambiguous_or_nonmatching(
    historical_state: str,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _create_mixed_v3_index(index_path)
    _create_populated_v2_state(index_path, state_path)
    with sqlite3.connect(state_path) as conn:
        if historical_state == "ambiguous_report":
            conn.execute(
                """
                INSERT INTO migration_reports (
                  index_path, migrated, orphaned, created_at
                ) VALUES (?, 1, 1, 'ambiguous-report')
                """,
                (str(index_path),),
            )
        else:
            conn.execute(
                """
                UPDATE task_states
                SET status = 'in_progress', note = 'newer persistent intent',
                    updated_at = 'newer-state'
                WHERE orphaned = 0
                """
            )
        conn.commit()

    IndexRepository(index_path, state_path=state_path)

    _assert_current_v4_rewrite(index_path)
    with sqlite3.connect(state_path) as conn:
        keyed_reports = int(
            conn.execute(
                "SELECT COUNT(*) FROM migration_reports WHERE migration_key IS NOT NULL"
            ).fetchone()[0]
        )
        unkeyed_reports = int(
            conn.execute(
                "SELECT COUNT(*) FROM migration_reports WHERE migration_key IS NULL"
            ).fetchone()[0]
        )
        assert keyed_reports == 1
        if historical_state == "ambiguous_report":
            assert unkeyed_reports == 2
            assert conn.execute("SELECT COUNT(*) FROM task_states").fetchone()[0] == 2
        else:
            assert unkeyed_reports == 1
            assert conn.execute(
                "SELECT status, note FROM task_states WHERE orphaned = 0"
            ).fetchall() == [("in_progress", "newer persistent intent")]
            assert conn.execute("SELECT COUNT(*) FROM task_states").fetchone()[0] == 3
            assert conn.execute(
                """
                SELECT status, note, orphaned_reason
                FROM task_states
                WHERE legacy_action_item_id = 1 AND orphaned = 1
                """
            ).fetchall() == [
                (
                    "done",
                    "migrated state",
                    "legacy status conflicts with persistent state",
                )
            ]
    _assert_database_ok(state_path)
