import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from meetily_memory.db.fts import build_fts_query
from meetily_memory.db.rows import rows_to_dicts
from meetily_memory.db.schema import index_connection
from meetily_memory.domain import (
    MeetingRef,
    MemoryEntity,
    SearchHit,
    SourceExcerpt,
    canonical_entity_kind,
    stable_evidence_id,
)
from meetily_memory.meeting_structure import ENTITY_KINDS, StructuredEntity
from meetily_memory.memory.entities import (
    ENTITY_DETAIL_SQL,
    ENTITY_INSERT_SQL,
    ENTITY_NODE_TYPES,
    ENTITY_SELECT_SQL,
    StructuredEntityContext,
    StructuredEntityRepository,
    assert_known_entity_kind,
    assert_known_task_status,
    assert_known_task_status_filter,
    structured_entity_sort_key,
)
from meetily_memory.memory.knowledge import KnowledgeContext, KnowledgeRepository
from meetily_memory.memory.task_status import TaskStatusContext, TaskStatusRepository
from meetily_memory.repositories.meetings import MeetingsContext, MeetingsRepository
from meetily_memory.repositories.records import ChunkRecord, MeetingRecord, ScanRunStats
from meetily_memory.repositories.search import SearchRepository
from meetily_memory.user_state import (
    UserStateRepository,
    prepare_user_state_migration,
    task_identity,
)

__all__ = [
    "ChunkRecord",
    "IndexRepository",
    "MeetingRecord",
    "ScanRunStats",
    "build_fts_query",
]


