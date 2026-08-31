from __future__ import annotations

import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING

from meetily_memory.db.row_decode import decode_required_integer, decode_required_text
from meetily_memory.db.schema import (
    OPERATION_STATE_SCHEMA,
    IndexReadError,
    missing_user_state_message,
)
from meetily_memory.db.state_schema import StateSchemaError
from meetily_memory.domain import MeetingRef
from meetily_memory.user_state import UserStateRepository, validate_existing_user_state_schema

if TYPE_CHECKING:
    from collections.abc import Generator

    from meetily_memory.repositories.index import IndexRepository

TAG_SUGGESTION_LIMIT = 5
TAG_READ_BATCH_SIZE = 200


@dataclass(frozen=True)
class Tag:
    normalized_name: str
    display_name: str


@dataclass(frozen=True)
class TagMutationResult:
    added_links: int = 0
    existing_links: int = 0
    removed_links: int = 0
    missing_links: int = 0
    added_tags: tuple[str, ...] = ()
    existing_tags: tuple[str, ...] = ()
    removed_tags: tuple[str, ...] = ()
    missing_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class TagMatch:
    meeting_ref: MeetingRef
    tag: Tag
    kind: str


@dataclass(frozen=True)
class TagAssignment:
    identity: MeetingRef
    tag: Tag


@dataclass(frozen=True)
class TagCount:
    normalized_name: str
    display_name: str
    active_meetings: int


@dataclass(frozen=True)
class TagSuggestion:
    tag: Tag
    reason: str
    similar_meeting_id: int | None = None


