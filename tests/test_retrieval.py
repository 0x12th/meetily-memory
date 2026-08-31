import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import override

import pytest

from meetily_memory import retrieval
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.domain import (
    MeetingSearchFilters,
    MeetingSearchResult,
    RetrievalSource,
    SearchHit,
)
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.semantic_search import (
    EmbeddingRole,
    LocalHashEmbeddingProvider,
    index_semantic_embeddings,
)
from meetily_memory.tagging import TagRepository, TagService
from meetily_memory.user_state import UserStateRepository
from tests.semantic_helpers import requires_sqlite_vec


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


class SnapshotTrackingEmbeddingProvider:
    def __init__(self) -> None:
        self.delegate: LocalHashEmbeddingProvider = LocalHashEmbeddingProvider()
        self.snapshot_open: bool = False
        self.query_snapshot_states: list[bool] = []

    @property
    def name(self) -> str:
        return self.delegate.name

    @property
    def model(self) -> str:
        return self.delegate.model

    @property
    def dims(self) -> int | None:
        return self.delegate.dims

    def embed(self, texts: list[str], *, role: EmbeddingRole) -> list[list[float]]:
        if role == "query":
            self.query_snapshot_states.append(self.snapshot_open)
        return self.delegate.embed(texts, role=role)


class FailingQueryEmbeddingProvider:
    name = "hash"
    model = "local-hash-v1"
    dims: int | None = 128

    def __init__(self) -> None:
        self.query_calls = 0

    def embed(self, texts: list[str], *, role: EmbeddingRole) -> list[list[float]]:
        del texts
        assert role == "query"
        self.query_calls += 1
        message = "provider unavailable during query preparation"
        raise RuntimeError(message)


class NoCallEmbeddingProvider:
    name = "hash"
    model = "local-hash-v1"
    dims: int | None = 128

    def __init__(self) -> None:
        self.query_calls = 0

    def embed(self, texts: list[str], *, role: EmbeddingRole) -> list[list[float]]:
        del texts, role
        self.query_calls += 1
        message = "incomplete semantic index must skip the provider"
        raise AssertionError(message)


