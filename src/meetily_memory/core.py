from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meetily_memory.context_builder import (
    DEFAULT_CONTEXT_NEIGHBORS,
    MAX_CONTEXT_EVIDENCE,
    ContextBundleBuilder,
)
from meetily_memory.domain import (
    ContextBundle,
    GraphEdge,
    GraphNode,
    Meeting,
    MeetingChunk,
    MeetingRef,
    MeetingSearchFilters,
    Person,
    PersonMemory,
    ProjectMemory,
    SearchHit,
    SearchResults,
    StructuredEntities,
    SummaryMemory,
    TaskStatusResult,
    TimelineMemory,
    Topic,
    TopicAliasResult,
    TopicGraph,
    TopicMemory,
)
from meetily_memory.local_memory import (
    person_memory,
    project_memory,
    ranked_excerpt_from_row,
    structured_entity_kind,
    structured_signal_from_row,
    summary_memory,
    timeline_signals,
)
from meetily_memory.repositories.index import IndexRepository, meeting_from_row, optional_str
from meetily_memory.retrieval import (
    LexicalRetrievalStrategy,
    LexicalTagMeetingRetrievalStrategy,
    MeetingRetrievalStrategy,
    RetrievalStrategy,
    TagRetrievalStrategy,
)
from meetily_memory.tagging import TagRepository


class MeetingNotFoundError(LookupError):
    pass


class EvidenceNotFoundError(LookupError):
    pass


