from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meetily_memory.context_builder import (
    DEFAULT_CONTEXT_NEIGHBORS,
    MAX_CONTEXT_EVIDENCE,
    ContextRenderer,
)
from meetily_memory.db.repository import IndexRepository
from meetily_memory.domain import CompactSearchHit, ContextBundle, SearchHit
from meetily_memory.local_memory import (
    person_memory,
    project_memory,
    summary_memory,
    timeline_signals,
)
from meetily_memory.retrieval import LexicalRetrievalStrategy, RetrievalStrategy

CORE_V1_VERSION = "meetily-memory.core.v1"
CORE_V2_VERSION = "meetily-memory.core.v2"
CONTRACT_VERSION = CORE_V1_VERSION


@dataclass(frozen=True)
class CoreResponse:
    kind: str
    data: dict[str, Any]
    contract_version: str = CORE_V1_VERSION

    def as_payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "kind": self.kind,
            "data": self.data,
        }


@dataclass(frozen=True)
class ContextRetrievalOptions:
    meeting_id: int | None = None
    neighbor_count: int = DEFAULT_CONTEXT_NEIGHBORS
    max_evidence: int = MAX_CONTEXT_EVIDENCE

    def __post_init__(self) -> None:
        if self.neighbor_count < 0:
            message = "neighbor_count must not be negative"
            raise ValueError(message)
        if self.max_evidence < 1:
            message = "max_evidence must be positive"
            raise ValueError(message)


class MeetilyMemoryCore:
    def __init__(
        self,
        index_path: Path,
        *,
        state_path: Path | None = None,
        retrieval_strategy: RetrievalStrategy | None = None,
    ) -> None:
        self.repo = IndexRepository(Path(index_path), state_path=state_path)
        self.retrieval_strategy = retrieval_strategy or LexicalRetrievalStrategy(self.repo)
        self.context_renderer = ContextRenderer()

    def search(
        self,
        query: str,
        limit: int = 10,
        context: int = 0,
        *,
        contract_version: str = CORE_V1_VERSION,
    ) -> CoreResponse:
        validate_contract_version(contract_version)
        if contract_version == CORE_V2_VERSION:
            rows: list[dict[str, Any]] = []
            hits = self.search_hits(query, limit, context)
        else:
            rows = self.repo.search(query, limit, context=context)
            hits = tuple(self.repo.search_hit_from_row(row) for row in rows)
        return CoreResponse(
            "search",
            {
                "query": query,
                "context": context,
                "results": serialize_hits(hits, rows, contract_version),
            },
            contract_version,
        )

    def search_hits(self, query: str, limit: int = 10, context: int = 0) -> tuple[SearchHit, ...]:
        return self.retrieval_strategy.search(query, limit, context=context)

    def compact_search_hits(
        self,
        query: str,
        limit: int = 10,
        *,
        preview_length: int = 240,
    ) -> tuple[CompactSearchHit, ...]:
        return tuple(hit.compact(preview_length) for hit in self.search_hits(query, limit))

    def get_search_hit(self, evidence_id: str) -> SearchHit | None:
        return self.repo.get_search_hit(evidence_id)

    def resolve_search_hit(self, evidence_id: str) -> SearchHit:
        hit = self.get_search_hit(evidence_id)
        if hit is None:
            message = f"Evidence not found: {evidence_id}"
            raise LookupError(message)
        return hit

    def context_bundle(
        self,
        question: str,
        limit: int = 8,
        *,
        options: ContextRetrievalOptions | None = None,
    ) -> ContextBundle:
        retrieval_options = options or ContextRetrievalOptions()
        evidence = self.retrieval_strategy.search(
            question,
            limit,
            meeting_id=retrieval_options.meeting_id,
            context=retrieval_options.neighbor_count,
        )[: retrieval_options.max_evidence]
        return ContextBundle(
            question=question,
            evidence=evidence,
            entities=self.repo.memory_entities_for_hits(evidence),
        )

    def build_context(
        self,
        question: str,
        limit: int = 8,
        *,
        contract_version: str = CORE_V1_VERSION,
        context: int = DEFAULT_CONTEXT_NEIGHBORS,
    ) -> CoreResponse:
        validate_contract_version(contract_version)
        if contract_version == CORE_V2_VERSION:
            bundle = self.context_bundle(
                question,
                limit,
                options=ContextRetrievalOptions(neighbor_count=context),
            )
            return CoreResponse("context", bundle.as_payload(), contract_version)
        rows = self.repo.search(question, limit, context=context)[:MAX_CONTEXT_EVIDENCE]
        evidence = tuple(self.repo.search_hit_from_row(row) for row in rows)
        bundle = ContextBundle(question=question, evidence=evidence, entities=())
        return CoreResponse(
            "context",
            {
                "question": question,
                "markdown": self.context_renderer.render(bundle),
                "evidence": serialize_hits(evidence, rows, CORE_V1_VERSION),
            },
            contract_version,
        )

    def build_meeting_context(
        self,
        question: str,
        meeting_id: str,
        limit: int = 8,
        *,
        contract_version: str = CORE_V1_VERSION,
        context: int = DEFAULT_CONTEXT_NEIGHBORS,
    ) -> CoreResponse:
        validate_contract_version(contract_version)
        meeting = self.repo.get_meeting(meeting_id)
        if meeting is None:
            message = f"Meeting not found: {meeting_id}"
            raise ValueError(message)
        if contract_version == CORE_V2_VERSION:
            bundle = self.context_bundle(
                question,
                limit,
                options=ContextRetrievalOptions(
                    meeting_id=int(meeting["id"]),
                    neighbor_count=context,
                ),
            )
            return CoreResponse("meeting_context", bundle.as_payload(), contract_version)
        rows = self.repo.search(
            question,
            limit,
            meeting_id=int(meeting["id"]),
            context=context,
        )[:MAX_CONTEXT_EVIDENCE]
        evidence = tuple(self.repo.search_hit_from_row(row) for row in rows)
        bundle = ContextBundle(question=question, evidence=evidence, entities=())
        return CoreResponse(
            "meeting_context",
            {
                "question": question,
                "meeting": meeting,
                "markdown": self.context_renderer.render(bundle),
                "evidence": serialize_hits(evidence, rows, CORE_V1_VERSION),
            },
            contract_version,
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


V1_SEARCH_FIELDS = (
    "meeting_id",
    "meeting_external_id",
    "title",
    "created_at",
    "updated_at",
    "folder_path",
    "language",
    "chunk_id",
    "chunk_external_id",
    "kind",
    "ordinal",
    "text",
    "speaker",
    "starts_at_seconds",
    "ends_at_seconds",
    "timestamp_label",
    "rank",
)


def serialize_hits(
    hits: tuple[SearchHit, ...],
    rows: list[dict[str, Any]],
    contract_version: str,
) -> list[dict[str, object]]:
    if contract_version == CORE_V2_VERSION:
        return [hit.as_payload() for hit in hits]
    return [serialize_v1_search_row(row) for row in rows]


def serialize_v1_search_row(row: dict[str, Any]) -> dict[str, object]:
    payload = {field: row.get(field) for field in V1_SEARCH_FIELDS}
    for field in ("matched_chunk_id", "is_context"):
        if field in row:
            payload[field] = row[field]
    return payload


def validate_contract_version(contract_version: str) -> None:
    if contract_version not in {CORE_V1_VERSION, CORE_V2_VERSION}:
        message = f"Unsupported core contract version: {contract_version}"
        raise ValueError(message)
