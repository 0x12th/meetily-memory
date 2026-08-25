import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from meetily_memory.json_codec import dumps_json_bytes

MemoryEntityKind = Literal["decision", "task", "risk", "question"]
StructuredEntityKind = Literal["decisions", "action_items", "risks", "open_questions"]
ENTITY_KIND_MAP: dict[str, MemoryEntityKind] = {
    "decisions": "decision",
    "action_items": "task",
    "risks": "risk",
    "open_questions": "question",
}


@dataclass(frozen=True)
class Meeting:
    id: int
    external_id: str
    title: str
    started_at: str | None
    ended_at: str | None
    created_at: str | None
    updated_at: str | None
    language: str | None
    summary_text: str | None = None
    chunk_count: int | None = None


@dataclass(frozen=True)
class SourceExcerpt:
    meeting_external_id: str
    chunk_external_id: str | None
    kind: str
    ordinal: int
    text: str
    speaker: str | None
    starts_at_seconds: float | None
    ends_at_seconds: float | None
    timestamp_label: str | None


@dataclass(frozen=True)
class SearchHit:
    id: str
    meeting: Meeting
    excerpt: SourceExcerpt
    is_context: bool = False


class RetrievalSource(StrEnum):
    FTS = "fts"
    SEMANTIC = "semantic"
    TAG = "tag"


@dataclass(frozen=True)
class MeetingSearchResult:
    meeting_id: int
    meeting: Meeting
    rank: int
    match_sources: tuple[RetrievalSource, ...]
    evidence: tuple[SearchHit, ...]
    matched_tags: tuple[str, ...]


@dataclass(frozen=True)
class SearchResults:
    query: str
    context: int
    results: tuple[MeetingSearchResult, ...]


@dataclass(frozen=True)
class MeetingSearchFilters:
    from_utc: datetime | None = None
    to_utc: datetime | None = None


@dataclass(frozen=True)
class MemoryEntity:
    kind: MemoryEntityKind
    content: str
    source: SourceExcerpt
    evidence_id: str
    extraction_method: str
    authoritative: bool = False


@dataclass(frozen=True)
class ContextBundle:
    question: str
    evidence: tuple[SearchHit, ...]
    entities: tuple[MemoryEntity, ...]


@dataclass(frozen=True)
class MeetingChunk:
    id: int
    external_id: str | None
    kind: str
    ordinal: int
    text: str
    speaker: str | None
    starts_at_seconds: float | None
    ends_at_seconds: float | None
    timestamp_label: str | None


@dataclass(frozen=True)
class MemoryStats:
    meetings: int
    chunks: int
    sources: int
    decisions: int
    action_items: int
    risks: int
    open_questions: int
    knowledge_nodes: int
    knowledge_edges: int


@dataclass(frozen=True)
class SummaryMemory:
    stats: MemoryStats
    latest_meeting: Meeting | None


@dataclass(frozen=True)
class RankedExcerpt:
    meeting_id: int
    meeting: Meeting
    excerpt: SourceExcerpt
    rank: float


@dataclass(frozen=True)
class StructuredSignal:
    kind: StructuredEntityKind
    id: int
    ordinal: int
    text: str
    extraction_method: str
    created_at: str | None
    updated_at: str | None
    status: str | None
    status_note: str | None
    status_source: str | None
    status_updated_at: str | None
    meeting_id: int
    meeting_external_id: str
    meeting_title: str
    meeting_language: str | None
    meeting_date: str | None
    chunk_external_id: str | None
    chunk_kind: str
    chunk_speaker: str | None
    chunk_timestamp_label: str | None


@dataclass(frozen=True)
class ProjectMemory:
    query: str
    meetings: tuple[RankedExcerpt, ...]
    structured_signals: tuple[StructuredSignal, ...]


@dataclass(frozen=True)
class PersonMemory:
    name: str
    meetings: tuple[Meeting, ...]
    structured_signals: tuple[StructuredSignal, ...]


@dataclass(frozen=True)
class TimelineMemory:
    query: str | None
    signals: tuple[StructuredSignal, ...]


@dataclass(frozen=True)
class Topic:
    id: int
    title: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Person:
    id: int
    display_name: str


@dataclass(frozen=True)
class TopicMemory:
    topic: Topic
    language: str | None
    query_terms: tuple[str, ...]
    meetings: tuple[RankedExcerpt, ...]
    evidence: tuple[RankedExcerpt, ...]
    structured_signals: tuple[StructuredSignal, ...]
    related_people: tuple[Person, ...]


@dataclass(frozen=True)
class TopicAliasResult:
    topic: Topic
    added_aliases: tuple[str, ...]


@dataclass(frozen=True)
class GraphNode:
    id: int
    type: str
    title: str


@dataclass(frozen=True)
class GraphEdge:
    id: int
    from_node_id: int
    relation: str
    to_node_id: int
    confidence: float
    source_meeting_id: int | None
    source_chunk_id: int | None
    extraction_method: str
    created_at: str | None


@dataclass(frozen=True)
class TopicGraph:
    topic: Topic
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]


@dataclass(frozen=True)
class StructuredEntities:
    entity_kind: StructuredEntityKind
    status: str
    entities: tuple[StructuredSignal, ...]


@dataclass(frozen=True)
class TaskStatusResult:
    id: int
    text: str
    status: str
    status_note: str | None
    status_source: str
    status_updated_at: str


def stable_evidence_id(  # noqa: PLR0913
    source_uuid: str,
    meeting_external_id: str,
    chunk_external_id: str | None,
    *,
    kind: str,
    ordinal: int,
    text: str,
) -> str:
    chunk_identity: object
    if chunk_external_id:
        chunk_identity = {"external_id": chunk_external_id}
    else:
        chunk_identity = {
            "kind": kind,
            "ordinal": ordinal,
            "content_fingerprint": hashlib.sha256(text.encode()).hexdigest(),
        }
    digest = hashlib.sha256(
        dumps_json_bytes(
            {
                "source_uuid": source_uuid,
                "meeting_external_id": meeting_external_id,
                "chunk": chunk_identity,
            }
        )
    ).hexdigest()
    return f"evidence:{digest}"


def canonical_entity_kind(storage_kind: str) -> MemoryEntityKind:
    try:
        return ENTITY_KIND_MAP[storage_kind]
    except KeyError as exc:
        message = f"Unknown memory entity storage kind: {storage_kind}"
        raise ValueError(message) from exc
