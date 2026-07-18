import json
import sqlite3
from pathlib import Path

import pytest

from meetily_memory.core import (
    CORE_V1_VERSION,
    CORE_V2_VERSION,
    ContextRetrievalOptions,
    MeetilyMemoryCore,
)
from meetily_memory.domain import CompactSearchHit, MemoryEntity, SearchHit
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner


def test_core_v1_remains_default_and_v2_is_explicit(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path)

    v1 = core.search("migration risks", limit=3).as_payload()
    v2 = core.search("migration risks", limit=3, contract_version=CORE_V2_VERSION).as_payload()

    fixture = json.loads(Path("tests/fixtures/core_v1_contract.json").read_text())
    assert v1["contract_version"] == fixture["contract_version"] == CORE_V1_VERSION
    assert set(v1["data"]) == set(fixture["search_data_fields"])
    assert set(v1["data"]["results"][0]) == set(fixture["search_result_fields"])
    assert v2["contract_version"] == CORE_V2_VERSION
    assert set(v2["data"]["results"][0]) == {"id", "meeting", "excerpt", "is_context"}
    assert "chunk_id" not in v2["data"]["results"][0]

    context = core.build_context("migration risks", limit=3).as_payload()
    assert set(context["data"]) == set(fixture["context_data_fields"])


def test_search_hit_identity_and_source_survive_index_rebuild(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    first = MeetilyMemoryCore(index_path, state_path=state_path).search_hits("pricing decision")[0]

    index_path.unlink()
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path, state_path=state_path)
    second = core.search_hits("pricing decision")[0]
    resolved = core.get_search_hit(first.id)

    assert isinstance(first, SearchHit)
    assert first.id == second.id
    assert first.meeting.source_uuid == second.meeting.source_uuid
    assert first.excerpt.text == "Alice confirmed the launch checklist and pricing decision."
    assert resolved == second


def test_compact_hit_reports_truncation_and_resolves_to_full_evidence(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path)

    compact = core.compact_search_hits("migration risks", preview_length=20)[0]
    wider = core.compact_search_hits("migration risks", preview_length=40)[0]
    full = core.resolve_search_hit(compact.id)

    assert isinstance(compact, CompactSearchHit)
    assert compact.truncated is True
    assert compact.preview.endswith("…")
    assert compact.preview_length == 20
    assert compact.projection_version == "compact.v1"
    assert compact.id == wider.id
    assert compact.preview != wider.preview
    assert full.id == compact.id
    assert full.excerpt.text.startswith("Dobrynya agreed")
    with pytest.raises(LookupError, match="Evidence not found"):
        core.resolve_search_hit("evidence:missing")


def test_v2_context_is_data_only_and_uses_canonical_memory_entities(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path)

    bundle = core.context_bundle("migration risks", limit=5)
    bounded = core.context_bundle(
        "migration risks",
        limit=3,
        options=ContextRetrievalOptions(neighbor_count=1, max_evidence=5),
    )
    payload = core.build_context(
        "migration risks", limit=5, contract_version=CORE_V2_VERSION
    ).as_payload()

    assert "markdown" not in payload["data"]
    assert payload["data"] == bundle.as_payload()
    assert bundle.evidence
    assert all(isinstance(hit, SearchHit) for hit in bundle.evidence)
    assert all(isinstance(entity, MemoryEntity) for entity in bundle.entities)
    assert {entity.kind for entity in bundle.entities} <= {"decision", "task", "risk", "question"}
    assert all(entity.authoritative is False for entity in bundle.entities)
    assert all(entity.evidence_id for entity in bundle.entities)
    assert all("confidence" not in entity.as_payload() for entity in bundle.entities)
    assert len(bounded.evidence) <= 5
    assert bounded.evidence[0].is_context is False
    assert any(hit.is_context for hit in bounded.evidence)
    assert all("is_context" in hit.as_payload() for hit in bounded.evidence)


def test_context_defaults_to_bounded_neighbors_without_changing_search_default(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path)

    search = core.search("migration risks", limit=3, contract_version=CORE_V2_VERSION)
    context = core.build_context("migration risks", limit=3, contract_version=CORE_V2_VERSION)

    assert all(result["is_context"] is False for result in search.data["results"])
    assert any(result["is_context"] is True for result in context.data["evidence"])
    assert len(context.data["evidence"]) <= 20


def test_memory_entities_require_chunks_and_cascade_on_chunk_delete(
    meetily_db: Path, tmp_path: Path
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