class TagRepository:
    def __init__(self, state_path: Path, *, _read_only: bool = False) -> None:
        self.state_path = Path(state_path)
        self.read_only = _read_only
        if self.read_only:
            if not self.state_path.is_file():
                raise IndexReadError(missing_user_state_message(self.state_path))
            UserStateRepository.open_existing(self.state_path)
            return
        UserStateRepository(self.state_path)

    @classmethod
    def open_existing(cls, state_path: Path) -> TagRepository:
        return cls(state_path, _read_only=True)

    def assign(
        self,
        source_uuid: str,
        meeting_external_ids: tuple[str, ...],
        tag_names: tuple[str, ...],
        *,
        now: str,
    ) -> TagMutationResult:
        identities = tuple(
            MeetingRef(source_uuid, meeting_external_id)
            for meeting_external_id in meeting_external_ids
        )
        return self.assign_many(identities, tag_names, now=now)

    def assign_many(
        self,
        identities: tuple[MeetingRef, ...],
        tag_names: tuple[str, ...],
        *,
        now: str,
    ) -> TagMutationResult:
        tags = normalize_tags(tag_names)
        added_links = 0
        added_tags: list[str] = []
        existing_tags: list[str] = []
        with self._connect() as conn:
            for tag in tags:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO manual_tags (
                      normalized_name, display_name, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (tag.normalized_name, tag.display_name, now),
                )
                stored = conn.execute(
                    """
                    SELECT id, display_name
                    FROM manual_tags
                    WHERE normalized_name = ?
                    """,
                    (tag.normalized_name,),
                ).fetchone()
                if stored is None:
                    message = f"Manual tag disappeared after insert: {tag.normalized_name}"
                    raise StateSchemaError(message)
                tag_id = _tag_integer(stored["id"], "manual_tags", "id", "tag assignment")
                tag_added_links = 0
                for identity in identities:
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO meeting_tags (
                          source_uuid, meeting_external_id, manual_tag_id, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            identity.source_uuid,
                            identity.external_id,
                            tag_id,
                            now,
                        ),
                    )
                    added_links += cursor.rowcount
                    tag_added_links += cursor.rowcount
                display_name = _tag_text(
                    stored["display_name"], "manual_tags", "display_name", "tag assignment"
                )
                if tag_added_links:
                    added_tags.append(display_name)
                if tag_added_links < len(identities):
                    existing_tags.append(display_name)
            conn.commit()
        attempted_links = len(identities) * len(tags)
        return TagMutationResult(
            added_links=added_links,
            existing_links=attempted_links - added_links,
            added_tags=tuple(added_tags),
            existing_tags=tuple(existing_tags),
        )

    def remove(
        self,
        source_uuid: str,
        meeting_external_ids: tuple[str, ...],
        tag_names: tuple[str, ...],
    ) -> TagMutationResult:
        identities = tuple(
            MeetingRef(source_uuid, meeting_external_id)
            for meeting_external_id in meeting_external_ids
        )
        return self.remove_many(identities, tag_names)

    def remove_many(
        self,
        identities: tuple[MeetingRef, ...],
        tag_names: tuple[str, ...],
    ) -> TagMutationResult:
        tags = normalize_tags(tag_names)
        removed_links = 0
        removed_tags: list[str] = []
        missing_tags: list[str] = []
        with self._connect() as conn:
            for tag in tags:
                row = conn.execute(
                    "SELECT id FROM manual_tags WHERE normalized_name = ?",
                    (tag.normalized_name,),
                ).fetchone()
                if row is None:
                    missing_tags.append(tag.display_name)
                    continue
                tag_id = _tag_integer(row["id"], "manual_tags", "id", "tag removal")
                tag_removed_links = 0
                for identity in identities:
                    cursor = conn.execute(
                        """
                        DELETE FROM meeting_tags
                        WHERE source_uuid = ?
                          AND meeting_external_id = ?
                          AND manual_tag_id = ?
                        """,
                        (
                            identity.source_uuid,
                            identity.external_id,
                            tag_id,
                        ),
                    )
                    removed_links += cursor.rowcount
                    tag_removed_links += cursor.rowcount
                if tag_removed_links:
                    removed_tags.append(tag.display_name)
                if tag_removed_links < len(identities):
                    missing_tags.append(tag.display_name)
                conn.execute(
                    """
                    DELETE FROM manual_tags
                    WHERE id = ?
                      AND NOT EXISTS (
                        SELECT 1 FROM meeting_tags
                        WHERE meeting_tags.manual_tag_id = manual_tags.id
                      )
                    """,
                    (tag_id,),
                )
            conn.commit()
        attempted_links = len(identities) * len(tags)
        return TagMutationResult(
            removed_links=removed_links,
            missing_links=attempted_links - removed_links,
            removed_tags=tuple(removed_tags),
            missing_tags=tuple(missing_tags),
        )

    def list_for_meeting(
        self,
        source_uuid: str,
        meeting_external_id: str,
    ) -> tuple[Tag, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.normalized_name, t.display_name
                FROM meeting_tags mt
                JOIN manual_tags t ON t.id = mt.manual_tag_id
                WHERE mt.source_uuid = ? AND mt.meeting_external_id = ?
                ORDER BY t.id
                """,
                (source_uuid, meeting_external_id),
            ).fetchall()
        return tuple(
            Tag(
                _tag_text(
                    row["normalized_name"],
                    "manual_tags",
                    "normalized_name",
                    "meeting tag list",
                ),
                _tag_text(row["display_name"], "manual_tags", "display_name", "meeting tag list"),
            )
            for row in rows
        )

    def list_for_meetings(self, refs: tuple[MeetingRef, ...]) -> dict[MeetingRef, tuple[Tag, ...]]:
        unique_refs = tuple(dict.fromkeys(refs))
        tags_by_ref: dict[MeetingRef, list[Tag]] = {ref: [] for ref in unique_refs}
        if not unique_refs:
            return {}
        with self._connect() as conn:
            for ref_batch in batched(unique_refs, TAG_READ_BATCH_SIZE):
                values_sql = ", ".join("(?, ?, ?)" for _ in ref_batch)
                params = tuple(
                    value
                    for request_order, ref in enumerate(ref_batch)
                    for value in (request_order, ref.source_uuid, ref.external_id)
                )
                sql = f"""
                    WITH requested(request_order, source_uuid, external_id) AS (
                      VALUES {values_sql}
                    )
                    SELECT
                      requested.source_uuid,
                      requested.external_id,
                      t.normalized_name,
                      t.display_name
                    FROM requested
                    JOIN meeting_tags mt
                      ON mt.source_uuid = requested.source_uuid
                     AND mt.meeting_external_id = requested.external_id
                    JOIN manual_tags t ON t.id = mt.manual_tag_id
                    ORDER BY requested.request_order, t.id
                    """  # noqa: S608
                rows = conn.execute(sql, params).fetchall()
                for row in rows:
                    context = "meeting tag batch list"
                    ref = MeetingRef(
                        _tag_text(row["source_uuid"], "meeting_tags", "source_uuid", context),
                        _tag_text(
                            row["external_id"],
                            "meeting_tags",
                            "meeting_external_id",
                            context,
                        ),
                    )
                    tags_by_ref[ref].append(
                        Tag(
                            _tag_text(
                                row["normalized_name"],
                                "manual_tags",
                                "normalized_name",
                                context,
                            ),
                            _tag_text(
                                row["display_name"],
                                "manual_tags",
                                "display_name",
                                context,
                            ),
                        )
                    )
        return {ref: tuple(tags) for ref, tags in tags_by_ref.items()}

    def search(self, query: str) -> tuple[TagMatch, ...]:
        with self._connect() as conn:
            return self._search_in_connection(conn, query, schema="main")

    def search_in_snapshot(
        self,
        conn: sqlite3.Connection,
        query: str,
    ) -> tuple[TagMatch, ...]:
        if not conn.in_transaction:
            message = "Tag connection must be inside an explicit read snapshot."
            raise RuntimeError(message)
        return self._search_in_connection(conn, query, schema=OPERATION_STATE_SCHEMA)

    def _search_in_connection(
        self,
        conn: sqlite3.Connection,
        query: str,
        *,
        schema: str,
    ) -> tuple[TagMatch, ...]:
        normalized_query = normalize_tag_name(query)
        if not normalized_query:
            return ()
        query_tokens = set(normalized_query.split())
        rows = conn.execute(
            f"""
            SELECT
              mt.source_uuid,
              mt.meeting_external_id,
              t.normalized_name,
              t.display_name
            FROM {schema}.meeting_tags mt
            JOIN {schema}.manual_tags t ON t.id = mt.manual_tag_id
            ORDER BY mt.meeting_external_id, t.normalized_name
            """  # noqa: S608
        ).fetchall()
        matches: list[TagMatch] = []
        for row in rows:
            context = "tag search"
            normalized_name = _tag_text(
                row["normalized_name"], "manual_tags", "normalized_name", context
            )
            kind = (
                "exact"
                if normalized_name == normalized_query
                else "token"
                if query_tokens.intersection(normalized_name.split())
                else None
            )
            if kind is None:
                continue
            matches.append(
                TagMatch(
                    meeting_ref=MeetingRef(
                        source_uuid=_tag_text(
                            row["source_uuid"], "meeting_tags", "source_uuid", context
                        ),
                        external_id=_tag_text(
                            row["meeting_external_id"],
                            "meeting_tags",
                            "meeting_external_id",
                            context,
                        ),
                    ),
                    tag=Tag(
                        normalized_name,
                        _tag_text(row["display_name"], "manual_tags", "display_name", context),
                    ),
                    kind=kind,
                )
            )
        return tuple(
            sorted(
                matches,
                key=lambda match: (match.kind != "exact", match.meeting_ref.external_id),
            )
        )

    def list_assignments(self) -> tuple[TagAssignment, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  mt.source_uuid,
                  mt.meeting_external_id,
                  t.normalized_name,
                  t.display_name
                FROM meeting_tags mt
                JOIN manual_tags t ON t.id = mt.manual_tag_id
                ORDER BY t.id, mt.source_uuid, mt.meeting_external_id
                """
            ).fetchall()
        return tuple(
            TagAssignment(
                identity=MeetingRef(
                    source_uuid=_tag_text(
                        row["source_uuid"],
                        "meeting_tags",
                        "source_uuid",
                        "tag assignment list",
                    ),
                    external_id=_tag_text(
                        row["meeting_external_id"],
                        "meeting_tags",
                        "meeting_external_id",
                        "tag assignment list",
                    ),
                ),
                tag=Tag(
                    normalized_name=_tag_text(
                        row["normalized_name"],
                        "manual_tags",
                        "normalized_name",
                        "tag assignment list",
                    ),
                    display_name=_tag_text(
                        row["display_name"],
                        "manual_tags",
                        "display_name",
                        "tag assignment list",
                    ),
                ),
            )
            for row in rows
        )

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        physical_path = self.state_path.resolve(strict=True)
        mode = "ro" if self.read_only else "rw"
        connection = sqlite3.connect(f"{physical_path.as_uri()}?mode={mode}", uri=True)
        with closing(connection) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            if self.read_only:
                conn.execute("PRAGMA query_only=ON")
            validate_existing_user_state_schema(conn)
            yield conn


