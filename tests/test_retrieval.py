import sqlite3
from datetime import UTC, datetime
from inspect import signature
from pathlib import Path
from typing import override

from meetily_memory import retrieval
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.domain import MeetingSearchFilters, SearchHit
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.tagging import TagRepository
from meetily_memory.user_state import UserStateRepository
from tests.index_helpers import publish_fresh_index


def test_core_constructor_exposes_only_runtime_storage_dependencies() -> None:
    assert tuple(signature(MeetilyMemoryCore).parameters) == ("index_path", "state_path")


class FixedIndexRepository(IndexRepository):
    def __init__(self, index_path: Path, hits: tuple[SearchHit, ...]) -> None:
        super().__init__(index_path)
        self.hits = hits

    @override
    def search_hits(
        self,
        query: str,
        limit: int = 10,
        *,
        meeting_id: int | None = None,
        context: int = 0,
        filters: MeetingSearchFilters | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[SearchHit, ...]:
        del query, meeting_id, context, filters, connection
        return self.hits[:limit]


def test_meeting_retrieval_expands_candidates_until_limit_has_unique_meetings(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    first = IndexRepository(index_path).search_hits("pricing decision", 1)[0]
    second = IndexRepository(index_path).search_hits("migration risks", 1)[0]
    repository = FixedIndexRepository(index_path, (*((first,) * 40), second))
    service = retrieval.MeetingSearchService(repository, TagRepository(repository.state_path))

    results = service.search("query ignored by repository", limit=2)

    assert [result.meeting.external_id for result in results] == [
        "meeting-1",
        "meeting-2",
    ]


def test_fts_date_filter_is_applied_before_candidate_limit(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    with sqlite3.connect(index_path) as conn:
        chunk_id = conn.execute(
            "SELECT id FROM chunks WHERE meeting_id = 1 ORDER BY ordinal LIMIT 1"
        ).fetchone()[0]
        dominant_text = "migration risks " * 20
        conn.execute("UPDATE chunks SET text = ? WHERE id = ?", (dominant_text, chunk_id))
        conn.execute("UPDATE chunks_fts SET text = ? WHERE chunk_id = ?", (dominant_text, chunk_id))
        conn.commit()
    core = MeetilyMemoryCore(index_path)
    filters = MeetingSearchFilters(
        from_utc=datetime(2026, 7, 2, tzinfo=UTC),
        to_utc=datetime(2026, 7, 3, tzinfo=UTC),
    )

    unfiltered = core.search("migration risks", limit=1).results
    filtered = core.search("migration risks", limit=1, filters=filters).results

    assert unfiltered[0].meeting.external_id == "meeting-1"
    assert filtered[0].meeting.external_id == "meeting-2"


def test_meeting_search_service_includes_tag_matches(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.sqlite"
    state = UserStateRepository(state_path)
    source_uuid = state.get_or_create_source("meetily_sqlite", "/source.sqlite", now="1")
    repository = TagRepository(state_path)
    repository.assign(source_uuid, ("meeting-1",), ("Сбер собес",), now="2")

    matches = repository.search("сбер")

    assert [(match.meeting_ref.external_id, match.kind) for match in matches] == [
        ("meeting-1", "token")
    ]
