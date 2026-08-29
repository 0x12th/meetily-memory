import sqlite3
from pathlib import Path

from meetily_memory.core import ContextRetrievalOptions, MeetilyMemoryCore
from meetily_memory.domain import MemoryEntity, SearchHit, SearchResults
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.serializers import context_bundle_payload, memory_entity_payload


def test_search_has_one_meeting_level_contract(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)

    search = MeetilyMemoryCore(index_path).search("migration risks", limit=3)

    assert isinstance(search, SearchResults)
    assert search.query == "migration risks"
    assert search.context == 0
    assert search.results[0].meeting.external_id == "meeting-2"
    assert search.results[0].match_sources
    assert search.results[0].evidence


def test_search_hit_identity_and_source_survive_index_rebuild(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    first = IndexRepository(index_path, state_path=state_path).search_hits("pricing decision")[0]

    index_path.unlink()
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path, state_path=state_path)
    second = IndexRepository(index_path, state_path=state_path).search_hits("pricing decision")[0]
    resolved = core.resolve_search_hit(first.id)

    assert isinstance(first, SearchHit)
    assert first.id == second.id
    assert first.meeting.external_id == second.meeting.external_id
    assert first.excerpt.text == "Alice confirmed the launch checklist and pricing decision."
    assert resolved == second


def test_context_is_data_only_and_uses_canonical_memory_entities(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path)

    bundle = core.build_context("migration risks", limit=5)
    bounded = core._build_context_bundle(  # noqa: SLF001
        "migration risks",
        3,
        ContextRetrievalOptions(neighbor_count=1, max_evidence=5),
    )
    payload = context_bundle_payload(bundle)

    assert "markdown" not in payload
    assert bundle.evidence
    assert all(isinstance(hit, SearchHit) for hit in bundle.evidence)
    assert all(isinstance(entity, MemoryEntity) for entity in bundle.entities)
    assert {entity.kind for entity in bundle.entities} <= {"decision", "task", "risk", "question"}
    assert all(entity.authoritative is False for entity in bundle.entities)
    assert all(entity.evidence_id for entity in bundle.entities)
    assert all("confidence" not in memory_entity_payload(entity) for entity in bundle.entities)
    assert bounded.evidence
    assert len(bounded.evidence) <= 5
    assert next(iter(bounded.evidence)).is_context is False
    assert any(hit.is_context for hit in bounded.evidence)


def test_context_neighbors_are_explicit_without_changing_search_default(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path)

    search = core.search("migration risks", limit=3)
    context = core.build_context("migration risks", limit=3)
    expanded = core.build_context("migration risks", limit=3, context=2)

    assert all(not evidence.is_context for result in search.results for evidence in result.evidence)
    assert all(not result.is_context for result in context.evidence)
    assert any(result.is_context for result in expanded.evidence)
    assert len(expanded.evidence) <= 20


def test_memory_entities_require_chunks_and_cascade_on_chunk_delete(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    with sqlite3.connect(index_path) as conn:
        table_info = conn.execute("PRAGMA table_info(action_items)").fetchall()
        source_chunk = next(row for row in table_info if row[1] == "source_chunk_id")
        foreign_keys = conn.execute("PRAGMA foreign_key_list(action_items)").fetchall()
        entity = conn.execute(
            "SELECT id, source_chunk_id FROM action_items WHERE source_chunk_id IS NOT NULL LIMIT 1"
        ).fetchone()
        assert entity is not None

        assert source_chunk[3] == 1
        assert any(row[3] == "source_chunk_id" and row[6] == "CASCADE" for row in foreign_keys)

        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM chunks WHERE id = ?", (entity[1],))
        conn.commit()
        remaining = conn.execute("SELECT 1 FROM action_items WHERE id = ?", (entity[0],)).fetchone()
        assert remaining is None
