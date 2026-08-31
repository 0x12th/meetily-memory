import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import override

from meetily_memory import retrieval
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.domain import (
    MeetingSearchFilters,
    MeetingSearchResult,
    RetrievalSource,
    SearchHit,
)
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.tagging import TagRepository
from meetily_memory.user_state import UserStateRepository
from tests.index_helpers import publish_fresh_index


@dataclass(frozen=True)
class FixedRetrievalStrategy:
    hits: tuple[SearchHit, ...]

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> tuple[SearchHit, ...]:
        del query, filters
        return self.hits[:limit]


@dataclass(frozen=True)
class FixedMeetingRetrievalStrategy:
    results: tuple[MeetingSearchResult, ...]

    def search_meetings(
        self,
        query: str,
        limit: int = 10,
        context: int = 0,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> tuple[MeetingSearchResult, ...]:
        del query, context, filters
        return self.results[:limit]


def test_core_delegates_public_search_to_meeting_retrieval_strategy(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    lexical_hit = IndexRepository(index_path).search_hits("pricing decision")[0]
    result = MeetingSearchResult(
        meeting_id=1,
        meeting=lexical_hit.meeting,
        rank=1,
        match_sources=(RetrievalSource.FTS,),
        evidence=(lexical_hit,),
        matched_tags=(),
    )
    core = MeetilyMemoryCore(
        index_path,
        meeting_retrieval_strategy=FixedMeetingRetrievalStrategy((result,)),
    )

    response = core.search("query ignored by strategy").results

    assert response == (result,)


def test_core_respects_overridden_builtin_meeting_strategy_method(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    repository = IndexRepository(index_path)
    lexical_hit = repository.search_hits("pricing decision")[0]
    expected = MeetingSearchResult(
        meeting_id=lexical_hit.meeting.id,
        meeting=lexical_hit.meeting,
        rank=1,
        match_sources=(RetrievalSource.FTS,),
        evidence=(lexical_hit,),
        matched_tags=(),
    )

    class CustomLexicalTagStrategy(retrieval.LexicalTagMeetingRetrievalStrategy):
        @override
        def search_meetings(
            self,
            query: str,
            limit: int = 10,
            context: int = 0,
            *,
            filters: MeetingSearchFilters | None = None,
        ) -> tuple[MeetingSearchResult, ...]:
            del query
            return super().search_meetings(
                "pricing decision",
                limit,
                context,
                filters=filters,
            )

    strategy = CustomLexicalTagStrategy(
        repository=repository,
        lexical=retrieval.LexicalRetrievalStrategy(repository),
        tags=retrieval.TagRetrievalStrategy(TagRepository(repository.state_path)),
    )
    core = MeetilyMemoryCore(index_path, meeting_retrieval_strategy=strategy)

    assert core.search("query ignored by override").results == (expected,)


def test_builtin_meeting_strategy_respects_overridden_builtin_retrieval_method(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    repository = IndexRepository(index_path)
    expected_hit = repository.search_hits("pricing decision")[0]

    class CustomLexicalStrategy(retrieval.LexicalRetrievalStrategy):
        @override
        def search(
            self,
            query: str,
            limit: int = 10,
            *,
            filters: MeetingSearchFilters | None = None,
        ) -> tuple[SearchHit, ...]:
            del query, limit, filters
            return (expected_hit,)

    core = MeetilyMemoryCore(index_path, retrieval_strategy=CustomLexicalStrategy(repository))

    result = core.search("query ignored by override").results[0]

    assert result.evidence == (expected_hit,)
    assert result.meeting == expected_hit.meeting


def test_meeting_retrieval_expands_candidates_until_limit_has_unique_meetings(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    first = IndexRepository(index_path).search_hits("pricing decision", 1)[0]
    second = IndexRepository(index_path).search_hits("migration risks", 1)[0]
    saturated = FixedRetrievalStrategy((*((first,) * 40), second))
    strategy = retrieval.LexicalTagMeetingRetrievalStrategy(
        repository=IndexRepository(index_path),
        lexical=saturated,
        tags=retrieval.TagRetrievalStrategy(TagRepository(IndexRepository(index_path).state_path)),
    )

    results = strategy.search_meetings("query ignored by strategy", limit=2)

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


def test_selected_strategy_drives_search(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    lexical_hit = IndexRepository(index_path).search_hits("pricing decision")[0]
    core = MeetilyMemoryCore(
        index_path,
        retrieval_strategy=FixedRetrievalStrategy((lexical_hit,)),
    )

    search = core.search("query ignored by strategy")

    assert search.results[0].evidence == (lexical_hit,)
    assert search.results[0].match_sources == (RetrievalSource.FTS,)


def test_tag_retrieval_strategy_keeps_lookup_behind_strategy_boundary(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.sqlite"
    state = UserStateRepository(state_path)
    source_uuid = state.get_or_create_source("meetily_sqlite", "/source.sqlite", now="1")
    repository = TagRepository(state_path)
    repository.assign(source_uuid, ("meeting-1",), ("Сбер собес",), now="2")

    matches = retrieval.TagRetrievalStrategy(repository).search("сбер")

    assert [(match.meeting_ref.external_id, match.kind) for match in matches] == [
        ("meeting-1", "token")
    ]
