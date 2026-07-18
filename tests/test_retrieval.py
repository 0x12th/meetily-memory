from dataclasses import dataclass
from pathlib import Path

import pytest

from meetily_memory import retrieval
from meetily_memory.context_builder import ContextRenderer
from meetily_memory.core import CORE_V2_VERSION, MeetilyMemoryCore
from meetily_memory.domain import ContextBundle, SearchHit
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.semantic_search import LocalHashEmbeddingProvider, index_semantic_embeddings
from tests.semantic_helpers import requires_sqlite_vec


@dataclass(frozen=True)
class FixedRetrievalStrategy:
    hits: tuple[SearchHit, ...]

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        meeting_id: int | None = None,
        context: int = 0,
    ) -> tuple[SearchHit, ...]:
        del query, meeting_id, context
        return self.hits[:limit]


def test_selected_strategy_drives_v2_search_and_context_bundle(
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
    bundle = core.context_bundle("question ignored by strategy")
    context = core.build_context(
        "question ignored by strategy",
        contract_version=CORE_V2_VERSION,
    )

    assert search.data["results"] == [lexical_hit.as_payload()]
    assert bundle.evidence == (lexical_hit,)
    assert context.data == bundle.as_payload()


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
        lexical=FixedRetrievalStrategy((migration_hit, pricing_hit)),
        semantic=FixedRetrievalStrategy((pricing_hit, migration_hit)),
    )

    result = strategy.search_with_trace("project history", 2)

    assert result.hits == (migration_hit, pricing_hit)
    assert result.trace.mode == "hybrid_rrf"
    assert result.trace.candidates[0].evidence_id == migration_hit.id
    assert result.trace.candidates[0].lexical_rank == 1
    assert result.trace.candidates[0].semantic_rank == 2
    assert "rank" not in result.hits[0].as_payload()


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
    with pytest.raises(ValueError, match="meeting-scoped"):
        strategy.search("migration risks", 3, meeting_id=2)
