import hashlib
import shutil
import sqlite3
import uuid
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

import pytest
from typer.testing import CliRunner

from meetily_memory import user_state
from meetily_memory.cli.app import app
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.db.migrations import CURRENT_SCHEMA_VERSION, SOURCE_AWARE_SCHEMA_VERSION
from meetily_memory.db.schema import (
    IndexReadError,
    existing_index_connection,
    sqlite_read_snapshot,
)
from meetily_memory.domain import MeetingRef, canonical_entity_kind, stable_evidence_id
from meetily_memory.evaluation import (
    EvaluationRetrievalConfig,
    build_manifest,
    corpus_fingerprint,
    load_dataset,
)
from meetily_memory.mcp_server import create_mcp_server
from meetily_memory.meeting_structure import ENTITY_KINDS
from meetily_memory.repositories.index import (
    EvidenceResolutionError,
    IndexRepository,
    memory_entity_select_sql,
    search_hit_from_row,
)
from meetily_memory.repositories.meetings import DuplicateEvidenceIdentityError
from meetily_memory.repositories.search import EVIDENCE_LOOKUP_SQL, EvidenceReference
from meetily_memory.retrieval import LexicalTagMeetingRetrievalStrategy
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.tagging import TagService
from meetily_memory.user_state import UserStateRepository

FIXED_SOURCE_UUID = "11111111-1111-1111-1111-111111111111"
SECOND_SOURCE_UUID = "22222222-2222-2222-2222-222222222222"
TRANSCRIPT_EVIDENCE_ID = "evidence:18bacd30cb463d73d0f6e459861a96e94f6c04b468771adf00231f46367ee960"
FALLBACK_EVIDENCE_ID = "evidence:b10b6468cdbbb4e14f7318bda9ed6ef0347043c5d378d0aa99c193ee27ce80b7"
INDEX_ROW_QUERIES = (
    ("sources", "SELECT COUNT(*) FROM sources"),
    ("meetings", "SELECT COUNT(*) FROM meetings"),
    ("chunks", "SELECT COUNT(*) FROM chunks"),
    ("chunks_fts", "SELECT COUNT(*) FROM chunks_fts"),
    ("people", "SELECT COUNT(*) FROM people"),
    ("meeting_people", "SELECT COUNT(*) FROM meeting_people"),
    ("scan_runs", "SELECT COUNT(*) FROM scan_runs"),
    ("decisions", "SELECT COUNT(*) FROM decisions"),
    ("action_items", "SELECT COUNT(*) FROM action_items"),
    ("risks", "SELECT COUNT(*) FROM risks"),
    ("open_questions", "SELECT COUNT(*) FROM open_questions"),
    ("knowledge_nodes", "SELECT COUNT(*) FROM knowledge_nodes"),
    ("knowledge_edges", "SELECT COUNT(*) FROM knowledge_edges"),
    ("topic_aliases", "SELECT COUNT(*) FROM topic_aliases"),
    ("index_generation", "SELECT COUNT(*) FROM index_generation"),
)
STATE_ROW_QUERIES = (
    ("sources", "SELECT COUNT(*) FROM sources"),
    ("task_states", "SELECT COUNT(*) FROM task_states"),
    ("migration_reports", "SELECT COUNT(*) FROM migration_reports"),
    ("migration_report_items", "SELECT COUNT(*) FROM migration_report_items"),
    ("tags", "SELECT COUNT(*) FROM tags"),
    ("meeting_tags", "SELECT COUNT(*) FROM meeting_tags"),
    ("topic_alias_topics", "SELECT COUNT(*) FROM topic_alias_topics"),
    ("topic_aliases", "SELECT COUNT(*) FROM topic_aliases"),
    ("index_generations", "SELECT COUNT(*) FROM index_generations"),
    ("topic_alias_imports", "SELECT COUNT(*) FROM topic_alias_imports"),
)
SOURCE_ROW_QUERIES = (
    ("meetings", "SELECT COUNT(*) FROM meetings"),
    ("transcripts", "SELECT COUNT(*) FROM transcripts"),
    ("summary_processes", "SELECT COUNT(*) FROM summary_processes"),
    ("meeting_notes", "SELECT COUNT(*) FROM meeting_notes"),
)


@dataclass(frozen=True)
class DatabaseSnapshot:
    digest: str
    mtime_ns: int
    size: int
    rows: tuple[tuple[str, int], ...]


