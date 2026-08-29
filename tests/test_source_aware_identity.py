import json
import multiprocessing
import os
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from meetily_memory import user_state
from meetily_memory.cli import lifecycle_commands as lifecycle_module
from meetily_memory.cli.app import app
from meetily_memory.context_builder import group_hits_by_meeting
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.db.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    initialize_current_schema,
)
from meetily_memory.domain import AmbiguousMeetingError, MeetingRef
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.scanner import meetily_sqlite as scanner_module
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.serializers import search_results_payload
from meetily_memory.tagging import TagService
from meetily_memory.user_state import (
    USER_STATE_SCHEMA,
    AmbiguousSourceIdentityError,
    SourcePathClaim,
    UserStateRepository,
)

CLAIM_CRASH_EXIT_CODE = 92
HOT_ALIAS_JOURNAL_EXIT_CODE = 93
MULTI_SOURCE_FINALIZE_EXIT_CODE = 94
REBIND_ROLLBACK_CRASH_EXIT_CODE = 95


def _claim_source_path_and_exit(state_path: str, source_uuid: str, target_path: str) -> None:
    UserStateRepository(Path(state_path)).claim_source_path(
        source_uuid,
        MeetilySQLiteScanner.source_kind,
        Path(target_path),
        now="claimed-before-process-death",
    )
    os._exit(CLAIM_CRASH_EXIT_CODE)


def _fail_rebind_and_exit_during_compensation(  # noqa: PLR0913
    index_path: str,
    state_path: str,
    settings_path: str,
    source_uuid: str,
    target_path: str,
    checkpoint: str,
) -> None:
    def fail_settings_update(**_kwargs: object) -> None:
        message = "injected settings failure before compensation"
        raise RuntimeError(message)

    def exit_at_boundary(name: str) -> None:
        if name == checkpoint:
            os._exit(REBIND_ROLLBACK_CRASH_EXIT_CODE)

    setattr(lifecycle_module, "update_app_settings", fail_settings_update)  # noqa: B010
    setattr(  # noqa: B010
        lifecycle_module,
        "_rebind_compensation_checkpoint",
        exit_at_boundary,
    )
    state = UserStateRepository(Path(state_path))
    repo = IndexRepository(Path(index_path), state_path=Path(state_path))
    claim = state.claim_source_path(
        source_uuid,
        MeetilySQLiteScanner.source_kind,
        Path(target_path),
        now="rebind-before-compensation-crash",
    )
    lifecycle_module.rebind_source_identity(
        Path(index_path),
        state,
        claim,
        Path(target_path),
        Path(settings_path),
        repo=repo,
    )


def _leave_hot_alias_journal_and_exit(index_path: str) -> None:
    conn = sqlite3.connect(index_path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA cache_size=1")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE topic_aliases SET alias = 'uncommitted', normalized_alias = 'uncommitted'")
    conn.executemany(
        """
        INSERT INTO plugin_state (plugin_name, key, value_json, updated_at)
        VALUES ('hot-journal', ?, ?, 'crash')
        """,
        [(str(index), "x" * 3000) for index in range(100)],
    )
    os._exit(HOT_ALIAS_JOURNAL_EXIT_CODE)


def _rebuild_and_exit_during_first_claim_finalize(
    index_path: str,
    state_path: str,
    source_path: str,
) -> None:
    def exit_after_first_row(_name: str) -> None:
        if _name == "row":
            os._exit(MULTI_SOURCE_FINALIZE_EXIT_CODE)

    setattr(  # noqa: B010
        user_state,
        "_source_claim_finalize_checkpoint",
        exit_after_first_row,
    )
    MeetilySQLiteScanner(Path(index_path), state_path=Path(state_path)).scan(Path(source_path))


def _create_v3_index_with_task_status(index_path: Path, source_path: Path) -> None:
    canonical_path = str(source_path.resolve(strict=True))
    with sqlite3.connect(index_path) as conn:
        for version in range(1, 4):
            MIGRATIONS[version](conn)
        conn.execute(
            """
            INSERT INTO sources (id, kind, path, created_at, updated_at)
            VALUES (1, 'meetily_sqlite', ?, 'legacy-created', 'legacy-updated')
            """,
            (canonical_path,),
        )
        conn.execute(
            """
            INSERT INTO meetings (
              id, source_id, external_id, title, source_path, fingerprint, indexed_at
            ) VALUES (
              1, 1, 'meeting-2', 'Dobrynya Follow-up', ?, 'legacy-meeting', 'legacy-indexed'
            )
            """,
            (canonical_path,),
        )
        conn.execute(
            """
            INSERT INTO chunks (
              id, meeting_id, external_id, kind, ordinal, text, fingerprint
            ) VALUES (
              1, 1, 'transcript-2', 'transcript', 0,
              'Dobrynya agreed to send migration risks by Friday.', 'legacy-chunk'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO action_items (
              id, meeting_id, source_chunk_id, ordinal, text, source, confidence,
              fingerprint, created_at, updated_at
            ) VALUES (
              1, 1, 1, 0, 'Dobrynya agreed to send migration risks by Friday.',
              'heuristic', 0.9, 'legacy-task', 'legacy-created', 'legacy-updated'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO task_status_overrides (
              action_item_id, status, note, source, created_at, updated_at
            ) VALUES (
              1, 'done', 'survives v3 rebind', 'manual', 'legacy-created', 'legacy-updated'
            )
            """
        )
        conn.commit()


def _remove_index_generation_marker(index_path: Path) -> None:
    with sqlite3.connect(index_path) as conn:
        conn.execute("DROP TABLE index_generation")
        conn.commit()


