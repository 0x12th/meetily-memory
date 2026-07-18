import hashlib
from dataclasses import asdict, dataclass
from typing import Literal

from meetily_memory.json_codec import dumps_json_bytes

MemoryEntityKind = Literal["decision", "task", "risk", "question"]
COMPACT_SEARCH_HIT_PROJECTION_VERSION = "compact.v1"
ENTITY_KIND_MAP: dict[str, MemoryEntityKind] = {
    "decisions": "decision",
    "action_items": "task",
    "risks": "risk",
    "open_questions": "question",
}


@dataclass(frozen=True)
class MeetingRef:
    source_uuid: str
    external_id: str
    title: str
    source_path: str
    created_at: str | None
    updated_at: str | None
    folder_path: str | None
    language: str | None

    def as_payload(self) -> dict[str, object]:
        return asdict(self)


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

    def as_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SearchHit:
    id: str
    meeting: MeetingRef
    excerpt: SourceExcerpt
    is_context: bool = False

    def as_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "meeting": self.meeting.as_payload(),
            "excerpt": self.excerpt.as_payload(),
            "is_context": self.is_context,
        }

    def compact(self, preview_length: int) -> "CompactSearchHit":
        if preview_length < 1:
            message = "preview_length must be positive"
            raise ValueError(message)
        truncated = len(self.excerpt.text) > preview_length
        preview = self.excerpt.text[:preview_length].rstrip()
        if truncated:
            preview = f"{preview}…"
        return CompactSearchHit(
            id=self.id,
            meeting=self.meeting,
            preview=preview,
            truncated=truncated,
            is_context=self.is_context,
            preview_length=preview_length,
            projection_version=COMPACT_SEARCH_HIT_PROJECTION_VERSION,
        )


@dataclass(frozen=True)
class CompactSearchHit:
    id: str
    meeting: MeetingRef
    preview: str
    truncated: bool
    is_context: bool
    preview_length: int
    projection_version: str

    def as_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "meeting": self.meeting.as_payload(),
            "preview": self.preview,
            "truncated": self.truncated,
            "is_context": self.is_context,
            "preview_length": self.preview_length,
            "projection_version": self.projection_version,
        }


@dataclass(frozen=True)
class MemoryEntity:
    kind: MemoryEntityKind
    content: str
    source: SourceExcerpt
    evidence_id: str
    extraction_method: str
    authoritative: bool = False

    def as_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "content": self.content,
            "source": self.source.as_payload(),
            "evidence_id": self.evidence_id,
            "extraction_method": self.extraction_method,
            "authoritative": self.authoritative,
        }


@dataclass(frozen=True)
class ContextBundle:
    question: str
    evidence: tuple[SearchHit, ...]
    entities: tuple[MemoryEntity, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "question": self.question,
            "evidence": [hit.as_payload() for hit in self.evidence],
            "entities": [entity.as_payload() for entity in self.entities],
        }


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
