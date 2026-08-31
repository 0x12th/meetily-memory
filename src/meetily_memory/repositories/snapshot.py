from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from meetily_memory.db.row_decode import decode_nullable_text, decode_required_text
from meetily_memory.db.schema import IndexReadError
from meetily_memory.domain import MeetingRef
from meetily_memory.repositories.index import IndexRepository

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


@dataclass(frozen=True)
class SnapshotTag:
    normalized_name: str
    display_name: str


@dataclass(frozen=True)
class SnapshotMeeting:
    ref: MeetingRef
    title: str
    started_at: str | None
    ended_at: str | None
    created_at: str | None
    updated_at: str | None
    source_summary: str | None
    manual_tags: tuple[SnapshotTag, ...]


@dataclass(frozen=True)
class MeetingTagSnapshot:
    meetings: tuple[SnapshotMeeting, ...]


class SnapshotRepository:
    """Read active meetings and manual tags from one pinned index/state snapshot."""

    def __init__(self, index_path: Path) -> None:
        self._index = IndexRepository.open_existing(index_path)

    def read(self, limit: int) -> MeetingTagSnapshot:
        if limit < 0:
            message = "Snapshot limit must not be negative."
            raise ValueError(message)
        with self._index.operation_snapshot() as conn:
            return self.read_in_snapshot(conn, limit)

    def read_in_snapshot(self, conn: sqlite3.Connection, limit: int) -> MeetingTagSnapshot:
        if not conn.in_transaction:
            message = "Snapshot connection must be inside an explicit read transaction."
            raise RuntimeError(message)
        if limit < 0:
            message = "Snapshot limit must not be negative."
            raise ValueError(message)
        meeting_rows = conn.execute(
            """
            SELECT
              m.source_uuid,
              m.external_id,
              m.title,
              m.started_at,
              m.ended_at,
              m.created_at,
              m.updated_at,
              m.summary_text
            FROM meetings m
            ORDER BY
              COALESCE(m.updated_at, m.created_at, m.indexed_at) DESC,
              m.source_uuid,
              m.external_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        tag_rows = conn.execute(
            """
            SELECT
              mt.source_uuid,
              mt.meeting_external_id,
              t.normalized_name,
              t.display_name
            FROM operation_state.meeting_tags mt
            JOIN operation_state.manual_tags t ON t.id = mt.manual_tag_id
            JOIN meetings m
              ON m.source_uuid = mt.source_uuid
             AND m.external_id = mt.meeting_external_id
            ORDER BY
              t.normalized_name,
              mt.source_uuid,
              mt.meeting_external_id
            """
        ).fetchall()

        tags_by_ref: dict[MeetingRef, list[SnapshotTag]] = {}
        for row in tag_rows:
            context = "manual-tag snapshot"
            ref = MeetingRef(
                _required_text(row["source_uuid"], "meeting_tags", "source_uuid", context),
                _required_text(
                    row["meeting_external_id"],
                    "meeting_tags",
                    "meeting_external_id",
                    context,
                ),
            )
            tags_by_ref.setdefault(ref, []).append(
                SnapshotTag(
                    _required_text(
                        row["normalized_name"], "manual_tags", "normalized_name", context
                    ),
                    _required_text(row["display_name"], "manual_tags", "display_name", context),
                )
            )

        meetings = tuple(_meeting_from_row(row, tags_by_ref) for row in meeting_rows)
        return MeetingTagSnapshot(meetings=meetings)


def _meeting_from_row(
    row: sqlite3.Row,
    tags_by_ref: dict[MeetingRef, list[SnapshotTag]],
) -> SnapshotMeeting:
    context = "meeting snapshot"
    ref = MeetingRef(
        _required_text(row["source_uuid"], "meetings", "source_uuid", context),
        _required_text(row["external_id"], "meetings", "external_id", context),
    )
    return SnapshotMeeting(
        ref=ref,
        title=_required_text(row["title"], "meetings", "title", context),
        started_at=_nullable_meeting_text(row["started_at"], "started_at", context),
        ended_at=_nullable_meeting_text(row["ended_at"], "ended_at", context),
        created_at=_nullable_meeting_text(row["created_at"], "created_at", context),
        updated_at=_nullable_meeting_text(row["updated_at"], "updated_at", context),
        source_summary=_nullable_meeting_text(row["summary_text"], "summary_text", context),
        manual_tags=tuple(
            sorted(
                tags_by_ref.get(ref, ()),
                key=lambda tag: (tag.normalized_name, tag.display_name),
            )
        ),
    )


def _required_text(value: object, table: str, column: str, context: str) -> str:
    return decode_required_text(
        value,
        table=table,
        column=column,
        context=context,
        error_type=IndexReadError,
    )


def _nullable_meeting_text(value: object, column: str, context: str) -> str | None:
    return decode_nullable_text(
        value,
        table="meetings",
        column=column,
        context=context,
        error_type=IndexReadError,
    )
