from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING

import pytest

from meetily_memory.db.row_decode import decode_nullable_integer
from meetily_memory.db.schema_family import (
    INDEX_APPLICATION_ID,
    INDEX_SCHEMA_EPOCH,
    INDEX_SCHEMA_FAMILY,
    INDEX_SCHEMA_USER_VERSION,
    SCHEMA_USER_VERSION_BASE,
    STATE_APPLICATION_ID,
    STATE_SCHEMA_EPOCH,
    STATE_SCHEMA_FAMILY,
    STATE_SCHEMA_USER_VERSION,
)
from meetily_memory.db.state_schema import (
    APPLICATION_TABLES,
    STATE_SCHEMA_SQL,
    StateSchemaError,
    create_state_database,
    validate_state_database,
)
from meetily_memory.user_state import (
    AmbiguousSourceIdentityError,
    SourcePathClaim,
    UserStateRepository,
)

if TYPE_CHECKING:
    from pathlib import Path


def _database_identity(path: Path) -> tuple[bytes, int]:
    return path.read_bytes(), path.stat().st_mtime_ns


def _application_tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }


def test_schema_family_constants_are_stable() -> None:
    assert SCHEMA_USER_VERSION_BASE == 1000
    assert STATE_SCHEMA_FAMILY == "meetily-memory-state"
    assert INDEX_SCHEMA_FAMILY == "meetily-memory-index"
    assert STATE_APPLICATION_ID == 0x4D4D5354
    assert INDEX_APPLICATION_ID == 0x4D4D4958
    assert STATE_SCHEMA_EPOCH == 1
    assert INDEX_SCHEMA_EPOCH == 1
    assert STATE_SCHEMA_USER_VERSION == 1001
    assert INDEX_SCHEMA_USER_VERSION == 1001


