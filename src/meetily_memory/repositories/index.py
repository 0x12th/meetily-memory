import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from meetily_memory.db.fts import build_fts_query
from meetily_memory.db.rows import rows_to_dicts
from meetily_memory.db.schema import index_connection
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

    def __init__(self, index_path: Path) -> None:
        self.index_path = Path(index_path)
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
        return rows_to_dicts(rows)

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
    ) -> list[dict[str, Any]]:
        return self.search_repo.search(query, limit, meeting_id=meeting_id)

    def list_meetings(self, limit: int = 20, person: str | None = None) -> list[dict[str, Any]]:
        return self.meetings.list_meetings(limit, person)

    def get_meeting(self, external_or_internal_id: str) -> dict[str, Any] | None:
        return self.meetings.get_meeting(external_or_internal_id)

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


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
