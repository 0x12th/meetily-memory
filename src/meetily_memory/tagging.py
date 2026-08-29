from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from meetily_memory.domain import MeetingRef
from meetily_memory.user_state import ensure_user_state_schema

if TYPE_CHECKING:
    from collections.abc import Iterator

    from meetily_memory.repositories.index import IndexRepository
    from meetily_memory.semantic_search import EmbeddingProvider

TAG_SUGGESTION_LIMIT = 5
SEMANTIC_SUGGESTION_CANDIDATES = 50
SEMANTIC_SUGGESTION_QUERY_LENGTH = 8_000


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

    @property
    def source_uuid(self) -> str:
        return self.meeting_ref.source_uuid

    @property
    def meeting_external_id(self) -> str:
        return self.meeting_ref.external_id


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
    def __init__(self, state_path: Path) -> None:
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            ensure_user_state_schema(conn)

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
                    INSERT OR IGNORE INTO tags (
                      normalized_name, display_name, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (tag.normalized_name, tag.display_name, now),
                )
                stored = conn.execute(
                    """
                    SELECT id, display_name
                    FROM tags
                    WHERE normalized_name = ?
                    """,
                    (tag.normalized_name,),
                ).fetchone()
                tag_id = int(stored["id"])
                tag_added_links = 0
                for identity in identities:
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO meeting_tags (
                          source_uuid, meeting_external_id, tag_id, source, created_at
                        ) VALUES (?, ?, ?, 'manual', ?)
                        """,
                        (
                            identity.source_uuid,
                            identity.meeting_external_id,
                            tag_id,
                            now,
                        ),
                    )
                    added_links += cursor.rowcount
                    tag_added_links += cursor.rowcount
                display_name = str(stored["display_name"])
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
                    "SELECT id FROM tags WHERE normalized_name = ?",
                    (tag.normalized_name,),
                ).fetchone()
                if row is None:
                    missing_tags.append(tag.display_name)
                    continue
                tag_id = int(row["id"])
                tag_removed_links = 0
                for identity in identities:
                    cursor = conn.execute(
                        """
                        DELETE FROM meeting_tags
                        WHERE source_uuid = ?
                          AND meeting_external_id = ?
                          AND tag_id = ?
                        """,
                        (
                            identity.source_uuid,
                            identity.meeting_external_id,
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
                    DELETE FROM tags
                    WHERE id = ?
                      AND NOT EXISTS (
                        SELECT 1 FROM meeting_tags WHERE meeting_tags.tag_id = tags.id
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
                JOIN tags t ON t.id = mt.tag_id
                WHERE mt.source_uuid = ? AND mt.meeting_external_id = ?
                ORDER BY t.id
                """,
                (source_uuid, meeting_external_id),
            ).fetchall()
        return tuple(Tag(str(row["normalized_name"]), str(row["display_name"])) for row in rows)

    def search(self, query: str) -> tuple[TagMatch, ...]:
        normalized_query = normalize_tag_name(query)
        if not normalized_query:
            return ()
        query_tokens = set(normalized_query.split())
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  mt.source_uuid,
                  mt.meeting_external_id,
                  t.normalized_name,
                  t.display_name
                FROM meeting_tags mt
                JOIN tags t ON t.id = mt.tag_id
                ORDER BY mt.meeting_external_id, t.normalized_name
                """
            ).fetchall()
        matches: list[TagMatch] = []
        for row in rows:
            normalized_name = str(row["normalized_name"])
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
                        source_uuid=str(row["source_uuid"]),
                        external_id=str(row["meeting_external_id"]),
                    ),
                    tag=Tag(normalized_name, str(row["display_name"])),
                    kind=kind,
                )
            )
        return tuple(
            sorted(
                matches,
                key=lambda match: (match.kind != "exact", match.meeting_external_id),
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
                JOIN tags t ON t.id = mt.tag_id
                ORDER BY t.id, mt.source_uuid, mt.meeting_external_id
                """
            ).fetchall()
        return tuple(
            TagAssignment(
                identity=MeetingRef(
                    source_uuid=str(row["source_uuid"]),
                    external_id=str(row["meeting_external_id"]),
                ),
                tag=Tag(
                    normalized_name=str(row["normalized_name"]),
                    display_name=str(row["display_name"]),
                ),
            )
            for row in rows
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.state_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


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
        self.repository = TagRepository(index_repository.state_path)

    def assign(
        self,
        meeting_ids: tuple[str, ...],
        tag_names: tuple[str, ...],
    ) -> TagMutationResult:
        identities = self._resolve_meetings(meeting_ids)
        self._require_tags(tag_names)
        return self.repository.assign_many(
            identities,
            tag_names,
            now=self.index_repository.utc_now(),
        )

    def remove(
        self,
        meeting_ids: tuple[str, ...],
        tag_names: tuple[str, ...],
    ) -> TagMutationResult:
        identities = self._resolve_meetings(meeting_ids)
        self._require_tags(tag_names)
        return self.repository.remove_many(identities, tag_names)

    def list_for_meeting(self, meeting_id: str) -> tuple[Tag, ...]:
        identity = self._resolve_meetings((meeting_id,))[0]
        return self.repository.list_for_meeting(
            identity.source_uuid,
            identity.meeting_external_id,
        )

    def list_all(self) -> tuple[TagCount, ...]:
        counts: dict[Tag, int] = {}
        for assignment in self.repository.list_assignments():
            if not self._assignment_is_active(assignment):
                continue
            counts[assignment.tag] = counts.get(assignment.tag, 0) + 1
        return tuple(
            TagCount(tag.normalized_name, tag.display_name, count) for tag, count in counts.items()
        )

    def orphaned_assignment_count(self) -> int:
        return sum(
            not self._assignment_is_active(assignment)
            for assignment in self.repository.list_assignments()
        )

    def suggest(
        self,
        meeting_id: str,
        *,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> tuple[TagSuggestion, ...]:
        identity = self._resolve_meetings((meeting_id,))[0]
        meeting = self.index_repository.get_meeting_by_ref(identity)
        if meeting is None:
            message = f"Meeting not found: {meeting_id}"
            raise ValueError(message)
        assigned = {
            tag.normalized_name
            for tag in self.repository.list_for_meeting(
                identity.source_uuid,
                identity.meeting_external_id,
            )
        }
        active_tags = tuple(
            Tag(item.normalized_name, item.display_name)
            for item in self.list_all()
            if item.normalized_name not in assigned
        )
        title = str(meeting["title"])
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
        if len(suggestions) >= TAG_SUGGESTION_LIMIT:
            return tuple(suggestions)
        if embedding_provider is None:
            return tuple(suggestions)
        return self._append_semantic_suggestions(
            suggestions,
            identity,
            f"{title}\n{text}",
            embedding_provider,
        )

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

    def _append_semantic_suggestions(
        self,
        suggestions: list[TagSuggestion],
        identity: MeetingRef,
        document: str,
        provider: EmbeddingProvider,
    ) -> tuple[TagSuggestion, ...]:
        from meetily_memory.semantic_search import semantic_index_coverage  # noqa: PLC0415

        try:
            coverage = semantic_index_coverage(
                self.index_repository.index_path,
                provider,
            )
        except (RuntimeError, sqlite3.Error):
            return tuple(suggestions)
        if not coverage.complete:
            return tuple(suggestions)
        source = self._semantic_suggestion_source(identity, document, provider)
        if source is None:
            return tuple(suggestions)
        similar_meeting_id, tags = source
        assigned = {
            tag.normalized_name
            for tag in self.repository.list_for_meeting(
                identity.source_uuid,
                identity.meeting_external_id,
            )
        }
        suggested = {item.tag.normalized_name for item in suggestions}
        for tag in tags:
            if len(suggestions) >= TAG_SUGGESTION_LIMIT:
                break
            if tag.normalized_name in assigned or tag.normalized_name in suggested:
                continue
            suggestions.append(
                TagSuggestion(
                    tag=tag,
                    reason="similar meeting",
                    similar_meeting_id=similar_meeting_id,
                )
            )
            suggested.add(tag.normalized_name)
        return tuple(suggestions)

    def _semantic_suggestion_source(
        self,
        identity: MeetingRef,
        document: str,
        provider: EmbeddingProvider,
    ) -> tuple[int, tuple[Tag, ...]] | None:
        target_ref = identity
        try:
            batches = self._semantic_candidate_batches(document, provider)
            for rows in batches:
                for row in rows:
                    hit = self.index_repository.search_hit_from_row(row)
                    candidate_ref = self.index_repository.source_identity_for_meeting(
                        hit.meeting.id
                    )
                    if candidate_ref == target_ref:
                        continue
                    tags = self.repository.list_for_meeting(
                        candidate_ref.source_uuid,
                        candidate_ref.external_id,
                    )
                    resolved = self.index_repository.meeting_by_ref(candidate_ref)
                    if tags and resolved is not None:
                        return resolved[0], tags
        except (RuntimeError, sqlite3.Error):
            return None
        return None

    def _semantic_candidate_batches(
        self,
        document: str,
        provider: EmbeddingProvider,
    ) -> Iterator[list[dict[str, object]]]:
        from meetily_memory.semantic_search import semantic_search  # noqa: PLC0415

        candidate_limit = SEMANTIC_SUGGESTION_CANDIDATES
        while True:
            rows = semantic_search(
                self.index_repository.index_path,
                document[:SEMANTIC_SUGGESTION_QUERY_LENGTH],
                candidate_limit,
                embedding_provider=provider,
            )
            yield rows
            if len(rows) < candidate_limit:
                return
            candidate_limit *= 2

    def _assignment_is_active(self, assignment: TagAssignment) -> bool:
        return self.index_repository.get_meeting_by_ref(assignment.identity) is not None

    def _resolve_meetings(
        self,
        meeting_ids: tuple[str, ...],
    ) -> tuple[MeetingRef, ...]:
        if not meeting_ids:
            message = "No meeting IDs provided."
            raise ValueError(message)
        resolved: list[MeetingRef] = []
        missing: list[str] = []
        for meeting_id in meeting_ids:
            try:
                local_id = int(meeting_id)
            except ValueError:
                missing.append(meeting_id)
                continue
            identity = self.index_repository.meeting_ref_for_local_id(local_id)
            if identity is None:
                missing.append(meeting_id)
                continue
            resolved.append(identity)
        if missing:
            message = f"Meetings not found: {', '.join(missing)}"
            raise ValueError(message)
        return tuple(resolved)

    def _require_tags(self, tag_names: tuple[str, ...]) -> None:
        if normalize_tags(tag_names):
            return
        message = "No tags provided."
        raise ValueError(message)


def normalized_phrase_in_text(normalized_phrase: str, text: str) -> bool:
    normalized_text = " ".join(text.casefold().split())
    pattern = rf"(?<!\w){re.escape(normalized_phrase)}(?!\w)"
    return re.search(pattern, normalized_text) is not None
