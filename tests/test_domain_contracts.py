import sqlite3
from pathlib import Path

from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.domain import SearchHit, SearchResults
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner


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
    repository = IndexRepository(index_path, state_path=state_path)
    second = repository.search_hits("pricing decision")[0]
    resolved = repository.get_search_hit(first.id)

    assert isinstance(first, SearchHit)
    assert first.id == second.id
    assert first.meeting.external_id == second.meeting.external_id
    assert first.excerpt.text == "Alice confirmed the launch checklist and pricing decision."
    assert resolved == second


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
