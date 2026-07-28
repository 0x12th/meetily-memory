from dataclasses import dataclass
from pathlib import Path

from meetily_memory import retrieval
from meetily_memory.context_builder import ContextRenderer
from meetily_memory.core import CORE_V2_VERSION, MeetilyMemoryCore
from meetily_memory.domain import ContextBundle, SearchHit
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


def test_selected_strategy_drives_only_explicit_v2_search(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    lexical_hit = MeetilyMemoryCore(index_path).search_hits("pricing decision")[0]
    core = MeetilyMemoryCore(
        index_path,
        retrieval_strategy=FixedRetrievalStrategy((lexical_hit,)),
    )

    search = core.search("query ignored by strategy", contract_version=CORE_V2_VERSION)
    bundle = core.context_bundle("migration risks")
    context = core.build_context(
        "migration risks",
        contract_version=CORE_V2_VERSION,
    )

    assert search.data["results"][0]["evidence"] == [lexical_hit.as_payload()]
    assert search.data["results"][0]["match_sources"] == ["fts"]
    assert bundle.evidence[0].excerpt.chunk_external_id == "transcript-2"
    assert bundle.evidence != (lexical_hit,)
    assert context.data == bundle.as_payload()


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
    hits = MeetilyMemoryCore(index_path).search_hits("migration risks", limit=1, context=1)
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
    core = MeetilyMemoryCore(index_path)
    migration_hit = core.search_hits("migration risks", 1)[0]
    pricing_hit = core.search_hits("pricing decision", 1)[0]
    assert hasattr(retrieval, "HybridRetrievalStrategy")
    strategy = retrieval.HybridRetrievalStrategy(
        repository=core.repo,
        lexical=FixedRetrievalStrategy((migration_hit, pricing_hit)),
        semantic=FixedRetrievalStrategy((pricing_hit, migration_hit)),
        tags=retrieval.TagRetrievalStrategy(core.tag_repository),
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
    assert "score" not in result.results[0].as_payload()


def test_hybrid_strategy_uses_tags_as_a_meeting_level_rrf_source(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path)
    migration_hit = core.search_hits("migration risks", 1)[0]
    pricing_hit = core.search_hits("pricing decision", 1)[0]
    TagService(core.repo).assign(("1",), ("project-history",))
    strategy = retrieval.HybridRetrievalStrategy(
        repository=core.repo,
        lexical=FixedRetrievalStrategy((migration_hit, pricing_hit)),
        semantic=FixedRetrievalStrategy((migration_hit, pricing_hit)),
        tags=retrieval.TagRetrievalStrategy(core.tag_repository),
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

    def search(self, query: str, limit: int = 10) -> tuple[SearchHit, ...]:
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
    core = MeetilyMemoryCore(index_path)
    lexical_hit = core.search_hits("migration risks", 1)[0]
    semantic = FailingRetrievalStrategy()
    strategy = retrieval.HybridRetrievalStrategy(
        repository=core.repo,
        lexical=FixedRetrievalStrategy((lexical_hit,)),
        semantic=semantic,
        tags=retrieval.TagRetrievalStrategy(core.tag_repository),
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
    core = MeetilyMemoryCore(index_path)
    lexical_hit = core.search_hits("migration risks", 1)[0]
    semantic = FailingRetrievalStrategy()
    strategy = retrieval.HybridRetrievalStrategy(
        repository=core.repo,
        lexical=FixedRetrievalStrategy((lexical_hit,)),
        semantic=semantic,
        tags=retrieval.TagRetrievalStrategy(core.tag_repository),
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
