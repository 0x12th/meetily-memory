from dataclasses import dataclass
from typing import Protocol

from meetily_memory.domain import SearchHit
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.semantic_search import EmbeddingProvider, semantic_search

RRF_K = 60
HYBRID_CANDIDATE_MULTIPLIER = 4


class RetrievalStrategy(Protocol):
    def search(
        self,
        query: str,
        limit: int = 10,
    ) -> tuple[SearchHit, ...]: ...


@dataclass(frozen=True)
class LexicalRetrievalStrategy:
    repository: IndexRepository

    def search(
        self,
        query: str,
        limit: int = 10,
    ) -> tuple[SearchHit, ...]:
        return self.repository.search_hits(query, limit)


@dataclass(frozen=True)
class SemanticRetrievalStrategy:
    repository: IndexRepository
    embedding_provider: EmbeddingProvider

    def search(
        self,
        query: str,
        limit: int = 10,
    ) -> tuple[SearchHit, ...]:
        rows = semantic_search(
            self.repository.index_path,
            query,
            limit,
            embedding_provider=self.embedding_provider,
        )
        return tuple(self.repository.search_hit_from_row(row) for row in rows)


@dataclass(frozen=True)
class RetrievalCandidateTrace:
    evidence_id: str
    lexical_rank: int | None
    semantic_rank: int | None
    fused_score: float


@dataclass(frozen=True)
class RetrievalTrace:
    query: str
    mode: str
    candidates: tuple[RetrievalCandidateTrace, ...]


@dataclass(frozen=True)
class RetrievalResult:
    hits: tuple[SearchHit, ...]
    trace: RetrievalTrace


@dataclass(frozen=True)
class HybridRetrievalStrategy:
    lexical: RetrievalStrategy
    semantic: RetrievalStrategy
    rrf_k: int = RRF_K
    candidate_multiplier: int = HYBRID_CANDIDATE_MULTIPLIER

    def search(
        self,
        query: str,
        limit: int = 10,
    ) -> tuple[SearchHit, ...]:
        return self.search_with_trace(query, limit).hits

    def search_with_trace(
        self,
        query: str,
        limit: int = 10,
    ) -> RetrievalResult:
        candidate_limit = max(limit, limit * self.candidate_multiplier)
        lexical_hits = self.lexical.search(query, candidate_limit)
        semantic_hits = self.semantic.search(query, candidate_limit)
        lexical_ranks = {hit.id: rank for rank, hit in enumerate(lexical_hits, start=1)}
        semantic_ranks = {hit.id: rank for rank, hit in enumerate(semantic_hits, start=1)}
        hits_by_id = {hit.id: hit for hit in (*lexical_hits, *semantic_hits)}
        traces = tuple(
            sorted(
                (
                    RetrievalCandidateTrace(
                        evidence_id=evidence_id,
                        lexical_rank=lexical_ranks.get(evidence_id),
                        semantic_rank=semantic_ranks.get(evidence_id),
                        fused_score=rrf_score(
                            lexical_ranks.get(evidence_id),
                            semantic_ranks.get(evidence_id),
                            self.rrf_k,
                        ),
                    )
                    for evidence_id in hits_by_id
                ),
                key=trace_sort_key,
            )
        )
        selected = traces[:limit]
        return RetrievalResult(
            hits=tuple(hits_by_id[trace.evidence_id] for trace in selected),
            trace=RetrievalTrace(query=query, mode="hybrid_rrf", candidates=traces),
        )


def rrf_score(lexical_rank: int | None, semantic_rank: int | None, rrf_k: int) -> float:
    return sum(1 / (rrf_k + rank) for rank in (lexical_rank, semantic_rank) if rank is not None)


def trace_sort_key(trace: RetrievalCandidateTrace) -> tuple[float, int, int, str]:
    missing_rank = 1_000_000
    return (
        -trace.fused_score,
        trace.lexical_rank if trace.lexical_rank is not None else missing_rank,
        trace.semantic_rank if trace.semantic_rank is not None else missing_rank,
        trace.evidence_id,
    )
