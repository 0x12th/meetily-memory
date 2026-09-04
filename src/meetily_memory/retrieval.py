from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

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
    import sqlite3

    from meetily_memory.tagging import TagRepository

RETRIEVAL_CANDIDATE_MULTIPLIER = 4
MAX_EVIDENCE_PER_SOURCE = 2
MeetingKey = MeetingRef


@dataclass(frozen=True)
class MeetingSearchService:
    repository: IndexRepository
    tags: TagRepository
    candidate_multiplier: int = RETRIEVAL_CANDIDATE_MULTIPLIER

    def search(
        self,
        query: str,
        limit: int = 10,
        context: int = 0,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> tuple[MeetingSearchResult, ...]:
        with self.repository.operation_snapshot() as operation_snapshot:
            return self._search_in_snapshot(
                query,
                limit,
                context,
                operation_snapshot=operation_snapshot,
                filters=filters,
            )

    def _search_in_snapshot(
        self,
        query: str,
        limit: int,
        context: int,
        *,
        operation_snapshot: sqlite3.Connection,
        filters: MeetingSearchFilters | None,
    ) -> tuple[MeetingSearchResult, ...]:
        fts_ranks, fts_evidence, fts_meetings = collect_hits_by_meeting(
            self.repository,
            query,
            limit,
            candidate_multiplier=self.candidate_multiplier,
            operation_snapshot=operation_snapshot,
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


def collect_hits_by_meeting(  # noqa: PLR0913
    repository: IndexRepository,
    query: str,
    meeting_limit: int,
    *,
    candidate_multiplier: int,
    operation_snapshot: sqlite3.Connection,
    filters: MeetingSearchFilters | None = None,
) -> tuple[
    dict[MeetingKey, int],
    dict[MeetingKey, tuple[SearchHit, ...]],
    dict[MeetingKey, Meeting],
]:
    candidate_limit = max(meeting_limit, meeting_limit * candidate_multiplier)
    while True:
        hits = repository.search_hits(
            query,
            candidate_limit,
            filters=filters,
            connection=operation_snapshot,
        )
        ranks, evidence, meetings = collapse_hits_by_meeting(hits)
        if len(ranks) >= meeting_limit or len(hits) < candidate_limit:
            return ranks, evidence, meetings
        candidate_limit *= 2


def tag_candidates(
    repository: IndexRepository,
    tags: TagRepository,
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
    matches = tags.search_in_snapshot(operation_snapshot, query)
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
