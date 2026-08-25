import sqlite3
from dataclasses import dataclass
from typing import Protocol

from meetily_memory.domain import (
    MeetingSearchResult,
    RetrievalSource,
    SearchHit,
)
from meetily_memory.repositories.index import IndexRepository, meeting_from_row
from meetily_memory.semantic_search import (
    EmbeddingProvider,
    semantic_index_coverage,
    semantic_search,
)
from meetily_memory.tagging import TagMatch, TagRepository

RRF_K = 60
HYBRID_CANDIDATE_MULTIPLIER = 4
MAX_EVIDENCE_PER_SOURCE = 2
MeetingKey = tuple[int, str]


class RetrievalStrategy(Protocol):
    def search(
        self,
        query: str,
        limit: int = 10,
    ) -> tuple[SearchHit, ...]: ...


class MeetingRetrievalStrategy(Protocol):
    def search_meetings(
        self,
        query: str,
        limit: int = 10,
        context: int = 0,
    ) -> tuple[MeetingSearchResult, ...]: ...


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
class LexicalTagMeetingRetrievalStrategy:
    repository: IndexRepository
    lexical: RetrievalStrategy
    tags: TagRetrievalStrategy
    candidate_multiplier: int = HYBRID_CANDIDATE_MULTIPLIER

    def search_meetings(
        self,
        query: str,
        limit: int = 10,
        context: int = 0,
    ) -> tuple[MeetingSearchResult, ...]:
        fts_ranks, fts_evidence = collect_hits_by_meeting(
            self.lexical,
            query,
            limit,
            candidate_multiplier=self.candidate_multiplier,
        )
        exact_tag_order, token_tag_order, matched_tags = tag_candidates(
            self.repository,
            self.tags,
            query,
        )
        ordered_keys = tuple(dict.fromkeys((*exact_tag_order, *fts_ranks, *token_tag_order)))
        results: list[MeetingSearchResult] = []
        for key in ordered_keys[:limit]:
            meeting_row = self.repository.get_meeting(str(key[0]))
            if meeting_row is None:
                continue
            meeting_id = key[0]
            meeting = meeting_from_row(meeting_row)
            evidence = fts_evidence.get(key, ())
            if context and evidence:
                evidence = self.repository.expand_search_hits(evidence, context)
            sources: list[RetrievalSource] = []
            if key in exact_tag_order:
                sources.append(RetrievalSource.TAG)
            if key in fts_ranks:
                sources.append(RetrievalSource.FTS)
            if key in token_tag_order and RetrievalSource.TAG not in sources:
                sources.append(RetrievalSource.TAG)
            results.append(
                MeetingSearchResult(
                    meeting_id=meeting_id,
                    meeting=meeting,
                    rank=len(results) + 1,
                    match_sources=tuple(sources),
                    evidence=evidence,
                    matched_tags=matched_tags.get(key, ()),
                )
            )
        return tuple(results)


@dataclass(frozen=True)
class RetrievalCandidateTrace:
    meeting_id: int
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
        fts_ranks, fts_evidence = collect_hits_by_meeting(
            self.lexical,
            query,
            limit,
            candidate_multiplier=self.candidate_multiplier,
        )
        semantic_ranks, semantic_evidence, semantic_status = self._semantic_candidates(
            query,
            limit,
        )
        tag_ranks, matched_tags = self._tag_candidates(query)
        candidate_keys = tuple(dict.fromkeys((*fts_ranks, *semantic_ranks, *tag_ranks)))
        traces = tuple(
            sorted(
                (
                    RetrievalCandidateTrace(
                        meeting_id=key[0],
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
            key = (trace.meeting_id, trace.meeting_external_id)
            meeting_row = self.repository.get_meeting(str(trace.meeting_id))
            if meeting_row is None:
                continue
            meeting_id = trace.meeting_id
            meeting = meeting_from_row(meeting_row)
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

    def _semantic_candidates(
        self,
        query: str,
        limit: int,
    ) -> tuple[
        dict[MeetingKey, int],
        dict[MeetingKey, tuple[SearchHit, ...]],
        str,
    ]:
        if self.require_complete_semantic_index:
            try:
                coverage = semantic_index_coverage(
                    self.repository.index_path,
                    self.semantic_provider,
                )
            except (RuntimeError, sqlite3.Error):
                return {}, {}, "unavailable"
            if not coverage.complete:
                return {}, {}, "incomplete"
            status = "complete"
        else:
            status = "forced"
        try:
            result = collect_hits_by_meeting(
                self.semantic,
                query,
                limit,
                candidate_multiplier=self.candidate_multiplier,
            )
        except (RuntimeError, sqlite3.Error):
            return {}, {}, "error"
        else:
            ranks, evidence = result
            return ranks, evidence, status

    def _tag_candidates(
        self,
        query: str,
    ) -> tuple[dict[MeetingKey, int], dict[MeetingKey, tuple[str, ...]]]:
        exact_order, token_order, names = tag_candidates(
            self.repository,
            self.tags,
            query,
        )
        ordered_keys = [*exact_order, *(key for key in token_order if key not in exact_order)]
        return (
            {key: rank for rank, key in enumerate(ordered_keys, start=1)},
            names,
        )


def collect_hits_by_meeting(
    strategy: RetrievalStrategy,
    query: str,
    meeting_limit: int,
    *,
    candidate_multiplier: int,
) -> tuple[dict[MeetingKey, int], dict[MeetingKey, tuple[SearchHit, ...]]]:
    candidate_limit = max(meeting_limit, meeting_limit * candidate_multiplier)
    while True:
        hits = strategy.search(query, candidate_limit)
        ranks, evidence = collapse_hits_by_meeting(hits)
        if len(ranks) >= meeting_limit or len(hits) < candidate_limit:
            return ranks, evidence
        candidate_limit *= 2


def tag_candidates(
    repository: IndexRepository,
    tags: TagRetrievalStrategy,
    query: str,
) -> tuple[list[MeetingKey], list[MeetingKey], dict[MeetingKey, tuple[str, ...]]]:
    exact_order: list[MeetingKey] = []
    token_order: list[MeetingKey] = []
    names: dict[MeetingKey, list[str]] = {}
    for match in tags.search(query):
        meeting = repository.get_meeting_by_identity(
            match.source_uuid,
            match.meeting_external_id,
        )
        if meeting is None:
            continue
        key = (int(meeting["id"]), match.meeting_external_id)
        order = exact_order if match.kind == "exact" else token_order
        if key not in order:
            order.append(key)
        values = names.setdefault(key, [])
        if match.tag.display_name not in values:
            values.append(match.tag.display_name)
    return (
        exact_order,
        token_order,
        {key: tuple(values) for key, values in names.items()},
    )


def collapse_hits_by_meeting(
    hits: tuple[SearchHit, ...],
) -> tuple[dict[MeetingKey, int], dict[MeetingKey, tuple[SearchHit, ...]]]:
    ranks: dict[MeetingKey, int] = {}
    evidence: dict[MeetingKey, list[SearchHit]] = {}
    for hit in hits:
        key = (hit.meeting.id, hit.meeting.external_id)
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