def test_fresh_state_has_exact_epoch_schema_identity_and_singletons(tmp_path: Path) -> None:
    state_path = tmp_path / "nested" / "state.sqlite"

    repository = UserStateRepository(state_path)

    assert repository.state_path == state_path
    assert _application_tables(state_path) == APPLICATION_TABLES
    with sqlite3.connect(state_path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone() == (STATE_APPLICATION_ID,)
        assert connection.execute("PRAGMA user_version").fetchone() == (STATE_SCHEMA_USER_VERSION,)
        assert connection.execute(
            "SELECT singleton, schema_family, schema_epoch FROM state_meta"
        ).fetchall() == [(1, STATE_SCHEMA_FAMILY, STATE_SCHEMA_EPOCH)]
        settings = connection.execute("SELECT * FROM app_settings").fetchall()
        assert len(settings) == 1
        assert settings[0][0] == 1
        setting_columns = [
            str(row[1]) for row in connection.execute("PRAGMA table_info(app_settings)")
        ]
        assert setting_columns == [
            "singleton",
            "source_uuid",
            "source_path",
            "ui_language",
            "last_update_at",
            "obsidian_vault_path",
            "obsidian_folder",
            "obsidian_last_sync_at",
        ]
        meeting_tag_columns = [
            str(row[1]) for row in connection.execute("PRAGMA table_info(meeting_tags)")
        ]
        assert meeting_tag_columns == [
            "source_uuid",
            "meeting_external_id",
            "manual_tag_id",
            "created_at",
        ]
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert indexes == {
            "idx_app_settings_source_uuid",
            "idx_meeting_tags_manual_tag_id",
            "idx_meeting_tags_meeting",
            "idx_sources_kind_projected_path",
        }
    validate_state_database(state_path)


def test_state_schema_sql_contains_only_the_supported_application_tables() -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(STATE_SCHEMA_SQL)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert tables == APPLICATION_TABLES
    assert (
        not {
            "task_states",
            "migration_reports",
            "migration_report_items",
            "topic_alias_topics",
            "topic_aliases",
            "topic_alias_imports",
            "index_generations",
            "tags",
        }
        & tables
    )


@pytest.mark.parametrize(
    "alter_sql",
    [
        "ALTER TABLE app_settings ADD COLUMN semantic_provider TEXT",
        "ALTER TABLE meeting_tags ADD COLUMN source TEXT DEFAULT 'manual'",
    ],
)
def test_exact_state_schema_rejects_removed_semantic_and_assignment_source_columns(
    tmp_path: Path,
    alter_sql: str,
) -> None:
    state_path = tmp_path / "state.sqlite"
    create_state_database(state_path)
    with sqlite3.connect(state_path) as connection:
        connection.execute(alter_sql)
        connection.commit()

    with pytest.raises(StateSchemaError, match="schema objects do not exactly match"):
        validate_state_database(state_path)


def test_fresh_create_refuses_any_existing_file_without_modifying_it(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    state_path.write_bytes(b"")
    before = _database_identity(state_path)

    with pytest.raises(StateSchemaError, match=r"fresh-only|Refusing"):
        create_state_database(state_path)

    assert _database_identity(state_path) == before


@pytest.mark.parametrize(
    "case",
    ["legacy", "foreign", "future", "future-epoch", "wrong-family", "tampered"],
)
def test_existing_non_epoch_state_is_rejected_actionably_and_read_only(
    tmp_path: Path,
    case: str,
) -> None:
    state_path = tmp_path / f"{case}.sqlite"
    if case == "legacy":
        with sqlite3.connect(state_path) as connection:
            connection.execute("CREATE TABLE sources (uuid TEXT PRIMARY KEY)")
            connection.execute("PRAGMA user_version=7")
    else:
        create_state_database(state_path)
        with sqlite3.connect(state_path) as connection:
            if case == "foreign":
                connection.execute(f"PRAGMA application_id={INDEX_APPLICATION_ID}")
            elif case == "future":
                connection.execute(f"PRAGMA user_version={STATE_SCHEMA_USER_VERSION + 1}")
            elif case in {"future-epoch", "wrong-family"}:
                connection.execute("PRAGMA ignore_check_constraints=ON")
                if case == "future-epoch":
                    connection.execute(
                        "UPDATE state_meta SET schema_epoch=? WHERE singleton=1",
                        (STATE_SCHEMA_EPOCH + 1,),
                    )
                else:
                    connection.execute(
                        "UPDATE state_meta "
                        "SET schema_family='another-state-family' WHERE singleton=1"
                    )
                connection.execute("PRAGMA ignore_check_constraints=OFF")
            else:
                connection.execute("DROP INDEX idx_meeting_tags_meeting")
            connection.commit()
    before = _database_identity(state_path)

    with pytest.raises(
        StateSchemaError,
        match=r"Deleting state permanently loses manual tags and application settings",
    ):
        UserStateRepository(state_path)

    assert _database_identity(state_path) == before


def test_corrupt_existing_state_is_rejected_actionably_and_read_only(tmp_path: Path) -> None:
    state_path = tmp_path / "corrupt.sqlite"
    state_path.write_bytes(b"not a sqlite database")
    before = _database_identity(state_path)

    with pytest.raises(
        StateSchemaError,
        match=r"Deleting state permanently loses manual tags and application settings",
    ):
        UserStateRepository(state_path)

    assert _database_identity(state_path) == before


def test_settings_preserve_selected_source_and_runtime_values(tmp_path: Path) -> None:
    state = UserStateRepository(tmp_path / "state.sqlite")
    source_uuid = state.get_or_create_source("meetily_sqlite", "/source.sqlite", now="created")

    state.replace_app_settings(
        {
            "source_uuid": source_uuid,
            "source_path": None,
            "ui_language": "ru",
            "last_update_at": "updated",
            "obsidian_vault_path": "/vault",
            "obsidian_folder": "Meetily",
            "obsidian_last_sync_at": "synced",
        }
    )

    row = state.read_app_settings()
    assert row == {
        "singleton": 1,
        "source_uuid": source_uuid,
        "source_path": None,
        "ui_language": "ru",
        "last_update_at": "updated",
        "obsidian_vault_path": "/vault",
        "obsidian_folder": "Meetily",
        "obsidian_last_sync_at": "synced",
    }


def test_pending_revision_decoder_accepts_only_sqlite_integer_or_null(tmp_path: Path) -> None:
    database = tmp_path / "row-values.sqlite"
    with sqlite3.connect(database) as connection:
        text_value = connection.execute("SELECT CAST('7' AS TEXT) AS pending_revision").fetchone()[
            0
        ]
        null_value = connection.execute("SELECT NULL AS pending_revision").fetchone()[0]

    with pytest.raises(
        StateSchemaError,
        match=r"sources\.pending_revision must be INTEGER, got TEXT",
    ):
        decode_nullable_integer(
            text_value,
            table="sources",
            column="pending_revision",
            context="source binding",
            error_type=StateSchemaError,
        )
    assert (
        decode_nullable_integer(
            null_value,
            table="sources",
            column="pending_revision",
            context="source binding",
            error_type=StateSchemaError,
        )
        is None
    )


def test_source_uuid_survives_explicit_path_update(tmp_path: Path) -> None:
    state = UserStateRepository(tmp_path / "state.sqlite")
    source_uuid = state.get_or_create_source("meetily_sqlite", "/old/source.sqlite", now="1")

    state.update_source_path(source_uuid, "/new/source.sqlite", now="2")

    assert (
        state.get_or_create_source("meetily_sqlite", "/new/source.sqlite", now="3") == source_uuid
    )


def test_atomic_source_path_claim_allows_only_one_competing_uuid(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    state = UserStateRepository(state_path)
    first_uuid = state.get_or_create_source("meetily_sqlite", "/old/first.sqlite", now="1")
    second_uuid = state.get_or_create_source("meetily_sqlite", "/old/second.sqlite", now="1")
    target_path = tmp_path / "target.sqlite"
    target_path.touch()
    barrier = Barrier(2)

    def claim(source_uuid: str) -> SourcePathClaim | AmbiguousSourceIdentityError:
        repository = UserStateRepository(state_path)
        barrier.wait(timeout=5)
        try:
            return repository.claim_source_path(
                source_uuid,
                "meetily_sqlite",
                target_path,
                now=source_uuid,
            )
        except AmbiguousSourceIdentityError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, (first_uuid, second_uuid)))

    claims = [result for result in results if isinstance(result, SourcePathClaim)]
    conflicts = [result for result in results if isinstance(result, AmbiguousSourceIdentityError)]
    assert len(claims) == 1
    assert len(conflicts) == 1


def test_same_target_retry_preserves_projection_and_finalizes_once(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    state = UserStateRepository(state_path)
    source_uuid = state.get_or_create_source("meetily_sqlite", "/old/source.sqlite", now="1")
    target_path = tmp_path / "target.sqlite"
    target_path.touch()
    first_claim = state.claim_source_path(
        source_uuid,
        "meetily_sqlite",
        target_path,
        now="2",
    )

    restarted_state = UserStateRepository(state_path)
    retry_claim = restarted_state.claim_source_path(
        source_uuid,
        "meetily_sqlite",
        target_path,
        now="3",
    )

    assert retry_claim.claimed_revision > first_claim.claimed_revision
    assert retry_claim.projected_path == "/old/source.sqlite"
    assert retry_claim.resumed is True
    assert restarted_state.finalize_source_path_claim(retry_claim) is True
    assert restarted_state.finalize_source_path_claim(first_claim) is False
    assert restarted_state.get_source_binding(source_uuid) == {
        "uuid": source_uuid,
        "kind": "meetily_sqlite",
        "current_path": str(target_path.resolve(strict=True)),
        "revision": retry_claim.claimed_revision,
        "projected_path": str(target_path.resolve(strict=True)),
        "pending_revision": None,
        "updated_at": "3",
    }


def test_source_path_claim_compensation_is_token_guarded(tmp_path: Path) -> None:
    state = UserStateRepository(tmp_path / "state.sqlite")
    source_uuid = state.get_or_create_source("meetily_sqlite", "/old/source.sqlite", now="1")
    target_path = tmp_path / "target.sqlite"
    target_path.touch()
    first_claim = state.claim_source_path(
        source_uuid,
        "meetily_sqlite",
        target_path,
        now="2",
    )

    rollback_claim = state.begin_source_path_rollback(first_claim, now="rollback")

    assert rollback_claim is not None
    assert rollback_claim.claimed_path == "/old/source.sqlite"
    assert rollback_claim.projected_path == str(target_path.resolve(strict=True))
    assert state.finalize_source_path_claim(rollback_claim) is True
    assert state.begin_source_path_rollback(first_claim, now="stale") is None
