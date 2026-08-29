import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from meetily_memory import user_state as user_state_module
from meetily_memory.db.migrations import (
    LATEST_IN_PLACE_SCHEMA_VERSION,
    migrate_to_v1,
    migrate_to_v2,
    migrate_to_v3,
)
from meetily_memory.db.repository import IndexRepository
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.user_state import (
    CURRENT_USER_STATE_SCHEMA_VERSION,
    INDEX_GENERATION_STATE_SCHEMA,
    MIGRATION_REPORT_SCHEMA,
    PENDING_SOURCE_BINDING_SCHEMA,
    SOURCE_REVISION_SCHEMA,
    TAG_STATE_SCHEMA,
    TOPIC_ALIAS_STATE_SCHEMA,
    USER_STATE_SCHEMA,
    AmbiguousSourceIdentityError,
    SourcePathClaim,
    StoredTopic,
    UserStateRepository,
)


def test_legacy_task_status_migrates_to_persistent_state_before_index_schema(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    source_path = str(tmp_path / "source.sqlite")
    _create_v3_index_with_task_status(index_path, source_path=source_path)
    state = UserStateRepository(state_path)
    source_uuid = state.get_or_create_source(
        "meetily_sqlite",
        source_path,
        now="state-created",
    )

    repo = IndexRepository(index_path, state_path=state_path)
    report = UserStateRepository(state_path).latest_migration_report()

    assert repo.requires_rebuild is True
    assert report == {"migrated": 1, "orphaned": 0}
    with sqlite3.connect(state_path) as conn:
        persisted_status = conn.execute(
            "SELECT status, note FROM task_states WHERE orphaned = 0"
        ).fetchone()
    assert persisted_status == ("done", "verified by user")
    with sqlite3.connect(state_path) as conn:
        assert conn.execute(
            "SELECT source_uuid FROM task_states WHERE orphaned = 0"
        ).fetchone() == (source_uuid,)
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_IN_PLACE_SCHEMA_VERSION
        assert (
            conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'task_status_overrides'
                """
            ).fetchone()
            is None
        )


def test_v3_task_transfer_reuses_only_registered_source_and_orphans_other_source(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    source_a = str(tmp_path / "source-a.sqlite")
    source_b = str(tmp_path / "source-b.sqlite")
    _create_v3_index_with_two_task_sources(index_path, source_a, source_b)
    state = UserStateRepository(state_path)
    source_a_uuid = state.get_or_create_source(
        "meetily_sqlite",
        source_a,
        now="registered-a",
    )

    IndexRepository(index_path, state_path=state_path)

    assert state.latest_migration_report() == {"migrated": 1, "orphaned": 1}
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT uuid, current_path FROM sources").fetchall() == [
            (source_a_uuid, source_a)
        ]
        active = conn.execute(
            """
            SELECT source_uuid, meeting_external_id, status, note, source
            FROM task_states
            WHERE orphaned = 0
            """
        ).fetchone()
        orphan = conn.execute(
            """
            SELECT source_uuid, meeting_external_id, status, note, source, orphaned_reason
            FROM task_states
            WHERE orphaned = 1
            """
        ).fetchone()
    assert active == (source_a_uuid, "meeting-a", "done", "note-a", "manual-a")
    assert orphan == (
        None,
        "meeting-b",
        "in_progress",
        "note-b",
        "manual-b",
        "legacy source identity is absent from persistent state",
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
    _create_v3_index_with_task_status(
        index_path,
        source_path=str(tmp_path / "source.sqlite"),
        chunk_external_id=None,
    )

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
    with sqlite3.connect(state_path) as conn:
        paths = dict(conn.execute("SELECT uuid, current_path FROM sources"))
    assert paths[claims[0].source_uuid] == str(target_path.resolve(strict=True))
    losing_uuid = second_uuid if claims[0].source_uuid == first_uuid else first_uuid
    expected_losing_path = (
        "/old/second.sqlite" if losing_uuid == second_uuid else "/old/first.sqlite"
    )
    assert paths[losing_uuid] == expected_losing_path


def test_same_target_retry_preserves_persisted_projection_and_finalizes_once(
    tmp_path: Path,
) -> None:
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
    with sqlite3.connect(state_path) as conn:
        assert conn.execute(
            """
            SELECT current_path, projected_path, revision, pending_revision
            FROM sources
            WHERE uuid = ?
            """,
            (source_uuid,),
        ).fetchone() == (
            str(target_path.resolve(strict=True)),
            "/old/source.sqlite",
            retry_claim.claimed_revision,
            retry_claim.claimed_revision,
        )

    assert restarted_state.finalize_source_path_claim(retry_claim) is True
    assert restarted_state.finalize_source_path_claim(first_claim) is False
    with sqlite3.connect(state_path) as conn:
        assert conn.execute(
            """
            SELECT current_path, projected_path, revision, pending_revision
            FROM sources
            WHERE uuid = ?
            """,
            (source_uuid,),
        ).fetchone() == (
            str(target_path.resolve(strict=True)),
            str(target_path.resolve(strict=True)),
            retry_claim.claimed_revision,
            None,
        )


def test_same_target_retry_compensation_persists_reverse_claim_with_fresh_token(
    tmp_path: Path,
) -> None:
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
    retry_claim = state.claim_source_path(
        source_uuid,
        "meetily_sqlite",
        target_path,
        now="3",
    )

    rollback_claim = state.begin_source_path_rollback(retry_claim, now="rollback")

    assert rollback_claim is not None
    persisted = state.get_pending_source_path_claim(source_uuid)
    assert persisted is not None
    assert persisted.claimed_path == rollback_claim.claimed_path
    assert persisted.projected_path == rollback_claim.projected_path
    assert persisted.claimed_revision == rollback_claim.claimed_revision
    assert rollback_claim.claimed_path == "/old/source.sqlite"
    assert rollback_claim.projected_path == str(target_path.resolve(strict=True))
    assert rollback_claim.claimed_revision > retry_claim.claimed_revision
    assert state.begin_source_path_rollback(first_claim, now="stale") is None
    assert state.finalize_source_path_claim(retry_claim) is False


def test_source_path_claim_compensation_and_aba_are_token_guarded(tmp_path: Path) -> None:
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
    assert state.finalize_source_path_claim(rollback_claim) is True
    second_claim = state.claim_source_path(
        source_uuid,
        "meetily_sqlite",
        target_path,
        now="3",
    )

    assert second_claim.claimed_revision > rollback_claim.claimed_revision
    assert state.begin_source_path_rollback(first_claim, now="stale") is None
    assert state.finalize_source_path_claim(first_claim) is False
    assert state.finalize_source_path_claim(second_claim) is True
    with sqlite3.connect(state.state_path) as conn:
        assert conn.execute(
            """
            SELECT current_path, projected_path, revision, pending_revision
            FROM sources
            WHERE uuid = ?
            """,
            (source_uuid,),
        ).fetchone() == (
            str(target_path.resolve(strict=True)),
            str(target_path.resolve(strict=True)),
            second_claim.claimed_revision,
            None,
        )


def test_source_path_claim_compensation_does_not_overwrite_newer_path(tmp_path: Path) -> None:
    state = UserStateRepository(tmp_path / "state.sqlite")
    source_uuid = state.get_or_create_source("meetily_sqlite", "/old/source.sqlite", now="1")
    target_path = tmp_path / "target.sqlite"
    target_path.touch()
    claim = state.claim_source_path(
        source_uuid,
        "meetily_sqlite",
        target_path,
        now="2",
    )
    state.update_source_path(source_uuid, "/concurrent/source.sqlite", now="3")

    rollback_claim = state.begin_source_path_rollback(claim, now="rollback")

    assert rollback_claim is None
    assert state.get_source(source_uuid) == {
        "uuid": source_uuid,
        "kind": "meetily_sqlite",
        "current_path": "/concurrent/source.sqlite",
    }


def test_user_state_v1_migrates_to_current_schema_idempotently(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    with sqlite3.connect(state_path) as conn:
        conn.executescript(USER_STATE_SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

    UserStateRepository(state_path)
    UserStateRepository(state_path)

    with sqlite3.connect(state_path) as conn:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_USER_STATE_SCHEMA_VERSION
        )
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "tags",
            "meeting_tags",
            "migration_report_items",
            "topic_alias_topics",
            "topic_aliases",
            "topic_alias_imports",
            "index_generations",
        } <= tables
        report_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(migration_reports)")
        }
        assert "migration_key" in report_columns
        meeting_tag_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(meeting_tags)").fetchall()
        }
        assert {"source_uuid", "meeting_external_id", "tag_id", "source"} <= meeting_tag_columns
        source_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(sources)").fetchall()
        }
        assert {"revision", "projected_path", "pending_revision"} <= source_columns


def test_pending_binding_and_topic_alias_schema_upgrades_are_atomic_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "state.sqlite"
    with sqlite3.connect(state_path) as conn:
        conn.executescript(USER_STATE_SCHEMA)
        conn.executescript(TAG_STATE_SCHEMA)
        conn.executescript(MIGRATION_REPORT_SCHEMA)
        conn.executescript(SOURCE_REVISION_SCHEMA)
        conn.execute(
            """
            INSERT INTO sources (
              uuid, kind, current_path, created_at, updated_at, revision
            ) VALUES ('source-uuid', 'meetily_sqlite', '/source.sqlite', '1', '1', 4)
            """
        )
        conn.execute("PRAGMA user_version = 4")
        conn.commit()

    original_execute = user_state_module.execute_sql_statements
    failed_scripts: list[str] = []

    def fail_each_new_schema_once(conn: sqlite3.Connection, script: str) -> None:
        original_execute(conn, script)
        if (
            script
            in {
                PENDING_SOURCE_BINDING_SCHEMA,
                TOPIC_ALIAS_STATE_SCHEMA,
                INDEX_GENERATION_STATE_SCHEMA,
            }
            and script not in failed_scripts
        ):
            failed_scripts.append(script)
            message = "injected additive state schema failure"
            raise RuntimeError(message)

    monkeypatch.setattr(user_state_module, "execute_sql_statements", fail_each_new_schema_once)
    with pytest.raises(RuntimeError, match="additive state schema failure"):
        UserStateRepository(state_path)
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert "projected_path" not in {
            str(row[1]) for row in conn.execute("PRAGMA table_info(sources)").fetchall()
        }

    with pytest.raises(RuntimeError, match="additive state schema failure"):
        UserStateRepository(state_path)
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute(
            "SELECT projected_path, pending_revision FROM sources WHERE uuid = 'source-uuid'"
        ).fetchone() == ("/source.sqlite", None)
        assert "topic_aliases" not in {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    with pytest.raises(RuntimeError, match="additive state schema failure"):
        UserStateRepository(state_path)
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        assert "generation_id" not in {
            str(row[1]) for row in conn.execute("PRAGMA table_info(topic_alias_imports)").fetchall()
        }
        assert "index_generations" not in {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    monkeypatch.setattr(user_state_module, "execute_sql_statements", original_execute)
    UserStateRepository(state_path)
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (
            CURRENT_USER_STATE_SCHEMA_VERSION
        )
        assert conn.execute(
            "SELECT current_path, projected_path, revision, pending_revision FROM sources"
        ).fetchall() == [("/source.sqlite", "/source.sqlite", 4, None)]
        assert {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        } >= {
            "topic_alias_topics",
            "topic_aliases",
            "topic_alias_imports",
            "index_generations",
        }


def test_topic_namespace_conflict_is_prevalidated_before_any_state_row(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.sqlite"
    state = UserStateRepository(state_path)
    beta = StoredTopic(
        stable_key="topic:beta",
        title="Beta",
        normalized_title="beta",
        created_at="beta-created",
        updated_at="beta-updated",
        raw_metadata_json='{"owner":"beta"}',
    )
    alpha = StoredTopic(
        stable_key="topic:alpha",
        title="Alpha",
        normalized_title="alpha",
        created_at="alpha-created",
        updated_at="alpha-updated",
        raw_metadata_json='{"owner":"alpha"}',
    )
    assert state.add_topic_aliases(beta, ["bee"], now="beta-alias") == ("bee",)
    before = state_path.read_bytes()

    added = state.add_topic_aliases(alpha, ["alpha-free", "  BETA  "], now="conflict")

    assert added == ()
    assert state_path.read_bytes() == before
    assert state.topic_for_query("beta") == beta
    assert state.topic_for_query("Alpha") is None
    assert state.topic_for_query("alpha-free") is None
    assert state.list_topics() == (beta,)


def test_v6_alias_import_ledger_migrates_to_generation_and_path_key(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    digest = "a" * 64
    with sqlite3.connect(state_path) as conn:
        conn.executescript(USER_STATE_SCHEMA)
        conn.executescript(TAG_STATE_SCHEMA)
        conn.executescript(MIGRATION_REPORT_SCHEMA)
        conn.executescript(SOURCE_REVISION_SCHEMA)
        conn.executescript(PENDING_SOURCE_BINDING_SCHEMA)
        conn.executescript(TOPIC_ALIAS_STATE_SCHEMA)
        conn.execute(
            """
            INSERT INTO topic_alias_imports (
              index_path, source_schema_version, source_alias_count,
              source_digest, imported_at
            ) VALUES ('/index.sqlite', 5, 1, ?, 'imported')
            """,
            (digest,),
        )
        conn.execute("PRAGMA user_version = 6")
        conn.commit()

    UserStateRepository(state_path)

    generation_id = f"legacy-import:{digest}"
    with sqlite3.connect(state_path) as conn:
        assert conn.execute(
            """
            SELECT generation_id, index_path, alias_owner, registered_at
            FROM index_generations
            """
        ).fetchone() == (generation_id, "/index.sqlite", "legacy", "imported")
        assert conn.execute(
            """
            SELECT generation_id, index_path, source_schema_version,
                   source_alias_count, source_digest, imported_at
            FROM topic_alias_imports
            """
        ).fetchone() == (generation_id, "/index.sqlite", 5, 1, digest, "imported")


def test_source_revision_schema_upgrade_is_atomic_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "state.sqlite"
    with sqlite3.connect(state_path) as conn:
        conn.executescript(USER_STATE_SCHEMA)
        conn.executescript(TAG_STATE_SCHEMA)
        conn.executescript(MIGRATION_REPORT_SCHEMA)
        conn.execute(
            """
            INSERT INTO sources (uuid, kind, current_path, created_at, updated_at)
            VALUES ('source-uuid', 'meetily_sqlite', '/source.sqlite', '1', '1')
            """
        )
        conn.execute("PRAGMA user_version = 3")
        conn.commit()

    original_execute = user_state_module.execute_sql_statements

    def fail_after_revision_ddl(conn: sqlite3.Connection, script: str) -> None:
        original_execute(conn, script)
        if script == SOURCE_REVISION_SCHEMA:
            message = "injected state schema failure"
            raise RuntimeError(message)

    monkeypatch.setattr(user_state_module, "execute_sql_statements", fail_after_revision_ddl)
    with pytest.raises(RuntimeError, match="injected state schema failure"):
        UserStateRepository(state_path)

    with sqlite3.connect(state_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "revision" not in {
            str(row[1]) for row in conn.execute("PRAGMA table_info(sources)").fetchall()
        }

    monkeypatch.setattr(user_state_module, "execute_sql_statements", original_execute)
    UserStateRepository(state_path)

    with sqlite3.connect(state_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (
            CURRENT_USER_STATE_SCHEMA_VERSION
        )
        assert conn.execute(
            "SELECT current_path, revision FROM sources WHERE uuid = 'source-uuid'"
        ).fetchone() == ("/source.sqlite", 0)


def _create_v3_index_with_task_status(
    index_path: Path,
    *,
    source_path: str,
    chunk_external_id: str | None = "chunk-1",
) -> None:
    with sqlite3.connect(index_path) as conn:
        migrate_to_v1(conn)
        migrate_to_v2(conn)
        migrate_to_v3(conn)
        conn.execute("PRAGMA user_version = 3")
        conn.execute(
            """
            INSERT INTO sources (id, kind, path, created_at, updated_at)
            VALUES (1, 'meetily_sqlite', ?, 'now', 'now')
            """,
            (source_path,),
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


def _create_v3_index_with_two_task_sources(
    index_path: Path,
    source_a: str,
    source_b: str,
) -> None:
    with sqlite3.connect(index_path) as conn:
        migrate_to_v1(conn)
        migrate_to_v2(conn)
        migrate_to_v3(conn)
        for source_id, suffix, source_path, status, note, provenance in (
            (1, "a", source_a, "done", "note-a", "manual-a"),
            (2, "b", source_b, "in_progress", "note-b", "manual-b"),
        ):
            conn.execute(
                """
                INSERT INTO sources (id, kind, path, created_at, updated_at)
                VALUES (?, 'meetily_sqlite', ?, 'created', 'updated')
                """,
                (source_id, source_path),
            )
            conn.execute(
                """
                INSERT INTO meetings (
                  id, source_id, external_id, title, fingerprint, indexed_at
                ) VALUES (?, ?, ?, ?, ?, 'indexed')
                """,
                (
                    source_id,
                    source_id,
                    f"meeting-{suffix}",
                    f"Meeting {suffix.upper()}",
                    f"meeting-fp-{suffix}",
                ),
            )
            conn.execute(
                """
                INSERT INTO chunks (
                  id, meeting_id, external_id, kind, ordinal, text, fingerprint
                ) VALUES (?, ?, ?, 'transcript', 0, ?, ?)
                """,
                (
                    source_id,
                    source_id,
                    f"chunk-{suffix}",
                    f"Ship plan {suffix}.",
                    f"chunk-fp-{suffix}",
                ),
            )
            conn.execute(
                """
                INSERT INTO action_items (
                  id, meeting_id, source_chunk_id, ordinal, text, source, confidence,
                  fingerprint, created_at, updated_at
                ) VALUES (?, ?, ?, 0, ?, 'heuristic', 0.8, ?, 'created', 'updated')
                """,
                (
                    source_id,
                    source_id,
                    source_id,
                    f"Ship plan {suffix}.",
                    f"entity-fp-{suffix}",
                ),
            )
            conn.execute(
                """
                INSERT INTO task_status_overrides (
                  action_item_id, status, note, source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'created', 'updated')
                """,
                (source_id, status, note, provenance),
            )
        conn.commit()
