from __future__ import annotations

from contextlib import nullcontext
from itertools import batched
from typing import TYPE_CHECKING, Any

from meetily_memory.db.row_decode import (
    decode_nullable_real,
    decode_nullable_text,
    decode_required_integer,
    decode_required_text,
)
from meetily_memory.db.schema import IndexReadError
from meetily_memory.domain import MeetingRef, MeetingSearchFilters
from meetily_memory.repositories.search import meeting_time_predicate

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping
    from pathlib import Path

    from meetily_memory.db.schema import IndexConnectionFactory

MEETING_READ_BATCH_SIZE = 200


def _decode_meeting_row(row: Mapping[str, Any], context: str) -> dict[str, Any]:
    decoded = dict(row)
    decoded["id"] = decode_required_integer(
        row["id"],
        table="meetings",
        column="id",
        context=context,
        error_type=IndexReadError,
    )
    for column in ("source_uuid", "external_id", "title", "indexed_at"):
        decoded[column] = decode_required_text(
            row[column],
            table="meetings",
            column=column,
            context=context,
            error_type=IndexReadError,
        )
    for column in (
        "started_at",
        "ended_at",
        "created_at",
        "updated_at",
        "folder_path",
        "source_path",
        "language",
        "summary_text",
    ):
        decoded[column] = decode_nullable_text(
            row[column],
            table="meetings",
            column=column,
            context=context,
            error_type=IndexReadError,
        )
    for column in ("request_order", "chunk_count"):
        if column in row:
            decoded[column] = decode_required_integer(
                row[column],
                table="meetings",
                column=column,
                context=context,
                error_type=IndexReadError,
            )
    return decoded


def _decode_chunk_row(row: Mapping[str, Any], context: str) -> dict[str, Any]:
    decoded = dict(row)
    for column in ("id", "meeting_id", "ordinal"):
        decoded[column] = decode_required_integer(
            row[column],
            table="chunks",
            column=column,
            context=context,
            error_type=IndexReadError,
        )
    for column in ("evidence_id", "kind", "text"):
        decoded[column] = decode_required_text(
            row[column],
            table="chunks",
            column=column,
            context=context,
            error_type=IndexReadError,
        )
    for column in (
        "external_id",
        "speaker",
        "timestamp_label",
    ):
        decoded[column] = decode_nullable_text(
            row[column],
            table="chunks",
            column=column,
            context=context,
            error_type=IndexReadError,
        )
    for column in ("starts_at_seconds", "ends_at_seconds"):
        decoded[column] = decode_nullable_real(
            row[column],
            table="chunks",
            column=column,
            context=context,
            error_type=IndexReadError,
        )
    return decoded


class MeetingsRepository:
    """Read-only meeting access for one exact-epoch disposable index snapshot."""

    def __init__(self, index_path: Path, connection: IndexConnectionFactory) -> None:
        self.index_path = index_path
        self.connection = connection

    def get_chunks_for_meeting(self, meeting_id: int) -> list[dict[str, Any]]:
        with self.connection(self.index_path) as conn:
            rows = conn.execute(
                "SELECT * FROM chunks WHERE meeting_id = ? ORDER BY ordinal",
                (meeting_id,),
            ).fetchall()
        return [_decode_chunk_row(row, "meeting chunk list") for row in rows]

    def chunk_rows(self, conn: sqlite3.Connection, meeting_id: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM chunks WHERE meeting_id = ? ORDER BY ordinal",
            (meeting_id,),
        ).fetchall()
        return [_decode_chunk_row(row, "meeting chunk projection") for row in rows]

    def list_meetings(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connection(self.index_path) as conn:
            rows = conn.execute(
                """
                SELECT m.*, COUNT(c.id) AS chunk_count
                FROM meetings m
                LEFT JOIN chunks c ON c.meeting_id = m.id
                GROUP BY m.id
                ORDER BY COALESCE(m.updated_at, m.created_at, m.indexed_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_decode_meeting_row(row, "meeting list") for row in rows]

    def get_meeting_by_local_id(
        self,
        meeting_id: int,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> dict[str, Any] | None:
        time_sql, time_params = meeting_time_predicate(filters)
        with self.connection(self.index_path) as conn:
            row = conn.execute(
                f"SELECT m.* FROM meetings m WHERE m.id = ? AND {time_sql}",
                (meeting_id, *time_params),
            ).fetchone()
        return _decode_meeting_row(row, "meeting lookup") if row is not None else None

    def get_meeting_by_ref(
        self,
        ref: MeetingRef,
        *,
        filters: MeetingSearchFilters | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        return self.get_meetings_by_refs(
            (ref,),
            filters=filters,
            connection=connection,
        ).get(ref)

    def get_meetings_by_refs(
        self,
        refs: tuple[MeetingRef, ...],
        *,
        filters: MeetingSearchFilters | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[MeetingRef, dict[str, Any]]:
        unique_refs = tuple(dict.fromkeys(refs))
        if not unique_refs:
            return {}
        time_sql, time_params = meeting_time_predicate(filters)
        meetings: dict[MeetingRef, dict[str, Any]] = {}
        connection_context = (
            self.connection(self.index_path) if connection is None else nullcontext(connection)
        )
        with connection_context as conn:
            for ref_batch in batched(unique_refs, MEETING_READ_BATCH_SIZE):
                values_sql = ", ".join("(?, ?, ?)" for _ in ref_batch)
                requested_params = tuple(
                    value
                    for request_order, ref in enumerate(ref_batch)
                    for value in (request_order, ref.source_uuid, ref.external_id)
                )
                rows = conn.execute(
                    f"""
                    WITH requested(request_order, source_uuid, external_id) AS (
                      VALUES {values_sql}
                    )
                    SELECT requested.request_order, m.*
                    FROM requested
                    JOIN meetings m
                      ON m.source_uuid = requested.source_uuid
                     AND m.external_id = requested.external_id
                    WHERE {time_sql}
                    ORDER BY requested.request_order
                    """,
                    (*requested_params, *time_params),
                ).fetchall()
                for row in rows:
                    meeting = _decode_meeting_row(row, "meeting reference lookup")
                    ref = MeetingRef(meeting["source_uuid"], meeting["external_id"])
                    meetings[ref] = meeting
        return meetings

    def get_meetings_by_local_ids(
        self,
        meeting_ids: tuple[int, ...],
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> dict[int, dict[str, Any]]:
        unique_ids = tuple(dict.fromkeys(meeting_ids))
        if not unique_ids:
            return {}
        time_sql, time_params = meeting_time_predicate(filters)
        meetings: dict[int, dict[str, Any]] = {}
        with self.connection(self.index_path) as conn:
            for meeting_id_batch in batched(unique_ids, MEETING_READ_BATCH_SIZE):
                placeholders = ", ".join("?" for _ in meeting_id_batch)
                rows = conn.execute(
                    f"""
                    SELECT m.*
                    FROM meetings m
                    WHERE m.id IN ({placeholders}) AND {time_sql}
                    """,
                    (*meeting_id_batch, *time_params),
                ).fetchall()
                for row in rows:
                    meeting = _decode_meeting_row(row, "local meeting lookup")
                    meetings[meeting["id"]] = meeting
        return meetings

    def dominant_meeting_language(self) -> str | None:
        with self.connection(self.index_path) as conn:
            row = conn.execute(
                """
                SELECT language
                FROM meetings
                WHERE language IS NOT NULL AND language != ''
                GROUP BY language
                ORDER BY COUNT(*) DESC, language ASC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return decode_required_text(
            row["language"],
            table="meetings",
            column="language",
            context="dominant meeting language",
            error_type=IndexReadError,
        )
