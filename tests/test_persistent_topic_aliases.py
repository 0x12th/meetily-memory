import hashlib
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

import meetily_memory.user_state as user_state_module
from meetily_memory.cli.app import app
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.db.schema import IndexReadError
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.user_state import UserStateRepository

INDEX_ROW_QUERIES = (
    ("knowledge_nodes", "SELECT COUNT(*) FROM knowledge_nodes"),
    ("knowledge_edges", "SELECT COUNT(*) FROM knowledge_edges"),
    ("topic_aliases", "SELECT COUNT(*) FROM topic_aliases"),
    ("index_generation", "SELECT COUNT(*) FROM index_generation"),
)
STATE_ROW_QUERIES = (
    ("sources", "SELECT COUNT(*) FROM sources"),
    ("topic_alias_topics", "SELECT COUNT(*) FROM topic_alias_topics"),
    ("topic_aliases", "SELECT COUNT(*) FROM topic_aliases"),
    ("index_generations", "SELECT COUNT(*) FROM index_generations"),
    ("topic_alias_imports", "SELECT COUNT(*) FROM topic_alias_imports"),
)


@dataclass(frozen=True)
class DatabaseSnapshot:
    digest: str
    mtime_ns: int
    rows: tuple[tuple[str, int], ...]


def database_snapshot(
    path: Path,
    row_queries: tuple[tuple[str, str], ...],
) -> DatabaseSnapshot:
    physical_path = path.resolve(strict=True)
    uri = f"{physical_path.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = tuple((name, int(conn.execute(sql).fetchone()[0])) for name, sql in row_queries)
    return DatabaseSnapshot(
        digest=hashlib.sha256(physical_path.read_bytes()).hexdigest(),
        mtime_ns=physical_path.stat().st_mtime_ns,
        rows=rows,
    )


def state_topic_alias_rows(state_path: Path) -> list[tuple[object, ...]]:
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


def index_topic_alias_rows(index_path: Path) -> list[tuple[object, ...]]:
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


def test_known_and_unknown_topic_reads_leave_both_databases_unchanged(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path, state_path=state_path)
    alias = core.add_topic_alias("migration", ["move"])
    assert alias.added_aliases == ("move",)

    before = (
        database_snapshot(index_path, INDEX_ROW_QUERIES),
        database_snapshot(state_path, STATE_ROW_QUERIES),
    )

    known = core.topic("MOVE")
    unknown = core.topic("never-recorded-topic")

    assert known.topic.title == "migration"
    assert known.topic.aliases == ("move",)
    assert known.query_terms == ("migration", "move")
    assert known.structured_signals
    assert unknown.topic.title == "never-recorded-topic"
    assert unknown.topic.aliases == ()
    assert unknown.structured_signals == ()

    after = (
        database_snapshot(index_path, INDEX_ROW_QUERIES),
        database_snapshot(state_path, STATE_ROW_QUERIES),
    )
    assert after == before


