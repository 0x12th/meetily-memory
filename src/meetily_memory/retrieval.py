from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeGuard

from meetily_memory.domain import (
    Meeting,
    MeetingRef,
    MeetingSearchFilters,
    MeetingSearchResult,
    RetrievalSource,
    SearchHit,
)
from meetily_memory.repositories.index import IndexRepository, meeting_from_row

if TYPE_CHECKING:
    from meetily_memory.semantic_search import EmbeddingProvider
    from meetily_memory.tagging import TagMatch, TagRepository

RRF_K = 60
HYBRID_CANDIDATE_MULTIPLIER = 4
MAX_EVIDENCE_PER_SOURCE = 2
MeetingKey = MeetingRef


class RetrievalStrategy(Protocol):
    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> tuple[SearchHit, ...]: ...


class MeetingRetrievalStrategy(Protocol):
    def search_meetings(
        self,
        query: str,
        limit: int = 10,
        context: int = 0,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> tuple[MeetingSearchResult, ...]: ...


@dataclass(frozen=True)
class LexicalRetrievalStrategy:
    repository: IndexRepository

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> tuple[SearchHit, ...]:
        return self.repository.search_hits(query, limit, filters=filters)

    def _prepare_query_for_snapshot(self, query: str) -> None:
        del query

    def _search_in_snapshot(
        self,
        query: str,
        limit: int,
        *,
        operation_snapshot: sqlite3.Connection,
        prepared_query: object | None,
        filters: MeetingSearchFilters | None,
    ) -> tuple[SearchHit, ...]:
        del prepared_query
        return self.repository.search_hits(
            query,
            limit,
            filters=filters,
            connection=operation_snapshot,
        )


@dataclass(frozen=True)
class SemanticRetrievalStrategy:
    repository: IndexRepository
    embedding_provider: EmbeddingProvider

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> tuple[SearchHit, ...]:
        from meetily_memory.semantic_search import semantic_search  # noqa: PLC0415

        rows = semantic_search(
            self.repository.index_path,
            query,
            limit,
            embedding_provider=self.embedding_provider,
            filters=filters,
        )
        return tuple(self.repository.search_hit_from_row(row) for row in rows)

    def _prepare_query_for_snapshot(self, query: str) -> object:
        from meetily_memory.semantic_search import prepare_semantic_query  # noqa: PLC0415

        return prepare_semantic_query(query, self.embedding_provider)

    def _search_in_snapshot(
        self,
        query: str,
        limit: int,
        *,
        operation_snapshot: sqlite3.Connection,
        prepared_query: object | None,
        filters: MeetingSearchFilters | None,
    ) -> tuple[SearchHit, ...]:
        from meetily_memory.semantic_search import (  # noqa: PLC0415
            PreparedSemanticQuery,
            semantic_search,
        )

        if not isinstance(prepared_query, PreparedSemanticQuery):
            message = "Semantic query must be prepared before opening the operation snapshot."
            raise TypeError(message)
        rows = semantic_search(
            self.repository.index_path,
            query,
            limit,
            embedding_provider=self.embedding_provider,
            filters=filters,
            connection=operation_snapshot,
            prepared_query=prepared_query,
        )
        return tuple(self.repository.search_hit_from_row(row) for row in rows)


@dataclass(frozen=True)
class TagRetrievalStrategy:
    repository: TagRepository

    def search(self, query: str) -> tuple[TagMatch, ...]:
        return self.repository.search(query)

    def _search_in_snapshot(
        self,
        query: str,
        operation_snapshot: sqlite3.Connection,
    ) -> tuple[TagMatch, ...]:
        return self.repository.search_in_snapshot(operation_snapshot, query)


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
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> tuple[MeetingSearchResult, ...]:
        prepared_query = self._prepare_query_for_snapshot(query)
        with self.repository.operation_snapshot() as operation_snapshot:
            return self._search_meetings_in_snapshot(
                query,
                limit,
                context,
                operation_snapshot=operation_snapshot,
                prepared_query=prepared_query,
                filters=filters,
            )

    def _prepare_query_for_snapshot(self, query: str) -> object | None:
        return _prepare_retrieval_query_for_builtin_snapshot(self.lexical, query)

    def _search_meetings_in_snapshot(  # noqa: PLR0913
        self,
        query: str,
        limit: int,
        context: int,
        *,
        operation_snapshot: sqlite3.Connection,
        prepared_query: object | None,
        filters: MeetingSearchFilters | None,
    ) -> tuple[MeetingSearchResult, ...]:
        fts_ranks, fts_evidence, fts_meetings = collect_hits_by_meeting(
            self.lexical,
            query,
            limit,
            candidate_multiplier=self.candidate_multiplier,
            operation_snapshot=operation_snapshot,
            prepared_query=prepared_query,
            filters=filters,
        )
        exact_tag_order, token_tag_order, matched_tags, tag_meetings = tag_candidates(
            self.repository,
            self.tags,
            query,
            operation_snapshot=operation_snapshot,
            filters=filters,
        )
        meetings = {**tag_meetings, **fts_meetings}
        ordered_keys = tuple(dict.fromkeys((*exact_tag_order, *fts_ranks, *token_tag_order)))
        selected_keys = tuple(key for key in ordered_keys if key in meetings)[:limit]
        evidence_by_key = expand_evidence_by_meeting(
            self.repository,
            selected_keys,
            fts_evidence,
            context,
            operation_snapshot=operation_snapshot,
        )
        results: list[MeetingSearchResult] = []
        for key in selected_keys:
            sources: list[RetrievalSource] = []
            if key in exact_tag_order:
                sources.append(RetrievalSource.TAG)
            if key in fts_ranks:
                sources.append(RetrievalSource.FTS)
            if key in token_tag_order and RetrievalSource.TAG not in sources:
                sources.append(RetrievalSource.TAG)
            results.append(
                MeetingSearchResult(
                    meeting_id=meetings[key].id,
                    meeting=meetings[key],
                    rank=len(results) + 1,
                    match_sources=tuple(sources),
                    evidence=evidence_by_key.get(key, ()),
                    matched_tags=matched_tags.get(key, ()),
                )
            )
        return tuple(results)


@dataclass(frozen=True)
class RetrievalCandidateTrace:
    meeting_id: int
    meeting_ref: MeetingRef
    fts_rank: int | None
    semantic_rank: int | None
    tag_rank: int | None
    fused_score: float

    @property
    def meeting_external_id(self) -> str:
        return self.meeting_ref.external_id


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
class PreparedHybridQuery:
    lexical: object | None
    semantic: object | None
    semantic_status: str


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
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> tuple[MeetingSearchResult, ...]:
        return self.search_with_trace(query, limit, context, filters=filters).results

    def search_with_trace(
        self,
        query: str,
        limit: int = 10,
        context: int = 0,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> RetrievalResult:
        prepared_query = self._prepare_query_for_snapshot(query)
        with self.repository.operation_snapshot() as operation_snapshot:
            return self._search_with_trace_in_snapshot(
                query,
                limit,
                context,
                operation_snapshot=operation_snapshot,
                prepared_query=prepared_query,
                filters=filters,
            )

    def _prepare_query_for_snapshot(self, query: str) -> PreparedHybridQuery:
        lexical = _prepare_retrieval_query_for_builtin_snapshot(self.lexical, query)
        semantic_status = "forced"
        if self.require_complete_semantic_index:
            from meetily_memory.semantic_search import semantic_index_coverage  # noqa: PLC0415

            try:
                coverage = semantic_index_coverage(
                    self.repository.index_path,
                    self.semantic_provider,
                )
            except (RuntimeError, sqlite3.Error):
                return PreparedHybridQuery(lexical, None, "unavailable")
            if not coverage.complete:
                return PreparedHybridQuery(lexical, None, "incomplete")
            semantic_status = "complete"
        try:
            semantic = _prepare_retrieval_query_for_builtin_snapshot(self.semantic, query)
        except (RuntimeError, sqlite3.Error, OSError, ValueError, TypeError):
            return PreparedHybridQuery(lexical, None, "error")
        return PreparedHybridQuery(lexical, semantic, semantic_status)

    def _search_meetings_in_snapshot(  # noqa: PLR0913
        self,
        query: str,
        limit: int,
        context: int,
        *,
        operation_snapshot: sqlite3.Connection,
        prepared_query: object | None,
        filters: MeetingSearchFilters | None,
    ) -> tuple[MeetingSearchResult, ...]:
        return self._search_with_trace_in_snapshot(
            query,
            limit,
            context,
            operation_snapshot=operation_snapshot,
            prepared_query=prepared_query,
            filters=filters,
        ).results

    def _search_with_trace_in_snapshot(  # noqa: PLR0913
        self,
        query: str,
        limit: int,
        context: int,
        *,
        operation_snapshot: sqlite3.Connection,
        prepared_query: object | None,
        filters: MeetingSearchFilters | None,
    ) -> RetrievalResult:
        if not isinstance(prepared_query, PreparedHybridQuery):
            message = "Hybrid query must be prepared before opening the operation snapshot."
            raise TypeError(message)
        fts_ranks, fts_evidence, fts_meetings = collect_hits_by_meeting(
            self.lexical,
            query,
            limit,
            candidate_multiplier=self.candidate_multiplier,
            operation_snapshot=operation_snapshot,
            prepared_query=prepared_query.lexical,
            filters=filters,
        )
        semantic_ranks, semantic_evidence, semantic_meetings, semantic_status = (
            self._semantic_candidates(
                query,
                limit,
                operation_snapshot=operation_snapshot,
                prepared_query=prepared_query.semantic,
                prepared_status=prepared_query.semantic_status,
                filters=filters,
            )
        )
        tag_ranks, matched_tags, tag_meetings = self._tag_candidates(
            query,
            operation_snapshot=operation_snapshot,
            filters=filters,
        )
        meetings = {**tag_meetings, **semantic_meetings, **fts_meetings}
        candidate_keys = tuple(dict.fromkeys((*fts_ranks, *semantic_ranks, *tag_ranks)))
        traces = tuple(
            sorted(
                (
                    RetrievalCandidateTrace(
                        meeting_id=meetings[key].id,
                        meeting_ref=key,
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
        selected = tuple(trace for trace in traces if trace.meeting_ref in meetings)[:limit]
        selected_keys = tuple(trace.meeting_ref for trace in selected)
        selected_evidence = {
            key: unique_hits((*fts_evidence.get(key, ()), *semantic_evidence.get(key, ())))[
                :MAX_EVIDENCE_PER_SOURCE
            ]
            for key in selected_keys
        }
        evidence_by_key = expand_evidence_by_meeting(
            self.repository,
            selected_keys,
            selected_evidence,
            context,
            operation_snapshot=operation_snapshot,
        )
        results: list[MeetingSearchResult] = []
        for trace in selected:
            key = trace.meeting_ref
            results.append(
                MeetingSearchResult(
                    meeting_id=trace.meeting_id,
                    meeting=meetings[key],
                    rank=len(results) + 1,
                    match_sources=trace_sources(trace),
                    evidence=evidence_by_key.get(key, ()),
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

    def _semantic_candidates(  # noqa: PLR0913
        self,
        query: str,
        limit: int,
        *,
        operation_snapshot: sqlite3.Connection,
        prepared_query: object | None,
        prepared_status: str,
        filters: MeetingSearchFilters | None = None,
    ) -> tuple[
        dict[MeetingKey, int],
        dict[MeetingKey, tuple[SearchHit, ...]],
        dict[MeetingKey, Meeting],
        str,
    ]:
        if self.require_complete_semantic_index:
            from meetily_memory.semantic_search import semantic_index_coverage  # noqa: PLC0415

            try:
                coverage = semantic_index_coverage(
                    self.repository.index_path,
                    self.semantic_provider,
                    connection=operation_snapshot,
                )
            except (RuntimeError, sqlite3.Error):
                return {}, {}, {}, "unavailable"
            if not coverage.complete:
                return {}, {}, {}, "incomplete"
        if prepared_status in {"unavailable", "incomplete", "error"}:
            return {}, {}, {}, prepared_status
        try:
            result = collect_hits_by_meeting(
                self.semantic,
                query,
                limit,
                candidate_multiplier=self.candidate_multiplier,
                operation_snapshot=operation_snapshot,
                prepared_query=prepared_query,
                filters=filters,
            )
        except (RuntimeError, sqlite3.Error, OSError, ValueError, TypeError):
            return {}, {}, {}, "error"
        else:
            ranks, evidence, meetings = result
            return ranks, evidence, meetings, prepared_status

    def _tag_candidates(
        self,
        query: str,
        *,
        operation_snapshot: sqlite3.Connection,
        filters: MeetingSearchFilters | None = None,
    ) -> tuple[
        dict[MeetingKey, int],
        dict[MeetingKey, tuple[str, ...]],
        dict[MeetingKey, Meeting],
    ]:
        exact_order, token_order, names, meetings = tag_candidates(
            self.repository,
            self.tags,
            query,
            operation_snapshot=operation_snapshot,
            filters=filters,
        )
        ordered_keys = [*exact_order, *(key for key in token_order if key not in exact_order)]
        return (
            {key: rank for rank, key in enumerate(ordered_keys, start=1)},
            names,
            meetings,
        )


def search_meetings_with_builtin_snapshot(  # noqa: PLR0913
    repository: IndexRepository,
    strategy: MeetingRetrievalStrategy,
    query: str,
    limit: int = 10,
    context: int = 0,
    *,
    filters: MeetingSearchFilters | None = None,
) -> tuple[MeetingSearchResult, ...]:
    """Call the public strategy API; built-in implementations own their operation snapshot."""
    del repository
    return strategy.search_meetings(query, limit, context, filters=filters)


def search_hits_with_builtin_snapshot(  # noqa: PLR0913
    repository: IndexRepository,
    strategy: RetrievalStrategy,
    query: str,
    limit: int = 10,
    context: int = 0,
    *,
    filters: MeetingSearchFilters | None = None,
) -> tuple[SearchHit, ...]:
    """Use one snapshot for built-in retrieval; call custom strategies through the public API."""
    if _uses_builtin_retrieval(strategy):
        prepared_query = strategy._prepare_query_for_snapshot(query)  # noqa: SLF001
        with repository.operation_snapshot() as operation_snapshot:
            hits = strategy._search_in_snapshot(  # noqa: SLF001
                query,
                limit,
                operation_snapshot=operation_snapshot,
                prepared_query=prepared_query,
                filters=filters,
            )
            if context:
                return repository.expand_search_hits(
                    hits,
                    context,
                    connection=operation_snapshot,
                )
            return hits
    hits = strategy.search(query, limit, filters=filters)
    return repository.expand_search_hits(hits, context) if context else hits


def _prepare_retrieval_query_for_builtin_snapshot(
    strategy: RetrievalStrategy,
    query: str,
) -> object | None:
    if _uses_builtin_retrieval(strategy):
        return strategy._prepare_query_for_snapshot(query)  # noqa: SLF001
    return None


def _search_retrieval_at_builtin_snapshot_boundary(  # noqa: PLR0913
    strategy: RetrievalStrategy,
    query: str,
    limit: int,
    *,
    operation_snapshot: sqlite3.Connection,
    prepared_query: object | None,
    filters: MeetingSearchFilters | None,
) -> tuple[SearchHit, ...]:
    if _uses_builtin_retrieval(strategy):
        return strategy._search_in_snapshot(  # noqa: SLF001
            query,
            limit,
            operation_snapshot=operation_snapshot,
            prepared_query=prepared_query,
            filters=filters,
        )
    # Never expose the raw SQLite connection through the public extension-point protocol.
    return strategy.search(query, limit, filters=filters)


def _uses_builtin_retrieval(
    strategy: RetrievalStrategy,
) -> TypeGuard[LexicalRetrievalStrategy | SemanticRetrievalStrategy]:
    strategy_type = type(strategy)
    return (
        isinstance(strategy, LexicalRetrievalStrategy)
        and strategy_type.search is LexicalRetrievalStrategy.search
    ) or (
        isinstance(strategy, SemanticRetrievalStrategy)
        and strategy_type.search is SemanticRetrievalStrategy.search
    )


def collect_hits_by_meeting(  # noqa: PLR0913
    strategy: RetrievalStrategy,
    query: str,
    meeting_limit: int,
    *,
    candidate_multiplier: int,
    operation_snapshot: sqlite3.Connection,
    prepared_query: object | None = None,
    filters: MeetingSearchFilters | None = None,
) -> tuple[
    dict[MeetingKey, int],
    dict[MeetingKey, tuple[SearchHit, ...]],
    dict[MeetingKey, Meeting],
]:
    candidate_limit = max(meeting_limit, meeting_limit * candidate_multiplier)
    while True:
        hits = _search_retrieval_at_builtin_snapshot_boundary(
            strategy,
            query,
            candidate_limit,
            operation_snapshot=operation_snapshot,
            prepared_query=prepared_query,
            filters=filters,
        )
        ranks, evidence, meetings = collapse_hits_by_meeting(hits)
        if len(ranks) >= meeting_limit or len(hits) < candidate_limit:
            return ranks, evidence, meetings
        candidate_limit *= 2


def tag_candidates(
    repository: IndexRepository,
    tags: TagRetrievalStrategy,
    query: str,
    *,
    operation_snapshot: sqlite3.Connection,
    filters: MeetingSearchFilters | None = None,
) -> tuple[
    list[MeetingKey],
    list[MeetingKey],
    dict[MeetingKey, tuple[str, ...]],
    dict[MeetingKey, Meeting],
]:
    matches = tags._search_in_snapshot(query, operation_snapshot)  # noqa: SLF001
    meeting_rows = repository.get_meetings_by_refs(
        tuple(dict.fromkeys(match.meeting_ref for match in matches)),
        filters=filters,
        connection=operation_snapshot,
    )
    exact_order: list[MeetingKey] = []
    token_order: list[MeetingKey] = []
    names: dict[MeetingKey, list[str]] = {}
    meetings: dict[MeetingKey, Meeting] = {}
    for match in matches:
        meeting_row = meeting_rows.get(match.meeting_ref)
        if meeting_row is None:
            continue
        key = match.meeting_ref
        if key not in meetings:
            meetings[key] = meeting_from_row(meeting_row)
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
        meetings,
    )


def collapse_hits_by_meeting(
    hits: tuple[SearchHit, ...],
) -> tuple[
    dict[MeetingKey, int],
    dict[MeetingKey, tuple[SearchHit, ...]],
    dict[MeetingKey, Meeting],
]:
    ranks: dict[MeetingKey, int] = {}
    evidence: dict[MeetingKey, list[SearchHit]] = {}
    meetings: dict[MeetingKey, Meeting] = {}
    for hit in hits:
        key = hit.meeting.ref
        if key not in ranks:
            ranks[key] = len(ranks) + 1
            meetings[key] = hit.meeting
        values = evidence.setdefault(key, [])
        if len(values) < MAX_EVIDENCE_PER_SOURCE:
            values.append(hit)
    return ranks, {key: tuple(values) for key, values in evidence.items()}, meetings


def unique_hits(hits: tuple[SearchHit, ...]) -> tuple[SearchHit, ...]:
    by_id: dict[str, SearchHit] = {}
    for hit in hits:
        if hit.id not in by_id:
            by_id[hit.id] = hit
    return tuple(by_id.values())


def expand_evidence_by_meeting(
    repository: IndexRepository,
    selected_keys: tuple[MeetingKey, ...],
    evidence_by_key: dict[MeetingKey, tuple[SearchHit, ...]],
    context: int,
    *,
    operation_snapshot: sqlite3.Connection,
) -> dict[MeetingKey, tuple[SearchHit, ...]]:
    selected_evidence = unique_hits(
        tuple(hit for key in selected_keys for hit in evidence_by_key.get(key, ()))
    )
    if context <= 0 or not selected_evidence:
        return {key: evidence_by_key.get(key, ()) for key in selected_keys}
    expanded = repository.expand_search_hits(
        selected_evidence,
        context,
        connection=operation_snapshot,
    )
    keys_by_ref = {key: key for key in selected_keys}
    grouped: dict[MeetingKey, list[SearchHit]] = {key: [] for key in selected_keys}
    for hit in expanded:
        key = keys_by_ref.get(hit.meeting.ref)
        if key is not None:
            grouped[key].append(hit)
    return {key: tuple(hits) for key, hits in grouped.items()}


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