def _insert_legacy_topic_alias(index_path: Path) -> tuple[object, ...]:
    expected = (
        "topic:migration",
        "migration",
        "migration",
        "topic-created",
        "topic-updated",
        '{"origin":"legacy"}',
        "move",
        "move",
        "alias-created",
    )
    with sqlite3.connect(index_path) as conn:
        conn.execute(
            """
            INSERT INTO knowledge_nodes (
              type, stable_key, title, normalized_title,
              created_at, updated_at, raw_metadata_json
            ) VALUES ('Topic', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(type, stable_key) DO UPDATE SET
              title = excluded.title,
              normalized_title = excluded.normalized_title,
              created_at = excluded.created_at,
              updated_at = excluded.updated_at,
              raw_metadata_json = excluded.raw_metadata_json
            """,
            expected[:6],
        )
        topic_id = int(
            conn.execute(
                "SELECT id FROM knowledge_nodes WHERE type = 'Topic' AND stable_key = ?",
                (expected[0],),
            ).fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO topic_aliases (
              topic_node_id, alias, normalized_alias, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (topic_id, *expected[6:]),
        )
        conn.commit()
    return expected


def _state_topic_alias_rows(state_path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(state_path) as conn:
        return conn.execute(
            """
            SELECT
              t.stable_key, t.title, t.normalized_title,
              t.created_at, t.updated_at, t.raw_metadata_json,
              a.alias, a.normalized_alias, a.created_at
            FROM topic_aliases a
            JOIN topic_alias_topics t ON t.stable_key = a.topic_stable_key
            ORDER BY a.normalized_alias
            """
        ).fetchall()


def _index_topic_alias_rows(index_path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(index_path) as conn:
        return conn.execute(
            """
            SELECT
              n.stable_key, n.title, n.normalized_title,
              n.created_at, n.updated_at, n.raw_metadata_json,
              a.alias, a.normalized_alias, a.created_at
            FROM topic_aliases a
            JOIN knowledge_nodes n ON n.id = a.topic_node_id
            ORDER BY a.normalized_alias
            """
        ).fetchall()


def _create_v5_index(
    index_path: Path,
    sources: tuple[tuple[str, str], ...],
) -> None:
    with sqlite3.connect(index_path) as conn:
        for version in MIGRATIONS:
            MIGRATIONS[version](conn)
        for source_id, (source_path, meeting_external_id) in enumerate(sources, start=1):
            conn.execute(
                """
                INSERT INTO sources (id, kind, path, created_at, updated_at)
                VALUES (?, 'meetily_sqlite', ?, 'legacy-created', 'legacy-updated')
                """,
                (source_id, source_path),
            )
            conn.execute(
                """
                INSERT INTO meetings (
                  id, source_id, external_id, title, source_path, fingerprint, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'legacy-indexed')
                """,
                (
                    source_id,
                    source_id,
                    meeting_external_id,
                    f"Legacy source {source_id}",
                    source_path,
                    f"legacy-fingerprint-{source_id}",
                ),
            )
        conn.commit()


def _copy_with_prefixed_meeting_ids(source_path: Path, target_path: Path, prefix: str) -> None:
    shutil.copy2(source_path, target_path)
    with sqlite3.connect(target_path) as conn:
        conn.execute("UPDATE meetings SET id = ? || id", (prefix,))
        conn.execute("UPDATE transcripts SET meeting_id = ? || meeting_id", (prefix,))
        conn.execute("UPDATE summary_processes SET meeting_id = ? || meeting_id", (prefix,))
        conn.execute("UPDATE meeting_notes SET meeting_id = ? || meeting_id", (prefix,))
        conn.commit()


def _table_counts(
    conn: sqlite3.Connection,
    tables: tuple[str, ...],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        query = f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - fixed test table names.
        counts[table] = int(conn.execute(query).fetchone()[0])
    return counts


def _legacy_index_semantics(index_path: Path) -> tuple[object, ...]:
    with sqlite3.connect(index_path) as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        schema = tuple(
            conn.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_master
                ORDER BY type, name
                """
            ).fetchall()
        )
        sources = tuple(conn.execute("SELECT id, kind, path FROM sources ORDER BY id").fetchall())
        meetings = tuple(
            conn.execute(
                """
                SELECT id, source_id, external_id, title, source_path, fingerprint, indexed_at
                FROM meetings
                ORDER BY id
                """
            ).fetchall()
        )
    return version, schema, sources, meetings


def test_duplicate_external_ids_keep_distinct_refs_evidence_context_and_json(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    second_source = tmp_path / "second-source.sqlite"
    shutil.copy2(meetily_db, second_source)
    index_path = tmp_path / "index.sqlite"
    first_scan = MeetilySQLiteScanner(index_path).scan(meetily_db)
    second_scan = MeetilySQLiteScanner(index_path).scan(second_source)
    core = MeetilyMemoryCore(index_path)

    search = core.search("pricing decision", limit=10)
    matching = tuple(
        result for result in search.results if result.meeting.external_id == "meeting-1"
    )
    refs = {result.meeting.ref for result in matching}
    evidence_ids = {hit.id for result in matching for hit in result.evidence}
    context = core.build_context("pricing decision", limit=10)
    groups = [
        group
        for group in group_hits_by_meeting(context.evidence)
        if group.ref.external_id == "meeting-1"
    ]

    assert len(matching) == 2
    assert {ref.source_uuid for ref in refs} == {
        first_scan.source_uuid,
        second_scan.source_uuid,
    }
    assert len(evidence_ids) == 2
    assert {group.ref for group in groups} == refs
    for ref in refs:
        meeting = core.get_meeting_by_ref(ref)
        assert meeting is not None
        assert meeting.ref == ref

    payload = search_results_payload(search)
    payload_results_value = payload["results"]
    assert isinstance(payload_results_value, list)
    payload_results = cast("list[dict[str, Any]]", payload_results_value)
    matching_payloads = [
        result
        for result in payload_results
        if result["meeting"]["ref"]["external_id"] == "meeting-1"
    ]
    assert {result["meeting"]["ref"]["source_uuid"] for result in matching_payloads} == {
        first_scan.source_uuid,
        second_scan.source_uuid,
    }
    assert all(
        result["evidence"][0]["excerpt"]["meeting_ref"] == result["meeting"]["ref"]
        for result in matching_payloads
    )
    assert all("id" not in result["meeting"] for result in matching_payloads)
    assert all("external_id" not in result["meeting"] for result in matching_payloads)

    with pytest.raises(AmbiguousMeetingError, match="ambiguous across sources"):
        core.get_meeting("meeting-1")

    cli = CliRunner().invoke(
        app,
        ["--index", str(index_path), "open", "--external-id", "meeting-1", "--print-path"],
    )
    assert cli.exit_code == 2
    assert "ambiguous across sources" in cli.output

    json_search = CliRunner().invoke(
        app,
        ["--index", str(index_path), "s", "pricing decision", "--json"],
    )
    assert json_search.exit_code == 0
    cli_payload = json.loads(json_search.stdout)
    assert {
        result["meeting"]["ref"]["source_uuid"]
        for result in cli_payload
        if result["meeting"]["ref"]["external_id"] == "meeting-1"
    } == {first_scan.source_uuid, second_scan.source_uuid}


def test_digit_only_external_id_is_not_confused_with_local_cli_id(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    with sqlite3.connect(meetily_db) as conn:
        conn.execute("UPDATE meetings SET id = '2' WHERE id = 'meeting-1'")
        conn.execute("UPDATE transcripts SET meeting_id = '2' WHERE meeting_id = 'meeting-1'")
        conn.execute("UPDATE summary_processes SET meeting_id = '2' WHERE meeting_id = 'meeting-1'")
        conn.commit()

    index_path = tmp_path / "index.sqlite"
    scan = MeetilySQLiteScanner(index_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path)
    digit_ref = MeetingRef(scan.source_uuid, "2")

    by_ref = core.get_meeting_by_ref(digit_ref)
    by_bare_external_id = core.get_meeting("2")
    by_local_id = core.get_meeting_by_local_id(2)

    assert by_ref is not None
    assert by_ref.title == "Launch Planning"
    assert by_bare_external_id == by_ref
    assert by_local_id is not None
    assert by_local_id.title == "Dobrynya Follow-up"

    runner = CliRunner()
    local_open = runner.invoke(
        app,
        ["--index", str(index_path), "open", "2", "--print-path"],
    )
    stable_open = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "open",
            "--source-uuid",
            scan.source_uuid,
            "--external-id",
            "2",
            "--print-path",
        ],
    )

    assert local_open.exit_code == 0
    assert local_open.stdout.strip() == str(tmp_path / "Dobrynya Follow-up")
    assert stable_open.exit_code == 0
    assert stable_open.stdout.strip() == str(tmp_path / "Launch Planning")


def test_relative_absolute_and_symlink_scans_share_one_canonical_identity(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_link = tmp_path / "source-link.sqlite"
    source_link.symlink_to(meetily_db)
    monkeypatch.chdir(tmp_path)
    index_path = tmp_path / "index.sqlite"
    scanner = MeetilySQLiteScanner(index_path)

    relative = scanner.scan(Path("meeting_minutes.sqlite"))
    absolute = scanner.scan(meetily_db.absolute())
    symlinked = scanner.scan(source_link)

    assert relative.source_uuid == absolute.source_uuid == symlinked.source_uuid
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        state_sources = conn.execute("SELECT uuid, current_path FROM sources").fetchall()
    with sqlite3.connect(index_path) as conn:
        index_sources = conn.execute("SELECT source_uuid, path FROM sources").fetchall()
    expected = (relative.source_uuid, str(meetily_db.resolve(strict=True)))
    assert state_sources == [expected]
    assert index_sources == [expected]


def test_ordinary_source_selection_ignores_unrelated_legacy_settings_alias(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "ordinary-selection"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    state_path = data_dir / "state.sqlite"
    settings_path = data_dir / "settings.json"
    legacy_link = tmp_path / "legacy-selected.sqlite"
    legacy_link.symlink_to(meetily_db)
    new_source = tmp_path / "new-source.sqlite"
    shutil.copy2(meetily_db, new_source)
    state = UserStateRepository(state_path)
    legacy_uuid = state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(legacy_link),
        now="legacy",
    )
    settings_path.write_text(
        json.dumps({"source_path": str(legacy_link)}) + "\n",
        encoding="utf-8",
    )

    selected = CliRunner().invoke(
        app,
        ["--index", str(index_path), "config", "source", str(new_source)],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert selected.exit_code == 0
    selected_uuid = json.loads(settings_path.read_text(encoding="utf-8"))["source_uuid"]
    assert selected_uuid != legacy_uuid
    with sqlite3.connect(state_path) as conn:
        assert set(conn.execute("SELECT uuid, current_path FROM sources")) == {
            (legacy_uuid, str(legacy_link)),
            (selected_uuid, str(new_source.resolve(strict=True))),
        }


@pytest.mark.parametrize("operation", ["scan", "select"])
def test_automatic_source_resolution_rejects_noncanonical_collision(
    meetily_db: Path,
    tmp_path: Path,
    operation: str,
) -> None:
    data_dir = tmp_path / operation
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    state_path = data_dir / "state.sqlite"
    source_link = tmp_path / f"{operation}-source.sqlite"
    source_link.symlink_to(meetily_db)
    state = UserStateRepository(state_path)
    source_uuid = state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(source_link),
        now="legacy-link",
    )
    IndexRepository(index_path, state_path=state_path)
    index_before = index_path.read_bytes()
    state_before = state_path.read_bytes()

    if operation == "scan":
        with pytest.raises(AmbiguousSourceIdentityError, match="--rebind"):
            MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    else:
        selected = CliRunner().invoke(
            app,
            ["--index", str(index_path), "config", "source", str(meetily_db)],
            env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
        )
        assert selected.exit_code != 0
        assert "--rebind" in selected.output

    assert index_path.read_bytes() == index_before
    assert state_path.read_bytes() == state_before
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT uuid, current_path FROM sources").fetchall() == [
            (source_uuid, str(source_link))
        ]


def test_canonical_state_ambiguity_aborts_without_merging_source_uuids(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.sqlite"
    index_path = tmp_path / "index.sqlite"
    state = UserStateRepository(state_path)
    canonical_path = meetily_db.resolve(strict=True)
    source_link = tmp_path / "ambiguous-source.sqlite"
    source_link.symlink_to(meetily_db)
    first_uuid = state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(canonical_path),
        now="first",
    )
    second_uuid = state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(source_link),
        now="second",
    )
    _create_v5_index(index_path, ((str(canonical_path), "meeting-1"),))
    active_before = index_path.read_bytes()

    with pytest.raises(AmbiguousSourceIdentityError, match="ambiguous"):
        MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)

    assert first_uuid != second_uuid
    assert index_path.read_bytes() == active_before
    with sqlite3.connect(state_path) as conn:
        assert set(conn.execute("SELECT uuid, current_path FROM sources")) == {
            (first_uuid, str(canonical_path)),
            (second_uuid, str(source_link)),
        }


def test_v3_task_status_transfer_precedes_rebind_claim_and_keeps_one_uuid(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "v3-rebind"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    state_path = data_dir / "state.sqlite"
    settings_path = data_dir / "settings.json"
    moved_source = tmp_path / "v3-moved.sqlite"
    shutil.copy2(meetily_db, moved_source)
    state = UserStateRepository(state_path)
    source_uuid = state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(meetily_db.resolve(strict=True)),
        now="state-created",
    )
    settings_path.write_text(json.dumps({"source_uuid": source_uuid}) + "\n", encoding="utf-8")
    _create_v3_index_with_task_status(index_path, meetily_db)

    rebound = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(moved_source),
            "--rebind",
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert rebound.exit_code == 0
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT uuid, current_path FROM sources").fetchall() == [
            (source_uuid, str(moved_source.resolve(strict=True)))
        ]
        assert conn.execute(
            "SELECT source_uuid, status, note FROM task_states WHERE orphaned = 0"
        ).fetchall() == [(source_uuid, "done", "survives v3 rebind")]
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5

    rebuilt = MeetilySQLiteScanner(index_path, state_path=state_path).scan(moved_source)
    tasks = MeetilyMemoryCore(index_path, state_path=state_path).structured_entities(
        "action_items", limit=100
    )

    assert rebuilt.source_uuid == source_uuid
    matching = [
        task
        for task in tasks.entities
        if task.text == "Dobrynya agreed to send migration risks by Friday."
    ]
    assert [(task.status, task.status_note) for task in matching] == [
        ("done", "survives v3 rebind")
    ]
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1


def test_legacy_v5_rebuild_projects_only_the_explicitly_registered_state_uuid(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    canonical_path = meetily_db.resolve(strict=True)
    state = UserStateRepository(state_path)
    expected_uuid = state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(canonical_path),
        now="registered",
    )
    with sqlite3.connect(index_path) as conn:
        for version in MIGRATIONS:
            MIGRATIONS[version](conn)
        conn.execute(
            """
            INSERT INTO sources (kind, path, created_at, updated_at)
            VALUES ('meetily_sqlite', ?, 'old', 'old')
            """,
            (str(canonical_path),),
        )
        conn.commit()

    def unexpected_uuid_generation() -> None:
        message = "source UUID must already belong to state.sqlite"
        raise AssertionError(message)

    monkeypatch.setattr(user_state.uuid, "uuid4", unexpected_uuid_generation)
    result = MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)

    assert result.source_uuid == expected_uuid
    with sqlite3.connect(index_path) as conn:
        source = conn.execute("SELECT source_uuid, path FROM sources").fetchone()
        source_uuid_column = next(
            row for row in conn.execute("PRAGMA table_info(sources)") if row[1] == "source_uuid"
        )
        unique_column_sets = {
            tuple(column[2] for column in conn.execute(f"PRAGMA index_info('{index_row[1]}')"))
            for index_row in conn.execute("PRAGMA index_list(sources)")
            if index_row[2]
        }
    assert source == (expected_uuid, str(canonical_path))
    assert source_uuid_column[3] == 1
    assert ("source_uuid",) in unique_column_sets

    backup_path = index_path.with_name(f"{index_path.name}.pre-v{CURRENT_SCHEMA_VERSION}")
    with sqlite3.connect(backup_path) as conn:
        legacy_columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
        legacy_path = conn.execute("SELECT path FROM sources").fetchone()[0]
    assert "source_uuid" not in legacy_columns
    assert legacy_path == str(canonical_path)


@pytest.mark.parametrize("collision_kind", ["cross-cwd-relative", "retargeted-symlink"])
def test_legacy_path_collision_requires_explicit_rebind_before_rebuild(  # noqa: PLR0915
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collision_kind: str,
) -> None:
    original_dir = tmp_path / "original"
    target_dir = tmp_path / "target"
    original_dir.mkdir()
    target_dir.mkdir()
    original_source = original_dir / "meeting_minutes.sqlite"
    target_source = target_dir / "meeting_minutes.sqlite"
    shutil.copy2(meetily_db, original_source)
    shutil.copy2(meetily_db, target_source)

    state_path = tmp_path / "state.sqlite"
    bootstrap_path = tmp_path / "bootstrap.sqlite"
    bootstrap_scan = MeetilySQLiteScanner(bootstrap_path, state_path=state_path).scan(
        original_source
    )
    bootstrap_repo = IndexRepository(bootstrap_path, state_path=state_path)
    bootstrap_core = MeetilyMemoryCore(bootstrap_path, state_path=state_path)
    meeting = bootstrap_core.get_meeting("meeting-2")
    assert meeting is not None
    task = next(
        entity
        for entity in bootstrap_core.structured_entities("action_items", limit=100).entities
        if entity.meeting_ref == meeting.ref
    )
    bootstrap_core.set_task_status(task.id, "done", note="survives explicit rebind")
    TagService(bootstrap_repo).assign((str(meeting.id),), ("explicit-rebind-tag",))

    legacy_link = tmp_path / "legacy-source.sqlite"
    if collision_kind == "cross-cwd-relative":
        monkeypatch.chdir(original_dir)
        stored_path = "meeting_minutes.sqlite"
    else:
        legacy_link.symlink_to(original_source)
        stored_path = str(legacy_link)
    bootstrap_repo.user_state.update_source_path(
        bootstrap_scan.source_uuid,
        stored_path,
        now="legacy-path",
    )

    index_path = tmp_path / "index.sqlite"
    settings_path = tmp_path / "settings.json"
    _create_v5_index(index_path, ((stored_path, "meeting-2"),))
    settings_path.write_text(
        json.dumps({"source_uuid": bootstrap_scan.source_uuid}) + "\n",
        encoding="utf-8",
    )
    if collision_kind == "cross-cwd-relative":
        monkeypatch.chdir(target_dir)
    else:
        legacy_link.unlink()
        legacy_link.symlink_to(target_source)

    index_bytes_before = index_path.read_bytes()
    index_semantics_before = _legacy_index_semantics(index_path)
    state_bytes_before = state_path.read_bytes()
    with pytest.raises(RuntimeError, match="--rebind"):
        MeetilySQLiteScanner(index_path, state_path=state_path).scan(target_source)

    assert index_path.read_bytes() == index_bytes_before
    assert _legacy_index_semantics(index_path) == index_semantics_before
    assert state_path.read_bytes() == state_bytes_before
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT uuid, current_path FROM sources").fetchall() == [
            (bootstrap_scan.source_uuid, stored_path)
        ]

    rebound = CliRunner().invoke(
        app,
        ["--index", str(index_path), "config", "source", str(target_source), "--rebind"],
        env={"MEETILY_MEMORY_DATA_DIR": str(tmp_path)},
    )

    assert rebound.exit_code == 0
    assert "matching meetings: 1" in rebound.stdout
    assert json.loads(settings_path.read_text(encoding="utf-8"))["source_uuid"] == (
        bootstrap_scan.source_uuid
    )
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT uuid, current_path FROM sources").fetchall() == [
            (bootstrap_scan.source_uuid, str(target_source.resolve(strict=True)))
        ]
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT path FROM sources").fetchone()[0] == str(
            target_source.resolve(strict=True)
        )

    rebuilt = MeetilySQLiteScanner(index_path, state_path=state_path).scan(target_source)
    rebuilt_core = MeetilyMemoryCore(index_path, state_path=state_path)
    rebuilt_meeting = rebuilt_core.get_meeting_by_ref(meeting.ref)

    assert rebuilt.source_uuid == bootstrap_scan.source_uuid
    assert rebuilt_meeting is not None
    rebuilt_tasks = [
        entity
        for entity in rebuilt_core.structured_entities("action_items", limit=100).entities
        if entity.meeting_ref == meeting.ref and entity.text == task.text
    ]
    assert [(entity.status, entity.status_note) for entity in rebuilt_tasks] == [
        ("done", "survives explicit rebind")
    ]
    assert [
        tag.display_name
        for tag in TagService(IndexRepository(index_path, state_path=state_path)).list_for_meeting(
            str(rebuilt_meeting.id)
        )
    ] == ["explicit-rebind-tag"]


def test_unindexed_explicit_rebind_preserves_selected_uuid(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    moved_db = tmp_path / "moved.sqlite"
    shutil.copy2(meetily_db, moved_db)
    repo = IndexRepository(index_path)
    selected_uuid = repo.user_state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(meetily_db.resolve(strict=True)),
        now="selected",
    )
    settings_path.write_text(json.dumps({"source_uuid": selected_uuid}) + "\n", encoding="utf-8")

    rebound = CliRunner().invoke(
        app,
        ["--index", str(index_path), "config", "source", str(moved_db), "--rebind"],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert rebound.exit_code == 0
    assert "matching meetings: 0" in rebound.stdout
    assert json.loads(settings_path.read_text(encoding="utf-8"))["source_uuid"] == selected_uuid
    with sqlite3.connect(data_dir / "state.sqlite") as conn:
        assert conn.execute("SELECT uuid, current_path FROM sources").fetchall() == [
            (selected_uuid, str(moved_db.resolve(strict=True)))
        ]
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0


def test_legacy_path_only_settings_rebinds_exact_raw_state_source(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "legacy-settings"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    state_path = data_dir / "state.sqlite"
    settings_path = data_dir / "settings.json"
    legacy_link = tmp_path / "legacy-settings-source.sqlite"
    legacy_link.symlink_to(meetily_db)
    state = UserStateRepository(state_path)
    source_uuid = state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(legacy_link),
        now="legacy",
    )
    _create_v5_index(index_path, ((str(legacy_link), "meeting-1"),))
    settings_path.write_text(
        json.dumps({"source_path": str(legacy_link)}) + "\n",
        encoding="utf-8",
    )

    rebound = CliRunner().invoke(
        app,
        ["--index", str(index_path), "config", "source", str(meetily_db), "--rebind"],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert rebound.exit_code == 0
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["source_uuid"] == source_uuid
    assert "source_path" not in settings
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT uuid, current_path FROM sources").fetchall() == [
            (source_uuid, str(meetily_db.resolve(strict=True)))
        ]
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("SELECT path FROM sources").fetchone()[0] == str(
            meetily_db.resolve(strict=True)
        )


def test_symlink_only_legacy_settings_fail_without_exact_raw_state_match(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "symlink-only-legacy-settings"
    index_path = data_dir / "index.sqlite"
    state_path = data_dir / "state.sqlite"
    settings_path = data_dir / "settings.json"
    scan = MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    source_link = tmp_path / "legacy-settings-link.sqlite"
    source_link.symlink_to(meetily_db)
    settings_payload = {"source_path": str(source_link)}
    settings_path.write_text(json.dumps(settings_payload) + "\n", encoding="utf-8")
    state_before = state_path.read_bytes()
    index_before = index_path.read_bytes()

    refreshed = CliRunner().invoke(
        app,
        ["--index", str(index_path), "refresh"],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert refreshed.exit_code != 0
    assert "exact state-owned current_path or pending projected_path" in refreshed.output
    assert json.loads(settings_path.read_text(encoding="utf-8")) == settings_payload
    assert state_path.read_bytes() == state_before
    assert index_path.read_bytes() == index_before
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT uuid FROM sources").fetchall() == [(scan.source_uuid,)]


def test_legacy_path_only_settings_missing_identity_advises_source_uuid(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "missing-legacy-settings"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    state_path = data_dir / "state.sqlite"
    settings_path = data_dir / "settings.json"
    UserStateRepository(state_path)
    IndexRepository(index_path, state_path=state_path)
    settings_path.write_text(
        json.dumps({"source_path": "legacy-relative.sqlite"}) + "\n",
        encoding="utf-8",
    )
    state_before = state_path.read_bytes()
    index_before = index_path.read_bytes()

    rebound = CliRunner().invoke(
        app,
        ["--index", str(index_path), "config", "source", str(meetily_db), "--rebind"],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert rebound.exit_code != 0
    assert "--source-uuid" in rebound.output
    assert state_path.read_bytes() == state_before
    assert index_path.read_bytes() == index_before


def test_explicit_rebind_rejects_target_owned_by_another_uuid(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "owned-target"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    other_source = tmp_path / "other-source.sqlite"
    shutil.copy2(meetily_db, other_source)
    repo = IndexRepository(index_path)
    selected_uuid = repo.user_state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(meetily_db.resolve(strict=True)),
        now="selected",
    )
    other_uuid = repo.user_state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(other_source.resolve(strict=True)),
        now="other",
    )
    settings_path.write_text(json.dumps({"source_uuid": selected_uuid}) + "\n", encoding="utf-8")
    state_before = (data_dir / "state.sqlite").read_bytes()
    index_before = index_path.read_bytes()
    settings_before = settings_path.read_bytes()

    rebound = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(other_source),
            "--rebind",
            "--source-uuid",
            selected_uuid,
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert rebound.exit_code != 0
    assert "another source UUID" in rebound.output
    assert (data_dir / "state.sqlite").read_bytes() == state_before
    assert index_path.read_bytes() == index_before
    assert settings_path.read_bytes() == settings_before
    assert selected_uuid != other_uuid


def test_explicit_rebind_owned_target_does_not_migrate_v3_index(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "owned-target-v3"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    state_path = data_dir / "state.sqlite"
    settings_path = data_dir / "settings.json"
    other_source = tmp_path / "owned-v3-other.sqlite"
    shutil.copy2(meetily_db, other_source)
    state = UserStateRepository(state_path)
    selected_uuid = state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(meetily_db.resolve(strict=True)),
        now="selected",
    )
    state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(other_source.resolve(strict=True)),
        now="owner",
    )
    settings_path.write_text(json.dumps({"source_uuid": selected_uuid}) + "\n", encoding="utf-8")
    _create_v3_index_with_task_status(index_path, meetily_db)
    index_before = index_path.read_bytes()
    state_before = state_path.read_bytes()

    rebound = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(other_source),
            "--rebind",
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert rebound.exit_code != 0
    assert "another source UUID" in rebound.output
    assert index_path.read_bytes() == index_before
    assert state_path.read_bytes() == state_before
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM task_status_overrides").fetchone()[0] == 1


def test_explicit_rebind_unknown_uuid_does_not_migrate_v1_index(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "unknown-v1-source-uuid"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    state_path = data_dir / "state.sqlite"
    with sqlite3.connect(state_path) as conn:
        conn.executescript(USER_STATE_SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    with sqlite3.connect(index_path) as conn:
        MIGRATIONS[1](conn)
    state_before = state_path.read_bytes()
    index_before = index_path.read_bytes()

    rebound = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(meetily_db),
            "--rebind",
            "--source-uuid",
            "missing-source-uuid",
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert rebound.exit_code != 0
    assert "Source UUID not found" in rebound.output
    assert state_path.read_bytes() == state_before
    assert index_path.read_bytes() == index_before
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_explicit_rebind_unknown_uuid_with_absent_state_creates_no_databases(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "absent-state-unknown-source-uuid"
    index_path = data_dir / "index.sqlite"

    rebound = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(meetily_db),
            "--rebind",
            "--source-uuid",
            "missing-source-uuid",
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert rebound.exit_code != 0
    assert "Source UUID not found" in rebound.output
    assert not (data_dir / "state.sqlite").exists()
    assert not index_path.exists()


def test_implicit_rebind_unknown_selected_uuid_with_absent_state_creates_no_databases(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "absent-state-unknown-selected-uuid"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    settings_payload = {"source_uuid": "missing-selected-source-uuid"}
    settings_path.write_text(json.dumps(settings_payload) + "\n", encoding="utf-8")

    rebound = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(meetily_db),
            "--rebind",
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert rebound.exit_code != 0
    assert "Source UUID not found" in rebound.output
    assert json.loads(settings_path.read_text(encoding="utf-8")) == settings_payload
    assert not (data_dir / "state.sqlite").exists()
    assert not index_path.exists()


@pytest.mark.parametrize("settings_kind", ["none", "unmatched-path"])
def test_rebind_without_readonly_identity_does_not_create_state_or_index(
    meetily_db: Path,
    tmp_path: Path,
    settings_kind: str,
) -> None:
    data_dir = tmp_path / f"unresolved-rebind-{settings_kind}"
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    settings_payload: dict[str, str] | None = None
    if settings_kind == "unmatched-path":
        data_dir.mkdir()
        settings_payload = {"source_path": str(meetily_db)}
        settings_path.write_text(json.dumps(settings_payload) + "\n", encoding="utf-8")

    rebound = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(meetily_db),
            "--rebind",
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert rebound.exit_code != 0
    expected_error = (
        "No source identity is selected"
        if settings_kind == "none"
        else "No state-owned source has the exact legacy path"
    )
    assert expected_error in rebound.output
    assert not (data_dir / "state.sqlite").exists()
    assert not index_path.exists()
    if settings_kind == "unmatched-path":
        assert settings_payload is not None
        assert json.loads(settings_path.read_text(encoding="utf-8")) == settings_payload


def test_path_only_rebind_missing_exact_identity_does_not_migrate_old_databases(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "unmatched-path-old-databases"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    state_path = data_dir / "state.sqlite"
    settings_path = data_dir / "settings.json"
    with sqlite3.connect(state_path) as conn:
        conn.executescript(USER_STATE_SCHEMA)
        conn.execute(
            """
            INSERT INTO sources (uuid, kind, current_path, created_at, updated_at)
            VALUES ('other-source', 'meetily_sqlite', '/other.sqlite', 'created', 'updated')
            """
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    with sqlite3.connect(index_path) as conn:
        MIGRATIONS[1](conn)
    settings_payload = {"source_path": str(meetily_db)}
    settings_path.write_text(json.dumps(settings_payload) + "\n", encoding="utf-8")
    state_before = state_path.read_bytes()
    index_before = index_path.read_bytes()

    rebound = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(meetily_db),
            "--rebind",
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert rebound.exit_code != 0
    assert "No state-owned source has the exact legacy path" in rebound.output
    assert json.loads(settings_path.read_text(encoding="utf-8")) == settings_payload
    assert state_path.read_bytes() == state_before
    assert index_path.read_bytes() == index_before
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_explicit_rebind_rejects_unknown_source_uuid_without_mutation(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "unknown-source-uuid"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    repo = IndexRepository(index_path)
    selected_uuid = repo.user_state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(meetily_db.resolve(strict=True)),
        now="selected",
    )
    settings_path.write_text(json.dumps({"source_uuid": selected_uuid}) + "\n", encoding="utf-8")
    state_before = (data_dir / "state.sqlite").read_bytes()
    index_before = index_path.read_bytes()
    settings_before = settings_path.read_bytes()

    rebound = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(meetily_db),
            "--rebind",
            "--source-uuid",
            "missing-source-uuid",
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert rebound.exit_code != 0
    assert "Source UUID not found" in rebound.output
    assert (data_dir / "state.sqlite").read_bytes() == state_before
    assert index_path.read_bytes() == index_before
    assert settings_path.read_bytes() == settings_before


def test_rebind_updates_v6_meeting_paths_even_when_fingerprints_are_unchanged(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "unchanged-fingerprints"
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    moved_source = tmp_path / "unchanged-moved.sqlite"
    shutil.copy2(meetily_db, moved_source)
    scan = MeetilySQLiteScanner(index_path).scan(meetily_db)
    settings_path.write_text(
        json.dumps({"source_uuid": scan.source_uuid}) + "\n",
        encoding="utf-8",
    )
    with sqlite3.connect(index_path) as conn:
        fingerprints_before = conn.execute(
            "SELECT external_id, fingerprint FROM meetings ORDER BY external_id"
        ).fetchall()

    rebound = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(moved_source),
            "--rebind",
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert rebound.exit_code == 0
    moved_path = str(moved_source.resolve(strict=True))
    with sqlite3.connect(index_path) as conn:
        assert (
            conn.execute(
                "SELECT external_id, fingerprint FROM meetings ORDER BY external_id"
            ).fetchall()
            == fingerprints_before
        )
        assert conn.execute("SELECT path FROM sources").fetchall() == [(moved_path,)]
        assert set(conn.execute("SELECT source_path FROM meetings")) == {(moved_path,)}


def test_run_refresh_heals_v6_projection_after_state_claim_crash(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "refresh-crash-recovery"
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    moved_source = tmp_path / "refresh-crash-moved.sqlite"
    shutil.copy2(meetily_db, moved_source)
    scan = MeetilySQLiteScanner(index_path).scan(meetily_db)
    settings_path.write_text(
        json.dumps({"source_uuid": scan.source_uuid}) + "\n",
        encoding="utf-8",
    )
    state = UserStateRepository(data_dir / "state.sqlite")
    state.claim_source_path(
        scan.source_uuid,
        MeetilySQLiteScanner.source_kind,
        moved_source,
        now="claimed-before-process-death",
    )
    with sqlite3.connect(index_path) as conn:
        fingerprints_before = conn.execute(
            "SELECT external_id, fingerprint FROM meetings ORDER BY external_id"
        ).fetchall()

    payload, _ = lifecycle_module.run_refresh(index_path, settings_path, moved_source)

    assert payload["meetings_updated"] == 0
    moved_path = str(moved_source.resolve(strict=True))
    with sqlite3.connect(index_path) as conn:
        assert (
            conn.execute(
                "SELECT external_id, fingerprint FROM meetings ORDER BY external_id"
            ).fetchall()
            == fingerprints_before
        )
        assert (
            conn.execute(
                "SELECT path FROM sources WHERE source_uuid = ?",
                (scan.source_uuid,),
            ).fetchone()[0]
            == moved_path
        )
        assert set(conn.execute("SELECT source_path FROM meetings")) == {(moved_path,)}
    opened = CliRunner().invoke(
        app,
        ["--index", str(index_path), "open", "1", "--source", "--print-path"],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )
    assert opened.exit_code == 0
    assert opened.stdout.strip() == moved_path


def test_same_target_rebind_repairs_v6_projection_after_state_claim_crash(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "rebind-crash-recovery"
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    moved_source = tmp_path / "rebind-crash-moved.sqlite"
    shutil.copy2(meetily_db, moved_source)
    scan = MeetilySQLiteScanner(index_path).scan(meetily_db)
    settings_path.write_text(
        json.dumps({"source_uuid": scan.source_uuid}) + "\n",
        encoding="utf-8",
    )
    state = UserStateRepository(data_dir / "state.sqlite")
    crashed_claim = state.claim_source_path(
        scan.source_uuid,
        MeetilySQLiteScanner.source_kind,
        moved_source,
        now="claimed-before-process-death",
    )

    rebound = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(moved_source),
            "--rebind",
            "--source-uuid",
            scan.source_uuid,
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert rebound.exit_code == 0
    assert "matching meetings: 2" in rebound.output
    assert "incomplete" not in rebound.output.lower()
    moved_path = str(moved_source.resolve(strict=True))
    with sqlite3.connect(data_dir / "state.sqlite") as conn:
        assert conn.execute(
            "SELECT current_path, revision FROM sources WHERE uuid = ?",
            (scan.source_uuid,),
        ).fetchone() == (moved_path, crashed_claim.claimed_revision + 1)
    with sqlite3.connect(index_path) as conn:
        assert (
            conn.execute(
                "SELECT path FROM sources WHERE source_uuid = ?",
                (scan.source_uuid,),
            ).fetchone()[0]
            == moved_path
        )
        assert set(conn.execute("SELECT source_path FROM meetings")) == {(moved_path,)}


def test_process_death_after_v5_claim_same_target_retry_repairs_and_rebuilds(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "v5-claim-crash"
    index_path = data_dir / "index.sqlite"
    state_path = data_dir / "state.sqlite"
    settings_path = data_dir / "settings.json"
    moved_source = tmp_path / "v5-claim-moved.sqlite"
    shutil.copy2(meetily_db, moved_source)
    scan = MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    settings_path.write_text(
        json.dumps({"source_uuid": scan.source_uuid}) + "\n",
        encoding="utf-8",
    )
    with sqlite3.connect(index_path) as conn:
        conn.execute("PRAGMA user_version = 5")
        conn.commit()

    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_claim_source_path_and_exit,
        args=(str(state_path), scan.source_uuid, str(moved_source)),
    )
    process.start()
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("claim crash child process did not exit")
    assert process.exitcode == CLAIM_CRASH_EXIT_CODE
    old_path = str(meetily_db.resolve(strict=True))
    moved_path = str(moved_source.resolve(strict=True))
    with sqlite3.connect(state_path) as conn:
        pending = conn.execute(
            """
            SELECT current_path, projected_path, revision, pending_revision
            FROM sources
            WHERE uuid = ?
            """,
            (scan.source_uuid,),
        ).fetchone()
    assert pending[0:2] == (moved_path, old_path)
    assert pending[2] == pending[3]

    restarted = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(moved_source),
            "--rebind",
            "--source-uuid",
            scan.source_uuid,
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )
    rebuilt = MeetilySQLiteScanner(index_path, state_path=state_path).scan(moved_source)

    assert restarted.exit_code == 0
    assert rebuilt.source_uuid == scan.source_uuid
    with sqlite3.connect(state_path) as conn:
        assert conn.execute(
            """
            SELECT uuid, current_path, projected_path, pending_revision
            FROM sources
            """
        ).fetchall() == [(scan.source_uuid, moved_path, moved_path, None)]
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert conn.execute("SELECT source_uuid, path FROM sources").fetchall() == [
            (scan.source_uuid, moved_path)
        ]
        assert set(conn.execute("SELECT source_path FROM meetings")) == {(moved_path,)}


def test_public_rebinds_do_not_allow_state_c_with_stale_index_b(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "serialized-rebinds"
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    source_b = tmp_path / "source-b.sqlite"
    source_c = tmp_path / "source-c.sqlite"
    shutil.copy2(meetily_db, source_b)
    shutil.copy2(meetily_db, source_c)
    scan = MeetilySQLiteScanner(index_path).scan(meetily_db)
    settings_path.write_text(
        json.dumps({"source_uuid": scan.source_uuid}) + "\n",
        encoding="utf-8",
    )
    original_projection = IndexRepository.rebind_source_path_projection
    competing_results: list[tuple[int, str]] = []
    attempted = False

    def project_with_competing_rebind(
        self: IndexRepository,
        claim: SourcePathClaim,
    ) -> set[str]:
        nonlocal attempted
        if not attempted:
            attempted = True
            competing = CliRunner().invoke(
                app,
                [
                    "--index",
                    str(index_path),
                    "config",
                    "source",
                    str(source_c),
                    "--rebind",
                    "--source-uuid",
                    scan.source_uuid,
                ],
                env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
            )
            competing_results.append((competing.exit_code, competing.output))
        return original_projection(self, claim)

    monkeypatch.setattr(
        IndexRepository,
        "rebind_source_path_projection",
        project_with_competing_rebind,
    )
    first = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(source_b),
            "--rebind",
            "--source-uuid",
            scan.source_uuid,
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert first.exit_code == 0
    assert len(competing_results) == 1
    assert competing_results[0][0] != 0
    assert "refresh is already running" in competing_results[0][1].lower()

    retried = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(source_c),
            "--rebind",
            "--source-uuid",
            scan.source_uuid,
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert retried.exit_code == 0
    final_path = str(source_c.resolve(strict=True))
    with sqlite3.connect(data_dir / "state.sqlite") as conn:
        assert (
            conn.execute(
                "SELECT current_path FROM sources WHERE uuid = ?",
                (scan.source_uuid,),
            ).fetchone()[0]
            == final_path
        )
    with sqlite3.connect(index_path) as conn:
        assert (
            conn.execute(
                "SELECT path FROM sources WHERE source_uuid = ?",
                (scan.source_uuid,),
            ).fetchone()[0]
            == final_path
        )
        assert set(conn.execute("SELECT source_path FROM meetings")) == {(final_path,)}


@pytest.mark.parametrize("checkpoint", ["rollback_pending", "index_rolled_back"])
def test_rebind_compensation_process_death_is_healed_without_duplicate_source(
    meetily_db: Path,
    tmp_path: Path,
    checkpoint: str,
) -> None:
    data_dir = tmp_path / f"compensation-crash-{checkpoint}"
    index_path = data_dir / "index.sqlite"
    state_path = data_dir / "state.sqlite"
    settings_path = data_dir / "settings.json"
    target_source = tmp_path / f"compensation-target-{checkpoint}.sqlite"
    shutil.copy2(meetily_db, target_source)
    scanner = MeetilySQLiteScanner(index_path, state_path=state_path)
    scan = scanner.scan(meetily_db)
    settings_path.write_text(
        json.dumps({"source_uuid": scan.source_uuid}) + "\n",
        encoding="utf-8",
    )

    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_fail_rebind_and_exit_during_compensation,
        args=(
            str(index_path),
            str(state_path),
            str(settings_path),
            scan.source_uuid,
            str(target_source),
            checkpoint,
        ),
    )
    process.start()
    process.join(timeout=30)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("rebind compensation crash child process did not exit")
    assert process.exitcode == REBIND_ROLLBACK_CRASH_EXIT_CODE

    old_path = str(meetily_db.resolve(strict=True))
    target_path = str(target_source.resolve(strict=True))
    with sqlite3.connect(state_path) as conn:
        pending = conn.execute(
            """
            SELECT current_path, projected_path, revision, pending_revision
            FROM sources
            WHERE uuid = ?
            """,
            (scan.source_uuid,),
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    assert pending[0:2] == (old_path, target_path)
    assert pending[2] == pending[3]
    with sqlite3.connect(index_path) as conn:
        projected_path = conn.execute(
            "SELECT path FROM sources WHERE source_uuid = ?",
            (scan.source_uuid,),
        ).fetchone()[0]
    expected_projection = target_path if checkpoint == "rollback_pending" else old_path
    assert projected_path == expected_projection

    state = UserStateRepository(state_path)
    with pytest.raises(AmbiguousSourceIdentityError, match="reserved"):
        state.resolve_source(
            MeetilySQLiteScanner.source_kind,
            target_source,
            now="must-not-create-duplicate",
        )
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1

    healed = scanner.scan(meetily_db)

    assert healed.source_uuid == scan.source_uuid
    with sqlite3.connect(state_path) as conn:
        assert conn.execute(
            """
            SELECT current_path, projected_path, pending_revision
            FROM sources
            WHERE uuid = ?
            """,
            (scan.source_uuid,),
        ).fetchone() == (old_path, old_path, None)
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    with sqlite3.connect(index_path) as conn:
        assert conn.execute(
            "SELECT path FROM sources WHERE source_uuid = ?",
            (scan.source_uuid,),
        ).fetchone() == (old_path,)
        assert set(conn.execute("SELECT source_path FROM meetings")) == {(old_path,)}


@pytest.mark.parametrize("failure_step", ["projection", "settings"])
def test_rebind_failure_leaves_rolled_back_or_recoverable_consistent_state(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_step: str,
) -> None:
    data_dir = tmp_path / failure_step
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    target_source = tmp_path / f"{failure_step}-target.sqlite"
    shutil.copy2(meetily_db, target_source)
    scanner = MeetilySQLiteScanner(index_path)
    scan = scanner.scan(meetily_db)
    previous_selected_uuid = UserStateRepository(data_dir / "state.sqlite").get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        "/previously-selected.sqlite",
        now="selected",
    )
    settings_path.write_text(
        json.dumps({"source_uuid": previous_selected_uuid}) + "\n",
        encoding="utf-8",
    )
    old_path = str(meetily_db.resolve(strict=True))
    settings_before = settings_path.read_bytes()

    if failure_step == "projection":
        original_rebind_projection = IndexRepository.rebind_source_path_projection

        def project_then_fail(
            self: IndexRepository,
            claim: SourcePathClaim,
        ) -> set[str]:
            original_rebind_projection(self, claim)
            message = "injected projection failure"
            raise RuntimeError(message)

        monkeypatch.setattr(
            IndexRepository,
            "rebind_source_path_projection",
            project_then_fail,
        )
    else:
        original_settings_update = lifecycle_module.update_app_settings

        def update_settings_then_fail(
            *,
            settings_path: Path | None = None,
            source_uuid: str | None = None,
            source_path: str | None = None,
        ) -> None:
            original_settings_update(
                settings_path=settings_path,
                source_uuid=source_uuid,
                source_path=source_path,
            )
            message = "injected settings failure"
            raise RuntimeError(message)

        monkeypatch.setattr(
            lifecycle_module,
            "update_app_settings",
            update_settings_then_fail,
        )

    rebound = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(target_source),
            "--rebind",
            "--source-uuid",
            scan.source_uuid,
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert rebound.exit_code != 0
    settings_payload = json.loads(settings_path.read_text(encoding="utf-8"))
    expected_selected_uuid = (
        previous_selected_uuid if failure_step == "projection" else scan.source_uuid
    )
    assert settings_payload["source_uuid"] == expected_selected_uuid
    assert "source_path" not in settings_payload
    if failure_step == "projection":
        assert settings_path.read_bytes() == settings_before
    else:
        assert lifecycle_module.configured_source_path(index_path, settings_path) == (
            meetily_db.resolve(strict=True)
        )
    with sqlite3.connect(data_dir / "state.sqlite") as conn:
        assert (
            conn.execute(
                "SELECT current_path FROM sources WHERE uuid = ?",
                (scan.source_uuid,),
            ).fetchone()[0]
            == old_path
        )
    with sqlite3.connect(index_path) as conn:
        assert (
            conn.execute(
                "SELECT path FROM sources WHERE source_uuid = ?",
                (scan.source_uuid,),
            ).fetchone()[0]
            == old_path
        )
        assert {
            row[0]
            for row in conn.execute(
                """
                SELECT DISTINCT source_path
                FROM meetings
                WHERE source_id = (SELECT id FROM sources WHERE source_uuid = ?)
                """,
                (scan.source_uuid,),
            )
        } == {old_path}


def test_old_rebind_compensation_cannot_undo_newer_same_target_state_or_settings(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "same-target-compensation"
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    target_source = tmp_path / "same-target.sqlite"
    shutil.copy2(meetily_db, target_source)
    scan = MeetilySQLiteScanner(index_path).scan(meetily_db)
    state = UserStateRepository(data_dir / "state.sqlite")
    previous_selected_uuid = state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        "/previously-selected.sqlite",
        now="selected",
    )
    settings_path.write_text(
        json.dumps({"source_uuid": previous_selected_uuid}) + "\n",
        encoding="utf-8",
    )
    original_settings_update = lifecycle_module.update_app_settings
    newer_claims: list[SourcePathClaim] = []

    def update_settings_claim_again_then_fail(
        *,
        settings_path: Path | None = None,
        source_uuid: str | None = None,
        source_path: str | None = None,
    ) -> None:
        original_settings_update(
            settings_path=settings_path,
            source_uuid=source_uuid,
            source_path=source_path,
        )
        assert source_uuid is not None
        newer_claims.append(
            state.claim_source_path(
                source_uuid,
                MeetilySQLiteScanner.source_kind,
                target_source,
                now="newer-same-target-claim",
            )
        )
        original_settings_update(
            settings_path=settings_path,
            source_uuid=source_uuid,
            source_path=source_path,
        )
        message = "injected failure after newer same-target selection"
        raise RuntimeError(message)

    monkeypatch.setattr(
        lifecycle_module,
        "update_app_settings",
        update_settings_claim_again_then_fail,
    )

    rebound = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(target_source),
            "--rebind",
            "--source-uuid",
            scan.source_uuid,
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert rebound.exit_code != 0
    assert "newer claim" in rebound.output
    assert len(newer_claims) == 1
    target_path = str(target_source.resolve(strict=True))
    with sqlite3.connect(data_dir / "state.sqlite") as conn:
        assert conn.execute(
            "SELECT current_path, revision FROM sources WHERE uuid = ?",
            (scan.source_uuid,),
        ).fetchone() == (target_path, newer_claims[0].claimed_revision)
    with sqlite3.connect(index_path) as conn:
        assert (
            conn.execute(
                "SELECT path FROM sources WHERE source_uuid = ?",
                (scan.source_uuid,),
            ).fetchone()[0]
            == target_path
        )
        assert set(conn.execute("SELECT source_path FROM meetings")) == {(target_path,)}
    assert json.loads(settings_path.read_text(encoding="utf-8"))["source_uuid"] == (
        scan.source_uuid
    )


def test_run_refresh_v5_rebuild_restores_full_derived_snapshot_and_task_state(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    settings_path = tmp_path / "settings.json"
    scan = MeetilySQLiteScanner(index_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path)
    task = core.structured_entities("action_items", limit=100).entities[0]
    core.set_task_status(task.id, "done", note="survives production refresh rebuild")
    entity_tables = ("action_items", "decisions", "risks", "open_questions")
    with sqlite3.connect(index_path) as conn:
        counts_before = _table_counts(conn, entity_tables)
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
    assert all(count > 0 for count in counts_before.values())
    settings_path.write_text(
        json.dumps({"source_uuid": scan.source_uuid}) + "\n",
        encoding="utf-8",
    )

    payload, _ = lifecycle_module.run_refresh(index_path, settings_path, meetily_db)

    assert payload["meetings_analyzed"] == 2
    with sqlite3.connect(index_path) as conn:
        counts_after = _table_counts(conn, entity_tables)
    assert counts_after == counts_before
    rebuilt_tasks = [
        entity
        for entity in MeetilyMemoryCore(index_path)
        .structured_entities("action_items", limit=100)
        .entities
        if entity.meeting_ref == task.meeting_ref and entity.text == task.text
    ]
    assert [(entity.status, entity.status_note) for entity in rebuilt_tasks] == [
        ("done", "survives production refresh rebuild")
    ]


def test_v6_manual_topic_alias_survives_full_index_deletion_from_state(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    MeetilyMemoryCore(index_path, state_path=state_path).add_topic_alias("migration", ["move"])
    expected = _state_topic_alias_rows(state_path)

    index_path.unlink()
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)

    assert _state_topic_alias_rows(state_path) == expected
    assert _index_topic_alias_rows(index_path) == expected
    topic = MeetilyMemoryCore(index_path, state_path=state_path).topic("move").topic
    assert topic.title == "migration"
    assert topic.aliases == ("move",)


def test_manual_topic_alias_reads_and_deletes_from_state_before_projection(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "alias-state-first.sqlite"
    state_path = tmp_path / "alias-state-first-state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path, state_path=state_path)
    core.add_topic_alias("migration", ["move"])
    with sqlite3.connect(index_path) as conn:
        conn.execute("DELETE FROM topic_aliases")
        conn.commit()

    healed = MeetilyMemoryCore(index_path, state_path=state_path).topic("move").topic
    removed = IndexRepository(index_path, state_path=state_path).remove_topic_aliases(["move"])

    assert healed.title == "migration"
    assert healed.aliases == ("move",)
    assert removed == ("move",)
    assert _state_topic_alias_rows(state_path) == []
    assert _index_topic_alias_rows(index_path) == []


def test_legacy_v5_topic_alias_imports_once_and_survives_swap_and_redelete(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    settings_path = tmp_path / "settings.json"
    scan = MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    _remove_index_generation_marker(index_path)
    expected = _insert_legacy_topic_alias(index_path)
    with sqlite3.connect(index_path) as conn:
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
    settings_path.write_text(
        json.dumps({"source_uuid": scan.source_uuid}) + "\n",
        encoding="utf-8",
    )

    lifecycle_module.run_refresh(index_path, settings_path, meetily_db)

    assert _state_topic_alias_rows(state_path) == [expected]
    assert _index_topic_alias_rows(index_path) == [expected]
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM topic_alias_imports").fetchone()[0] == 1

    for _ in range(2):
        index_path.unlink()
        MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
        assert _state_topic_alias_rows(state_path) == [expected]
        assert _index_topic_alias_rows(index_path) == [expected]
    topic = MeetilyMemoryCore(index_path, state_path=state_path).topic("move").topic
    assert topic.title == "migration"
    assert topic.aliases == ("move",)


def test_existing_v6_topic_alias_imports_before_rebuildable_index_is_deleted(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "legacy-v6-alias.sqlite"
    state_path = tmp_path / "legacy-v6-alias-state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    _remove_index_generation_marker(index_path)
    expected = _insert_legacy_topic_alias(index_path)

    IndexRepository(index_path, state_path=state_path)
    assert _state_topic_alias_rows(state_path) == [expected]

    index_path.unlink()
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    assert _state_topic_alias_rows(state_path) == [expected]
    assert _index_topic_alias_rows(index_path) == [expected]


@pytest.mark.parametrize(
    "checkpoint",
    ["row", "before_recheck", "before_commit", "committed"],
)
def test_topic_alias_import_crash_retry_is_atomic_and_idempotent(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
) -> None:
    index_path = tmp_path / f"alias-import-{checkpoint}.sqlite"
    state_path = tmp_path / f"alias-import-{checkpoint}-state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    _remove_index_generation_marker(index_path)
    expected = _insert_legacy_topic_alias(index_path)
    with sqlite3.connect(index_path) as conn:
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
    triggered = False

    def fail_once(name: str) -> None:
        nonlocal triggered
        if name == checkpoint and not triggered:
            triggered = True
            message = f"injected alias import crash at {checkpoint}"
            raise RuntimeError(message)

    monkeypatch.setattr(user_state, "_topic_alias_import_checkpoint", fail_once)
    with pytest.raises(RuntimeError, match="injected alias import crash"):
        IndexRepository(index_path, state_path=state_path)

    if checkpoint != "committed":
        assert _state_topic_alias_rows(state_path) == []
        with sqlite3.connect(state_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM topic_alias_imports").fetchone()[0] == 0
    else:
        assert _state_topic_alias_rows(state_path) == [expected]

    monkeypatch.setattr(user_state, "_topic_alias_import_checkpoint", lambda _name: None)
    IndexRepository(index_path, state_path=state_path)
    IndexRepository(index_path, state_path=state_path)

    assert _state_topic_alias_rows(state_path) == [expected]
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM topic_aliases").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM topic_alias_topics").fetchone()[0] == 1
        ledger = conn.execute(
            "SELECT source_alias_count, source_digest FROM topic_alias_imports"
        ).fetchone()
        assert ledger is not None
        assert ledger[0] == 1
        assert len(ledger[1]) == 64


def test_rebind_at_before_backup_aborts_stale_swap_and_retry_uses_new_projection(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    settings_path = tmp_path / "settings.json"
    moved_source = tmp_path / "rebuild-moved.sqlite"
    shutil.copy2(meetily_db, moved_source)
    scanner = MeetilySQLiteScanner(index_path, state_path=state_path)
    scan = scanner.scan(meetily_db)
    settings_path.write_text(
        json.dumps({"source_uuid": scan.source_uuid}) + "\n",
        encoding="utf-8",
    )
    with sqlite3.connect(index_path) as conn:
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
    state = UserStateRepository(state_path)
    injected = False

    def inject_rebind(checkpoint: str) -> None:
        nonlocal injected
        if checkpoint != "before_backup" or injected:
            return
        injected = True
        claim = state.claim_source_path(
            scan.source_uuid,
            MeetilySQLiteScanner.source_kind,
            moved_source,
            now="rebind-during-rebuild",
        )
        lifecycle_module.rebind_source_identity(
            index_path,
            state,
            claim,
            moved_source,
            settings_path,
        )

    monkeypatch.setattr(scanner_module, "_rebuild_checkpoint", inject_rebind)
    with pytest.raises(RuntimeError, match="changed while rebuilding"):
        scanner.scan(meetily_db)

    moved_path = str(moved_source.resolve(strict=True))
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT path FROM sources").fetchone()[0] == moved_path
        assert set(conn.execute("SELECT source_path FROM meetings")) == {(moved_path,)}
    with sqlite3.connect(state_path) as conn:
        assert (
            conn.execute(
                "SELECT current_path FROM sources WHERE uuid = ?",
                (scan.source_uuid,),
            ).fetchone()[0]
            == moved_path
        )

    monkeypatch.setattr(scanner_module, "_rebuild_checkpoint", lambda _checkpoint: None)
    rebuilt = scanner.scan(moved_source)

    assert rebuilt.source_uuid == scan.source_uuid
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert conn.execute("SELECT path FROM sources").fetchone()[0] == moved_path


def test_two_source_v5_rebuild_retains_all_sources_and_meetings(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    second_source = tmp_path / "second-source.sqlite"
    _copy_with_prefixed_meeting_ids(meetily_db, second_source, "second-")
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    state = UserStateRepository(state_path)
    first_uuid = state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(meetily_db.resolve(strict=True)),
        now="first",
    )
    second_uuid = state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(second_source.resolve(strict=True)),
        now="second",
    )
    _create_v5_index(
        index_path,
        (
            (str(meetily_db.resolve(strict=True)), "meeting-1"),
            (str(second_source.resolve(strict=True)), "second-meeting-1"),
        ),
    )

    result = MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)

    assert result.source_uuid == first_uuid
    assert result.meetings_seen == 2
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert set(conn.execute("SELECT source_uuid, path FROM sources")) == {
            (first_uuid, str(meetily_db.resolve(strict=True))),
            (second_uuid, str(second_source.resolve(strict=True))),
        }
        assert set(
            conn.execute(
                """
                SELECT s.source_uuid, m.external_id
                FROM meetings m
                JOIN sources s ON s.id = m.source_id
                """
            )
        ) == {
            (first_uuid, "meeting-1"),
            (first_uuid, "meeting-2"),
            (second_uuid, "second-meeting-1"),
            (second_uuid, "second-meeting-2"),
        }


@pytest.mark.parametrize("secondary_path_kind", ["relative", "symlink"])
def test_secondary_v5_source_rebind_by_uuid_enables_full_rebuild(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secondary_path_kind: str,
) -> None:
    secondary_dir = tmp_path / "secondary"
    secondary_dir.mkdir()
    secondary_source = secondary_dir / "meeting_minutes.sqlite"
    _copy_with_prefixed_meeting_ids(meetily_db, secondary_source, "secondary-")
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    settings_path = tmp_path / "settings.json"
    bootstrap_path = tmp_path / "bootstrap-multi.sqlite"
    bootstrap_scanner = MeetilySQLiteScanner(bootstrap_path, state_path=state_path)
    primary_scan = bootstrap_scanner.scan(meetily_db)
    secondary_scan = bootstrap_scanner.scan(secondary_source)
    bootstrap_repo = IndexRepository(bootstrap_path, state_path=state_path)
    bootstrap_core = MeetilyMemoryCore(bootstrap_path, state_path=state_path)
    primary_meeting = bootstrap_core.get_meeting_by_ref(
        MeetingRef(primary_scan.source_uuid, "meeting-1")
    )
    secondary_meeting = bootstrap_core.get_meeting_by_ref(
        MeetingRef(secondary_scan.source_uuid, "secondary-meeting-2")
    )
    assert primary_meeting is not None
    assert secondary_meeting is not None
    secondary_task = next(
        entity
        for entity in bootstrap_core.structured_entities("action_items", limit=100).entities
        if entity.meeting_ref == secondary_meeting.ref
    )
    bootstrap_core.set_task_status(
        secondary_task.id,
        "done",
        note="secondary survives explicit repair",
    )
    TagService(bootstrap_repo).assign((str(secondary_meeting.id),), ("secondary-repair",))

    legacy_link = tmp_path / "secondary-legacy-link.sqlite"
    if secondary_path_kind == "relative":
        monkeypatch.chdir(secondary_dir)
        stored_secondary_path = "meeting_minutes.sqlite"
    else:
        legacy_link.symlink_to(secondary_source)
        stored_secondary_path = str(legacy_link)
    _create_v5_index(
        index_path,
        (
            (str(meetily_db.resolve(strict=True)), "meeting-1"),
            (stored_secondary_path, "secondary-meeting-2"),
        ),
    )
    settings_path.write_text(
        json.dumps({"source_uuid": primary_scan.source_uuid}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError) as blocked:
        MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    blocked_message = str(blocked.value)
    assert secondary_scan.source_uuid in blocked_message
    assert stored_secondary_path in blocked_message
    assert "--source-uuid" in blocked_message

    rebound = CliRunner().invoke(
        app,
        [
            "--index",
            str(index_path),
            "config",
            "source",
            str(secondary_source),
            "--rebind",
            "--source-uuid",
            secondary_scan.source_uuid,
        ],
        env={"MEETILY_MEMORY_DATA_DIR": str(tmp_path)},
    )

    assert rebound.exit_code == 0
    assert json.loads(settings_path.read_text(encoding="utf-8"))["source_uuid"] == (
        secondary_scan.source_uuid
    )
    rebuilt = MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    rebuilt_core = MeetilyMemoryCore(index_path, state_path=state_path)

    assert rebuilt.source_uuid == primary_scan.source_uuid
    assert rebuilt_core.get_meeting_by_ref(primary_meeting.ref) is not None
    rebuilt_secondary = rebuilt_core.get_meeting_by_ref(secondary_meeting.ref)
    assert rebuilt_secondary is not None
    rebuilt_tasks = [
        entity
        for entity in rebuilt_core.structured_entities("action_items", limit=100).entities
        if entity.meeting_ref == secondary_meeting.ref and entity.text == secondary_task.text
    ]
    assert [(entity.status, entity.status_note) for entity in rebuilt_tasks] == [
        ("done", "secondary survives explicit repair")
    ]
    assert [
        tag.display_name
        for tag in TagService(IndexRepository(index_path, state_path=state_path)).list_for_meeting(
            str(rebuilt_secondary.id)
        )
    ] == ["secondary-repair"]
    with sqlite3.connect(index_path) as conn:
        assert set(conn.execute("SELECT source_uuid, path FROM sources")) == {
            (primary_scan.source_uuid, str(meetily_db.resolve(strict=True))),
            (secondary_scan.source_uuid, str(secondary_source.resolve(strict=True))),
        }


def test_requested_legacy_source_without_state_identity_aborts_without_uuid_backfill(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    _create_v5_index(
        index_path,
        ((str(meetily_db.resolve(strict=True)), "meeting-1"),),
    )
    active_before = index_path.read_bytes()

    def unexpected_uuid_generation() -> None:
        message = "legacy source UUID must already belong to state.sqlite"
        raise AssertionError(message)

    monkeypatch.setattr(user_state.uuid, "uuid4", unexpected_uuid_generation)
    with pytest.raises(RuntimeError, match="not mapped"):
        MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)

    assert index_path.read_bytes() == active_before
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0


def test_legacy_settings_do_not_backfill_v5_source_identity_before_refresh_preflight(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "legacy-settings-preflight"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    canonical_source = str(meetily_db.resolve(strict=True))
    _create_v5_index(index_path, ((canonical_source, "meeting-1"),))
    settings_payload = {"source_path": canonical_source}
    settings_path.write_text(json.dumps(settings_payload) + "\n", encoding="utf-8")
    index_before = index_path.read_bytes()
    runner = CliRunner()
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}

    refresh = runner.invoke(app, ["--index", str(index_path), "refresh"], env=env)

    assert refresh.exit_code != 0
    assert "mm config source" in f"{refresh.output} {refresh.exception}"
    assert json.loads(settings_path.read_text(encoding="utf-8")) == settings_payload
    assert index_path.read_bytes() == index_before
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    assert not (data_dir / "state.sqlite").exists()

    registered = runner.invoke(
        app,
        ["--index", str(index_path), "config", "source", canonical_source],
        env=env,
    )

    assert registered.exit_code == 0
    assert index_path.read_bytes() == index_before
    recovered = runner.invoke(app, ["--index", str(index_path), "refresh"], env=env)

    assert recovered.exit_code == 0
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert conn.execute("SELECT source_uuid, path FROM sources").fetchone()[1] == (
            canonical_source
        )
    with sqlite3.connect(data_dir / "state.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("failure_kind", "error_pattern"),
    [
        ("unavailable", "unavailable"),
        ("unmappable", "not mapped"),
    ],
)
def test_incomplete_v5_snapshot_aborts_without_changing_active_index(
    meetily_db: Path,
    tmp_path: Path,
    failure_kind: str,
    error_pattern: str,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    state = UserStateRepository(state_path)
    state.get_or_create_source(
        MeetilySQLiteScanner.source_kind,
        str(meetily_db.resolve(strict=True)),
        now="requested",
    )
    if failure_kind == "unavailable":
        second_source = tmp_path / "missing-source.sqlite"
        state.get_or_create_source(
            MeetilySQLiteScanner.source_kind,
            str(second_source),
            now="missing",
        )
        second_external_id = "missing-meeting"
    else:
        second_source = tmp_path / "unmapped-source.sqlite"
        _copy_with_prefixed_meeting_ids(meetily_db, second_source, "unmapped-")
        second_external_id = "unmapped-meeting-1"
    _create_v5_index(
        index_path,
        (
            (str(meetily_db.resolve(strict=True)), "meeting-1"),
            (str(second_source), second_external_id),
        ),
    )
    bytes_before = index_path.read_bytes()
    semantics_before = _legacy_index_semantics(index_path)

    with pytest.raises(RuntimeError, match=error_pattern):
        MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)

    assert index_path.read_bytes() == bytes_before
    assert _legacy_index_semantics(index_path) == semantics_before
    assert not index_path.with_name(f"{index_path.name}.pre-v{CURRENT_SCHEMA_VERSION}").exists()


def test_v5_rebuild_aborts_before_swap_while_keeper_holds_live_wal(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "keeper-wal-index.sqlite"
    state_path = tmp_path / "keeper-wal-state.sqlite"
    scanner = MeetilySQLiteScanner(index_path, state_path=state_path)
    original = scanner.scan(meetily_db)
    with sqlite3.connect(index_path) as conn:
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
    conn.close()

    keeper = sqlite3.connect(index_path)
    try:
        assert keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute(
            """
            INSERT INTO plugin_state (plugin_name, key, value_json, updated_at)
            VALUES ('keeper-wal', 'sentinel', '{}', 'keeper-write')
            """
        )
        keeper.commit()
        wal_path = index_path.with_name(index_path.name + "-wal")
        assert wal_path.stat().st_size > 0

        with pytest.raises(RuntimeError, match="quiesced under exclusive SQLite control"):
            scanner.scan(meetily_db)

        assert keeper.execute("PRAGMA user_version").fetchone()[0] == 5
        assert keeper.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert keeper.execute(
            """
            SELECT value_json
            FROM plugin_state
            WHERE plugin_name = 'keeper-wal' AND key = 'sentinel'
            """
        ).fetchone() == ("{}",)
        assert not index_path.with_name(f"{index_path.name}.pre-v{CURRENT_SCHEMA_VERSION}").exists()
    finally:
        keeper.close()

    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()
    rebuilt = scanner.scan(meetily_db)
    assert rebuilt.source_uuid == original.source_uuid
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT source_uuid FROM sources").fetchone() == (original.source_uuid,)
    conn.close()


def test_failed_then_successful_rebuild_preserves_refs_and_source_backed_state(  # noqa: PLR0915
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    scanner = MeetilySQLiteScanner(index_path, state_path=state_path)
    scanner.scan(meetily_db)
    repo = IndexRepository(index_path, state_path=state_path)
    core = MeetilyMemoryCore(index_path, state_path=state_path)
    meeting = core.get_meeting("meeting-2")
    assert meeting is not None
    original_ref = meeting.ref
    original_evidence_id = core.search("migration risks", limit=1).results[0].evidence[0].id
    task = next(
        entity
        for entity in core.structured_entities("action_items", limit=100).entities
        if entity.meeting_ref == original_ref
    )
    core.set_task_status(task.id, "done", note="survives incompatible rebuild")
    TagService(repo).assign((str(meeting.id),), ("persistent-tag",))

    canonical_source = str(meetily_db.resolve(strict=True))
    with sqlite3.connect(index_path) as conn:
        conn.execute("UPDATE sources SET path = ?", (canonical_source,))
        conn.execute("UPDATE meetings SET source_path = ?", (canonical_source,))
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
    active_before_failure = index_path.read_bytes()

    def fail_before_replace(checkpoint: str) -> None:
        if checkpoint == "before_replace":
            message = "injected rebuild failure"
            raise RuntimeError(message)

    monkeypatch.setattr(scanner_module, "_rebuild_checkpoint", fail_before_replace)
    with pytest.raises(RuntimeError, match="injected rebuild failure"):
        scanner.scan(meetily_db)
    assert index_path.read_bytes() == active_before_failure
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM meeting_tags").fetchone()[0] == 1
        active_task_states = conn.execute(
            "SELECT COUNT(*) FROM task_states WHERE orphaned = 0"
        ).fetchone()[0]
        assert active_task_states == 1

    monkeypatch.setattr(scanner_module, "_rebuild_checkpoint", lambda _checkpoint: None)
    rebuilt = scanner.scan(meetily_db)
    rebuilt_core = MeetilyMemoryCore(index_path, state_path=state_path)
    rebuilt_meeting = rebuilt_core.get_meeting_by_ref(original_ref)

    assert rebuilt.source_uuid == original_ref.source_uuid
    assert rebuilt_meeting is not None
    assert rebuilt_meeting.ref == original_ref
    assert (
        rebuilt_core.search("migration risks", limit=1).results[0].evidence[0].id
        == original_evidence_id
    )
    rebuilt_tasks = [
        entity
        for entity in rebuilt_core.structured_entities("action_items", limit=100).entities
        if entity.meeting_ref == original_ref and entity.text == task.text
    ]
    assert rebuilt_tasks[0].status == "done"
    assert rebuilt_tasks[0].status_note == "survives incompatible rebuild"
    assert [
        tag.display_name
        for tag in TagService(IndexRepository(index_path, state_path=state_path)).list_for_meeting(
            str(rebuilt_meeting.id)
        )
    ] == ["persistent-tag"]

    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        projected_uuid = conn.execute("SELECT source_uuid FROM sources").fetchone()[0]
        assert projected_uuid == original_ref.source_uuid
        assert conn.execute("SELECT path FROM sources").fetchone()[0] == str(
            meetily_db.resolve(strict=True)
        )
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT uuid FROM sources").fetchone()[0] == original_ref.source_uuid
        assert conn.execute("SELECT COUNT(*) FROM meeting_tags").fetchone()[0] == 1
        active_task_states = conn.execute(
            "SELECT COUNT(*) FROM task_states WHERE orphaned = 0"
        ).fetchone()[0]
        assert active_task_states == 1

    backup_path = index_path.with_name(f"{index_path.name}.pre-v{CURRENT_SCHEMA_VERSION}")
    assert backup_path.exists()
    with sqlite3.connect(backup_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT path FROM sources").fetchone()[0] == canonical_source

    scanner.scan(meetily_db)
    assert not backup_path.exists()


def test_state_owned_generation_does_not_resurrect_alias_deleted_before_projection(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    scan = MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    MeetilyMemoryCore(index_path, state_path=state_path).add_topic_alias("migration", ["move"])
    projection = IndexRepository(index_path, state_path=state_path)
    projection.project_topic_aliases()
    with sqlite3.connect(index_path) as conn:
        generation_before = conn.execute(
            "SELECT generation_id, alias_owner FROM index_generation"
        ).fetchone()
    assert generation_before is not None
    assert generation_before[0] != scan.source_uuid
    assert generation_before[1] == "state"

    state = UserStateRepository(state_path)
    assert state.delete_topic_aliases(["move"]) == ("move",)
    assert _state_topic_alias_rows(state_path) == []
    assert len(_index_topic_alias_rows(index_path)) == 1
    stale_projection_read = MeetilyMemoryCore(index_path, state_path=state_path).topic("move")
    assert stale_projection_read.topic.title == "move"
    assert stale_projection_read.topic.aliases == ()

    projection.project_topic_aliases()

    assert _state_topic_alias_rows(state_path) == []
    assert _index_topic_alias_rows(index_path) == []
    with sqlite3.connect(index_path) as conn:
        assert (
            conn.execute("SELECT generation_id, alias_owner FROM index_generation").fetchone()
            == generation_before
        )
    with sqlite3.connect(state_path) as conn:
        assert conn.execute(
            """
            SELECT alias_owner
            FROM index_generations
            WHERE generation_id = ? AND index_path = ?
            """,
            (generation_before[0], str(index_path.resolve(strict=True))),
        ).fetchone() == ("state",)


def test_future_alias_index_is_rejected_before_any_state_mutation(tmp_path: Path) -> None:
    index_path = tmp_path / "future.sqlite"
    state_path = tmp_path / "state.sqlite"
    state = UserStateRepository(state_path)
    state.get_or_create_source("meetily_sqlite", "/registered.sqlite", now="registered")
    state_before = state_path.read_bytes()
    with sqlite3.connect(index_path) as conn:
        initialize_current_schema(conn)
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
        conn.commit()
    expected_alias = _insert_legacy_topic_alias(index_path)

    with pytest.raises(
        RuntimeError,
        match=rf"Unsupported index schema version {CURRENT_SCHEMA_VERSION + 1}",
    ):
        IndexRepository(index_path, state_path=state_path)

    assert state_path.read_bytes() == state_before
    assert _index_topic_alias_rows(index_path) == [expected_alias]


def test_legacy_alias_import_recovers_hot_rollback_journal_before_snapshot(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "legacy-hot.sqlite"
    bootstrap_state_path = tmp_path / "bootstrap-state.sqlite"
    state_path = tmp_path / "imported-state.sqlite"
    IndexRepository(index_path, state_path=bootstrap_state_path)
    _remove_index_generation_marker(index_path)
    expected = _insert_legacy_topic_alias(index_path)

    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_leave_hot_alias_journal_and_exit,
        args=(str(index_path),),
    )
    process.start()
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("hot-journal child process did not exit")
    assert process.exitcode == HOT_ALIAS_JOURNAL_EXIT_CODE
    journal_path = index_path.with_name(index_path.name + "-journal")
    assert journal_path.read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7")

    IndexRepository(index_path, state_path=state_path)

    assert _state_topic_alias_rows(state_path) == [expected]
    assert _index_topic_alias_rows(index_path) == [expected]


def test_locked_same_count_alias_mutation_is_imported_instead_of_overwritten(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "legacy-locked.sqlite"
    bootstrap_state_path = tmp_path / "bootstrap-state.sqlite"
    state_path = tmp_path / "imported-state.sqlite"
    IndexRepository(index_path, state_path=bootstrap_state_path)
    _remove_index_generation_marker(index_path)
    expected = _insert_legacy_topic_alias(index_path)
    shifted = (*expected[:6], "shift", "shift", expected[8])

    with sqlite3.connect(index_path) as writer, ThreadPoolExecutor(max_workers=1) as executor:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE topic_aliases SET alias = 'shift', normalized_alias = 'shift'")
        future = executor.submit(IndexRepository, index_path, state_path=state_path)
        time.sleep(0.25)
        writer.commit()
        future.result(timeout=20)

    assert _state_topic_alias_rows(state_path) == [shifted]
    assert _index_topic_alias_rows(index_path) == [shifted]


def test_path_only_projected_settings_retry_uses_authoritative_current_path(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "pending-settings"
    index_path = data_dir / "index.sqlite"
    state_path = data_dir / "state.sqlite"
    settings_path = data_dir / "settings.json"
    moved_source = tmp_path / "moved-source.sqlite"
    shutil.copy2(meetily_db, moved_source)
    scan = MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    old_path = str(meetily_db.resolve(strict=True))
    moved_path = str(moved_source.resolve(strict=True))
    settings_path.write_text(json.dumps({"source_path": old_path}) + "\n", encoding="utf-8")
    UserStateRepository(state_path).claim_source_path(
        scan.source_uuid,
        MeetilySQLiteScanner.source_kind,
        moved_source,
        now="claim-before-settings-write",
    )

    refreshed = CliRunner().invoke(
        app,
        ["--index", str(index_path), "refresh", "--json"],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert refreshed.exit_code == 0
    assert json.loads(settings_path.read_text(encoding="utf-8"))["source_uuid"] == (
        scan.source_uuid
    )
    with sqlite3.connect(state_path) as conn:
        assert conn.execute(
            """
            SELECT current_path, projected_path, pending_revision
            FROM sources
            WHERE uuid = ?
            """,
            (scan.source_uuid,),
        ).fetchone() == (moved_path, moved_path, None)
    with sqlite3.connect(index_path) as conn:
        assert conn.execute(
            "SELECT path FROM sources WHERE source_uuid = ?",
            (scan.source_uuid,),
        ).fetchone() == (moved_path,)
        assert set(conn.execute("SELECT source_path FROM meetings")) == {(moved_path,)}


def test_process_death_during_multi_source_finalize_retries_all_pending_claims(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    secondary_source = tmp_path / "secondary.sqlite"
    _copy_with_prefixed_meeting_ids(meetily_db, secondary_source, "secondary-")
    moved_primary = tmp_path / "moved-primary.sqlite"
    moved_secondary = tmp_path / "moved-secondary.sqlite"
    shutil.copy2(meetily_db, moved_primary)
    shutil.copy2(secondary_source, moved_secondary)
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    scanner = MeetilySQLiteScanner(index_path, state_path=state_path)
    primary = scanner.scan(meetily_db)
    secondary = scanner.scan(secondary_source)
    state = UserStateRepository(state_path)
    state.claim_source_path(
        primary.source_uuid,
        MeetilySQLiteScanner.source_kind,
        moved_primary,
        now="move-primary",
    )
    state.claim_source_path(
        secondary.source_uuid,
        MeetilySQLiteScanner.source_kind,
        moved_secondary,
        now="move-secondary",
    )
    with sqlite3.connect(index_path) as conn:
        conn.execute("PRAGMA user_version = 5")
        conn.commit()

    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_rebuild_and_exit_during_first_claim_finalize,
        args=(str(index_path), str(state_path), str(moved_primary)),
    )
    process.start()
    process.join(timeout=60)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("multi-source finalize child process did not exit")
    assert process.exitcode == MULTI_SOURCE_FINALIZE_EXIT_CODE

    expected_paths = {
        primary.source_uuid: str(moved_primary.resolve(strict=True)),
        secondary.source_uuid: str(moved_secondary.resolve(strict=True)),
    }
    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert dict(conn.execute("SELECT source_uuid, path FROM sources")) == expected_paths
    with sqlite3.connect(state_path) as conn:
        pending = conn.execute(
            """
            SELECT uuid, current_path, projected_path, pending_revision
            FROM sources
            ORDER BY uuid
            """
        ).fetchall()
    assert len(pending) == 2
    assert all(row[1] == expected_paths[row[0]] for row in pending)
    assert all(row[2] != row[1] and row[3] is not None for row in pending)

    IndexRepository(index_path, state_path=state_path)
    repeated = scanner.scan(moved_primary)

    assert repeated.source_uuid == primary.source_uuid
    with sqlite3.connect(state_path) as conn:
        healed = conn.execute(
            """
            SELECT uuid, current_path, projected_path, pending_revision
            FROM sources
            ORDER BY uuid
            """
        ).fetchall()
    assert len(healed) == 2
    assert all(row[1] == expected_paths[row[0]] for row in healed)
    assert all(row[2] == row[1] and row[3] is None for row in healed)
