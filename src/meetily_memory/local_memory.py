from typing import Any, cast

from meetily_memory.domain import (
    MeetingRef,
    MemoryStats,
    PersonMemory,
    ProjectMemory,
    RankedExcerpt,
    StructuredEntityKind,
    StructuredSignal,
    SummaryMemory,
    TimelineMemory,
)
from meetily_memory.repositories.index import (
    IndexRepository,
    meeting_from_row,
    optional_str,
    source_excerpt_from_search_row,
)

Row = dict[str, Any]


def summary_memory(repo: IndexRepository) -> SummaryMemory:
    latest = repo.list_meetings(limit=1)
    stats = repo.stats()
    return SummaryMemory(
        stats=MemoryStats(
            meetings=stats["meetings"],
            chunks=stats["chunks"],
            sources=stats["sources"],
            decisions=stats["decisions"],
            action_items=stats["action_items"],
            risks=stats["risks"],
            open_questions=stats["open_questions"],
            knowledge_nodes=stats["knowledge_nodes"],
            knowledge_edges=stats["knowledge_edges"],
        ),
        latest_meeting=meeting_from_row(latest[0]) if latest else None,
    )


def timeline_signals(
    repo: IndexRepository,
    query: str | None,
    limit: int,
) -> TimelineMemory:
    rows = matching_entities(repo.list_all_structured_entity_details(limit * 4), query)
    return TimelineMemory(
        query=query,
        signals=tuple(structured_signal_from_row(row) for row in rows[:limit]),
    )


def project_memory(
    repo: IndexRepository,
    query: str,
    limit: int,
) -> ProjectMemory:
    search_results = repo.search(query, limit)
    meeting_ids = {int(row["meeting_id"]) for row in search_results}
    entity_rows = [
        row
        for row in repo.list_all_structured_entity_details(limit * 4)
        if int(row["meeting_id"]) in meeting_ids or row_matches_query(row, query)
    ][:limit]
    return ProjectMemory(
        query=query,
        meetings=tuple(ranked_excerpt_from_row(row) for row in search_results),
        structured_signals=tuple(structured_signal_from_row(row) for row in entity_rows),
    )


def person_memory(
    repo: IndexRepository,
    name: str,
    limit: int,
) -> PersonMemory:
    meetings = repo.list_meetings(limit=limit, person=name)
    meeting_ids = {int(row["id"]) for row in meetings}
    entity_rows = [
        row
        for row in repo.list_all_structured_entity_details(limit * 4)
        if int(row["meeting_id"]) in meeting_ids or row_matches_query(row, name)
    ][:limit]
    return PersonMemory(
        name=name,
        meetings=tuple(meeting_from_row(row) for row in meetings),
        structured_signals=tuple(structured_signal_from_row(row) for row in entity_rows),
    )


def ranked_excerpt_from_row(row: Row) -> RankedExcerpt:
    return RankedExcerpt(
        meeting_id=int(row["meeting_id"]),
        meeting=meeting_from_row(row),
        excerpt=source_excerpt_from_search_row(row),
        rank=float(row["rank"]),
    )


def structured_signal_from_row(row: Row) -> StructuredSignal:
    return StructuredSignal(
        kind=structured_entity_kind(str(row["kind"])),
        id=int(row["id"]),
        ordinal=int(row["ordinal"]),
        text=str(row["text"]),
        extraction_method=str(row["source"]),
        created_at=optional_str(row.get("created_at")),
        updated_at=optional_str(row.get("updated_at")),
        status=optional_str(row.get("status")),
        status_note=optional_str(row.get("status_note")),
        status_source=optional_str(row.get("status_source")),
        status_updated_at=optional_str(row.get("status_updated_at")),
        meeting_id=int(row["meeting_id"]),
        meeting_ref=MeetingRef(
            source_uuid=str(row["source_uuid"]),
            external_id=str(row["meeting_external_id"]),
        ),
        meeting_title=str(row["meeting_title"]),
        meeting_language=optional_str(row.get("meeting_language")),
        meeting_date=optional_str(row.get("meeting_date")),
        chunk_external_id=optional_str(row.get("chunk_external_id")),
        chunk_evidence_id=str(row["chunk_evidence_id"]),
        chunk_kind=str(row["chunk_kind"]),
        chunk_speaker=optional_str(row.get("chunk_speaker")),
        chunk_timestamp_label=optional_str(row.get("chunk_timestamp_label")),
    )


def structured_entity_kind(value: str) -> StructuredEntityKind:
    if value not in {"decisions", "action_items", "risks", "open_questions"}:
        message = f"Unknown structured entity kind: {value}"
        raise ValueError(message)
    return cast("StructuredEntityKind", value)


def matching_entities(rows: list[Row], query: str | None) -> list[Row]:
    if not query:
        return rows
    return [row for row in rows if row_matches_query(row, query)]


def row_matches_query(row: Row, query: str) -> bool:
    terms = [term.casefold() for term in query.split() if term.strip()]
    if not terms:
        return True
    haystack = " ".join(
        str(row.get(key) or "")
        for key in (
            "text",
            "meeting_title",
            "meeting_external_id",
            "chunk_external_id",
            "chunk_speaker",
        )
    ).casefold()
    return all(term in haystack for term in terms)