def test_core_delegates_public_search_to_meeting_retrieval_strategy(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
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
    MeetilySQLiteScanner(index_path).scan(meetily_db)
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
    MeetilySQLiteScanner(index_path).scan(meetily_db)
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
    MeetilySQLiteScanner(index_path).scan(meetily_db)
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
    MeetilySQLiteScanner(index_path).scan(meetily_db)
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
    MeetilySQLiteScanner(index_path).scan(meetily_db)
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

    assert [(match.meeting_external_id, match.kind) for match in matches] == [
        ("meeting-1", "token")
    ]


def test_hybrid_strategy_fuses_ranks_without_polluting_search_hits(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    migration_hit = IndexRepository(index_path).search_hits("migration risks", 1)[0]
    pricing_hit = IndexRepository(index_path).search_hits("pricing decision", 1)[0]
    assert hasattr(retrieval, "HybridRetrievalStrategy")
    strategy = retrieval.HybridRetrievalStrategy(
        repository=IndexRepository(index_path),
        lexical=FixedRetrievalStrategy((migration_hit, pricing_hit)),
        semantic=FixedRetrievalStrategy((pricing_hit, migration_hit)),
        tags=retrieval.TagRetrievalStrategy(TagRepository(IndexRepository(index_path).state_path)),
        semantic_provider=LocalHashEmbeddingProvider(),
        require_complete_semantic_index=False,
    )

    result = strategy.search_with_trace("project history", 2)

    assert [item.meeting.external_id for item in result.results] == ["meeting-2", "meeting-1"]
    assert result.trace.mode == "hybrid_rrf"
    assert result.trace.semantic_status == "forced"
    assert result.trace.candidates[0].meeting_external_id == "meeting-2"
    assert result.trace.candidates[0].fts_rank == 1
    assert result.trace.candidates[0].semantic_rank == 2
    assert result.results[0].match_sources == (
        retrieval.RetrievalSource.FTS,
        retrieval.RetrievalSource.SEMANTIC,
    )
    assert not hasattr(result.results[0], "score")


def test_hybrid_strategy_uses_tags_as_a_meeting_level_rrf_source(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    repository = IndexRepository(index_path)
    migration_hit = repository.search_hits("migration risks", 1)[0]
    pricing_hit = repository.search_hits("pricing decision", 1)[0]
    meeting_ref = repository.meeting_ref_for_local_id(1)
    assert meeting_ref is not None
    TagService(repository).assign((meeting_ref,), ("project-history",))
    strategy = retrieval.HybridRetrievalStrategy(
        repository=repository,
        lexical=FixedRetrievalStrategy((migration_hit, pricing_hit)),
        semantic=FixedRetrievalStrategy((migration_hit, pricing_hit)),
        tags=retrieval.TagRetrievalStrategy(TagRepository(IndexRepository(index_path).state_path)),
        semantic_provider=LocalHashEmbeddingProvider(),
        require_complete_semantic_index=False,
    )

    result = strategy.search_with_trace("project-history", 2)

    assert result.results[0].meeting.external_id == "meeting-1"
    assert result.results[0].matched_tags == ("project-history",)
    assert retrieval.RetrievalSource.TAG in result.results[0].match_sources
    assert result.trace.candidates[0].tag_rank == 1


@dataclass
class FailingRetrievalStrategy:
    calls: int = 0

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> tuple[SearchHit, ...]:
        del query, limit, filters
        self.calls += 1
        message = "semantic unavailable"
        raise RuntimeError(message)


def test_hybrid_strategy_skips_provider_when_semantic_index_is_incomplete(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    repository = IndexRepository(index_path)
    provider = NoCallEmbeddingProvider()
    strategy = retrieval.HybridRetrievalStrategy(
        repository=repository,
        lexical=retrieval.LexicalRetrievalStrategy(repository),
        semantic=retrieval.SemanticRetrievalStrategy(repository, provider),
        tags=retrieval.TagRetrievalStrategy(TagRepository(repository.state_path)),
        semantic_provider=provider,
    )

    result = strategy.search_with_trace("migration risks", 5)

    assert provider.query_calls == 0
    assert result.trace.semantic_status == "incomplete"
    assert result.results[0].match_sources == (retrieval.RetrievalSource.FTS,)


@requires_sqlite_vec
def test_hybrid_strategy_falls_back_when_ready_semantic_search_fails(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    provider = LocalHashEmbeddingProvider()
    index_semantic_embeddings(index_path, embedding_provider=provider)
    lexical_hit = IndexRepository(index_path).search_hits("migration risks", 1)[0]
    semantic = FailingRetrievalStrategy()
    strategy = retrieval.HybridRetrievalStrategy(
        repository=IndexRepository(index_path),
        lexical=FixedRetrievalStrategy((lexical_hit,)),
        semantic=semantic,
        tags=retrieval.TagRetrievalStrategy(TagRepository(IndexRepository(index_path).state_path)),
        semantic_provider=provider,
    )

    result = strategy.search_with_trace("migration risks", 5)

    assert semantic.calls == 1
    assert result.trace.semantic_status == "error"
    assert result.results[0].match_sources == (retrieval.RetrievalSource.FTS,)


@requires_sqlite_vec
def test_hybrid_strategy_keeps_lexical_and_tag_results_when_query_prepare_fails(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    index_semantic_embeddings(
        index_path,
        embedding_provider=LocalHashEmbeddingProvider(),
    )
    repository = IndexRepository(index_path)
    meeting_ref = repository.meeting_ref_for_local_id(1)
    assert meeting_ref is not None
    TagService(repository).assign((meeting_ref,), ("migration risks",))
    provider = FailingQueryEmbeddingProvider()
    strategy = retrieval.HybridRetrievalStrategy(
        repository=repository,
        lexical=retrieval.LexicalRetrievalStrategy(repository),
        semantic=retrieval.SemanticRetrievalStrategy(repository, provider),
        tags=retrieval.TagRetrievalStrategy(TagRepository(repository.state_path)),
        semantic_provider=provider,
    )

    result = strategy.search_with_trace("migration risks", 5)

    assert provider.query_calls == 1
    assert result.trace.semantic_status == "error"
    assert result.results
    assert {source for item in result.results for source in item.match_sources} >= {
        retrieval.RetrievalSource.FTS,
        retrieval.RetrievalSource.TAG,
    }
    assert any(item.matched_tags == ("migration risks",) for item in result.results)


@requires_sqlite_vec
def test_core_prepares_hybrid_embedding_before_opening_operation_snapshot(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    provider = SnapshotTrackingEmbeddingProvider()
    index_semantic_embeddings(index_path, embedding_provider=provider)
    repository = IndexRepository.open_existing(index_path)
    strategy = retrieval.HybridRetrievalStrategy(
        repository=repository,
        lexical=retrieval.LexicalRetrievalStrategy(repository),
        semantic=retrieval.SemanticRetrievalStrategy(repository, provider),
        tags=retrieval.TagRetrievalStrategy(TagRepository.open_existing(repository.state_path)),
        semantic_provider=provider,
    )
    core = MeetilyMemoryCore(index_path, meeting_retrieval_strategy=strategy)
    original_operation_snapshot = IndexRepository.operation_snapshot

    @contextmanager
    def tracked_operation_snapshot(
        snapshot_repository: IndexRepository,
    ) -> Generator[sqlite3.Connection, None, None]:
        provider.snapshot_open = True
        try:
            with original_operation_snapshot(snapshot_repository) as connection:
                yield connection
        finally:
            provider.snapshot_open = False

    monkeypatch.setattr(IndexRepository, "operation_snapshot", tracked_operation_snapshot)

    results = core.search("migration risks", limit=1).results

    assert results
    assert provider.query_snapshot_states == [False]


@requires_sqlite_vec
def test_semantic_strategy_returns_domain_search_hits(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    provider = LocalHashEmbeddingProvider()
    index_semantic_embeddings(index_path, embedding_provider=provider)
    assert hasattr(retrieval, "SemanticRetrievalStrategy")
    strategy = retrieval.SemanticRetrievalStrategy(IndexRepository(index_path), provider)

    hits = strategy.search("migration risks", 3)
    filtered = strategy.search(
        "pricing decision",
        1,
        filters=MeetingSearchFilters(
            from_utc=datetime(2026, 7, 2, tzinfo=UTC),
            to_utc=datetime(2026, 7, 3, tzinfo=UTC),
        ),
    )

    assert hits
    assert all(isinstance(hit, SearchHit) for hit in hits)
    assert filtered
    assert {hit.meeting.external_id for hit in filtered} == {"meeting-2"}