def _tag_text(value: object, table: str, column: str, context: str) -> str:
    return decode_required_text(
        value,
        table=table,
        column=column,
        context=context,
        error_type=StateSchemaError,
    )


def _tag_integer(value: object, table: str, column: str, context: str) -> int:
    return decode_required_integer(
        value,
        table=table,
        column=column,
        context=context,
        error_type=StateSchemaError,
    )


def normalize_tag_name(value: str) -> str:
    return " ".join(value.casefold().split())


def display_tag_name(value: str) -> str:
    return " ".join(value.split())


def normalize_tags(values: tuple[str, ...]) -> tuple[Tag, ...]:
    tags: list[Tag] = []
    seen: set[str] = set()
    for value in values:
        normalized_name = normalize_tag_name(value)
        if not normalized_name or normalized_name in seen:
            continue
        seen.add(normalized_name)
        tags.append(Tag(normalized_name, display_tag_name(value)))
    return tuple(tags)


class TagService:
    def __init__(self, index_repository: IndexRepository) -> None:
        self.index_repository = index_repository
        self.repository = (
            TagRepository.open_existing(index_repository.state_path)
            if index_repository.read_only
            else TagRepository(index_repository.state_path)
        )

    def assign(
        self,
        meeting_refs: tuple[MeetingRef, ...],
        tag_names: tuple[str, ...],
    ) -> TagMutationResult:
        self._require_meetings(meeting_refs)
        self._require_tags(tag_names)
        return self.repository.assign_many(
            meeting_refs,
            tag_names,
            now=self.index_repository.utc_now(),
        )

    def remove(
        self,
        meeting_refs: tuple[MeetingRef, ...],
        tag_names: tuple[str, ...],
    ) -> TagMutationResult:
        self._require_meetings(meeting_refs)
        self._require_tags(tag_names)
        return self.repository.remove_many(meeting_refs, tag_names)

    def list_for_meeting(self, identity: MeetingRef) -> tuple[Tag, ...]:
        self._require_meetings((identity,))
        return self.repository.list_for_meeting(
            identity.source_uuid,
            identity.external_id,
        )

    def list_all(self) -> tuple[TagCount, ...]:
        assignments = self.repository.list_assignments()
        active_refs = self._active_assignment_refs(assignments)
        counts: dict[Tag, int] = {}
        for assignment in assignments:
            if assignment.identity not in active_refs:
                continue
            counts[assignment.tag] = counts.get(assignment.tag, 0) + 1
        return tuple(
            TagCount(tag.normalized_name, tag.display_name, count) for tag, count in counts.items()
        )

    def orphaned_assignment_count(self) -> int:
        assignments = self.repository.list_assignments()
        active_refs = self._active_assignment_refs(assignments)
        return sum(assignment.identity not in active_refs for assignment in assignments)

    def suggest(self, identity: MeetingRef) -> tuple[TagSuggestion, ...]:
        meeting = self.index_repository.get_meeting_by_ref(identity)
        if meeting is None:
            message = f"Meeting not found: {identity.source_uuid}/{identity.external_id}"
            raise ValueError(message)
        assigned = {
            tag.normalized_name
            for tag in self.repository.list_for_meeting(
                identity.source_uuid,
                identity.external_id,
            )
        }
        active_tags = tuple(
            Tag(item.normalized_name, item.display_name)
            for item in self.list_all()
            if item.normalized_name not in assigned
        )
        title = decode_required_text(
            meeting["title"],
            table="meetings",
            column="title",
            context="tag suggestion meeting",
            error_type=IndexReadError,
        )
        text = self.index_repository.meeting_transcript_text(identity)
        suggestions: list[TagSuggestion] = []
        self._append_text_suggestions(
            suggestions,
            active_tags,
            title,
            "title match",
        )
        remaining = tuple(
            tag
            for tag in active_tags
            if tag.normalized_name not in {item.tag.normalized_name for item in suggestions}
        )
        self._append_text_suggestions(
            suggestions,
            remaining,
            text,
            "text match",
        )
        return tuple(suggestions)

    def _append_text_suggestions(
        self,
        suggestions: list[TagSuggestion],
        tags: tuple[Tag, ...],
        text: str,
        reason: str,
    ) -> None:
        for tag in tags:
            if len(suggestions) >= TAG_SUGGESTION_LIMIT:
                return
            if normalized_phrase_in_text(tag.normalized_name, text):
                suggestions.append(TagSuggestion(tag, reason))

    def _active_assignment_refs(
        self,
        assignments: tuple[TagAssignment, ...],
    ) -> frozenset[MeetingRef]:
        refs = tuple(dict.fromkeys(assignment.identity for assignment in assignments))
        return frozenset(self.index_repository.get_meetings_by_refs(refs))

    def _require_meetings(self, meeting_refs: tuple[MeetingRef, ...]) -> None:
        if not meeting_refs:
            message = "No meeting references provided."
            raise ValueError(message)
        meetings = self.index_repository.get_meetings_by_refs(meeting_refs)
        missing = tuple(ref for ref in meeting_refs if ref not in meetings)
        if missing:
            labels = ", ".join(f"{ref.source_uuid}/{ref.external_id}" for ref in missing)
            message = f"Meetings not found: {labels}"
            raise ValueError(message)

    def _require_tags(self, tag_names: tuple[str, ...]) -> None:
        if normalize_tags(tag_names):
            return
        message = "No tags provided."
        raise ValueError(message)


def normalized_phrase_in_text(normalized_phrase: str, text: str) -> bool:
    normalized_text = " ".join(text.casefold().split())
    pattern = rf"(?<!\w){re.escape(normalized_phrase)}(?!\w)"
    return re.search(pattern, normalized_text) is not None
