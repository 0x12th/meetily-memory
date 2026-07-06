from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meetily_memory.context_builder import build_context_markdown
from meetily_memory.db.repository import IndexRepository
from meetily_memory.local_memory import (
    person_memory,
    project_memory,
    summary_memory,
    timeline_signals,
)

CONTRACT_VERSION = "meetily-memory.core.v1"


@dataclass(frozen=True)
class CoreResponse:
    kind: str
    data: dict[str, Any]
    contract_version: str = CONTRACT_VERSION

    def as_payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "kind": self.kind,
            "data": self.data,
        }


class MeetilyMemoryCore:
    def __init__(self, index_path: Path) -> None:
        self.repo = IndexRepository(Path(index_path))

    def search(self, query: str, limit: int = 10, context: int = 0) -> CoreResponse:
        return CoreResponse(
            "search",
            {
                "query": query,
                "context": context,
                "results": self.repo.search(query, limit, context=context),
            },
        )

    def build_context(self, question: str, limit: int = 8) -> CoreResponse:
        evidence = self.repo.search(question, limit)
        return CoreResponse(
            "context",
            {
                "question": question,
                "markdown": build_context_markdown(question, evidence),
                "evidence": evidence,
            },
        )

    def build_meeting_context(self, question: str, meeting_id: str, limit: int = 8) -> CoreResponse:
        meeting = self.repo.get_meeting(meeting_id)
        if meeting is None:
            message = f"Meeting not found: {meeting_id}"
            raise ValueError(message)
        evidence = self.repo.search(question, limit, meeting_id=int(meeting["id"]))
        return CoreResponse(
            "meeting_context",
            {
                "question": question,
                "meeting": meeting,
                "markdown": build_context_markdown(question, evidence),
                "evidence": evidence,
            },
        )

    def meetings(self, limit: int = 20, person: str | None = None) -> CoreResponse:
        return CoreResponse(
            "meetings",
            {
                "person": person,
                "meetings": self.repo.list_meetings(limit=limit, person=person),
            },
        )

    def latest_meeting(self, person: str | None = None) -> CoreResponse:
        rows = self.repo.list_meetings(limit=1, person=person)
        return CoreResponse(
            "latest_meeting",
            {
                "person": person,
                "meeting": rows[0] if rows else None,
            },
        )

    def get_meeting(self, meeting_id: str) -> CoreResponse:
        return CoreResponse(
            "meeting",
            {
                "meeting": self.repo.get_meeting(meeting_id),
            },
        )

    def meeting_chunks(self, meeting_id: int) -> CoreResponse:
        return CoreResponse(
            "meeting_chunks",
            {
                "meeting_id": meeting_id,
                "chunks": self.repo.get_chunks_for_meeting(meeting_id),
            },
        )

    def summary(self) -> CoreResponse:
        return CoreResponse("summary", summary_memory(self.repo).as_payload())

    def timeline(self, query: str | None = None, limit: int = 20) -> CoreResponse:
        return CoreResponse(
            "timeline",
            {
                "query": query,
                "signals": timeline_signals(self.repo, query, limit),
            },
        )

    def project(self, query: str, limit: int = 10) -> CoreResponse:
        return CoreResponse("project", project_memory(self.repo, query, limit).as_payload())

    def person(self, name: str, limit: int = 10) -> CoreResponse:
        return CoreResponse("person", person_memory(self.repo, name, limit).as_payload())

    def topic(self, query: str, limit: int = 10) -> CoreResponse:
        return CoreResponse("topic", self.repo.topic_memory(query, limit))

    def add_topic_alias(self, query: str, aliases: list[str]) -> CoreResponse:
        return CoreResponse("topic_alias", self.repo.ensure_topic(query, aliases=aliases))

    def graph(self, query: str, limit: int = 50) -> CoreResponse:
        return CoreResponse("graph", self.repo.graph_for_topic(query, limit))

    def structured_entities(
        self,
        kind: str,
        limit: int = 20,
        *,
        status: str = "all",
    ) -> CoreResponse:
        return CoreResponse(
            "structured_entities",
            {
                "entity_kind": kind,
                "status": status,
                "entities": self.repo.list_structured_entity_details(kind, limit, status=status),
            },
        )

    def set_task_status(
        self,
        task_id: int,
        status: str,
        *,
        note: str | None = None,
    ) -> CoreResponse:
        return CoreResponse(
            "task_status",
            self.repo.set_task_status(task_id, status, note=note),
        )
