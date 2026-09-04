from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Never

from meetily_memory.db.row_decode import (
    decode_nullable_integer,
    decode_nullable_real,
    decode_nullable_text,
    decode_required_integer,
    decode_required_text,
)
from meetily_memory.db.schema import (
    OPERATION_STATE_SCHEMA,
    IndexConnectionFactory,
    IndexReadError,
    existing_index_connection,
    sqlite_read_snapshot,
)
from meetily_memory.domain import (
    Meeting,
    MeetingRef,
    MeetingSearchFilters,
    SearchHit,
    SourceExcerpt,
)
from meetily_memory.repositories.meetings import MeetingsRepository
from meetily_memory.repositories.search import SearchRepository
from meetily_memory.user_state import UserStateRepository, validate_existing_user_state_schema

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Generator, Mapping


OPERATION_SNAPSHOT_PIN_SQL = """
SELECT EXISTS (
  SELECT 1
  FROM main.index_meta i
  JOIN operation_state.app_settings a ON a.singleton = 1
  JOIN operation_state.sources s ON s.uuid = a.source_uuid
  WHERE i.singleton = 1
    AND i.source_uuid = s.uuid
    AND i.source_path = s.current_path
    AND i.source_revision = s.revision
) AS source_binding_matches
"""


class IndexRepository:
    """Read access to one fresh exact-epoch index and its authoritative state."""

    def __init__(
        self,
        index_path: Path,
        *,
        state_path: Path | None = None,
        _read_only: bool = False,
        _user_state: UserStateRepository | None = None,
    ) -> None:
        self.index_path = Path(index_path)
        self.state_path = (
            Path(state_path) if state_path else self.index_path.with_name("state.sqlite")
        )
        self.read_only = _read_only
        self.connection: IndexConnectionFactory = existing_index_connection
        self.operation_connection: IndexConnectionFactory = existing_index_connection

        with existing_index_connection(self.index_path) as conn:
            metadata = conn.execute(
                """
                SELECT source_uuid, source_path, source_revision
                FROM index_meta WHERE singleton = 1
                """
            ).fetchone()
        self.user_state = _user_state or UserStateRepository.open_existing(self.state_path)
        selected = self.user_state.get_selected_source_binding()
        if metadata is None or selected is None:
            raise IndexReadError(_binding_error())
        selected_current_path = decode_required_text(
            selected["current_path"],
            table="sources",
            column="current_path",
            context="selected source binding",
            error_type=IndexReadError,
        )

        expected = (
            decode_required_text(
                selected["uuid"],
                table="sources",
                column="uuid",
                context="selected source binding",
                error_type=IndexReadError,
            ),
            str(Path(selected_current_path).resolve()),
            decode_required_integer(
                selected["revision"],
                table="sources",
                column="revision",
                context="selected source binding",
                error_type=IndexReadError,
            ),
        )
        actual = (
            decode_required_text(
                metadata["source_uuid"],
                table="index_meta",
                column="source_uuid",
                context="index source binding",
                error_type=IndexReadError,
            ),
            str(
                Path(
                    decode_required_text(
                        metadata["source_path"],
                        table="index_meta",
                        column="source_path",
                        context="index source binding",
                        error_type=IndexReadError,
                    )
                ).resolve()
            ),
            decode_required_integer(
                metadata["source_revision"],
                table="index_meta",
                column="source_revision",
                context="index source binding",
                error_type=IndexReadError,
            ),
        )
        if actual != expected:
            raise IndexReadError(_binding_error(actual=actual, expected=expected))

        self.meetings = MeetingsRepository(self.index_path, self.connection)
        self.search_repo = SearchRepository(self.index_path, self.connection)

    @classmethod
    def open_existing(
        cls,
        index_path: Path,
        *,
        state_path: Path | None = None,
    ) -> IndexRepository:
        return cls(index_path, state_path=state_path, _read_only=True)

    @contextmanager
    def operation_snapshot(self) -> Generator[sqlite3.Connection, None, None]:
        state_uri = self.user_state.read_only_uri
        with self.operation_connection(self.index_path) as conn:
            conn.execute(f"ATTACH DATABASE ? AS {OPERATION_STATE_SCHEMA}", (state_uri,))
            self.user_state.recheck_identity()
            validate_existing_user_state_schema(conn, schema=OPERATION_STATE_SCHEMA)
            with sqlite_read_snapshot(conn):
                pinned = conn.execute(OPERATION_SNAPSHOT_PIN_SQL).fetchone()
                if (
                    pinned is None
                    or decode_required_integer(
                        pinned["source_binding_matches"],
                        table="index_meta/app_settings/sources",
                        column="source_binding_matches",
                        context="operation snapshot binding check",
                        error_type=IndexReadError,
                    )
                    != 1
                ):
                    raise IndexReadError(_binding_error())
                yield conn

    def utc_now(self) -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def get_chunks_for_meeting(self, meeting_id: int) -> list[dict[str, Any]]:
        return self.meetings.get_chunks_for_meeting(meeting_id)

    def search(  # noqa: PLR0913
        self,
        query: str,
        limit: int = 10,
        *,
        meeting_id: int | None = None,
        context: int = 0,
        filters: MeetingSearchFilters | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        if connection is not None:
            return self.search_repo.search_in_snapshot(
                connection,
                query,
                limit,
                meeting_id=meeting_id,
                context=context,
                filters=filters,
            )
        return self.search_repo.search(
            query,
            limit,
            meeting_id=meeting_id,
            context=context,
            filters=filters,
        )

    def search_hit_from_row(self, row: Mapping[str, Any]) -> SearchHit:
        return search_hit_from_row(row)

    def search_hits(  # noqa: PLR0913
        self,
        query: str,
        limit: int = 10,
        *,
        meeting_id: int | None = None,
        context: int = 0,
        filters: MeetingSearchFilters | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[SearchHit, ...]:
        rows = self.search(
            query,
            limit,
            meeting_id=meeting_id,
            context=context,
            filters=filters,
            connection=connection,
        )
        return tuple(search_hit_from_row(row) for row in rows)

    def expand_search_hits(
        self,
        hits: tuple[SearchHit, ...],
        context: int,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[SearchHit, ...]:
        if context <= 0 or not hits:
            return hits
        evidence_refs = tuple((hit.id, hit.meeting.ref) for hit in hits)
        rows = (
            self.search_repo.expand_evidence_refs_in_snapshot(connection, evidence_refs, context)
            if connection is not None
            else self.search_repo.expand_evidence_refs(evidence_refs, context)
        )
        return tuple(search_hit_from_row(row) for row in rows)

    def get_search_hit(self, evidence_id: str) -> SearchHit | None:
        row = self.search_repo.evidence_by_id(evidence_id)
        return search_hit_from_row(row) if row is not None else None

    def list_meetings(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.meetings.list_meetings(limit)

    def get_meeting_by_local_id(
        self,
        meeting_id: int,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> dict[str, Any] | None:
        return self.meetings.get_meeting_by_local_id(meeting_id, filters=filters)

    def get_meeting_by_ref(
        self,
        ref: MeetingRef,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> dict[str, Any] | None:
        return self.meetings.get_meeting_by_ref(ref, filters=filters)

    def get_meetings_by_refs(
        self,
        refs: tuple[MeetingRef, ...],
        *,
        filters: MeetingSearchFilters | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[MeetingRef, dict[str, Any]]:
        return self.meetings.get_meetings_by_refs(
            refs,
            filters=filters,
            connection=connection,
        )

    def get_meetings_by_local_ids(
        self,
        meeting_ids: tuple[int, ...],
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> dict[int, dict[str, Any]]:
        return self.meetings.get_meetings_by_local_ids(meeting_ids, filters=filters)

    def meeting_ref_for_local_id(self, meeting_id: int) -> MeetingRef | None:
        meeting = self.get_meeting_by_local_id(meeting_id)
        return meeting_ref_from_row(meeting) if meeting is not None else None

    def meeting_transcript_text(self, ref: MeetingRef) -> str:
        meeting = self.get_meeting_by_ref(ref)
        if meeting is None:
            _raise_value_error(f"Meeting not found: {ref.source_uuid}/{ref.external_id}")
        meeting_id = decode_required_integer(
            meeting["id"],
            table="meetings",
            column="id",
            context="meeting transcript lookup",
            error_type=IndexReadError,
        )
        with self.connection(self.index_path) as conn:
            rows = self.meetings.chunk_rows(conn, meeting_id)
        transcripts: list[str] = []
        for row in rows:
            context = "meeting transcript projection"
            kind = decode_required_text(
                row["kind"],
                table="chunks",
                column="kind",
                context=context,
                error_type=IndexReadError,
            )
            text = decode_required_text(
                row["text"],
                table="chunks",
                column="text",
                context=context,
                error_type=IndexReadError,
            )
            if kind == "transcript" and text:
                transcripts.append(text)
        return "\n".join(transcripts)

    def dominant_meeting_language(self) -> str | None:
        return self.meetings.dominant_meeting_language()

    def stats(self) -> dict[str, int]:
        with self.connection(self.index_path) as conn:
            return {
                "meetings": int(conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]),
                "chunks": int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]),
                "sources": int(conn.execute("SELECT COUNT(*) FROM index_meta").fetchone()[0]),
            }


def search_hit_from_row(row: Mapping[str, Any]) -> SearchHit:
    is_context = row.get("is_context", False)
    if type(is_context) is not bool:
        message = (
            "Invalid search projection: is_context must be bool, "
            f"got {type(is_context).__name__} ({is_context!r})."
        )
        raise IndexReadError(message)
    return SearchHit(
        id=decode_required_text(
            row["evidence_id"],
            table="chunks",
            column="evidence_id",
            context="search hit",
            error_type=IndexReadError,
        ),
        meeting=meeting_from_row(row),
        excerpt=source_excerpt_from_search_row(row),
        source_chunk_id=decode_required_integer(
            row["chunk_id"],
            table="chunks",
            column="id",
            context="search hit",
            error_type=IndexReadError,
        ),
        is_context=is_context,
    )


def meeting_ref_from_row(row: Mapping[str, Any]) -> MeetingRef:
    external_column = "meeting_external_id" if "meeting_external_id" in row else "external_id"
    return MeetingRef(
        source_uuid=decode_required_text(
            row["source_uuid"],
            table="meetings",
            column="source_uuid",
            context="meeting reference",
            error_type=IndexReadError,
        ),
        external_id=decode_required_text(
            row[external_column],
            table="meetings",
            column="external_id",
            context="meeting reference",
            error_type=IndexReadError,
        ),
    )


def meeting_from_row(row: Mapping[str, Any]) -> Meeting:
    id_column = "meeting_id" if "meeting_id" in row else "id"
    context = "meeting projection"
    chunk_count = (
        decode_nullable_integer(
            row["chunk_count"],
            table="meetings",
            column="chunk_count",
            context=context,
            error_type=IndexReadError,
        )
        if "chunk_count" in row
        else None
    )
    return Meeting(
        id=decode_required_integer(
            row[id_column],
            table="meetings",
            column="id",
            context=context,
            error_type=IndexReadError,
        ),
        ref=meeting_ref_from_row(row),
        title=decode_required_text(
            row["title"],
            table="meetings",
            column="title",
            context=context,
            error_type=IndexReadError,
        ),
        started_at=_index_nullable_text(row, "meetings", "started_at", context),
        ended_at=_index_nullable_text(row, "meetings", "ended_at", context),
        created_at=_index_nullable_text(row, "meetings", "created_at", context),
        updated_at=_index_nullable_text(row, "meetings", "updated_at", context),
        language=_index_nullable_text(row, "meetings", "language", context),
        summary_text=_index_nullable_text(row, "meetings", "summary_text", context),
        chunk_count=chunk_count,
    )


def source_excerpt_from_search_row(row: Mapping[str, Any]) -> SourceExcerpt:
    context = "search excerpt"
    return SourceExcerpt(
        meeting_ref=meeting_ref_from_row(row),
        chunk_external_id=_index_nullable_text(row, "chunks", "chunk_external_id", context),
        kind=decode_required_text(
            row["kind"],
            table="chunks",
            column="kind",
            context=context,
            error_type=IndexReadError,
        ),
        ordinal=decode_required_integer(
            row["ordinal"],
            table="chunks",
            column="ordinal",
            context=context,
            error_type=IndexReadError,
        ),
        text=decode_required_text(
            row["text"],
            table="chunks",
            column="text",
            context=context,
            error_type=IndexReadError,
        ),
        speaker=_index_nullable_text(row, "chunks", "speaker", context),
        starts_at_seconds=decode_nullable_real(
            row["starts_at_seconds"],
            table="chunks",
            column="starts_at_seconds",
            context=context,
            error_type=IndexReadError,
        ),
        ends_at_seconds=decode_nullable_real(
            row["ends_at_seconds"],
            table="chunks",
            column="ends_at_seconds",
            context=context,
            error_type=IndexReadError,
        ),
        timestamp_label=_index_nullable_text(row, "chunks", "timestamp_label", context),
    )


def _index_nullable_text(
    row: Mapping[str, Any],
    table: str,
    column: str,
    context: str,
) -> str | None:
    return decode_nullable_text(
        row[column],
        table=table,
        column=column,
        context=context,
        error_type=IndexReadError,
    )


def _raise_value_error(message: str) -> Never:
    raise ValueError(message)


def _binding_error(
    *,
    actual: tuple[str, str, int] | None = None,
    expected: tuple[str, str, int] | None = None,
) -> str:
    detail = f" (index={actual!r}, state={expected!r})" if actual and expected else ""
    return (
        "The disposable index does not match the single active source selected in state"
        f"{detail}. Run `mm refresh --source PATH` to rebuild it; in-place migration is not "
        "supported."
    )
