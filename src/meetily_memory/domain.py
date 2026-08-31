import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import override

from meetily_memory.json_codec import dumps_json_bytes


@dataclass(frozen=True)
class MeetingRef:
    source_uuid: str
    external_id: str

    def __post_init__(self) -> None:
        if not self.source_uuid.strip() or not self.external_id:
            message = "Meeting reference requires non-empty source and meeting IDs."
            raise ValueError(message)

    @override
    def __str__(self) -> str:
        return f"{self.source_uuid}/{self.external_id}"

    @classmethod
    def parse(cls, value: str) -> "MeetingRef":
        source_uuid, separator, external_id = value.partition("/")
        if not separator or not source_uuid.strip() or not external_id:
            message = f"Invalid meeting reference: {value!r}. Expected SOURCE_UUID/EXTERNAL_ID."
            raise ValueError(message)
        return cls(source_uuid, external_id)


@dataclass(frozen=True)
class Meeting:
    id: int
    ref: MeetingRef
    title: str
    started_at: str | None
    ended_at: str | None
    created_at: str | None
    updated_at: str | None
    language: str | None
    summary_text: str | None = None
    chunk_count: int | None = None

    @property
    def external_id(self) -> str:
        return self.ref.external_id


@dataclass(frozen=True)
class SourceExcerpt:
    meeting_ref: MeetingRef
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
    source_chunk_id: int
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
