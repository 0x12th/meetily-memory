from dataclasses import dataclass
from pathlib import Path

from meetily_memory import retrieval
from meetily_memory.context_builder import ContextRenderer
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.domain import (
    ContextBundle,
    MeetingSearchResult,
    RetrievalSource,
    SearchHit,
)
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.semantic_search import LocalHashEmbeddingProvider, index_semantic_embeddings
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
    ) -> tuple[SearchHit, ...]:
        del query
        return self.hits[:limit]


@dataclass(frozen=True)
class FixedMeetingRetrievalStrategy:
    results: tuple[MeetingSearchResult, ...]

    def search_meetings(
        self,
        query: str,
        limit: int = 10,
        context: int = 0,
    ) -> tuple[MeetingSearchResult, ...]:
        del query, context
        return self.results[:limit]


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


def test_selected_strategy_drives_only_explicit_v3_search(
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
    bundle = core.build_context("migration risks")
    context = core.build_context("migration risks")

    assert search.results[0].evidence == (lexical_hit,)
    assert search.results[0].match_sources == (RetrievalSource.FTS,)
    assert bundle.evidence[0].excerpt.chunk_external_id == "transcript-2"
    assert bundle.evidence != (lexical_hit,)
    assert context == bundle


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


def test_context_renderer_uses_context_bundle_without_storage_rows(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    hits = IndexRepository(index_path).search_hits("migration risks", limit=1, context=1)
    bundle = ContextBundle(
        question="Who owns migration risks?",
        evidence=hits,
        entities=(),
    )

    markdown = ContextRenderer().render(bundle)

    assert markdown.startswith("# Question\n\nWho owns migration risks?")
    assert "## Meeting: Dobrynya Follow-up" in markdown
    assert "Source: meeting-2 / transcript-2" in markdown
    assert "Dobrynya agreed to send migration risks by Friday." in markdown
    assert "Evidence role: neighboring context" in markdown
    assert markdown.endswith("# Question\n\nWho owns migration risks?\n")


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
    migration_hit = IndexRepository(index_path).search_hits("migration risks", 1)[0]
    pricing_hit = IndexRepository(index_path).search_hits("pricing decision", 1)[0]
    TagService(IndexRepository(index_path)).assign(("1",), ("project-history",))
    strategy = retrieval.HybridRetrievalStrategy(
        repository=IndexRepository(index_path),
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
    ) -> tuple[SearchHit, ...]:
        del query, limit
        self.calls += 1
        message = "semantic unavailable"
        raise RuntimeError(message)


def test_hybrid_strategy_skips_semantic_when_index_is_incomplete(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    lexical_hit = IndexRepository(index_path).search_hits("migration risks", 1)[0]
    semantic = FailingRetrievalStrategy()
    strategy = retrieval.HybridRetrievalStrategy(
        repository=IndexRepository(index_path),
        lexical=FixedRetrievalStrategy((lexical_hit,)),
        semantic=semantic,
        tags=retrieval.TagRetrievalStrategy(TagRepository(IndexRepository(index_path).state_path)),
        semantic_provider=LocalHashEmbeddingProvider(),
    )

    result = strategy.search_with_trace("migration risks", 5)

    assert semantic.calls == 0
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

    assert hits
    assert all(isinstance(hit, SearchHit) for hit in hits)
