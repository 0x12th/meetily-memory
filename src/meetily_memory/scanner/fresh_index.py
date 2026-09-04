from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, Never

from meetily_memory.db.index_snapshot import (
    create_index_snapshot_schema,
    fsync_index_snapshot,
    remove_index_snapshot,
    update_index_snapshot_counts,
    validate_index_snapshot_database,
)
from meetily_memory.db.row_decode import (
    decode_nullable_real,
    decode_nullable_text,
    decode_required_text,
)
from meetily_memory.domain import stable_evidence_id
from meetily_memory.scanner.meetily_sqlite import normalize_meeting, validate_meetily_schema
from meetily_memory.scanner.sqlite_source import readonly_sqlite_connection
from meetily_memory.source_fingerprint import capture_source_fingerprint

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from meetily_memory.scanner.meetily_sqlite import ChunkRecord

CANDIDATE_PREFIX = ".meetily-memory-index-candidate-"
CANDIDATE_SUFFIX = ".sqlite"


class DuplicateEvidenceIdentityError(ValueError):
    pass


@dataclass(frozen=True)
class FreshIndexTimings:
    schema_seconds: float
    populate_seconds: float
    validate_seconds: float
    fsync_seconds: float
    total_seconds: float

    def as_payload(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class FreshIndexResult:
    candidate_path: Path
    source_uuid: str
    source_path: Path
    source_revision: int
    meetings: int
    chunks: int
    fts_rows: int
    bytes: int
    source_fingerprint: str | None
    timings: FreshIndexTimings

    def as_payload(self) -> dict[str, object]:
        return {
            "candidate_path": str(self.candidate_path),
            "source_uuid": self.source_uuid,
            "source_path": str(self.source_path),
            "source_revision": self.source_revision,
            "counts": {
                "meetings": self.meetings,
                "chunks": self.chunks,
                "fts_rows": self.fts_rows,
            },
            "bytes": self.bytes,
            "source_fingerprint": self.source_fingerprint,
            "timings": self.timings.as_payload(),
        }


@contextmanager
def pinned_meetily_snapshot(source_path: Path) -> Generator[sqlite3.Connection, None, None]:
    with readonly_sqlite_connection(source_path) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        try:
            validate_meetily_schema(conn)
            conn.execute("SELECT COUNT(*) FROM meetings").fetchone()
            yield conn
        finally:
            if conn.in_transaction:
                conn.rollback()


def iter_meetily_meetings(conn: sqlite3.Connection) -> Iterator[dict[str, Any]]:
    summary_query = (
        "SELECT result, metadata FROM summary_processes WHERE meeting_id = ?"
        if _table_has_column(conn, "summary_processes", "metadata")
        else "SELECT result FROM summary_processes WHERE meeting_id = ?"
    )
    meeting_rows = conn.execute(
        """
        SELECT id, title, created_at, updated_at, folder_path
        FROM meetings
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()
    for row in meeting_rows:
        meeting = _decode_source_meeting(row)
        meeting_id = meeting["id"]
        meeting["transcripts"] = [
            _decode_source_transcript(transcript, meeting_id)
            for transcript in conn.execute(
                """
                SELECT id, transcript, timestamp, audio_start_time, audio_end_time, speaker
                FROM transcripts
                WHERE meeting_id = ?
                ORDER BY COALESCE(audio_start_time, 0), timestamp, id
                """,
                (meeting_id,),
            ).fetchall()
        ]
        summary_row = fetch_optional_row(conn, summary_query, (meeting_id,))
        meeting["summary_process"] = (
            _decode_summary_process(summary_row, meeting_id) if summary_row is not None else None
        )
        notes_row = fetch_optional_row(
            conn,
            "SELECT notes_markdown FROM meeting_notes WHERE meeting_id = ?",
            (meeting_id,),
        )
        meeting["notes"] = (
            _decode_meeting_notes(notes_row, meeting_id) if notes_row is not None else None
        )
        yield meeting


def build_fresh_index(  # noqa: C901, PLR0915
    *,
    selected_source_uuid: str,
    selected_source_path: Path,
    selected_source_revision: int = 0,
    destination_directory: Path,
) -> FreshIndexResult:
    started = perf_counter()
    source_uuid = selected_source_uuid.strip()
    if not source_uuid:
        message = "Selected source UUID/token must not be empty."
        raise ValueError(message)
    if selected_source_revision < 0:
        message = "Selected source revision/token must not be negative."
        raise ValueError(message)
    source_path = Path(selected_source_path).resolve(strict=True)
    if not source_path.is_file():
        message = f"Selected Meetily source is not a regular file: {selected_source_path}."
        raise ValueError(message)
    source_fingerprint_before = capture_source_fingerprint(source_path)
    destination = Path(destination_directory)
    destination.mkdir(parents=True, exist_ok=True)
    if not destination.is_dir():
        message = f"Candidate destination is not a directory: {destination}."
        raise NotADirectoryError(message)

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination,
        prefix=CANDIDATE_PREFIX,
        suffix=CANDIDATE_SUFFIX,
    )
    os.close(descriptor)
    candidate_path = Path(temporary_name)
    candidate_path.unlink()

    schema_started = perf_counter()
    try:
        with closing(sqlite3.connect(candidate_path)) as candidate:
            candidate.row_factory = sqlite3.Row
            candidate.execute("PRAGMA foreign_keys=ON")
            candidate.execute("PRAGMA synchronous=FULL")
            create_index_snapshot_schema(
                candidate,
                source_uuid=source_uuid,
                source_path=source_path,
                source_revision=selected_source_revision,
            )
        schema_finished = perf_counter()

        populate_started = perf_counter()
        with (
            pinned_meetily_snapshot(source_path) as source,
            closing(sqlite3.connect(candidate_path)) as candidate,
        ):
            candidate.row_factory = sqlite3.Row
            candidate.execute("PRAGMA foreign_keys=ON")
            candidate.execute("PRAGMA synchronous=FULL")
            candidate.execute("BEGIN IMMEDIATE")
            try:
                _populate_candidate(candidate, source, source_uuid, source_path)
                meeting_count, chunk_count = update_index_snapshot_counts(candidate)
                candidate.commit()
            except BaseException:
                candidate.rollback()
                raise
        populate_finished = perf_counter()
        source_fingerprint_after = capture_source_fingerprint(source_path)
        source_fingerprint = (
            source_fingerprint_before
            if source_fingerprint_before == source_fingerprint_after
            else None
        )
        if source_fingerprint is not None:
            with closing(sqlite3.connect(candidate_path)) as candidate:
                candidate.execute(
                    "UPDATE index_meta SET source_fingerprint=? WHERE singleton=1",
                    (source_fingerprint,),
                )
                candidate.commit()

        validate_started = perf_counter()
        validated = validate_index_snapshot_database(candidate_path)
        validate_finished = perf_counter()
        if validated["source_uuid"] != source_uuid:
            _raise_runtime("Validated candidate source UUID/token changed during the build.")
        if int(validated["source_revision"]) != selected_source_revision:
            _raise_runtime("Validated candidate source revision/token changed during the build.")
        if int(validated["meetings"]) != meeting_count or int(validated["chunks"]) != chunk_count:
            _raise_runtime("Validated candidate counts changed after commit.")

        fsync_started = perf_counter()
        fsync_index_snapshot(candidate_path)
        fsync_finished = perf_counter()
        timings = FreshIndexTimings(
            schema_seconds=schema_finished - schema_started,
            populate_seconds=populate_finished - populate_started,
            validate_seconds=validate_finished - validate_started,
            fsync_seconds=fsync_finished - fsync_started,
            total_seconds=fsync_finished - started,
        )
        return FreshIndexResult(
            candidate_path=candidate_path,
            source_uuid=source_uuid,
            source_path=source_path,
            source_revision=selected_source_revision,
            meetings=meeting_count,
            chunks=chunk_count,
            fts_rows=int(validated["fts_rows"]),
            bytes=candidate_path.stat().st_size,
            source_fingerprint=source_fingerprint,
            timings=timings,
        )
    except BaseException:
        remove_index_snapshot(candidate_path)
        raise


def cleanup_fresh_index(result_or_path: FreshIndexResult | Path) -> None:
    candidate_path = (
        result_or_path.candidate_path
        if isinstance(result_or_path, FreshIndexResult)
        else Path(result_or_path)
    )
    if not candidate_path.name.startswith(CANDIDATE_PREFIX):
        message = f"Refusing to clean a non-candidate path: {candidate_path}."
        raise ValueError(message)
    remove_index_snapshot(candidate_path)


def _populate_candidate(
    candidate: sqlite3.Connection,
    source: sqlite3.Connection,
    source_uuid: str,
    source_path: Path,
) -> None:
    evidence_owners: dict[str, tuple[str, str]] = {}
    for upstream in iter_meetily_meetings(source):
        deterministic_indexed_at = str(
            upstream.get("updated_at") or upstream.get("created_at") or "1970-01-01T00:00:00Z"
        )
        meeting, chunks = normalize_meeting(
            source_path,
            upstream,
            deterministic_indexed_at,
        )
        cursor = candidate.execute(
            """
            INSERT INTO meetings (
              source_uuid, external_id, title, started_at, ended_at, created_at, updated_at,
              folder_path, source_path, language, summary_text, indexed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_uuid,
                meeting.external_id,
                meeting.title,
                meeting.started_at,
                meeting.ended_at,
                meeting.created_at,
                meeting.updated_at,
                meeting.folder_path,
                meeting.source_path,
                meeting.language,
                meeting.summary_text,
                meeting.indexed_at,
            ),
        )
        if cursor.lastrowid is None:
            message = "Fresh index meeting insert did not return a row ID."
            raise RuntimeError(message)
        meeting_id = int(cursor.lastrowid)
        for chunk in chunks:
            evidence_id = _evidence_id(source_uuid, meeting.external_id, chunk)
            previous = evidence_owners.get(evidence_id)
            if previous is not None:
                first_meeting, first_chunk = previous
                current_chunk = chunk.external_id or f"{chunk.kind}#{chunk.ordinal}"
                message = (
                    "Duplicate upstream chunk identity while building fresh index: "
                    f"{first_meeting}/{first_chunk} and "
                    f"{meeting.external_id}/{current_chunk} map to {evidence_id}."
                )
                raise DuplicateEvidenceIdentityError(message)
            evidence_owners[evidence_id] = (
                meeting.external_id,
                chunk.external_id or f"{chunk.kind}#{chunk.ordinal}",
            )
            chunk_id = _insert_chunk(candidate, meeting_id, evidence_id, chunk)
            candidate.execute(
                """
                INSERT INTO chunks_fts (chunk_id, meeting_id, title, text, speaker)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chunk_id, meeting_id, meeting.title, chunk.text, chunk.speaker),
            )


def _insert_chunk(
    conn: sqlite3.Connection,
    meeting_id: int,
    evidence_id: str,
    chunk: ChunkRecord,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO chunks (
          meeting_id, external_id, evidence_id, kind, ordinal, text, speaker,
          starts_at_seconds, ends_at_seconds, timestamp_label
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            meeting_id,
            chunk.external_id,
            evidence_id,
            chunk.kind,
            chunk.ordinal,
            chunk.text,
            chunk.speaker,
            chunk.starts_at_seconds,
            chunk.ends_at_seconds,
            chunk.timestamp_label,
        ),
    )
    if cursor.lastrowid is None:
        message = "Fresh index chunk insert did not return a row ID."
        raise RuntimeError(message)
    return int(cursor.lastrowid)


def _evidence_id(source_uuid: str, meeting_external_id: str, chunk: ChunkRecord) -> str:
    return stable_evidence_id(
        source_uuid,
        meeting_external_id,
        chunk.external_id,
        kind=chunk.kind,
        ordinal=chunk.ordinal,
        text=chunk.text,
    )


def _raise_runtime(message: str) -> Never:
    raise RuntimeError(message)


def fetch_optional_row(
    conn: sqlite3.Connection,
    query: str,
    params: tuple[object, ...],
) -> sqlite3.Row | None:
    return conn.execute(query, params).fetchone()


def _decode_source_meeting(row: sqlite3.Row) -> dict[str, Any]:
    context = "Meetily source meeting"
    return {
        "id": decode_required_text(
            row["id"],
            table="meetings",
            column="id",
            context=context,
            error_type=RuntimeError,
        ),
        "title": decode_required_text(
            row["title"],
            table="meetings",
            column="title",
            context=context,
            error_type=RuntimeError,
        ),
        "created_at": _source_nullable_text(row["created_at"], "meetings", "created_at", context),
        "updated_at": _source_nullable_text(row["updated_at"], "meetings", "updated_at", context),
        "folder_path": _source_nullable_text(
            row["folder_path"], "meetings", "folder_path", context
        ),
    }


def _decode_source_transcript(row: sqlite3.Row, meeting_id: str) -> dict[str, Any]:
    context = f"Meetily source transcript for meeting {meeting_id!r}"
    return {
        "id": _source_nullable_text(row["id"], "transcripts", "id", context),
        "transcript": _source_nullable_text(
            row["transcript"], "transcripts", "transcript", context
        ),
        "timestamp": _source_nullable_text(row["timestamp"], "transcripts", "timestamp", context),
        "audio_start_time": decode_nullable_real(
            row["audio_start_time"],
            table="transcripts",
            column="audio_start_time",
            context=context,
            error_type=RuntimeError,
        ),
        "audio_end_time": decode_nullable_real(
            row["audio_end_time"],
            table="transcripts",
            column="audio_end_time",
            context=context,
            error_type=RuntimeError,
        ),
        "speaker": _source_nullable_text(row["speaker"], "transcripts", "speaker", context),
    }


def _decode_summary_process(row: sqlite3.Row, meeting_id: str) -> dict[str, Any]:
    context = f"Meetily source summary for meeting {meeting_id!r}"
    summary = {
        "result": _source_nullable_text(row["result"], "summary_processes", "result", context)
    }
    if "metadata" in row.keys():  # noqa: SIM118 - sqlite3.Row contains checks values.
        summary["metadata"] = _source_nullable_text(
            row["metadata"], "summary_processes", "metadata", context
        )
    return summary


def _decode_meeting_notes(row: sqlite3.Row, meeting_id: str) -> dict[str, Any]:
    context = f"Meetily source notes for meeting {meeting_id!r}"
    return {
        "notes_markdown": _source_nullable_text(
            row["notes_markdown"], "meeting_notes", "notes_markdown", context
        )
    }


def _source_nullable_text(
    value: object,
    table: str,
    column: str,
    context: str,
) -> str | None:
    return decode_nullable_text(
        value,
        table=table,
        column=column,
        context=context,
        error_type=RuntimeError,
    )


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))
