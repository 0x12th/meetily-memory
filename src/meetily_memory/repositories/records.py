from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class MeetingRecord:
    source_id: int
    external_id: str
    title: str
    started_at: str | None
    ended_at: str | None
    created_at: str | None
    updated_at: str | None
    folder_path: str | None
    source_path: str | None
    language: str | None
    summary_text: str | None
    raw_summary_json: str | None
    raw_metadata_json: str | None
    fingerprint: str
    indexed_at: str


@dataclass(frozen=True)
class ChunkRecord:
    external_id: str | None
    kind: str
    ordinal: int
    text: str
    speaker: str | None
    starts_at_seconds: float | None
    ends_at_seconds: float | None
    timestamp_label: str | None
    token_count: int | None
    fingerprint: str
    raw_metadata_json: str | None


class ScanRunStats(Protocol):
    meetings_seen: int
    meetings_inserted: int
    meetings_updated: int
    chunks_seen: int
    chunks_inserted: int
    chunks_updated: int