class TaskNotFoundError(LookupError):
    pass


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
        meeting_retrieval_strategy: MeetingRetrievalStrategy | None = None,
    ) -> None:
        repository = IndexRepository(Path(index_path), state_path=state_path)
        self._repository = repository
        lexical = retrieval_strategy or LexicalRetrievalStrategy(repository)
        tag_repository = TagRepository(repository.state_path)
        tag_retrieval = TagRetrievalStrategy(tag_repository)
        self._meeting_retrieval = meeting_retrieval_strategy or LexicalTagMeetingRetrievalStrategy(
            repository=repository,
            lexical=lexical,
            tags=tag_retrieval,
        )
        self._context_builder = ContextBundleBuilder(repository)

    def search(
        self,
        query: str,
        limit: int = 10,
        context: int = 0,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> SearchResults:
        return SearchResults(
            query=query,
            context=context,
            results=self._meeting_retrieval.search_meetings(query, limit, context, filters=filters),
        )

    def resolve_search_hit(self, evidence_id: str) -> SearchHit:
        hit = self._repository.get_search_hit(evidence_id)
        if hit is None:
            message = f"Evidence not found: {evidence_id}"
            raise EvidenceNotFoundError(message)
        return hit

    def build_context(
        self,
        question: str,
        limit: int = 8,
        *,
        context: int = DEFAULT_CONTEXT_NEIGHBORS,
    ) -> ContextBundle:
        return self._build_context_bundle(
            question,
            limit,
            ContextRetrievalOptions(neighbor_count=context),
        )

    def build_meeting_context(
        self,
        question: str,
        meeting_ref: MeetingRef,
        limit: int = 8,
        *,
        context: int = DEFAULT_CONTEXT_NEIGHBORS,
    ) -> ContextBundle:
        meeting = self._repository.get_meeting_by_ref(meeting_ref)
        if meeting is None:
            message = f"Meeting not found: {meeting_ref.source_uuid}/{meeting_ref.external_id}"
            raise MeetingNotFoundError(message)
        return self._build_context_bundle(
            question,
            limit,
            ContextRetrievalOptions(
                meeting_id=int(meeting["id"]),
                neighbor_count=context,
            ),
        )

    def meetings(self, limit: int = 20, person: str | None = None) -> tuple[Meeting, ...]:
        return tuple(
            meeting_from_row(row)
            for row in self._repository.list_meetings(limit=limit, person=person)
        )

    def latest_meeting(self, person: str | None = None) -> Meeting | None:
        meetings = self.meetings(limit=1, person=person)
        return meetings[0] if meetings else None

    def get_meeting(self, external_id: str) -> Meeting | None:
        row = self._repository.get_meeting(external_id)
        return meeting_from_row(row) if row is not None else None

    def get_meeting_by_local_id(self, meeting_id: int) -> Meeting | None:
        row = self._repository.get_meeting_by_local_id(meeting_id)
        return meeting_from_row(row) if row is not None else None

    def get_meeting_by_ref(self, meeting_ref: MeetingRef) -> Meeting | None:
        row = self._repository.get_meeting_by_ref(meeting_ref)
        return meeting_from_row(row) if row is not None else None

    def meeting_chunks(self, meeting_id: int) -> tuple[MeetingChunk, ...]:
        return tuple(
            meeting_chunk_from_row(row)
            for row in self._repository.get_chunks_for_meeting(meeting_id)
        )

    def summary(self) -> SummaryMemory:
        return summary_memory(self._repository)

    def timeline(self, query: str | None = None, limit: int = 20) -> TimelineMemory:
        return timeline_signals(self._repository, query, limit)

    def project(self, query: str, limit: int = 10) -> ProjectMemory:
        return project_memory(self._repository, query, limit)

    def person(self, name: str, limit: int = 10) -> PersonMemory:
        return person_memory(self._repository, name, limit)

    def topic(self, query: str, limit: int = 10) -> TopicMemory:
        payload = self._repository.topic_memory(query, limit)
        return TopicMemory(
            topic=topic_from_row(payload["topic"]),
            language=optional_str(payload.get("language")),
            query_terms=tuple(str(term) for term in payload["query_terms"]),
            meetings=tuple(ranked_excerpt_from_row(row) for row in payload["meetings"]),
            evidence=tuple(ranked_excerpt_from_row(row) for row in payload["evidence"]),
            structured_signals=tuple(
                structured_signal_from_row(row) for row in payload["structured_signals"]
            ),
            related_people=tuple(person_from_row(row) for row in payload["related_people"]),
        )

    def topics(self, limit: int = 100) -> tuple[Topic, ...]:
        return tuple(topic_from_row(row) for row in self._repository.list_topics(limit))

    def add_topic_alias(self, query: str, aliases: list[str]) -> TopicAliasResult:
        payload = self._repository.ensure_topic(query, aliases=aliases)
        return TopicAliasResult(
            topic=topic_from_row(payload),
            added_aliases=tuple(str(alias) for alias in payload["added_aliases"]),
        )

    def graph(self, query: str, limit: int = 50) -> TopicGraph:
        payload = self._repository.graph_for_topic(query, limit)
        return TopicGraph(
            topic=topic_from_row(payload["topic"]),
            nodes=tuple(graph_node_from_row(row) for row in payload["nodes"]),
            edges=tuple(graph_edge_from_row(row) for row in payload["edges"]),
        )

    def structured_entities(
        self,
        kind: str,
        limit: int = 20,
        *,
        status: str = "all",
    ) -> StructuredEntities:
        entity_kind = structured_entity_kind(kind)
        rows = self._repository.list_structured_entity_details(kind, limit, status=status)
        return StructuredEntities(
            entity_kind=entity_kind,
            status=status,
            entities=tuple(structured_signal_from_row(row) for row in rows),
        )

    def set_task_status(
        self,
        task_id: int,
        status: str,
        *,
        note: str | None = None,
    ) -> TaskStatusResult:
        try:
            row = self._repository.set_task_status(task_id, status, note=note)
        except ValueError as exc:
            if str(exc).startswith("Task not found:"):
                raise TaskNotFoundError(str(exc)) from exc
            raise
        return TaskStatusResult(
            id=int(row["id"]),
            text=str(row["text"]),
            status=str(row["status"]),
            status_note=optional_str(row.get("status_note")),
            status_source=str(row["status_source"]),
            status_updated_at=str(row["status_updated_at"]),
        )

    def _build_context_bundle(
        self,
        question: str,
        limit: int,
        options: ContextRetrievalOptions,
    ) -> ContextBundle:
        return self._context_builder.build(
            question,
            limit,
            meeting_id=options.meeting_id,
            neighbor_count=options.neighbor_count,
            max_evidence=options.max_evidence,
        )


def meeting_chunk_from_row(row: dict[str, Any]) -> MeetingChunk:
    return MeetingChunk(
        id=int(row["id"]),
        external_id=optional_str(row.get("external_id")),
        kind=str(row["kind"]),
        ordinal=int(row["ordinal"]),
        text=str(row["text"]),
        speaker=optional_str(row.get("speaker")),
        starts_at_seconds=optional_float(row.get("starts_at_seconds")),
        ends_at_seconds=optional_float(row.get("ends_at_seconds")),
        timestamp_label=optional_str(row.get("timestamp_label")),
    )


def topic_from_row(row: dict[str, Any]) -> Topic:
    return Topic(
        id=int(row["id"]),
        title=str(row["title"]),
        aliases=tuple(str(alias) for alias in row["aliases"]),
    )


def person_from_row(row: dict[str, Any]) -> Person:
    return Person(id=int(row["id"]), display_name=str(row["display_name"]))


def graph_node_from_row(row: dict[str, Any]) -> GraphNode:
    return GraphNode(id=int(row["id"]), type=str(row["type"]), title=str(row["title"]))


def graph_edge_from_row(row: dict[str, Any]) -> GraphEdge:
    return GraphEdge(
        id=int(row["id"]),
        from_node_id=int(row["from_node_id"]),
        relation=str(row["relation"]),
        to_node_id=int(row["to_node_id"]),
        confidence=float(row["confidence"]),
        source_meeting_id=optional_int(row.get("source_meeting_id")),
        source_chunk_id=optional_int(row.get("source_chunk_id")),
        extraction_method=str(row["extraction_method"]),
        created_at=optional_str(row.get("created_at")),
    )


def optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None
