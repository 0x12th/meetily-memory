import sqlite3
from dataclasses import dataclass
from typing import Protocol

from meetily_memory.domain import MeetingSearchResult, RetrievalSource, SearchHit
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.semantic_search import (
    EmbeddingProvider,
    semantic_index_coverage,
    semantic_search,
)
from meetily_memory.tagging import TagMatch, TagRepository

RRF_K = 60
HYBRID_CANDIDATE_MULTIPLIER = 4
MAX_EVIDENCE_PER_SOURCE = 2
MeetingKey = tuple[str, str]


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
class TagRetrievalStrategy:
    repository: TagRepository

    def search(self, query: str) -> tuple[TagMatch, ...]:
        return self.repository.search(query)


@dataclass(frozen=True)
class RetrievalCandidateTrace:
    source_uuid: str
    meeting_external_id: str
    fts_rank: int | None
    semantic_rank: int | None
    tag_rank: int | None
    fused_score: float


@dataclass(frozen=True)
class RetrievalTrace:
    query: str
    mode: str
    semantic_status: str
    candidates: tuple[RetrievalCandidateTrace, ...]


@dataclass(frozen=True)
class RetrievalResult:
    results: tuple[MeetingSearchResult, ...]
    trace: RetrievalTrace


@dataclass(frozen=True)
class HybridRetrievalStrategy:
    repository: IndexRepository
    lexical: RetrievalStrategy
    semantic: RetrievalStrategy
    tags: TagRetrievalStrategy
    semantic_provider: EmbeddingProvider
    rrf_k: int = RRF_K
    candidate_multiplier: int = HYBRID_CANDIDATE_MULTIPLIER
    fts_weight: float = 1.0
    semantic_weight: float = 1.0
    tag_weight: float = 1.0
    require_complete_semantic_index: bool = True

    def search_meetings(
        self,
        query: str,
        limit: int = 10,
        context: int = 0,
    ) -> tuple[MeetingSearchResult, ...]:
        return self.search_with_trace(query, limit, context).results

    def search_with_trace(
        self,
        query: str,
        limit: int = 10,
        context: int = 0,
    ) -> RetrievalResult:
        candidate_limit = max(limit, limit * self.candidate_multiplier)
        lexical_hits = self.lexical.search(query, candidate_limit)
        semantic_hits, semantic_status = self._semantic_hits(query, candidate_limit)
        fts_ranks, fts_evidence = collapse_hits_by_meeting(lexical_hits)
        semantic_ranks, semantic_evidence = collapse_hits_by_meeting(semantic_hits)
        tag_ranks, matched_tags = self._tag_candidates(query)
        candidate_keys = tuple(dict.fromkeys((*fts_ranks, *semantic_ranks, *tag_ranks)))
        traces = tuple(
            sorted(
                (
                    RetrievalCandidateTrace(
                        source_uuid=key[0],
                        meeting_external_id=key[1],
                        fts_rank=fts_ranks.get(key),
                        semantic_rank=semantic_ranks.get(key),
                        tag_rank=tag_ranks.get(key),
                        fused_score=weighted_rrf_score(
                            (
                                (fts_ranks.get(key), self.fts_weight),
                                (semantic_ranks.get(key), self.semantic_weight),
                                (tag_ranks.get(key), self.tag_weight),
                            ),
                            self.rrf_k,
                        ),
                    )
                    for key in candidate_keys
                ),
                key=trace_sort_key,
            )
        )
        selected = traces[:limit]
        results: list[MeetingSearchResult] = []
        for trace in selected:
            key = (trace.source_uuid, trace.meeting_external_id)
            resolved = self.repository.meeting_ref_by_identity(*key)
            if resolved is None:
                continue
            meeting_id, meeting = resolved
            evidence = unique_hits((*fts_evidence.get(key, ()), *semantic_evidence.get(key, ())))[
                :MAX_EVIDENCE_PER_SOURCE
            ]
            if context and evidence:
                evidence = self.repository.expand_search_hits(evidence, context)
            results.append(
                MeetingSearchResult(
                    meeting_id=meeting_id,
                    meeting=meeting,
                    rank=len(results) + 1,
                    match_sources=trace_sources(trace),
                    evidence=evidence,
                    matched_tags=matched_tags.get(key, ()),
                )
            )
        return RetrievalResult(
            results=tuple(results),
            trace=RetrievalTrace(
                query=query,
                mode="hybrid_rrf",
                semantic_status=semantic_status,
                candidates=traces,
            ),
        )

    def _semantic_hits(
        self,
        query: str,
        limit: int,
    ) -> tuple[tuple[SearchHit, ...], str]:
        if self.require_complete_semantic_index:
            try:
                coverage = semantic_index_coverage(
                    self.repository.index_path,
                    self.semantic_provider,
                )
            except (RuntimeError, sqlite3.Error):
                return (), "unavailable"
            if not coverage.complete:
                return (), "incomplete"
            status = "complete"
        else:
            status = "forced"
        try:
            return self.semantic.search(query, limit), status
        except (RuntimeError, sqlite3.Error):
            return (), "error"

    def _tag_candidates(
        self,
        query: str,
    ) -> tuple[dict[MeetingKey, int], dict[MeetingKey, tuple[str, ...]]]:
        ordered_keys: list[MeetingKey] = []
        names: dict[MeetingKey, list[str]] = {}
        for match in self.tags.search(query):
            key = (match.source_uuid, match.meeting_external_id)
            if self.repository.get_meeting_by_identity(*key) is None:
                continue
            if key not in ordered_keys:
                ordered_keys.append(key)
            values = names.setdefault(key, [])
            if match.tag.display_name not in values:
                values.append(match.tag.display_name)
        return (
            {key: rank for rank, key in enumerate(ordered_keys, start=1)},
            {key: tuple(values) for key, values in names.items()},
        )


