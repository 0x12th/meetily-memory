from dataclasses import dataclass

from meetily_memory.db.repository import IndexRepository

Row = dict[str, object]


@dataclass(frozen=True)
class SummaryMemory:
    stats: dict[str, int]
    latest_meeting: Row | None

    def as_payload(self) -> dict[str, object]:
        return {
            "stats": self.stats,
            "latest_meeting": self.latest_meeting,
        }


@dataclass(frozen=True)
class ProjectMemory:
    query: str
    meetings: list[Row]
    structured_signals: list[Row]

    def as_payload(self) -> dict[str, object]:
        return {
            "query": self.query,
            "meetings": self.meetings,
            "structured_signals": self.structured_signals,
        }


@dataclass(frozen=True)
class PersonMemory:
    name: str
    meetings: list[Row]
    structured_signals: list[Row]

    def as_payload(self) -> dict[str, object]:
        return {
            "person": self.name,
            "meetings": self.meetings,
            "structured_signals": self.structured_signals,
        }


def summary_memory(repo: IndexRepository) -> SummaryMemory:
    latest = repo.list_meetings(limit=1)
    return SummaryMemory(stats=repo.stats(), latest_meeting=latest[0] if latest else None)


def timeline_signals(
    repo: IndexRepository,
    query: str | None,
    limit: int,
) -> list[Row]:
    rows = matching_entities(repo.list_all_structured_entity_details(limit * 4), query)
    return rows[:limit]


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
    return ProjectMemory(query=query, meetings=search_results, structured_signals=entity_rows)


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
    return PersonMemory(name=name, meetings=meetings, structured_signals=entity_rows)


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