def database_snapshot(
    path: Path,
    row_queries: tuple[tuple[str, str], ...],
) -> DatabaseSnapshot:
    physical_path = path.resolve(strict=True)
    with sqlite3.connect(f"{physical_path.as_uri()}?mode=ro", uri=True) as conn:
        rows = tuple((name, int(conn.execute(sql).fetchone()[0])) for name, sql in row_queries)
    path_stat = physical_path.stat()
    return DatabaseSnapshot(
        digest=hashlib.sha256(physical_path.read_bytes()).hexdigest(),
        mtime_ns=path_stat.st_mtime_ns,
        size=path_stat.st_size,
        rows=rows,
    )


@pytest.mark.anyio
async def test_core_cli_and_mcp_reads_leave_index_state_and_source_unchanged(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    before = (
        database_snapshot(index_path, INDEX_ROW_QUERIES),
        database_snapshot(state_path, STATE_ROW_QUERIES),
        database_snapshot(meetily_db, SOURCE_ROW_QUERIES),
    )

    core = MeetilyMemoryCore(index_path, state_path=state_path)
    search = core.search("migration risks", limit=5, context=1)
    evidence_id = search.results[0].evidence[0].id
    assert core.resolve_search_hit(evidence_id).id == evidence_id
    assert core.build_context("migration risks", limit=5, context=1).evidence
    assert core.meetings()
    assert core.structured_entities("action_items", limit=100).entities

    runner = CliRunner()
    commands = (
        ("s", "migration risks", "--json"),
        ("c", "migration risks", "--context", "1"),
        ("tag", "list"),
        ("open", "1", "--source", "--print-path"),
    )
    for command in commands:
        result = runner.invoke(app, ["--index", str(index_path), *command])
        assert result.exit_code == 0, result.output

    server = create_mcp_server(index_path)
    _, payload = await server.call_tool(
        "search_meetings",
        {"query": "migration risks", "limit": 5},
    )
    assert payload is not None

    after = (
        database_snapshot(index_path, INDEX_ROW_QUERIES),
        database_snapshot(state_path, STATE_ROW_QUERIES),
        database_snapshot(meetily_db, SOURCE_ROW_QUERIES),
    )
    assert after == before


def test_sqlite_read_snapshot_rolls_back_owned_transaction_and_preserves_outer_transaction(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)

    with existing_index_connection(index_path) as conn:
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        message = "snapshot failure"

        def fail_inside_snapshot(*, nested: bool) -> None:
            with sqlite_read_snapshot(conn):
                assert conn.in_transaction
                if nested:
                    with sqlite_read_snapshot(conn):
                        assert conn.in_transaction
                raise RuntimeError(message)

        with pytest.raises(RuntimeError, match=message):
            fail_inside_snapshot(nested=True)
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 0

        conn.execute("BEGIN")
        try:
            with pytest.raises(RuntimeError, match=message):
                fail_inside_snapshot(nested=False)
            assert conn.in_transaction
        finally:
            conn.rollback()

    controls = [
        statement.strip().upper()
        for statement in statements
        if statement.strip().upper() in {"BEGIN", "ROLLBACK"}
    ]
    assert controls == ["BEGIN", "ROLLBACK", "BEGIN", "ROLLBACK"]


def test_search_mapping_resolver_and_context_have_bounded_connections_and_queries(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with sqlite3.connect(meetily_db) as conn:
        conn.executemany(
            """
            INSERT INTO transcripts (
              id, meeting_id, transcript, timestamp,
              audio_start_time, audio_end_time, duration, speaker
            ) VALUES (?, 'meeting-1', ?, ?, ?, ?, 1.0, 'Alice')
            """,
            [
                (
                    f"bounded-{index}",
                    f"boundedtoken excerpt {index}",
                    f"11:{index:02d}:00",
                    2_000.0 + index,
                    2_001.0 + index,
                )
                for index in range(24)
            ],
        )
        conn.commit()

    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    repo = IndexRepository.open_existing(index_path)
    row = repo.search("boundedtoken", limit=1)[0]
    original_row = dict(row)
    hit = search_hit_from_row(MappingProxyType(row))
    assert row == original_row
    assert hit.id == row["evidence_id"]
    assert hit.source_chunk_id == row["chunk_id"]
    assert {
        "source_uuid",
        "evidence_id",
        "started_at",
        "ended_at",
        "created_at",
        "updated_at",
        "language",
        "summary_text",
        "chunk_count",
        "starts_at_seconds",
        "ends_at_seconds",
    }.issubset(row)

    counts = {"connections": 0, "queries": 0}
    statements: list[str] = []

    @contextmanager
    def counted_connection(path: Path) -> Generator[sqlite3.Connection, None, None]:
        counts["connections"] += 1
        with existing_index_connection(path) as conn:

            def trace(statement: str) -> None:
                normalized = statement.lstrip().upper()
                if normalized.startswith(("SELECT", "WITH")):
                    counts["queries"] += 1
                    statements.append(statement)

            conn.set_trace_callback(trace)
            yield conn

    monkeypatch.setattr(repo.search_repo, "_connection", counted_connection)
    hits = repo.search_hits("boundedtoken", limit=20)
    assert len(hits) == 20
    assert counts["connections"] == 1
    assert counts["queries"] <= 2

    counts.update(connections=0, queries=0)
    statements.clear()
    resolved = repo.get_search_hit(hits[0].id)
    assert resolved == hits[0]
    assert counts == {"connections": 1, "queries": 1}
    assert "WHERE c.evidence_id =" in statements[0]

    counts.update(connections=0, queries=0)
    statements.clear()
    expanded = repo.expand_search_hits(hits, context=1)
    assert expanded
    assert counts == {"connections": 1, "queries": 2}
    assert any(statement.lstrip().upper().startswith("WITH MATCHED") for statement in statements)


def test_operation_snapshot_validates_pinned_state_and_pins_both_schemas_first(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    repository = IndexRepository.open_existing(index_path, state_path=state_path)
    statements: list[str] = []

    @contextmanager
    def traced_connection(path: Path) -> Generator[sqlite3.Connection, None, None]:
        with existing_index_connection(path) as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(repository, "operation_connection", traced_connection)

    with repository.operation_snapshot() as snapshot:
        assert snapshot.in_transaction

    normalized = [" ".join(statement.upper().split()) for statement in statements]
    begin_index = normalized.index("BEGIN")
    state_validation_index = next(
        index
        for index, statement in enumerate(normalized)
        if statement.startswith("PRAGMA OPERATION_STATE.USER_VERSION")
    )
    first_snapshot_statement = normalized[begin_index + 1]
    assert state_validation_index < begin_index
    assert first_snapshot_statement.startswith("SELECT")
    assert "FROM MAIN.SOURCES" in first_snapshot_statement
    assert "FROM OPERATION_STATE.SOURCES" in first_snapshot_statement

    replacement_path = tmp_path / "replacement-state.sqlite"
    shutil.copy2(state_path, replacement_path)
    replacement_path.replace(state_path)

    with (
        pytest.raises(IndexReadError, match="no longer matches"),
        repository.operation_snapshot(),
    ):
        pass


def test_shared_core_cli_mcp_retrieval_counts_stay_bounded_from_limit_one_to_eight(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with sqlite3.connect(meetily_db) as conn:
        conn.executemany(
            """
            INSERT INTO meetings (id, title, created_at, updated_at, folder_path)
            VALUES (?, ?, '2026-07-03T10:00:00Z', '2026-07-03T11:00:00Z', ?)
            """,
            [
                (
                    f"bounded-meeting-{index}",
                    f"Bounded meeting {index}",
                    str(tmp_path / f"Bounded meeting {index}"),
                )
                for index in range(8)
            ],
        )
        conn.executemany(
            """
            INSERT INTO transcripts (
              id, meeting_id, transcript, timestamp,
              audio_start_time, audio_end_time, duration, speaker
            ) VALUES (?, ?, ?, '10:05:00', 300.0, 301.0, 1.0, 'Alice')
            """,
            [
                (
                    f"bounded-retrieval-{index}",
                    f"bounded-meeting-{index}",
                    f"boundedretrieval evidence {index}",
                )
                for index in range(8)
            ],
        )
        conn.commit()

    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    writer = IndexRepository(index_path)
    with sqlite3.connect(index_path) as conn:
        local_ids = tuple(
            str(row[0])
            for row in conn.execute(
                "SELECT id FROM meetings WHERE external_id LIKE 'bounded-meeting-%' ORDER BY id"
            )
        )
    TagService(writer).assign(local_ids, ("boundedretrieval",))
    core = MeetilyMemoryCore(index_path)
    repository = core._repository  # noqa: SLF001
    strategy = core._meeting_retrieval  # noqa: SLF001
    assert isinstance(strategy, LexicalTagMeetingRetrievalStrategy)

    counts = {
        "index_connections": 0,
        "index_queries": 0,
    }

    @contextmanager
    def counted_index_connection(path: Path) -> Generator[sqlite3.Connection, None, None]:
        counts["index_connections"] += 1
        with existing_index_connection(path) as conn:

            def trace(statement: str) -> None:
                if statement.lstrip().upper().startswith(("SELECT", "WITH")):
                    counts["index_queries"] += 1

            conn.set_trace_callback(trace)
            yield conn

    def reject_state_connection() -> None:
        message = "Core search must read attached state through the operation snapshot."
        raise AssertionError(message)

    monkeypatch.setattr(repository, "operation_connection", counted_index_connection)
    monkeypatch.setattr(strategy.tags.repository, "_connect", reject_state_connection)

    measurements: dict[int, dict[str, int]] = {}
    for limit in (1, 8):
        counts.update(index_connections=0, index_queries=0)
        results = core.search("boundedretrieval", limit=limit, context=1).results
        assert len(results) == limit
        measurements[limit] = dict(counts)

    assert measurements[1] == {
        "index_connections": 1,
        "index_queries": 7,
    }
    assert measurements[8] == {
        "index_connections": 1,
        "index_queries": 7,
    }


def test_evidence_lookup_uses_materialized_unique_index(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    repo = IndexRepository.open_existing(index_path)
    evidence_id = repo.search_hits("pricing decision", limit=1)[0].id

    with existing_index_connection(index_path) as conn:
        evidence_column = next(
            row for row in conn.execute("PRAGMA table_info(chunks)") if row["name"] == "evidence_id"
        )
        unique_indexes = [
            str(row["name"])
            for row in conn.execute("PRAGMA index_list(chunks)")
            if int(row["unique"]) == 1
        ]
        unique_column_sets = {
            tuple(
                str(column["name"])
                for column in conn.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                    (index_name,),
                )
            )
            for index_name in unique_indexes
        }
        plan = conn.execute(
            f"EXPLAIN QUERY PLAN {EVIDENCE_LOOKUP_SQL}",
            (evidence_id,),
        ).fetchall()

    assert str(evidence_column["type"]).upper() == "TEXT"
    assert int(evidence_column["notnull"]) == 1
    assert ("evidence_id",) in unique_column_sets
    details = "\n".join(str(row["detail"]) for row in plan)
    assert "USING INDEX" in details
    assert "evidence_id=?" in details
    assert repo.get_search_hit(evidence_id) is not None


def test_direct_entity_query_matches_legacy_correct_set_for_context_chunks(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    repo = IndexRepository.open_existing(index_path)
    hits = repo.search_hits("migration risks", limit=3, context=1)
    assert any(hit.is_context for hit in hits)
    chunk_ids = {hit.source_chunk_id for hit in hits}
    evidence_chunks = {hit.id: hit.source_chunk_id for hit in hits}
    legacy_rows = [
        row
        for row in repo.list_all_structured_entity_details(limit=10_000)
        if int(row["source_chunk_id"]) in chunk_ids
    ]
    expected = {
        (
            canonical_entity_kind(str(row["kind"])),
            str(row["text"]),
            int(row["source_chunk_id"]),
            str(row["source"]),
        )
        for row in legacy_rows
    }

    def reject_global_entity_scan(_limit: int = 100) -> list[dict[str, object]]:
        message = "context entities must not use the global 10k list path"
        raise AssertionError(message)

    monkeypatch.setattr(repo, "list_all_structured_entity_details", reject_global_entity_scan)
    entity_counts = {"connections": 0, "queries": 0}
    entity_statements: list[str] = []

    @contextmanager
    def counted_entity_connection(path: Path) -> Generator[sqlite3.Connection, None, None]:
        entity_counts["connections"] += 1
        with existing_index_connection(path) as conn:

            def trace(statement: str) -> None:
                if statement.lstrip().upper().startswith("SELECT"):
                    entity_counts["queries"] += 1
                    entity_statements.append(statement)

            conn.set_trace_callback(trace)
            yield conn

    monkeypatch.setattr(repo, "connection", counted_entity_connection)
    entities = repo.memory_entities_for_hits(hits)
    actual = {
        (
            entity.kind,
            entity.content,
            evidence_chunks[entity.evidence_id],
            entity.extraction_method,
        )
        for entity in entities
    }

    assert actual == expected
    assert {entity.kind for entity in entities} >= {"decision", "task", "risk"}
    assert entity_counts == {"connections": 1, "queries": 2}
    assert "c.evidence_id IN" in entity_statements[0]
    assert entity_statements[1].count("source_chunk_id IN") == 4

    state_counts = {"connections": 0, "queries": 0}
    original_state_connection = repo.user_state._connect  # noqa: SLF001

    @contextmanager
    def counted_state_connection() -> Generator[sqlite3.Connection, None, None]:
        state_counts["connections"] += 1
        with original_state_connection() as conn:

            def trace(statement: str) -> None:
                if statement.lstrip().upper().startswith("SELECT"):
                    state_counts["queries"] += 1

            conn.set_trace_callback(trace)
            yield conn

    monkeypatch.setattr(repo.user_state, "_connect", counted_state_connection)
    assert repo.list_structured_entity_details("action_items", limit=100)
    assert state_counts == {"connections": 1, "queries": 1}


def test_entity_union_branches_use_source_chunk_indexes(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)

    with existing_index_connection(index_path) as conn:
        for kind in ENTITY_KINDS:
            plan = conn.execute(
                f"EXPLAIN QUERY PLAN {memory_entity_select_sql(kind, '?')}",
                (1,),
            ).fetchall()
            details = "\n".join(str(row["detail"]) for row in plan)
            assert f"SEARCH e USING INDEX idx_{kind}_source_chunk" in details
            assert "SCAN e" not in details


def test_expand_search_hits_keeps_one_snapshot_during_incremental_chunk_id_reuse(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    scanner = MeetilySQLiteScanner(index_path)
    scanner.scan(meetily_db)
    with sqlite3.connect(index_path) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        assert journal_mode is not None
        assert journal_mode[0] == "wal"
        conn.execute("PRAGMA wal_autocheckpoint=0")

    repository = IndexRepository.open_existing(index_path)
    old_hit = repository.search_hits("migration risks", limit=1)[0]
    old_snapshot = repository.expand_search_hits((old_hit,), context=1)
    with sqlite3.connect(meetily_db) as conn:
        conn.execute(
            """
            INSERT INTO transcripts (
              id, meeting_id, transcript, timestamp,
              audio_start_time, audio_end_time, duration, speaker
            ) VALUES (
              'racing-insert', 'meeting-2', 'Concurrent administrative preface.', '09:01:00',
              60.0, 61.0, 1.0, 'Facilitator'
            )
            """
        )
        conn.execute(
            "UPDATE meetings SET updated_at = '2026-07-02T09:40:00Z' WHERE id = 'meeting-2'"
        )
        conn.commit()

    statements: list[str] = []

    @contextmanager
    def traced_connection(path: Path) -> Generator[sqlite3.Connection, None, None]:
        with existing_index_connection(path) as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    original_resolve = repository.search_repo.resolve_evidence_rows
    committed_reused_evidence_ids: list[str] = []
    committed_original_chunk_ids: list[int] = []
    writer_updates: list[int] = []

    def resolve_then_commit_insert(
        conn: sqlite3.Connection,
        evidence_refs: tuple[EvidenceReference, ...],
    ) -> list[dict[str, object]]:
        rows = original_resolve(conn, evidence_refs)
        assert conn.in_transaction
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(scanner.scan, meetily_db).result(timeout=30)
        writer_updates.append(result.meetings_updated)
        with sqlite3.connect(index_path) as current:
            reused = current.execute(
                "SELECT evidence_id FROM chunks WHERE id = ?",
                (old_hit.source_chunk_id,),
            ).fetchone()
            original = current.execute(
                "SELECT id FROM chunks WHERE evidence_id = ?",
                (old_hit.id,),
            ).fetchone()
        assert reused is not None
        assert original is not None
        committed_reused_evidence_ids.append(str(reused[0]))
        committed_original_chunk_ids.append(int(original[0]))
        return rows

    monkeypatch.setattr(repository.search_repo, "_connection", traced_connection)
    monkeypatch.setattr(
        repository.search_repo,
        "resolve_evidence_rows",
        resolve_then_commit_insert,
    )

    actual = repository.expand_search_hits((old_hit,), context=1)

    assert actual == old_snapshot
    assert writer_updates == [1]
    assert committed_reused_evidence_ids[0] != old_hit.id
    assert committed_original_chunk_ids[0] != old_hit.source_chunk_id
    queries = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("SELECT", "WITH"))
    ]
    assert len(queries) == 2
    controls = [
        statement.strip().upper()
        for statement in statements
        if statement.strip().upper() in {"BEGIN", "ROLLBACK"}
    ]
    assert controls == ["BEGIN", "ROLLBACK"]


def test_memory_entities_keep_one_snapshot_during_concurrent_evidence_deletion(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    scanner = MeetilySQLiteScanner(index_path)
    scanner.scan(meetily_db)
    with sqlite3.connect(index_path) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        assert journal_mode is not None
        assert journal_mode[0] == "wal"
        conn.execute("PRAGMA wal_autocheckpoint=0")

    repository = IndexRepository.open_existing(index_path)
    old_hit = repository.search_hits("migration risks", limit=1)[0]
    old_snapshot = repository.memory_entities_for_hits((old_hit,))
    assert old_snapshot
    with sqlite3.connect(meetily_db) as conn:
        conn.execute("DELETE FROM transcripts WHERE id = 'transcript-2'")
        conn.execute(
            "UPDATE meetings SET updated_at = '2026-07-02T09:50:00Z' WHERE id = 'meeting-2'"
        )
        conn.commit()

    statements: list[str] = []

    @contextmanager
    def traced_connection(path: Path) -> Generator[sqlite3.Connection, None, None]:
        with existing_index_connection(path) as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    original_resolve = repository.search_repo.resolve_evidence_rows
    committed_missing: list[bool] = []
    writer_updates: list[int] = []

    def resolve_then_commit_delete(
        conn: sqlite3.Connection,
        evidence_refs: tuple[EvidenceReference, ...],
    ) -> list[dict[str, object]]:
        rows = original_resolve(conn, evidence_refs)
        assert conn.in_transaction
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(scanner.scan, meetily_db).result(timeout=30)
        writer_updates.append(result.meetings_updated)
        with sqlite3.connect(index_path) as current:
            missing = current.execute(
                "SELECT 1 FROM chunks WHERE evidence_id = ?",
                (old_hit.id,),
            ).fetchone()
        committed_missing.append(missing is None)
        return rows

    monkeypatch.setattr(repository, "connection", traced_connection)
    monkeypatch.setattr(
        repository.search_repo,
        "resolve_evidence_rows",
        resolve_then_commit_delete,
    )

    actual = repository.memory_entities_for_hits((old_hit,))

    assert actual == old_snapshot
    assert writer_updates == [1]
    assert committed_missing == [True]
    queries = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("SELECT", "WITH"))
    ]
    assert len(queries) == 2
    controls = [
        statement.strip().upper()
        for statement in statements
        if statement.strip().upper() in {"BEGIN", "ROLLBACK"}
    ]
    assert controls == ["BEGIN", "ROLLBACK"]


def test_old_hit_resolves_stable_evidence_after_incremental_chunk_id_reuse(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    scanner = MeetilySQLiteScanner(index_path)
    scanner.scan(meetily_db)
    repository = IndexRepository.open_existing(index_path)
    old_hit = repository.search_hits("migration risks", limit=1)[0]
    old_chunk_id = old_hit.source_chunk_id

    with sqlite3.connect(meetily_db) as conn:
        conn.execute(
            """
            INSERT INTO transcripts (
              id, meeting_id, transcript, timestamp,
              audio_start_time, audio_end_time, duration, speaker
            ) VALUES (
              'inserted-before-old-hit', 'meeting-2', 'Administrative preface.', '09:01:00',
              60.0, 61.0, 1.0, 'Facilitator'
            )
            """
        )
        conn.execute(
            "UPDATE meetings SET updated_at = '2026-07-02T09:40:00Z' WHERE id = 'meeting-2'"
        )
        conn.commit()
    scanner.scan(meetily_db)

    with sqlite3.connect(index_path) as conn:
        reused_evidence_id = str(
            conn.execute("SELECT evidence_id FROM chunks WHERE id = ?", (old_chunk_id,)).fetchone()[
                0
            ]
        )
        current_chunk_id = int(
            conn.execute("SELECT id FROM chunks WHERE evidence_id = ?", (old_hit.id,)).fetchone()[0]
        )
    assert reused_evidence_id != old_hit.id
    assert current_chunk_id != old_chunk_id

    expanded = repository.expand_search_hits((old_hit,), context=1)
    assert expanded[0].id == old_hit.id
    assert expanded[0].source_chunk_id == current_chunk_id
    assert expanded[0].is_context is False
    entities = repository.memory_entities_for_hits((old_hit,))
    assert entities
    assert {entity.evidence_id for entity in entities} == {old_hit.id}
    assert any("migration risks" in entity.content for entity in entities)

    mismatched_hit = replace(
        old_hit,
        meeting=replace(
            old_hit.meeting,
            ref=MeetingRef("wrong-source", old_hit.meeting.external_id),
        ),
    )
    with pytest.raises(EvidenceResolutionError, match="Evidence identity mismatch"):
        repository.expand_search_hits((mismatched_hit,), context=1)

    with sqlite3.connect(meetily_db) as conn:
        conn.execute("DELETE FROM transcripts WHERE id = 'transcript-2'")
        conn.execute(
            "UPDATE meetings SET updated_at = '2026-07-02T09:50:00Z' WHERE id = 'meeting-2'"
        )
        conn.commit()
    scanner.scan(meetily_db)

    with pytest.raises(EvidenceResolutionError, match="Evidence no longer exists"):
        repository.expand_search_hits((old_hit,), context=1)
    with pytest.raises(EvidenceResolutionError, match="Evidence no longer exists"):
        repository.memory_entities_for_hits((old_hit,))


def test_exact_evidence_ids_survive_v6_rebuild_and_rebind_but_change_for_new_uuid(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with sqlite3.connect(meetily_db) as conn:
        conn.execute("UPDATE transcripts SET id = NULL WHERE id = 'transcript-3'")
        conn.commit()
    monkeypatch.setattr(user_state.uuid, "uuid4", lambda: uuid.UUID(FIXED_SOURCE_UUID))
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    scanner = MeetilySQLiteScanner(index_path, state_path=state_path)
    first_scan = scanner.scan(meetily_db)
    assert first_scan.source_uuid == FIXED_SOURCE_UUID

    def evidence_rows(source_uuid: str) -> dict[tuple[str, int], str]:
        with sqlite3.connect(index_path) as conn:
            return {
                (str(row[0]) if row[0] is not None else "<fallback>", int(row[1])): str(row[2])
                for row in conn.execute(
                    """
                    SELECT c.external_id, c.ordinal, c.evidence_id
                    FROM chunks c
                    JOIN meetings m ON m.id = c.meeting_id
                    JOIN sources s ON s.id = m.source_id
                    WHERE s.source_uuid = ? AND m.external_id = 'meeting-1'
                    ORDER BY c.ordinal
                    """,
                    (source_uuid,),
                )
            }

    original = evidence_rows(FIXED_SOURCE_UUID)
    assert original[("transcript-1", 0)] == TRANSCRIPT_EVIDENCE_ID
    assert original[("<fallback>", 1)] == FALLBACK_EVIDENCE_ID
    assert (
        stable_evidence_id(
            FIXED_SOURCE_UUID,
            "meeting-1",
            None,
            kind="transcript",
            ordinal=1,
            text="Open question: who owns partner review?",
        )
        == FALLBACK_EVIDENCE_ID
    )

    with sqlite3.connect(index_path) as conn:
        conn.execute(f"PRAGMA user_version = {SOURCE_AWARE_SCHEMA_VERSION}")
        conn.commit()
    rebuilt = scanner.scan(meetily_db)
    assert rebuilt.source_uuid == FIXED_SOURCE_UUID
    assert evidence_rows(FIXED_SOURCE_UUID) == original
    with sqlite3.connect(index_path) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == CURRENT_SCHEMA_VERSION

    moved_source = tmp_path / "moved.sqlite"
    shutil.copy2(meetily_db, moved_source)
    state = UserStateRepository(state_path)
    writer = IndexRepository(index_path, state_path=state_path)
    claim = state.claim_source_path(
        FIXED_SOURCE_UUID,
        MeetilySQLiteScanner.source_kind,
        moved_source,
        now="rebind",
    )
    writer.rebind_source_path_projection(claim)
    assert state.finalize_source_path_claims((claim,)) is True
    scanner.scan(moved_source)
    assert evidence_rows(FIXED_SOURCE_UUID) == original

    second_source = tmp_path / "second.sqlite"
    shutil.copy2(meetily_db, second_source)
    monkeypatch.setattr(user_state.uuid, "uuid4", lambda: uuid.UUID(SECOND_SOURCE_UUID))
    second_scan = scanner.scan(second_source)
    assert second_scan.source_uuid == SECOND_SOURCE_UUID
    second = evidence_rows(SECOND_SOURCE_UUID)
    assert second[("transcript-1", 0)] == stable_evidence_id(
        SECOND_SOURCE_UUID,
        "meeting-1",
        "transcript-1",
        kind="transcript",
        ordinal=0,
        text="Alice confirmed the launch checklist and pricing decision.",
    )
    assert second[("transcript-1", 0)] != original[("transcript-1", 0)]


def test_duplicate_upstream_chunk_identity_fails_build_diagnostically(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    with sqlite3.connect(meetily_db) as conn:
        conn.execute("UPDATE transcripts SET id = 'summary:meeting-1' WHERE id = 'transcript-1'")
        conn.commit()
    source_before = database_snapshot(meetily_db, SOURCE_ROW_QUERIES)
    index_path = tmp_path / "index.sqlite"

    with pytest.raises(
        DuplicateEvidenceIdentityError,
        match=r"Duplicate upstream chunk identity.*meeting-1.*summary:meeting-1",
    ):
        MeetilySQLiteScanner(index_path).scan(meetily_db)

    assert database_snapshot(meetily_db, SOURCE_ROW_QUERIES) == source_before
    with sqlite3.connect(index_path) as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]) == 0
        failed = conn.execute(
            "SELECT status, error_message FROM scan_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert failed == ("failed", "DuplicateEvidenceIdentityError during source_scan.")


def test_missing_legacy_and_missing_state_reads_create_or_migrate_nothing(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    missing_index = tmp_path / "missing" / "index.sqlite"
    with pytest.raises(IndexReadError, match=r"index not found.*mm refresh"):
        MeetilyMemoryCore(missing_index)
    dataset = load_dataset(Path("tests/fixtures/evaluation/synthetic_dataset.json"))
    with pytest.raises(IndexReadError, match=r"index not found.*mm refresh"):
        corpus_fingerprint(missing_index)
    with pytest.raises(IndexReadError, match=r"index not found.*mm refresh"):
        build_manifest(
            dataset,
            missing_index,
            config=EvaluationRetrievalConfig(),
            retrieval_parameters={},
        )
    assert not missing_index.parent.exists()

    runner = CliRunner()
    missing_cli = runner.invoke(
        app,
        ["--index", str(missing_index), "s", "migration"],
    )
    assert missing_cli.exit_code != 0
    assert "mm refresh" in missing_cli.output
    assert not missing_index.parent.exists()

    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    legacy_index = legacy_dir / "index.sqlite"
    with sqlite3.connect(legacy_index) as conn:
        conn.execute(f"PRAGMA user_version = {SOURCE_AWARE_SCHEMA_VERSION}")
        conn.commit()
    legacy_before = (legacy_index.read_bytes(), legacy_index.stat().st_mtime_ns)
    with pytest.raises(IndexReadError, match=rf"schema {SOURCE_AWARE_SCHEMA_VERSION} is outdated"):
        MeetilyMemoryCore(legacy_index)
    legacy_cli = runner.invoke(
        app,
        ["--index", str(legacy_index), "s", "migration"],
    )
    assert legacy_cli.exit_code != 0
    assert "rebuild the disposable index" in legacy_cli.output
    assert (legacy_index.read_bytes(), legacy_index.stat().st_mtime_ns) == legacy_before
    assert not legacy_index.with_name("state.sqlite").exists()

    current_index = tmp_path / "current" / "index.sqlite"
    current_state = current_index.with_name("state.sqlite")
    MeetilySQLiteScanner(current_index, state_path=current_state).scan(meetily_db)
    current_state.unlink()
    current_before = (current_index.read_bytes(), current_index.stat().st_mtime_ns)
    with pytest.raises(IndexReadError, match="user state not found") as error:
        MeetilyMemoryCore(current_index, state_path=current_state)
    guidance = str(error.value)
    assert "Restore the authoritative `state.sqlite`" in guidance
    assert "`mm refresh` alone cannot recover" in guidance
    assert "move or remove the disposable `index.sqlite`" in guidance
    assert "Manual tags, task statuses, and task notes cannot be recovered" in guidance
    assert not current_state.exists()
    assert (current_index.read_bytes(), current_index.stat().st_mtime_ns) == current_before


def test_lexical_search_and_meeting_ranking_remain_stable(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    repo = IndexRepository.open_existing(index_path)

    migration_rows = repo.search("migration risks", limit=5)
    pricing_rows = repo.search("pricing decision", limit=5)
    results = MeetilyMemoryCore(index_path).search("migration risks", limit=5)

    assert (migration_rows[0]["meeting_external_id"], migration_rows[0]["chunk_external_id"]) == (
        "meeting-2",
        "transcript-2",
    )
    assert (pricing_rows[0]["meeting_external_id"], pricing_rows[0]["chunk_external_id"]) == (
        "meeting-1",
        "transcript-1",
    )
    assert results.results[0].rank == 1
    assert results.results[0].meeting.external_id == "meeting-2"
    assert results.results[0].evidence[0].excerpt.chunk_external_id == "transcript-2"