def collapse_hits_by_meeting(
    hits: tuple[SearchHit, ...],
) -> tuple[dict[MeetingKey, int], dict[MeetingKey, tuple[SearchHit, ...]]]:
    ranks: dict[MeetingKey, int] = {}
    evidence: dict[MeetingKey, list[SearchHit]] = {}
    for hit in hits:
        key = (hit.meeting.source_uuid, hit.meeting.external_id)
        if key not in ranks:
            ranks[key] = len(ranks) + 1
        values = evidence.setdefault(key, [])
        if len(values) < MAX_EVIDENCE_PER_SOURCE:
            values.append(hit)
    return ranks, {key: tuple(values) for key, values in evidence.items()}


def unique_hits(hits: tuple[SearchHit, ...]) -> tuple[SearchHit, ...]:
    by_id: dict[str, SearchHit] = {}
    for hit in hits:
        by_id.setdefault(hit.id, hit)
    return tuple(by_id.values())


def weighted_rrf_score(
    ranked_sources: tuple[tuple[int | None, float], ...],
    rrf_k: int,
) -> float:
    return sum(weight / (rrf_k + rank) for rank, weight in ranked_sources if rank is not None)


def trace_sources(trace: RetrievalCandidateTrace) -> tuple[RetrievalSource, ...]:
    sources: list[RetrievalSource] = []
    if trace.fts_rank is not None:
        sources.append(RetrievalSource.FTS)
    if trace.tag_rank is not None:
        sources.append(RetrievalSource.TAG)
    if trace.semantic_rank is not None:
        sources.append(RetrievalSource.SEMANTIC)
    return tuple(sources)


def trace_sort_key(trace: RetrievalCandidateTrace) -> tuple[float, int, int, int, str]:
    missing_rank = 1_000_000
    return (
        -trace.fused_score,
        trace.fts_rank if trace.fts_rank is not None else missing_rank,
        trace.tag_rank if trace.tag_rank is not None else missing_rank,
        trace.semantic_rank if trace.semantic_rank is not None else missing_rank,
        trace.meeting_external_id,
    )
