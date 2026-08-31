from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Never

from meetily_memory.json_codec import dumps_json, dumps_json_bytes, loads_json
from meetily_memory.scanner.sqlite_source import readonly_sqlite_connection

if TYPE_CHECKING:
    from pathlib import Path

MEETING_NORMALIZATION_VERSION = 2
SOURCE_KIND = "meetily_sqlite"


@dataclass(frozen=True)
class MeetingRecord:
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


REQUIRED_MEETILY_SCHEMA = {
    "meetings": {"id", "title", "created_at", "updated_at", "folder_path"},
    "transcripts": {
        "id",
        "meeting_id",
        "transcript",
        "timestamp",
        "audio_start_time",
        "audio_end_time",
        "speaker",
    },
    "summary_processes": {"meeting_id", "result"},
    "meeting_notes": {"meeting_id", "notes_markdown"},
}


def inspect_meetily_schema(source_path: Path) -> tuple[bool, str | None]:
    try:
        with readonly_sqlite_connection(source_path) as conn:
            validate_meetily_schema(conn)
    except (RuntimeError, sqlite3.Error) as exc:
        return False, str(exc)
    return True, None


def validate_meetily_schema(conn: Any) -> None:
    for table, required_columns in REQUIRED_MEETILY_SCHEMA.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            _raise_runtime(f"Meetily DB schema is unsupported: missing table {table}")
        actual_columns = {str(row["name"]) for row in rows}
        missing_columns = sorted(required_columns - actual_columns)
        if missing_columns:
            message = (
                "Meetily DB schema is unsupported: "
                f"missing columns {table}.{', '.join(missing_columns)}"
            )
            raise RuntimeError(message)


def _raise_runtime(message: str) -> Never:
    raise RuntimeError(message)


def meeting_external_ids(source_path: Path) -> set[str]:
    with readonly_sqlite_connection(source_path) as conn:
        validate_meetily_schema(conn)
        return {str(row["id"]) for row in conn.execute("SELECT id FROM meetings")}


def normalize_meeting(
    source_path: Path,
    upstream: dict[str, Any],
    indexed_at: str,
) -> tuple[MeetingRecord, list[ChunkRecord]]:
    chunks: list[ChunkRecord] = []
    summary_text = extract_summary_text(upstream.get("summary_process"))
    language = extract_language(upstream.get("summary_process"))

    for transcript in upstream["transcripts"]:
        text = normalize_text(transcript.get("transcript") or "")
        if not text:
            continue
        ordinal = len(chunks)
        chunks.append(
            ChunkRecord(
                external_id=transcript.get("id"),
                kind="transcript",
                ordinal=ordinal,
                text=text,
                speaker=clean_optional(transcript.get("speaker")),
                starts_at_seconds=transcript.get("audio_start_time"),
                ends_at_seconds=transcript.get("audio_end_time"),
                timestamp_label=clean_optional(transcript.get("timestamp")),
                token_count=len(text.split()),
                fingerprint=fingerprint_json(transcript),
                raw_metadata_json=dumps_json(transcript),
            )
        )

    if summary_text:
        summary_payload = upstream.get("summary_process") or {}
        chunks.append(
            ChunkRecord(
                external_id=f"summary:{upstream['id']}",
                kind="summary",
                ordinal=len(chunks),
                text=summary_text,
                speaker=None,
                starts_at_seconds=None,
                ends_at_seconds=None,
                timestamp_label=None,
                token_count=len(summary_text.split()),
                fingerprint=fingerprint_json({"kind": "summary", "payload": summary_payload}),
                raw_metadata_json=dumps_json(summary_payload),
            )
        )

    notes = upstream.get("notes")
    notes_text = normalize_text((notes or {}).get("notes_markdown") or "")
    if notes_text:
        chunks.append(
            ChunkRecord(
                external_id=f"note:{upstream['id']}",
                kind="note",
                ordinal=len(chunks),
                text=notes_text,
                speaker=None,
                starts_at_seconds=None,
                ends_at_seconds=None,
                timestamp_label=None,
                token_count=len(notes_text.split()),
                fingerprint=fingerprint_json({"kind": "note", "payload": notes}),
                raw_metadata_json=dumps_json(notes),
            )
        )

    meeting_fingerprint_payload = {
        "normalization_version": MEETING_NORMALIZATION_VERSION,
        "meeting": {
            "id": upstream.get("id"),
            "title": upstream.get("title"),
            "created_at": upstream.get("created_at"),
            "updated_at": upstream.get("updated_at"),
            "folder_path": upstream.get("folder_path"),
        },
        "chunks": [chunk.fingerprint for chunk in chunks],
        "summary": upstream.get("summary_process"),
        "notes": upstream.get("notes"),
    }
    meeting = MeetingRecord(
        external_id=upstream["id"],
        title=upstream["title"],
        started_at=upstream.get("created_at"),
        ended_at=None,
        created_at=upstream.get("created_at"),
        updated_at=upstream.get("updated_at"),
        folder_path=upstream.get("folder_path"),
        source_path=str(source_path),
        language=language,
        summary_text=summary_text,
        raw_summary_json=(
            dumps_json(upstream.get("summary_process")) if upstream.get("summary_process") else None
        ),
        raw_metadata_json=dumps_json({"source_kind": SOURCE_KIND}),
        fingerprint=fingerprint_json(meeting_fingerprint_payload),
        indexed_at=indexed_at,
    )
    return meeting, chunks


def extract_summary_text(summary_process: dict[str, Any] | None) -> str | None:
    if not summary_process or not summary_process.get("result"):
        return None
    raw = summary_process["result"]
    try:
        parsed = loads_json(raw)
    except ValueError:
        return normalize_text(raw)
    if isinstance(parsed, dict):
        for key in ("markdown", "summary", "raw_summary", "MeetingName"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return normalize_text(value)
        return normalize_text(dumps_json(parsed))
    if isinstance(parsed, str):
        return normalize_text(parsed)
    return normalize_text(dumps_json(parsed))


def extract_language(summary_process: dict[str, Any] | None) -> str | None:
    if not summary_process or not summary_process.get("metadata"):
        return None
    try:
        metadata = loads_json(summary_process["metadata"])
    except ValueError:
        return None
    if not isinstance(metadata, dict):
        return None
    language = metadata.get("language")
    return language if isinstance(language, str) and language.strip() else None


def normalize_text(value: str) -> str:
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized_lines: list[str] = []
    blank = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if not blank:
                normalized_lines.append("")
            blank = True
        else:
            normalized_lines.append(stripped)
            blank = False
    return "\n".join(normalized_lines).strip()


def clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def fingerprint_json(payload: Any) -> str:
    return hashlib.sha256(dumps_json_bytes(payload)).hexdigest()
