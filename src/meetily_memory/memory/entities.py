from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meetily_memory.db.schema import index_connection
from meetily_memory.meeting_structure import ENTITY_KINDS, StructuredEntity, empty_entity_counts

DeleteKnowledge = Callable[[Any, int], None]
DeleteStructuredEntities = Callable[[Any, int], None]
SyncKnowledge = Callable[[Any, int, str], None]
EntityDetails = Callable[[Any, str, int], list[dict[str, Any]]]
AllEntityDetails = Callable[[Any, int], list[dict[str, Any]]]


@dataclass(frozen=True)
class StructuredEntityContext:
    index_path: Path
    delete_structured_knowledge: DeleteKnowledge
    delete_structured_entities: DeleteStructuredEntities
    sync_meeting_knowledge: SyncKnowledge
    list_entity_details: EntityDetails
    list_all_entity_details: AllEntityDetails


ENTITY_INSERT_SQL = {
    "decisions": """
        INSERT INTO decisions (
          meeting_id, source_chunk_id, ordinal, text, source, confidence,
          fingerprint, created_at, updated_at, raw_metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    "action_items": """
        INSERT INTO action_items (
          meeting_id, source_chunk_id, ordinal, text, source, confidence,
          fingerprint, created_at, updated_at, raw_metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    "risks": """
        INSERT INTO risks (
          meeting_id, source_chunk_id, ordinal, text, source, confidence,
          fingerprint, created_at, updated_at, raw_metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    "open_questions": """
        INSERT INTO open_questions (
          meeting_id, source_chunk_id, ordinal, text, source, confidence,
          fingerprint, created_at, updated_at, raw_metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
}
ENTITY_DELETE_SQL = {
    "decisions": "DELETE FROM decisions WHERE meeting_id = ?",
    "action_items": "DELETE FROM action_items WHERE meeting_id = ?",
    "risks": "DELETE FROM risks WHERE meeting_id = ?",
    "open_questions": "DELETE FROM open_questions WHERE meeting_id = ?",
}
ENTITY_SELECT_SQL = {
    "decisions": "SELECT * FROM decisions WHERE meeting_id = ? ORDER BY ordinal",
    "action_items": "SELECT * FROM action_items WHERE meeting_id = ? ORDER BY ordinal",
    "risks": "SELECT * FROM risks WHERE meeting_id = ? ORDER BY ordinal",
    "open_questions": "SELECT * FROM open_questions WHERE meeting_id = ? ORDER BY ordinal",
}
ENTITY_COUNT_SQL = {
    "decisions": "SELECT COUNT(*) FROM decisions",
    "action_items": "SELECT COUNT(*) FROM action_items",
    "risks": "SELECT COUNT(*) FROM risks",
    "open_questions": "SELECT COUNT(*) FROM open_questions",
}
ENTITY_DETAIL_SQL = {
    "decisions": """
        SELECT
          'decisions' AS kind,
          e.*,
          m.external_id AS meeting_external_id,
          m.title AS meeting_title,
          m.language AS meeting_language,
          COALESCE(m.updated_at, m.created_at, m.indexed_at) AS meeting_date,
          c.external_id AS chunk_external_id,
          c.kind AS chunk_kind,
          c.speaker AS chunk_speaker,
          c.timestamp_label AS chunk_timestamp_label
        FROM decisions e
        JOIN meetings m ON m.id = e.meeting_id
        JOIN chunks c ON c.id = e.source_chunk_id
        ORDER BY meeting_date DESC, e.ordinal ASC
        LIMIT ?
    """,
    "action_items": """
        SELECT
          'action_items' AS kind,
          e.*,
          'open' AS status,
          NULL AS status_note,
          NULL AS status_source,
          NULL AS status_updated_at,
          m.external_id AS meeting_external_id,
          m.title AS meeting_title,
          m.language AS meeting_language,
          COALESCE(m.updated_at, m.created_at, m.indexed_at) AS meeting_date,
          c.external_id AS chunk_external_id,
          c.kind AS chunk_kind,
          c.speaker AS chunk_speaker,
          c.timestamp_label AS chunk_timestamp_label
        FROM action_items e
        JOIN meetings m ON m.id = e.meeting_id
        JOIN chunks c ON c.id = e.source_chunk_id
        ORDER BY meeting_date DESC, e.ordinal ASC
        LIMIT ?
    """,
    "risks": """
        SELECT
          'risks' AS kind,
          e.*,
          m.external_id AS meeting_external_id,
          m.title AS meeting_title,
          m.language AS meeting_language,
          COALESCE(m.updated_at, m.created_at, m.indexed_at) AS meeting_date,
          c.external_id AS chunk_external_id,
          c.kind AS chunk_kind,
          c.speaker AS chunk_speaker,
          c.timestamp_label AS chunk_timestamp_label
        FROM risks e
        JOIN meetings m ON m.id = e.meeting_id
        JOIN chunks c ON c.id = e.source_chunk_id
        ORDER BY meeting_date DESC, e.ordinal ASC
        LIMIT ?
    """,
    "open_questions": """
        SELECT
          'open_questions' AS kind,
          e.*,
          m.external_id AS meeting_external_id,
          m.title AS meeting_title,
          m.language AS meeting_language,
          COALESCE(m.updated_at, m.created_at, m.indexed_at) AS meeting_date,
          c.external_id AS chunk_external_id,
          c.kind AS chunk_kind,
          c.speaker AS chunk_speaker,
          c.timestamp_label AS chunk_timestamp_label
        FROM open_questions e
        JOIN meetings m ON m.id = e.meeting_id
        JOIN chunks c ON c.id = e.source_chunk_id
        ORDER BY meeting_date DESC, e.ordinal ASC
        LIMIT ?
    """,
}
ENTITY_NODE_TYPES = {
    "decisions": "Decision",
    "action_items": "Task",
    "risks": "Risk",
    "open_questions": "Question",
}
TASK_STATUSES = {"open", "done", "cancelled", "unknown"}


def structured_entity_sort_key(row: dict[str, Any]) -> tuple[str, int]:
    return (str(row.get("meeting_date") or ""), -int(row.get("ordinal") or 0))


def assert_known_entity_kind(kind: str) -> None:
    if kind not in ENTITY_KINDS:
        message = f"Unknown structured entity kind: {kind}"
        raise ValueError(message)


def assert_known_task_status(status: str) -> None:
    if status not in TASK_STATUSES:
        message = f"Unknown task status: {status}"
        raise ValueError(message)


def assert_known_task_status_filter(status: str) -> None:
    if status != "all" and status not in TASK_STATUSES:
        message = f"Unknown task status filter: {status}"
        raise ValueError(message)


class StructuredEntityRepository:
    def __init__(self, context: StructuredEntityContext) -> None:
        self.context = context

    def replace_structured_entities(
        self,
        meeting_id: int,
        entities: Iterable[StructuredEntity],
        now: str,
    ) -> dict[str, int]:
        counts = empty_entity_counts()
        with index_connection(self.context.index_path) as conn:
            self.context.delete_structured_knowledge(conn, meeting_id)
            self.context.delete_structured_entities(conn, meeting_id)
            for entity in entities:
                assert_known_entity_kind(entity.kind)
                conn.execute(
                    ENTITY_INSERT_SQL[entity.kind],
                    (
                        meeting_id,
                        entity.source_chunk_id,
                        entity.ordinal,
                        entity.text,
                        entity.source,
                        entity.confidence,
                        entity.fingerprint,
                        now,
                        now,
                        entity.raw_metadata_json,
                    ),
                )
                counts[entity.kind] += 1
            self.context.sync_meeting_knowledge(conn, meeting_id, now)
            conn.commit()
            return counts

    def list_structured_entities(
        self,
        meeting_id: int,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        if kind is not None:
            assert_known_entity_kind(kind)
            kinds = (kind,)
        else:
            kinds = ENTITY_KINDS
        with index_connection(self.context.index_path) as conn:
            rows: list[dict[str, Any]] = []
            for entity_kind in kinds:
                entity_rows = conn.execute(
                    ENTITY_SELECT_SQL[entity_kind],
                    (meeting_id,),
                ).fetchall()
                rows.extend({"kind": entity_kind, **dict(row)} for row in entity_rows)
            return rows

    def list_structured_entity_details(
        self,
        kind: str,
        limit: int = 20,
        *,
        status: str = "all",
    ) -> list[dict[str, Any]]:
        assert_known_entity_kind(kind)
        assert_known_task_status_filter(status)
        with index_connection(self.context.index_path) as conn:
            rows = self.context.list_entity_details(
                conn,
                kind,
                limit if status == "all" else limit * 8,
            )
            if kind == "action_items" and status != "all":
                rows = [row for row in rows if row.get("status") == status]
            return rows[:limit]

    def list_all_structured_entity_details(self, limit: int = 100) -> list[dict[str, Any]]:
        with index_connection(self.context.index_path) as conn:
            rows = self.context.list_all_entity_details(conn, limit)
            rows.sort(key=structured_entity_sort_key, reverse=True)
            return rows[:limit]