class IndexRepository:
    entity_insert_sql = ENTITY_INSERT_SQL
    entity_select_sql = ENTITY_SELECT_SQL
    entity_node_types = ENTITY_NODE_TYPES

    def __init__(self, index_path: Path, *, state_path: Path | None = None) -> None:
        self.index_path = Path(index_path)
        self.state_path = (
            Path(state_path) if state_path else self.index_path.with_name("state.sqlite")
        )
        self.user_state = UserStateRepository(self.state_path)
        prepare_user_state_migration(
            self.index_path,
            self.user_state,
            now=utc_now(),
        )
        with index_connection(self.index_path):
            pass
        self.meetings = MeetingsRepository(
            MeetingsContext(
                index_path=self.index_path,
                sync_meeting_knowledge=self._sync_meeting_knowledge,
                delete_meeting_knowledge=self._delete_meeting_knowledge,
            )
        )
        self.search_repo = SearchRepository(self.index_path)
        self.entities = StructuredEntityRepository(
            StructuredEntityContext(
                index_path=self.index_path,
                delete_structured_knowledge=self._delete_structured_knowledge,
                delete_structured_entities=self._delete_structured_entities,
                sync_meeting_knowledge=self._sync_meeting_knowledge,
                list_entity_details=self._list_structured_entity_details_conn,
                list_all_entity_details=self._list_all_structured_entity_details_conn,
            )
        )
        self.knowledge = KnowledgeRepository(
            KnowledgeContext(
                index_path=self.index_path,
                search_meetings=self.search,
                chunk_rows=self.meetings.chunk_rows,
                meeting_people_rows=self.meetings.meeting_people_rows,
                structured_entity_rows=self.meetings.structured_entity_rows,
                all_structured_entity_details=self._list_all_structured_entity_details_conn,
                now=utc_now,
            )
        )
        self.task_status = TaskStatusRepository(
            TaskStatusContext(
                index_path=self.index_path,
                user_state=self.user_state,
                validate_status=assert_known_task_status,
                now=utc_now,
            )
        )

    def structured_entity_sort_key(self, row: dict[str, Any]) -> tuple[str, int]:
        return structured_entity_sort_key(row)

    def assert_known_entity_kind(self, kind: str) -> None:
        assert_known_entity_kind(kind)

    def assert_known_task_status(self, status: str) -> None:
        assert_known_task_status(status)

    def assert_known_task_status_filter(self, status: str) -> None:
        assert_known_task_status_filter(status)

    def utc_now(self) -> str:
        return utc_now()

    def rows_to_dicts(self, rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        return rows_to_dicts(rows)

    def upsert_source(self, kind: str, path: str, now: str, label: str | None = None) -> int:
        self.user_state.get_or_create_source(kind, path, now=now)
        return self.meetings.upsert_source(kind, path, now, label)

    def get_source(self, kind: str, path: str) -> dict[str, Any] | None:
        return self.meetings.get_source(kind, path)

    def get_meeting_by_external_id(self, source_id: int, external_id: str) -> dict[str, Any] | None:
        return self.meetings.get_meeting_by_external_id(source_id, external_id)

    def upsert_meeting_with_chunks(
        self,
        meeting: MeetingRecord,
        chunks: Iterable[ChunkRecord],
        *,
        force: bool = False,
    ) -> tuple[int, bool, int]:
        return self.meetings.upsert_meeting_with_chunks(meeting, chunks, force=force)

    def _delete_structured_entities(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        self.meetings.delete_structured_entities(conn, meeting_id)

    def _delete_meeting_knowledge(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        self.knowledge.delete_meeting_knowledge(conn, meeting_id)

    def _delete_structured_knowledge(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        self.knowledge.delete_structured_knowledge(conn, meeting_id)

    def _sync_meeting_knowledge(
        self,
        conn: sqlite3.Connection,
        meeting_id: int,
        now: str,
    ) -> None:
        self.knowledge.sync_meeting_knowledge(conn, meeting_id, now)

    def get_chunks_for_meeting(self, meeting_id: int) -> list[dict[str, Any]]:
        return self.meetings.get_chunks_for_meeting(meeting_id)

    def replace_structured_entities(
        self,
        meeting_id: int,
        entities: Iterable[StructuredEntity],
        now: str,
    ) -> dict[str, int]:
        return self.entities.replace_structured_entities(meeting_id, entities, now)

    def list_meeting_ids(self) -> list[int]:
        return self.meetings.list_meeting_ids()

    def list_structured_entities(
        self,
        meeting_id: int,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.entities.list_structured_entities(meeting_id, kind)

    def list_structured_entity_details(
        self,
        kind: str,
        limit: int = 20,
        *,
        status: str = "all",
    ) -> list[dict[str, Any]]:
        return self.entities.list_structured_entity_details(kind, limit, status=status)

    def list_all_structured_entity_details(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.entities.list_all_structured_entity_details(limit)

    def set_task_status(
        self,
        action_item_id: int,
        status: str,
        *,
        note: str | None,
        now: str | None = None,
    ) -> dict[str, Any]:
        return self.task_status.set_task_status(action_item_id, status, note=note, now=now)

    def ensure_topic(
        self,
        title: str,
        *,
        aliases: Iterable[str] = (),
    ) -> dict[str, Any]:
        return self.knowledge.ensure_topic(title, aliases=aliases)

    def topic_memory(self, title: str, limit: int = 10) -> dict[str, Any]:
        return self.knowledge.topic_memory(title, limit)

    def list_topics(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.knowledge.list_topics(limit)

    def graph_for_topic(self, title: str, limit: int = 50) -> dict[str, Any]:
        return self.knowledge.graph_for_topic(title, limit)

    def _list_structured_entity_details_conn(
        self,
        conn: sqlite3.Connection,
        kind: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(ENTITY_DETAIL_SQL[kind], (limit,)).fetchall()
        details = rows_to_dicts(rows)
        if kind == "action_items":
            for row in details:
                self._hydrate_task_status(row)
        return details

    def _list_all_structured_entity_details_conn(
        self,
        conn: sqlite3.Connection,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for kind in ENTITY_KINDS:
            rows.extend(self._list_structured_entity_details_conn(conn, kind, limit))
        return rows

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        meeting_id: int | None = None,
        context: int = 0,
    ) -> list[dict[str, Any]]:
        return self.search_repo.search(query, limit, meeting_id=meeting_id, context=context)

    def search_hits(
        self,
        query: str,
        limit: int = 10,
        *,
        meeting_id: int | None = None,
        context: int = 0,
    ) -> tuple[SearchHit, ...]:
        rows = self.search(query, limit, meeting_id=meeting_id, context=context)
        return tuple(self.search_hit_from_row(row) for row in rows)

    def search_hit_from_row(self, row: dict[str, Any]) -> SearchHit:
        source = self._source_details(int(row["meeting_id"]))
        source_uuid = self.user_state.source_uuid(
            str(source["kind"]),
            str(source["path"]),
            now=utc_now(),
        )
        excerpt = source_excerpt_from_search_row(row)
        domain_row = {**row, "source_path": source["path"]}
        return SearchHit(
            id=stable_evidence_id(
                source_uuid,
                excerpt.meeting_external_id,
                excerpt.chunk_external_id,
                kind=excerpt.kind,
                ordinal=excerpt.ordinal,
                text=excerpt.text,
            ),
            meeting=meeting_ref_from_row(domain_row, source_uuid),
            excerpt=excerpt,
            is_context=bool(row.get("is_context", False)),
        )

    def get_search_hit(self, evidence_id: str) -> SearchHit | None:
        for row in self.search_repo.all_evidence_rows():
            hit = self.search_hit_from_row(row)
            if hit.id == evidence_id:
                return hit
        return None

    def memory_entities_for_hits(self, hits: tuple[SearchHit, ...]) -> tuple[MemoryEntity, ...]:
        evidence_ids = {hit.id for hit in hits}
        entities: list[MemoryEntity] = []
        for row in self.list_all_structured_entity_details(limit=10_000):
            evidence_row = self._entity_evidence_details(int(row["source_chunk_id"]))
            source_uuid = self.user_state.source_uuid(
                str(evidence_row["source_kind"]),
                str(evidence_row["source_path"]),
                now=utc_now(),
            )
            excerpt = source_excerpt_from_entity_row(evidence_row)
            evidence_id = stable_evidence_id(
                source_uuid,
                excerpt.meeting_external_id,
                excerpt.chunk_external_id,
                kind=excerpt.kind,
                ordinal=excerpt.ordinal,
                text=excerpt.text,
            )
            if evidence_id not in evidence_ids:
                continue
            entities.append(
                MemoryEntity(
                    kind=canonical_entity_kind(str(row["kind"])),
                    content=str(row["text"]),
                    source=excerpt,
                    evidence_id=evidence_id,
                    extraction_method=str(row["source"]),
                )
            )
        return tuple(entities)

    def list_meetings(self, limit: int = 20, person: str | None = None) -> list[dict[str, Any]]:
        return self.meetings.list_meetings(limit, person)

    def get_meeting(self, external_or_internal_id: str) -> dict[str, Any] | None:
        return self.meetings.get_meeting(external_or_internal_id)

    def dominant_meeting_language(self) -> str | None:
        return self.meetings.dominant_meeting_language()

    def record_scan_run(
        self,
        source_id: int,
        started_at: str,
        finished_at: str,
        result: ScanRunStats,
    ) -> None:
        self.meetings.record_scan_run(source_id, started_at, finished_at, result)

    def stats(self) -> dict[str, int]:
        return self.meetings.stats()

    def _hydrate_task_status(self, row: dict[str, Any]) -> None:
        chunk_external_id = row.get("chunk_external_id")
        if not chunk_external_id:
            return
        source = self._source_details(int(row["meeting_id"]))
        source_uuid = self.user_state.source_uuid(
            str(source["kind"]),
            str(source["path"]),
            now=utc_now(),
        )
        state = self.user_state.get_task_state(
            task_identity(
                source_uuid,
                str(row["meeting_external_id"]),
                str(chunk_external_id),
                str(row["text"]),
            )
        )
        if state is None:
            return
        row["status"] = state["status"]
        row["status_note"] = state["note"]
        row["status_source"] = state["source"]
        row["status_updated_at"] = state["updated_at"]

    def _source_details(self, meeting_id: int) -> dict[str, Any]:
        with index_connection(self.index_path) as conn:
            row = conn.execute(
                """
                SELECT s.kind, s.path
                FROM meetings m
                JOIN sources s ON s.id = m.source_id
                WHERE m.id = ?
                """,
                (meeting_id,),
            ).fetchone()
            if row is None:
                message = f"Source not found for meeting: {meeting_id}"
                raise ValueError(message)
            return dict(row)

    def _entity_evidence_details(self, source_chunk_id: int) -> dict[str, Any]:
        with index_connection(self.index_path) as conn:
            row = conn.execute(
                """
                SELECT
                  m.external_id AS meeting_external_id,
                  c.external_id AS chunk_external_id,
                  c.kind AS chunk_kind,
                  c.ordinal AS chunk_ordinal,
                  c.text AS chunk_text,
                  c.speaker AS chunk_speaker,
                  c.starts_at_seconds AS chunk_starts_at_seconds,
                  c.ends_at_seconds AS chunk_ends_at_seconds,
                  c.timestamp_label AS chunk_timestamp_label,
                  s.kind AS source_kind,
                  s.path AS source_path
                FROM chunks c
                JOIN meetings m ON m.id = c.meeting_id
                JOIN sources s ON s.id = m.source_id
                WHERE c.id = ?
                """,
                (source_chunk_id,),
            ).fetchone()
            if row is None:
                message = f"Source chunk not found: {source_chunk_id}"
                raise ValueError(message)
            return dict(row)


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def meeting_ref_from_row(row: dict[str, Any], source_uuid: str) -> MeetingRef:
    return MeetingRef(
        source_uuid=source_uuid,
        external_id=str(row["meeting_external_id"]),
        title=str(row["title"]),
        source_path=str(row["source_path"]),
        created_at=optional_str(row.get("created_at")),
        updated_at=optional_str(row.get("updated_at")),
        folder_path=optional_str(row.get("folder_path")),
        language=optional_str(row.get("language")),
    )


def source_excerpt_from_search_row(row: dict[str, Any]) -> SourceExcerpt:
    return SourceExcerpt(
        meeting_external_id=str(row["meeting_external_id"]),
        chunk_external_id=optional_str(row.get("chunk_external_id")),
        kind=str(row["kind"]),
        ordinal=int(row["ordinal"]),
        text=str(row["text"]),
        speaker=optional_str(row.get("speaker")),
        starts_at_seconds=optional_float(row.get("starts_at_seconds")),
        ends_at_seconds=optional_float(row.get("ends_at_seconds")),
        timestamp_label=optional_str(row.get("timestamp_label")),
    )


def source_excerpt_from_entity_row(row: dict[str, Any]) -> SourceExcerpt:
    return SourceExcerpt(
        meeting_external_id=str(row["meeting_external_id"]),
        chunk_external_id=optional_str(row.get("chunk_external_id")),
        kind=str(row["chunk_kind"]),
        ordinal=int(row["chunk_ordinal"]),
        text=str(row["chunk_text"]),
        speaker=optional_str(row.get("chunk_speaker")),
        starts_at_seconds=optional_float(row.get("chunk_starts_at_seconds")),
        ends_at_seconds=optional_float(row.get("chunk_ends_at_seconds")),
        timestamp_label=optional_str(row.get("chunk_timestamp_label")),
    )


def optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


def optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
