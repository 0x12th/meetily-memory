import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from meetily_memory.json_codec import dumps_json, dumps_json_bytes, loads_json
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.repositories.records import ChunkRecord, MeetingRecord
from meetily_memory.scanner.sqlite_source import readonly_sqlite_connection
from meetily_memory.structure_analyzer import StructureAnalyzer


@dataclass
class ScanResult:
    source_id: int = 0
    meetings_seen: int = 0
    meetings_inserted: int = 0
    meetings_updated: int = 0
    meetings_analyzed: int = 0
    chunks_seen: int = 0
    chunks_inserted: int = 0
    chunks_updated: int = 0


class MeetilySQLiteScanner:
    source_kind = "meetily_sqlite"

    def __init__(self, index_path: Path) -> None:
        self.repo = IndexRepository(Path(index_path))
        self.structure_analyzer = StructureAnalyzer(self.repo)

    def scan(self, source_path: Path, *, force: bool = False, analyze: bool = True) -> ScanResult:
        source_path = Path(source_path)
        started_at = utc_now()
        with readonly_sqlite_connection(source_path) as conn:
            result = ScanResult()
            source_id = self.repo.upsert_source(
                self.source_kind,
                str(source_path),
                now=started_at,
            )
            result.source_id = source_id

            for upstream in self._read_meetings(conn):
                result.meetings_seen += 1
                meeting, chunks = normalize_meeting(source_id, source_path, upstream, utc_now())
                result.chunks_seen += len(chunks)
                existing = self.repo.get_meeting_by_external_id(source_id, meeting.external_id)
                meeting_id, updated, inserted_chunks = self.repo.upsert_meeting_with_chunks(
                    meeting,
                    chunks,
                    force=force,
                )
                if analyze and (existing is None or updated):
                    self.structure_analyzer.analyze_meeting(meeting_id)
                    result.meetings_analyzed += 1
                result.chunks_inserted += inserted_chunks
                if existing is None:
                    result.meetings_inserted += 1
                elif updated:
                    result.meetings_updated += 1
                    result.chunks_updated += inserted_chunks

            self.repo.record_scan_run(source_id, started_at, utc_now(), result)
            return result

    def _read_meetings(self, conn: Any) -> Iterator[dict[str, Any]]:
        meetings = (
            dict(row)
            for row in conn.execute(
                """
                SELECT id, title, created_at, updated_at, folder_path
                FROM meetings
                ORDER BY created_at ASC
                """
            )
        )
        for meeting in meetings:
            meeting_id = meeting["id"]
            meeting["transcripts"] = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM transcripts
                    WHERE meeting_id = ?
                    ORDER BY COALESCE(audio_start_time, 0), timestamp, id
                    """,
                    (meeting_id,),
                ).fetchall()
            ]
            meeting["summary_process"] = optional_row(
                conn,
                "SELECT * FROM summary_processes WHERE meeting_id = ?",
                (meeting_id,),
            )
            meeting["notes"] = optional_row(
                conn,
                "SELECT * FROM meeting_notes WHERE meeting_id = ?",
                (meeting_id,),
            )
            yield meeting


def optional_row(conn: Any, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def normalize_meeting(
    source_id: int,
    source_path: Path,
    upstream: dict[str, Any],
    indexed_at: str,
) -> tuple[MeetingRecord, list[ChunkRecord]]:
    chunks: list[ChunkRecord] = []
    summary_text = extract_summary_text(upstream.get("summary_process"))
    language = extract_language(upstream.get("summary_process"))

    for index, transcript in enumerate(upstream["transcripts"]):
        text = normalize_text(transcript.get("transcript") or "")
        if not text:
            continue
        chunks.append(
            ChunkRecord(
                external_id=transcript.get("id"),
                kind="transcript",
                ordinal=index,
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

    next_ordinal = len(chunks)
    if summary_text:
        summary_payload = upstream.get("summary_process") or {}
        chunks.append(
            ChunkRecord(
                external_id=f"summary:{upstream['id']}",
                kind="summary",
                ordinal=next_ordinal,
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
        next_ordinal += 1

    notes = upstream.get("notes")
    notes_text = normalize_text((notes or {}).get("notes_markdown") or "")
    if notes_text:
        chunks.append(
            ChunkRecord(
                external_id=f"note:{upstream['id']}",
                kind="note",
                ordinal=next_ordinal,
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
        source_id=source_id,
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
        raw_summary_json=dumps_json(upstream.get("summary_process"))
        if upstream.get("summary_process")
        else None,
        raw_metadata_json=dumps_json({"source_kind": MeetilySQLiteScanner.source_kind}),
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
    language = metadata.get("language")
    return language if isinstance(language, str) else None


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


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