def test_alias_add_command_and_explicit_delete_only_mutate_state(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    index_before_add = database_snapshot(index_path, INDEX_ROW_QUERIES)
    state_before_add = database_snapshot(state_path, STATE_ROW_QUERIES)

    result = MeetilyMemoryCore(index_path, state_path=state_path).add_topic_alias(
        "migration",
        ["Move"],
    )

    assert result.added_aliases == ("Move",)
    assert database_snapshot(index_path, INDEX_ROW_QUERIES) == index_before_add
    assert database_snapshot(state_path, STATE_ROW_QUERIES) != state_before_add
    assert [row[6:8] for row in state_topic_alias_rows(state_path)] == [("Move", "move")]

    index_before_delete = database_snapshot(index_path, INDEX_ROW_QUERIES)
    removed = IndexRepository.open_existing(
        index_path,
        state_path=state_path,
    ).remove_topic_aliases([" MOVE "])

    assert removed == ("Move",)
    assert state_topic_alias_rows(state_path) == []
    assert database_snapshot(index_path, INDEX_ROW_QUERIES) == index_before_delete


def test_topic_alias_normalization_preserves_unicode_casefold_uniqueness(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    repository = IndexRepository.open_existing(index_path, state_path=state_path)

    added = repository.add_topic_aliases(
        "Migration",
        ["  Straße  ", "STRASSE", "МИГРАЦИЯ", "миграция"],
    )
    before_conflict = database_snapshot(state_path, STATE_ROW_QUERIES)
    conflict = repository.add_topic_aliases("Other", [" strasse "])

    assert added["added_aliases"] == ["  Straße  ", "МИГРАЦИЯ"]
    assert conflict["added_aliases"] == []
    assert database_snapshot(state_path, STATE_ROW_QUERIES) == before_conflict
    state = UserStateRepository.open_existing(state_path)
    strasse_topic = state.topic_for_query("STRASSE")
    cyrillic_topic = state.topic_for_query("  миграция ")
    assert strasse_topic is not None
    assert cyrillic_topic is not None
    assert strasse_topic.title == "Migration"
    assert cyrillic_topic.title == "Migration"
    assert state.topic_for_query("Other") is None
    assert [topic.title for topic in state.list_topics()] == ["Migration"]
    assert [(row[6], row[7]) for row in state_topic_alias_rows(state_path)] == [
        ("  Straße  ", "strasse"),
        ("МИГРАЦИЯ", "миграция"),
    ]


def test_default_and_workspace_topic_alias_state_remain_isolated(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEETILY_MEMORY_DATA_DIR", raising=False)
    global_dir = tmp_path / "global"
    global_index = global_dir / "index.sqlite"
    global_state = global_dir / "state.sqlite"
    workspace_dir = tmp_path / "workspace"
    workspace_index = workspace_dir / "index.sqlite"
    workspace_state = workspace_dir / "state.sqlite"
    runner = CliRunner()

    global_scan = runner.invoke(
        app,
        ["scan", "--source", str(meetily_db), "--no-analyze"],
        env={"MEETILY_MEMORY_DATA_DIR": str(global_dir)},
    )
    workspace_scan = runner.invoke(
        app,
        ["--index", str(workspace_index), "scan", "--source", str(meetily_db), "--no-analyze"],
    )
    assert global_scan.exit_code == 0, global_scan.output
    assert workspace_scan.exit_code == 0, workspace_scan.output

    global_core = MeetilyMemoryCore(global_index, state_path=global_state)
    workspace_core = MeetilyMemoryCore(workspace_index, state_path=workspace_state)
    global_core.add_topic_alias("migration", ["global-only"])
    workspace_core.add_topic_alias("migration", ["workspace-only"])
    assert global_core.topic("global-only").topic.title == "migration"
    assert workspace_core.topic("workspace-only").topic.title == "migration"
    assert global_core.topic("workspace-only").topic.title == "workspace-only"
    assert workspace_core.topic("global-only").topic.title == "global-only"
    assert [row[6] for row in state_topic_alias_rows(global_state)] == ["global-only"]
    assert [row[6] for row in state_topic_alias_rows(workspace_state)] == ["workspace-only"]


def test_projection_and_index_recreation_preserve_authoritative_alias_rows(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    scanner = MeetilySQLiteScanner(index_path, state_path=state_path)
    scanner.scan(meetily_db)
    writer = IndexRepository(index_path, state_path=state_path)
    IndexRepository.open_existing(index_path, state_path=state_path).add_topic_aliases(
        "migration",
        ["move"],
    )
    authoritative_rows = state_topic_alias_rows(state_path)
    state_before_projection = database_snapshot(state_path, STATE_ROW_QUERIES)
    index_before_projection = database_snapshot(index_path, INDEX_ROW_QUERIES)

    writer.project_topic_aliases()

    assert database_snapshot(state_path, STATE_ROW_QUERIES) == state_before_projection
    assert database_snapshot(index_path, INDEX_ROW_QUERIES) != index_before_projection
    assert index_topic_alias_rows(index_path) == authoritative_rows

    index_path.unlink()
    scanner.scan(meetily_db)

    assert state_topic_alias_rows(state_path) == authoritative_rows
    assert index_topic_alias_rows(index_path) == authoritative_rows
    rebuilt = MeetilyMemoryCore(index_path, state_path=state_path).topic("MOVE")
    assert rebuilt.topic.title == "migration"
    assert rebuilt.topic.aliases == ("move",)


def test_topics_excludes_stale_alias_projection_before_and_after_rebuild(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    scanner = MeetilySQLiteScanner(index_path, state_path=state_path)
    scanner.scan(meetily_db)
    core = MeetilyMemoryCore(index_path, state_path=state_path)
    core.add_topic_alias("migration", ["move"])
    IndexRepository(index_path, state_path=state_path).project_topic_aliases()
    assert {topic.title for topic in core.topics()} == {"migration"}

    removed = IndexRepository.open_existing(
        index_path,
        state_path=state_path,
    ).remove_topic_aliases(["move"])
    assert removed == ("move",)
    with sqlite3.connect(index_path) as conn:
        stale_projection = conn.execute(
            """
            SELECT COUNT(*)
            FROM knowledge_nodes n
            JOIN topic_aliases a ON a.topic_node_id = n.id
            WHERE n.stable_key = 'topic:migration'
            """
        ).fetchone()[0]
    assert stale_projection == 1

    before_read = (
        database_snapshot(index_path, INDEX_ROW_QUERIES),
        database_snapshot(state_path, STATE_ROW_QUERIES),
    )
    without_rebuild = core.topics()
    assert without_rebuild == ()
    assert (
        database_snapshot(index_path, INDEX_ROW_QUERIES),
        database_snapshot(state_path, STATE_ROW_QUERIES),
    ) == before_read

    index_path.unlink()
    scanner.scan(meetily_db)
    after_rebuild = MeetilyMemoryCore(index_path, state_path=state_path).topics()

    assert after_rebuild == without_rebuild


def test_topics_keeps_index_topic_backed_by_current_knowledge_relationship(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    with sqlite3.connect(index_path) as conn:
        entity = conn.execute(
            """
            SELECT id
            FROM knowledge_nodes
            WHERE type IN ('Task', 'Decision', 'Risk', 'Question')
            ORDER BY id
            LIMIT 1
            """
        ).fetchone()
        assert entity is not None
        source = conn.execute(
            """
            SELECT source_meeting_id, source_chunk_id
            FROM knowledge_edges
            WHERE from_node_id = ? OR to_node_id = ?
            ORDER BY id
            LIMIT 1
            """,
            (entity[0], entity[0]),
        ).fetchone()
        assert source is not None
        topic_id = conn.execute(
            """
            INSERT INTO knowledge_nodes (
              type, stable_key, title, normalized_title,
              created_at, updated_at, raw_metadata_json
            ) VALUES ('Topic', 'topic:derived', 'Derived', 'derived', 'created', 'updated', NULL)
            RETURNING id
            """
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO knowledge_edges (
              from_node_id, relation, to_node_id, confidence,
              source_meeting_id, source_chunk_id, extraction_method,
              created_at, raw_metadata_json
            ) VALUES (?, 'belongs_to', ?, 0.7, ?, ?, 'topic_query', 'created', NULL)
            """,
            (entity[0], topic_id, source[0], source[1]),
        )
        conn.commit()

    topics = MeetilyMemoryCore(index_path, state_path=state_path).topics()

    assert [(topic.title, topic.aliases) for topic in topics] == [("Derived", ())]


def test_state_only_topic_ids_are_deterministic_and_unique(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path, state_path=state_path)
    core.add_topic_alias("migration", ["move"])
    core.add_topic_alias("Architecture", ["design"])

    before_reads = (
        database_snapshot(index_path, INDEX_ROW_QUERIES),
        database_snapshot(state_path, STATE_ROW_QUERIES),
    )
    first = {topic.title: topic.id for topic in core.topics()}
    second = {
        topic.title: topic.id
        for topic in MeetilyMemoryCore(index_path, state_path=state_path).topics()
    }

    assert first == second
    assert set(first) == {"migration", "Architecture"}
    assert len(set(first.values())) == len(first)
    assert all(topic_id <= -(1 << 62) for topic_id in first.values())
    assert (
        database_snapshot(index_path, INDEX_ROW_QUERIES),
        database_snapshot(state_path, STATE_ROW_QUERIES),
    ) == before_reads


def test_graph_resolve_and_reads_share_one_explicit_index_snapshot(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    repository = IndexRepository.open_existing(index_path, state_path=state_path)
    original_connection = repository.knowledge.context.connection
    connection_count = 0
    statements: list[str] = []

    @contextmanager
    def tracked_connection(path: Path) -> Generator[sqlite3.Connection, None, None]:
        nonlocal connection_count
        connection_count += 1
        with original_connection(path) as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    repository.knowledge.context = replace(
        repository.knowledge.context,
        connection=tracked_connection,
    )

    graph = repository.graph_for_topic("migration")

    assert connection_count == 1
    assert graph["nodes"]
    controls = [
        statement.strip().upper()
        for statement in statements
        if statement.strip().upper() in {"BEGIN", "ROLLBACK"}
    ]
    assert controls == ["BEGIN", "ROLLBACK"]


def test_topic_evidence_uses_one_snapshot_and_deduplicates_only_by_evidence_id(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    repository = IndexRepository.open_existing(index_path, state_path=state_path)
    repository.add_topic_aliases("migration", ["move"])
    original_connection = repository.knowledge.context.connection
    connection_count = 0
    search_connection_ids: set[int] = set()

    @contextmanager
    def tracked_connection(path: Path) -> Generator[sqlite3.Connection, None, None]:
        nonlocal connection_count
        connection_count += 1
        with original_connection(path) as conn:
            yield conn

    def search_in_snapshot(
        conn: sqlite3.Connection,
        term: str,
        _limit: int,
    ) -> list[dict[str, object]]:
        assert conn.in_transaction
        search_connection_ids.add(id(conn))
        if term == "migration":
            return [{"evidence_id": "evidence:one", "chunk_id": 11, "language": "en"}]
        return [
            {"evidence_id": "evidence:one", "chunk_id": 99, "language": "en"},
            {"evidence_id": "evidence:two", "chunk_id": 11, "language": "en"},
        ]

    repository.knowledge.context = replace(
        repository.knowledge.context,
        connection=tracked_connection,
        search_meetings=search_in_snapshot,
    )

    memory = repository.topic_memory("migration")

    assert connection_count == 1
    assert len(search_connection_ids) == 1
    assert [row["evidence_id"] for row in memory["evidence"]] == [
        "evidence:one",
        "evidence:two",
    ]


def test_cross_owner_canonical_alias_conflict_is_atomic(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    repository = IndexRepository.open_existing(index_path, state_path=state_path)
    repository.add_topic_aliases("Beta", ["bee"])
    before = (
        database_snapshot(index_path, INDEX_ROW_QUERIES),
        database_snapshot(state_path, STATE_ROW_QUERIES),
    )

    conflict = repository.add_topic_aliases("Alpha", ["alpha-free", "  BETA  "])

    assert conflict["added_aliases"] == []
    assert (
        database_snapshot(index_path, INDEX_ROW_QUERIES),
        database_snapshot(state_path, STATE_ROW_QUERIES),
    ) == before
    state = UserStateRepository.open_existing(state_path)
    beta = state.topic_for_query(" beta ")
    assert beta is not None
    assert beta.title == "Beta"
    assert state.topic_for_query("Alpha") is None
    assert state.topic_for_query("alpha-free") is None
    assert [topic.title for topic in state.list_topics()] == ["Beta"]


def test_canonical_topic_resolution_precedes_conflicting_legacy_alias(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    repository = IndexRepository.open_existing(index_path, state_path=state_path)
    repository.add_topic_aliases("Beta", ["bee"])
    repository.add_topic_aliases("Alpha", ["aye"])
    with sqlite3.connect(state_path) as conn:
        conn.execute(
            """
            INSERT INTO topic_aliases (
              normalized_alias, topic_stable_key, alias, created_at
            ) VALUES ('beta', 'topic:alpha', 'Beta', 'legacy-conflict')
            """
        )
        conn.commit()

    resolved = UserStateRepository.open_existing(state_path).topic_for_query("  BETA ")

    assert resolved is not None
    assert resolved.title == "Beta"
    assert resolved.stable_key == "topic:beta"


def test_alias_add_after_state_deletion_does_not_recreate_database_or_directory(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_dir = tmp_path / "authoritative"
    state_path = state_dir / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path, state_path=state_path)
    index_before = database_snapshot(index_path, INDEX_ROW_QUERIES)
    state_path.unlink()
    state_dir.rmdir()

    with pytest.raises(IndexReadError, match="Restore the authoritative"):
        core.add_topic_alias("migration", ["move"])

    assert not state_dir.exists()
    assert database_snapshot(index_path, INDEX_ROW_QUERIES) == index_before


def test_alias_add_rolls_back_if_validated_state_is_retargeted_before_commit(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    first_state = tmp_path / "first-state.sqlite"
    second_state = tmp_path / "second-state.sqlite"
    logical_state = tmp_path / "current-state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=first_state).scan(meetily_db)
    UserStateRepository(second_state)
    logical_state.symlink_to(first_state)
    core = MeetilyMemoryCore(index_path, state_path=logical_state)
    first_before = database_snapshot(first_state, STATE_ROW_QUERIES)
    second_before = database_snapshot(second_state, STATE_ROW_QUERIES)
    index_before = database_snapshot(index_path, INDEX_ROW_QUERIES)
    retargeted = False

    def retarget_state(name: str) -> None:
        nonlocal retargeted
        if name != "before_identity_recheck" or retargeted:
            return
        retargeted = True
        logical_state.unlink()
        logical_state.symlink_to(second_state)

    monkeypatch.setattr(user_state_module, "_topic_alias_mutation_checkpoint", retarget_state)

    with pytest.raises(IndexReadError, match="Restore the authoritative"):
        core.add_topic_alias("migration", ["move"])

    assert retargeted
    first_after = database_snapshot(first_state, STATE_ROW_QUERIES)
    second_after = database_snapshot(second_state, STATE_ROW_QUERIES)
    assert first_after.digest == first_before.digest
    assert first_after.rows == first_before.rows
    assert second_after == second_before
    assert database_snapshot(index_path, INDEX_ROW_QUERIES) == index_before
    assert state_topic_alias_rows(first_state) == []
    assert state_topic_alias_rows(second_state) == []


def test_index_canonical_metadata_is_preserved_on_read_and_alias_seed(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    with sqlite3.connect(index_path) as conn:
        topic_id = conn.execute(
            """
            INSERT INTO knowledge_nodes (
              type, stable_key, title, normalized_title,
              created_at, updated_at, raw_metadata_json
            ) VALUES (
              'Topic', 'topic:beta', 'Beta', 'beta',
              'canonical-created', 'canonical-updated', '{"origin":"index"}'
            )
            RETURNING id
            """
        ).fetchone()[0]
        conn.commit()
    core = MeetilyMemoryCore(index_path, state_path=state_path)
    before_read = (
        database_snapshot(index_path, INDEX_ROW_QUERIES),
        database_snapshot(state_path, STATE_ROW_QUERIES),
    )

    memory = core.topic("  bEtA  ")

    assert memory.topic.id == topic_id
    assert memory.topic.title == "Beta"
    assert core.topics() == ()
    assert UserStateRepository.open_existing(state_path).list_topics() == ()
    assert (
        database_snapshot(index_path, INDEX_ROW_QUERIES),
        database_snapshot(state_path, STATE_ROW_QUERIES),
    ) == before_read

    index_before_alias = database_snapshot(index_path, INDEX_ROW_QUERIES)
    added = core.add_topic_alias("bETA", ["Second"])

    assert added.topic.id == topic_id
    assert added.topic.title == "Beta"
    assert added.added_aliases == ("Second",)
    assert database_snapshot(index_path, INDEX_ROW_QUERIES) == index_before_alias
    stored = UserStateRepository.open_existing(state_path).list_topics()
    assert len(stored) == 1
    assert stored[0].stable_key == "topic:beta"
    assert stored[0].title == "Beta"
    assert stored[0].normalized_title == "beta"
    assert stored[0].created_at == "canonical-created"
    assert stored[0].updated_at == "canonical-updated"
    assert stored[0].raw_metadata_json == '{"origin":"index"}'
    assert core.topic("SECOND").topic.title == "Beta"
