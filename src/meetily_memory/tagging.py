import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from meetily_memory.user_state import ensure_user_state_schema

if TYPE_CHECKING:
    from meetily_memory.repositories.index import IndexRepository


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
    source_uuid: str
    meeting_external_id: str
    tag: Tag
    kind: str


@dataclass(frozen=True)
class MeetingTagIdentity:
    source_uuid: str
    meeting_external_id: str


@dataclass(frozen=True)
class TagAssignment:
    identity: MeetingTagIdentity
    tag: Tag


@dataclass(frozen=True)
class TagCount:
    normalized_name: str
    display_name: str
    active_meetings: int


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
            MeetingTagIdentity(source_uuid, meeting_external_id)
            for meeting_external_id in meeting_external_ids
        )
        return self.assign_many(identities, tag_names, now=now)

    def assign_many(
        self,
        identities: tuple[MeetingTagIdentity, ...],
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
            MeetingTagIdentity(source_uuid, meeting_external_id)
            for meeting_external_id in meeting_external_ids
        )
        return self.remove_many(identities, tag_names)

    def remove_many(
        self,
        identities: tuple[MeetingTagIdentity, ...],
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
                    source_uuid=str(row["source_uuid"]),
                    meeting_external_id=str(row["meeting_external_id"]),
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
                identity=MeetingTagIdentity(
                    source_uuid=str(row["source_uuid"]),
                    meeting_external_id=str(row["meeting_external_id"]),
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
    def __init__(self, index_repository: "IndexRepository") -> None:
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

    def _assignment_is_active(self, assignment: TagAssignment) -> bool:
        return (
            self.index_repository.get_meeting_by_identity(
                assignment.identity.source_uuid,
                assignment.identity.meeting_external_id,
            )
            is not None
        )

    def _resolve_meetings(
        self,
        meeting_ids: tuple[str, ...],
    ) -> tuple[MeetingTagIdentity, ...]:
        if not meeting_ids:
            message = "No meeting IDs provided."
            raise ValueError(message)
        resolved: list[MeetingTagIdentity] = []
        missing: list[str] = []
        for meeting_id in meeting_ids:
            identity = self.index_repository.meeting_source_identity(meeting_id)
            if identity is None:
                missing.append(meeting_id)
                continue
            resolved.append(
                MeetingTagIdentity(
                    source_uuid=str(identity["source_uuid"]),
                    meeting_external_id=str(identity["meeting_external_id"]),
                )
            )
        if missing:
            message = f"Meetings not found: {', '.join(missing)}"
            raise ValueError(message)
        return tuple(resolved)

    def _require_tags(self, tag_names: tuple[str, ...]) -> None:
        if normalize_tags(tag_names):
            return
        message = "No tags provided."
        raise ValueError(message)
