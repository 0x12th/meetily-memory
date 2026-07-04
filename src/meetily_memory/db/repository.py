import re
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from meetily_memory.db.schema import index_connection
from meetily_memory.meeting_structure import ENTITY_KINDS, StructuredEntity, empty_entity_counts

FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
MAX_FTS_QUERY_TOKENS = 16
NO_MATCH_FTS_QUERY = '"meetilymemorynomatchtoken"'
FTS_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "and",
        "are",
        "by",
        "did",
        "do",
        "for",
        "how",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "was",
        "we",
        "what",
        "when",
        "where",
        "who",
        "why",
        "как",
        "кто",
        "мы",
        "на",
        "по",
        "про",
        "что",
    }
)
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
          COALESCE(m.updated_at, m.created_at, m.indexed_at) AS meeting_date,
          c.external_id AS chunk_external_id,
          c.kind AS chunk_kind,
          c.speaker AS chunk_speaker,
          c.timestamp_label AS chunk_timestamp_label
        FROM decisions e
        JOIN meetings m ON m.id = e.meeting_id
        LEFT JOIN chunks c ON c.id = e.source_chunk_id
        ORDER BY meeting_date DESC, e.ordinal ASC
        LIMIT ?
    """,
    "action_items": """
        SELECT
          'action_items' AS kind,
          e.*,
          COALESCE(o.status, 'open') AS status,
          o.note AS status_note,
          o.source AS status_source,
          o.updated_at AS status_updated_at,
          m.external_id AS meeting_external_id,
          m.title AS meeting_title,
          COALESCE(m.updated_at, m.created_at, m.indexed_at) AS meeting_date,
          c.external_id AS chunk_external_id,
          c.kind AS chunk_kind,
          c.speaker AS chunk_speaker,
          c.timestamp_label AS chunk_timestamp_label
        FROM action_items e
        JOIN meetings m ON m.id = e.meeting_id
        LEFT JOIN chunks c ON c.id = e.source_chunk_id
        LEFT JOIN task_status_overrides o ON o.action_item_id = e.id
        ORDER BY meeting_date DESC, e.ordinal ASC
        LIMIT ?
    """,
    "risks": """
        SELECT
          'risks' AS kind,
          e.*,
          m.external_id AS meeting_external_id,
          m.title AS meeting_title,
          COALESCE(m.updated_at, m.created_at, m.indexed_at) AS meeting_date,
          c.external_id AS chunk_external_id,
          c.kind AS chunk_kind,
          c.speaker AS chunk_speaker,
          c.timestamp_label AS chunk_timestamp_label
        FROM risks e
        JOIN meetings m ON m.id = e.meeting_id
        LEFT JOIN chunks c ON c.id = e.source_chunk_id
        ORDER BY meeting_date DESC, e.ordinal ASC
        LIMIT ?
    """,
    "open_questions": """
        SELECT
          'open_questions' AS kind,
          e.*,
          m.external_id AS meeting_external_id,
          m.title AS meeting_title,
          COALESCE(m.updated_at, m.created_at, m.indexed_at) AS meeting_date,
          c.external_id AS chunk_external_id,
          c.kind AS chunk_kind,
          c.speaker AS chunk_speaker,
          c.timestamp_label AS chunk_timestamp_label
        FROM open_questions e
        JOIN meetings m ON m.id = e.meeting_id
        LEFT JOIN chunks c ON c.id = e.source_chunk_id
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


@dataclass(frozen=True)
class MeetingRecord:
    source_id: int
    external_id: str
    title: str
    started_at: str | None
    ended_at: str | None
    created_at: str | None
    updated_at: str | None
    folder_path: str | None
    source_path: str | None
    language: str | None
    summary_text: str | None
    raw_summary_json: str | None
    raw_metadata_json: str | None
    fingerprint: str
    indexed_at: str


@dataclass(frozen=True)
class ChunkRecord:
    external_id: str | None
    kind: str
    ordinal: int
    text: str
    speaker: str | None
    starts_at_seconds: float | None
    ends_at_seconds: float | None
    timestamp_label: str | None
    token_count: int | None
    fingerprint: str
    raw_metadata_json: str | None


class ScanRunStats(Protocol):
    meetings_seen: int
    meetings_inserted: int
    meetings_updated: int
    chunks_seen: int
    chunks_inserted: int
    chunks_updated: int


class IndexRepository:
    def __init__(self, index_path: Path) -> None:
        self.index_path = Path(index_path)
        with index_connection(self.index_path):
            pass

    def upsert_source(self, kind: str, path: str, now: str, label: str | None = None) -> int:
        existing = self.get_source(kind, path)
        with index_connection(self.index_path) as conn:
            if existing:
                conn.execute(
                    """
                    UPDATE sources
                    SET last_seen_at = ?, updated_at = ?, label = ?
                    WHERE id = ?
                    """,
                    (now, now, label or existing["label"], existing["id"]),
                )
                conn.commit()
                return int(existing["id"])

            cursor = conn.execute(
                """
                INSERT INTO sources (
                  kind, path, label, external_app, external_version,
                  last_seen_at, created_at, updated_at
                )
                VALUES (?, ?, ?, 'Meetily', NULL, ?, ?, ?)
                """,
                (kind, path, label, now, now, now),
            )
            conn.commit()
            return last_insert_id(cursor)

    def get_source(self, kind: str, path: str) -> dict[str, Any] | None:
        with index_connection(self.index_path) as conn:
            row = conn.execute(
                "SELECT * FROM sources WHERE kind = ? AND path = ?",
                (kind, path),
            ).fetchone()
            return row_to_dict(row)

    def get_meeting_by_external_id(self, source_id: int, external_id: str) -> dict[str, Any] | None:
        with index_connection(self.index_path) as conn:
            row = conn.execute(
                "SELECT * FROM meetings WHERE source_id = ? AND external_id = ?",
                (source_id, external_id),
            ).fetchone()
            return row_to_dict(row)

    def upsert_meeting_with_chunks(
        self,
        meeting: MeetingRecord,
        chunks: Iterable[ChunkRecord],
        *,
        force: bool = False,
    ) -> tuple[int, bool, int]:
        chunk_list = list(chunks)
        with index_connection(self.index_path) as conn:
            existing = conn.execute(
                "SELECT * FROM meetings WHERE source_id = ? AND external_id = ?",
                (meeting.source_id, meeting.external_id),
            ).fetchone()

            if existing and existing["fingerprint"] == meeting.fingerprint and not force:
                return int(existing["id"]), False, 0

            meeting_values = asdict(meeting)
            if existing:
                meeting_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE meetings
                    SET source_id = :source_id,
                        external_id = :external_id,
                        title = :title,
                        started_at = :started_at,
                        ended_at = :ended_at,
                        created_at = :created_at,
                        updated_at = :updated_at,
                        folder_path = :folder_path,
                        source_path = :source_path,
                        language = :language,
                        summary_text = :summary_text,
                        raw_summary_json = :raw_summary_json,
                        raw_metadata_json = :raw_metadata_json,
                        fingerprint = :fingerprint,
                        indexed_at = :indexed_at
                    WHERE id = :id
                    """,
                    {**meeting_values, "id": meeting_id},
                )
                self._delete_meeting_children(conn, meeting_id)
                updated = True
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO meetings (
                      source_id, external_id, title, started_at, ended_at,
                      created_at, updated_at, folder_path, source_path, language,
                      summary_text, raw_summary_json, raw_metadata_json,
                      fingerprint, indexed_at
                    )
                    VALUES (
                      :source_id, :external_id, :title, :started_at, :ended_at,
                      :created_at, :updated_at, :folder_path, :source_path,
                      :language, :summary_text, :raw_summary_json,
                      :raw_metadata_json, :fingerprint, :indexed_at
                    )
                    """,
                    meeting_values,
                )
                meeting_id = last_insert_id(cursor)
                updated = False

            inserted_chunks = 0
            people_seen: set[str] = set()
            for chunk in chunk_list:
                chunk_values = asdict(chunk)
                cursor = conn.execute(
                    """
                    INSERT INTO chunks (
                      meeting_id, external_id, kind, ordinal, text, speaker,
                      starts_at_seconds, ends_at_seconds, timestamp_label,
                      token_count, fingerprint, raw_metadata_json
                    )
                    VALUES (
                      :meeting_id, :external_id, :kind, :ordinal, :text,
                      :speaker, :starts_at_seconds, :ends_at_seconds,
                      :timestamp_label, :token_count, :fingerprint,
                      :raw_metadata_json
                    )
                    """,
                    {"meeting_id": meeting_id, **chunk_values},
                )
                chunk_id = last_insert_id(cursor)
                conn.execute(
                    """
                    INSERT INTO chunks_fts (chunk_id, meeting_id, title, text, speaker)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (chunk_id, meeting_id, meeting.title, chunk.text, chunk.speaker),
                )
                inserted_chunks += 1
                if chunk.speaker and chunk.speaker.strip():
                    people_seen.add(chunk.speaker.strip())

            for person_name in sorted(people_seen):
                self._link_person(conn, meeting_id, person_name)

            self._sync_meeting_knowledge(conn, meeting_id, meeting.indexed_at)
            conn.commit()
            return meeting_id, updated, inserted_chunks

    def _delete_meeting_children(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        self._delete_meeting_knowledge(conn, meeting_id)
        self._delete_structured_entities(conn, meeting_id)
        conn.execute("DELETE FROM chunks_fts WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM meeting_people WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM chunks WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM artifacts WHERE meeting_id = ?", (meeting_id,))

    def _delete_structured_entities(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        for sql in ENTITY_DELETE_SQL.values():
            conn.execute(sql, (meeting_id,))

    def _link_person(self, conn: sqlite3.Connection, meeting_id: int, display_name: str) -> None:
        normalized = display_name.casefold().strip()
        row = conn.execute(
            "SELECT * FROM people WHERE normalized_name = ? AND email IS NULL",
            (normalized,),
        ).fetchone()
        if row:
            person_id = int(row["id"])
        else:
            cursor = conn.execute(
                """
                INSERT INTO people (
                  display_name, normalized_name, email, external_ref, raw_metadata_json
                )
                VALUES (?, ?, NULL, NULL, NULL)
                """,
                (display_name, normalized),
            )
            person_id = last_insert_id(cursor)

        conn.execute(
            """
            INSERT OR IGNORE INTO meeting_people
              (meeting_id, person_id, role, confidence, source)
            VALUES (?, ?, 'speaker', 0.8, 'speaker')
            """,
            (meeting_id, person_id),
        )

    def _delete_meeting_knowledge(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        node_ids = self._meeting_scoped_knowledge_node_ids(conn, meeting_id)
        if node_ids:
            placeholders = ",".join("?" for _ in node_ids)
            delete_edges_sql = f"""
                DELETE FROM knowledge_edges
                WHERE source_meeting_id = ?
                   OR from_node_id IN ({placeholders})
                   OR to_node_id IN ({placeholders})
                """  # noqa: S608
            conn.execute(delete_edges_sql, (meeting_id, *node_ids, *node_ids))
            conn.execute(
                f"DELETE FROM knowledge_nodes WHERE id IN ({placeholders})",  # noqa: S608
                tuple(node_ids),
            )
        else:
            conn.execute("DELETE FROM knowledge_edges WHERE source_meeting_id = ?", (meeting_id,))

    def _delete_structured_knowledge(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        node_ids = self._structured_knowledge_node_ids(conn, meeting_id)
        if not node_ids:
            return
        placeholders = ",".join("?" for _ in node_ids)
        delete_edges_sql = f"""
            DELETE FROM knowledge_edges
            WHERE from_node_id IN ({placeholders})
               OR to_node_id IN ({placeholders})
            """  # noqa: S608
        conn.execute(delete_edges_sql, (*node_ids, *node_ids))
        conn.execute(
            f"DELETE FROM knowledge_nodes WHERE id IN ({placeholders})",  # noqa: S608
            tuple(node_ids),
        )

    def _meeting_scoped_knowledge_node_ids(
        self,
        conn: sqlite3.Connection,
        meeting_id: int,
    ) -> list[int]:
        keys = [("Meeting", f"meeting:{meeting_id}")]
        keys.extend(("Chunk", f"chunk:{row['id']}") for row in self._chunk_rows(conn, meeting_id))
        keys.extend(
            (ENTITY_NODE_TYPES[str(row["kind"])], entity_stable_key(row))
            for row in self._structured_entity_rows(conn, meeting_id)
        )
        return self._knowledge_node_ids_for_keys(conn, keys)

    def _structured_knowledge_node_ids(
        self,
        conn: sqlite3.Connection,
        meeting_id: int,
    ) -> list[int]:
        keys = [
            (ENTITY_NODE_TYPES[str(row["kind"])], entity_stable_key(row))
            for row in self._structured_entity_rows(conn, meeting_id)
        ]
        return self._knowledge_node_ids_for_keys(conn, keys)

    def _knowledge_node_ids_for_keys(
        self,
        conn: sqlite3.Connection,
        keys: list[tuple[str, str]],
    ) -> list[int]:
        node_ids: list[int] = []
        for node_type, stable_key in keys:
            row = conn.execute(
                """
                SELECT id
                FROM knowledge_nodes
                WHERE type = ? AND stable_key = ?
                """,
                (node_type, stable_key),
            ).fetchone()
            if row:
                node_ids.append(int(row["id"]))
        return node_ids

    def _sync_meeting_knowledge(
        self,
        conn: sqlite3.Connection,
        meeting_id: int,
        now: str,
    ) -> None:
        meeting = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if meeting is None:
            return

        meeting_node_id = self._upsert_knowledge_node(
            conn,
            "Meeting",
            f"meeting:{meeting_id}",
            str(meeting["title"]),
            now,
        )

        chunk_nodes: dict[int, int] = {}
        for chunk in self._chunk_rows(conn, meeting_id):
            chunk_title = f"{meeting['title']} / {chunk['kind']} #{chunk['ordinal']}"
            chunk_node_id = self._upsert_knowledge_node(
                conn,
                "Chunk",
                f"chunk:{chunk['id']}",
                chunk_title,
                now,
            )
            chunk_nodes[int(chunk["id"])] = chunk_node_id
            self._upsert_knowledge_edge(
                conn,
                meeting_node_id,
                "contains",
                chunk_node_id,
                1.0,
                source_meeting_id=meeting_id,
                source_chunk_id=int(chunk["id"]),
                extraction_method="scan",
                now=now,
            )

        person_nodes: list[tuple[int, str]] = []
        for person in self._meeting_people_rows(conn, meeting_id):
            person_node_id = self._upsert_knowledge_node(
                conn,
                "Person",
                f"person:{person['id']}",
                str(person["display_name"]),
                now,
            )
            person_nodes.append((person_node_id, str(person["display_name"])))
            self._upsert_knowledge_edge(
                conn,
                meeting_node_id,
                "mentions",
                person_node_id,
                float(person["confidence"] or 0.8),
                source_meeting_id=meeting_id,
                source_chunk_id=None,
                extraction_method=str(person["source"] or "speaker"),
                now=now,
            )

        for entity in self._structured_entity_rows(conn, meeting_id):
            entity_kind = str(entity["kind"])
            entity_node_id = self._upsert_knowledge_node(
                conn,
                ENTITY_NODE_TYPES[entity_kind],
                entity_stable_key(entity),
                str(entity["text"]),
                now,
            )
            source_chunk_id = optional_int(entity["source_chunk_id"])
            self._upsert_knowledge_edge(
                conn,
                meeting_node_id,
                "contains",
                entity_node_id,
                float(entity["confidence"]),
                source_meeting_id=meeting_id,
                source_chunk_id=source_chunk_id,
                extraction_method=entity_kind,
                now=now,
            )
            if source_chunk_id is not None and source_chunk_id in chunk_nodes:
                self._upsert_knowledge_edge(
                    conn,
                    entity_node_id,
                    "originated_in",
                    chunk_nodes[source_chunk_id],
                    1.0,
                    source_meeting_id=meeting_id,
                    source_chunk_id=source_chunk_id,
                    extraction_method=entity_kind,
                    now=now,
                )
            if entity_kind == "action_items":
                entity_text = str(entity["text"]).casefold()
                for person_node_id, display_name in person_nodes:
                    if display_name.casefold() in entity_text:
                        self._upsert_knowledge_edge(
                            conn,
                            entity_node_id,
                            "assigned_to",
                            person_node_id,
                            0.7,
                            source_meeting_id=meeting_id,
                            source_chunk_id=source_chunk_id,
                            extraction_method="heuristic_assignee",
                            now=now,
                        )

    def _upsert_knowledge_node(  # noqa: PLR0913
        self,
        conn: sqlite3.Connection,
        node_type: str,
        stable_key: str,
        title: str,
        now: str,
        raw_metadata_json: str | None = None,
    ) -> int:
        normalized_title = normalize_key(title)
        cursor = conn.execute(
            """
            INSERT INTO knowledge_nodes (
              type, stable_key, title, normalized_title,
              created_at, updated_at, raw_metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(type, stable_key) DO UPDATE SET
              title = excluded.title,
              normalized_title = excluded.normalized_title,
              updated_at = excluded.updated_at,
              raw_metadata_json = excluded.raw_metadata_json
            RETURNING id
            """,
            (node_type, stable_key, title, normalized_title, now, now, raw_metadata_json),
        )
        return int(cursor.fetchone()["id"])

    def _upsert_knowledge_edge(  # noqa: PLR0913
        self,
        conn: sqlite3.Connection,
        from_node_id: int,
        relation: str,
        to_node_id: int,
        confidence: float,
        *,
        source_meeting_id: int,
        source_chunk_id: int | None,
        extraction_method: str,
        now: str,
        raw_metadata_json: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO knowledge_edges (
              from_node_id, relation, to_node_id, confidence,
              source_meeting_id, source_chunk_id, extraction_method,
              created_at, raw_metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO UPDATE SET
              confidence = excluded.confidence,
              raw_metadata_json = excluded.raw_metadata_json
            """,
            (
                from_node_id,
                relation,
                to_node_id,
                confidence,
                source_meeting_id,
                source_chunk_id,
                extraction_method,
                now,
                raw_metadata_json,
            ),
        )

    def _chunk_rows(self, conn: sqlite3.Connection, meeting_id: int) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT * FROM chunks WHERE meeting_id = ? ORDER BY ordinal",
                (meeting_id,),
            ).fetchall()
        )

    def _meeting_people_rows(self, conn: sqlite3.Connection, meeting_id: int) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                """
                SELECT p.*, mp.confidence, mp.source
                FROM meeting_people mp
                JOIN people p ON p.id = mp.person_id
                WHERE mp.meeting_id = ?
                ORDER BY p.display_name
                """,
                (meeting_id,),
            ).fetchall()
        )

    def _structured_entity_rows(
        self, conn: sqlite3.Connection, meeting_id: int
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for kind in ENTITY_KINDS:
            entity_rows = conn.execute(ENTITY_SELECT_SQL[kind], (meeting_id,)).fetchall()
            rows.extend({"kind": kind, **dict(row)} for row in entity_rows)
        return rows

    def get_chunks_for_meeting(self, meeting_id: int) -> list[dict[str, Any]]:
        with index_connection(self.index_path) as conn:
            rows = conn.execute(
                "SELECT * FROM chunks WHERE meeting_id = ? ORDER BY ordinal",
                (meeting_id,),
            ).fetchall()
            return rows_to_dicts(rows)

    def replace_structured_entities(
        self,
        meeting_id: int,
        entities: Iterable[StructuredEntity],
        now: str,
    ) -> dict[str, int]:
        counts = empty_entity_counts()
        with index_connection(self.index_path) as conn:
            self._delete_structured_knowledge(conn, meeting_id)
            self._delete_structured_entities(conn, meeting_id)
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
            self._sync_meeting_knowledge(conn, meeting_id, now)
            conn.commit()
            return counts

    def list_meeting_ids(self) -> list[int]:
        with index_connection(self.index_path) as conn:
            rows = conn.execute(
                """
                SELECT id
                FROM meetings
                ORDER BY COALESCE(updated_at, created_at, indexed_at) DESC
                """
            ).fetchall()
            return [int(row["id"]) for row in rows]

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
        with index_connection(self.index_path) as conn:
            rows: list[dict[str, Any]] = []
            for entity_kind in kinds:
                entity_rows = conn.execute(ENTITY_SELECT_SQL[entity_kind], (meeting_id,)).fetchall()
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
        with index_connection(self.index_path) as conn:
            rows = self._list_structured_entity_details_conn(
                conn,
                kind,
                limit if status == "all" else limit * 8,
            )
            if kind == "action_items" and status != "all":
                rows = [row for row in rows if row.get("status") == status]
            return rows[:limit]

    def list_all_structured_entity_details(self, limit: int = 100) -> list[dict[str, Any]]:
        with index_connection(self.index_path) as conn:
            rows = self._list_all_structured_entity_details_conn(conn, limit)
            rows.sort(key=structured_entity_sort_key, reverse=True)
            return rows[:limit]

    def set_task_status(
        self,
        action_item_id: int,
        status: str,
        *,
        note: str | None,
        now: str | None = None,
    ) -> dict[str, Any]:
        assert_known_task_status(status)
        now = now or utc_now()
        with index_connection(self.index_path) as conn:
            task = conn.execute(
                "SELECT * FROM action_items WHERE id = ?",
                (action_item_id,),
            ).fetchone()
            if task is None:
                message = f"Task not found: {action_item_id}"
                raise ValueError(message)
            conn.execute(
                """
                INSERT INTO task_status_overrides (
                  action_item_id, status, note, source, created_at, updated_at
                )
                VALUES (?, ?, ?, 'manual', ?, ?)
                ON CONFLICT(action_item_id) DO UPDATE SET
                  status = excluded.status,
                  note = excluded.note,
                  source = excluded.source,
                  updated_at = excluded.updated_at
                """,
                (action_item_id, status, note, now, now),
            )
            conn.commit()
            row = conn.execute(
                """
                SELECT e.id, e.text, COALESCE(o.status, 'open') AS status,
                       o.note AS status_note, o.source AS status_source,
                       o.updated_at AS status_updated_at
                FROM action_items e
                LEFT JOIN task_status_overrides o ON o.action_item_id = e.id
                WHERE e.id = ?
                """,
                (action_item_id,),
            ).fetchone()
            return dict(row)

    def ensure_topic(
        self,
        title: str,
        *,
        aliases: Iterable[str] = (),
    ) -> dict[str, Any]:
        now = utc_now()
        with index_connection(self.index_path) as conn:
            topic_id = self._resolve_topic_id(conn, title)
            if topic_id is None:
                topic_id = self._upsert_knowledge_node(
                    conn,
                    "Topic",
                    topic_stable_key(title),
                    title,
                    now,
                )
            added_aliases: list[str] = []
            for alias in aliases:
                normalized_alias = normalize_key(alias)
                if not normalized_alias:
                    continue
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO topic_aliases (
                      topic_node_id, alias, normalized_alias, created_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (topic_id, alias, normalized_alias, now),
                )
                if cursor.rowcount:
                    added_aliases.append(alias)
            self._link_topic_matches(conn, topic_id, now)
            conn.commit()
            payload = self._topic_payload(conn, topic_id)
            payload["added_aliases"] = added_aliases
            return payload

    def topic_memory(self, title: str, limit: int = 10) -> dict[str, Any]:
        topic = self.ensure_topic(title)
        topic_terms = [str(topic["title"]), *(str(alias) for alias in topic["aliases"])]
        meetings = self.search(str(topic["title"]), limit)
        with index_connection(self.index_path) as conn:
            rows = self._list_topic_entity_details(conn, int(topic["id"]), limit)
            related_people = self._list_topic_people(conn, int(topic["id"]), limit)
        return {
            "topic": without_added_aliases(topic),
            "query_terms": topic_terms,
            "meetings": meetings,
            "structured_signals": rows,
            "related_people": related_people,
        }

    def graph_for_topic(self, title: str, limit: int = 50) -> dict[str, Any]:
        topic = self.ensure_topic(title)
        topic_id = int(topic["id"])
        with index_connection(self.index_path) as conn:
            linked_entity_rows = conn.execute(
                """
                SELECT from_node_id
                FROM knowledge_edges
                WHERE relation = 'belongs_to' AND to_node_id = ?
                LIMIT ?
                """,
                (topic_id, limit),
            ).fetchall()
            linked_entity_ids = [int(row["from_node_id"]) for row in linked_entity_rows]
            edge_rows = self._graph_edge_rows(conn, topic_id, linked_entity_ids, limit)
            node_ids = sorted(
                {topic_id}
                | {int(row["from_node_id"]) for row in edge_rows}
                | {int(row["to_node_id"]) for row in edge_rows}
            )
            nodes = self._knowledge_nodes_by_id(conn, node_ids)
            edges = rows_to_dicts(edge_rows)
        return {
            "topic": without_added_aliases(topic),
            "nodes": nodes,
            "edges": edges,
        }

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

    def _resolve_topic_id(self, conn: sqlite3.Connection, title: str) -> int | None:
        normalized = normalize_key(title)
        alias_row = conn.execute(
            """
            SELECT topic_node_id
            FROM topic_aliases
            WHERE normalized_alias = ?
            """,
            (normalized,),
        ).fetchone()
        if alias_row:
            return int(alias_row["topic_node_id"])
        node_row = conn.execute(
            """
            SELECT id
            FROM knowledge_nodes
            WHERE type = 'Topic' AND stable_key = ?
            """,
            (topic_stable_key(title),),
        ).fetchone()
        return int(node_row["id"]) if node_row else None

    def _topic_payload(self, conn: sqlite3.Connection, topic_id: int) -> dict[str, Any]:
        topic = conn.execute(
            "SELECT * FROM knowledge_nodes WHERE id = ?",
            (topic_id,),
        ).fetchone()
        aliases = [
            str(row["alias"])
            for row in conn.execute(
                """
                SELECT alias
                FROM topic_aliases
                WHERE topic_node_id = ?
                ORDER BY alias
                """,
                (topic_id,),
            ).fetchall()
        ]
        return {**dict(topic), "aliases": aliases}

    def _link_topic_matches(
        self,
        conn: sqlite3.Connection,
        topic_id: int,
        now: str,
    ) -> None:
        topic = self._topic_payload(conn, topic_id)
        terms = [str(topic["title"]), *(str(alias) for alias in topic["aliases"])]
        for row in self._list_all_structured_entity_details_conn(conn, 500):
            if not row_matches_terms(row, terms):
                continue
            kind = str(row["kind"])
            entity_node_id = self._upsert_knowledge_node(
                conn,
                ENTITY_NODE_TYPES[kind],
                entity_stable_key(row),
                str(row["text"]),
                now,
            )
            self._upsert_knowledge_edge(
                conn,
                entity_node_id,
                "belongs_to",
                topic_id,
                0.7,
                source_meeting_id=int(row["meeting_id"]),
                source_chunk_id=optional_int(row.get("source_chunk_id")),
                extraction_method="topic_query",
                now=now,
            )

    def _list_topic_entity_details(
        self,
        conn: sqlite3.Connection,
        topic_id: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        linked_keys = {
            (str(row["type"]), str(row["stable_key"]))
            for row in conn.execute(
                """
                SELECT n.type, n.stable_key
                FROM knowledge_edges e
                JOIN knowledge_nodes n ON n.id = e.from_node_id
                WHERE e.relation = 'belongs_to' AND e.to_node_id = ?
                """,
                (topic_id,),
            ).fetchall()
        }
        rows = [
            row
            for row in self._list_all_structured_entity_details_conn(conn, limit * 8)
            if (ENTITY_NODE_TYPES[str(row["kind"])], entity_stable_key(row)) in linked_keys
        ]
        rows.sort(key=structured_entity_sort_key, reverse=True)
        return rows[:limit]

    def _list_topic_people(
        self,
        conn: sqlite3.Connection,
        topic_id: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT DISTINCT p.id, p.display_name, p.normalized_name
            FROM knowledge_edges topic_edge
            JOIN knowledge_edges meeting_edge
              ON meeting_edge.to_node_id = topic_edge.from_node_id
             AND meeting_edge.relation = 'contains'
            JOIN meeting_people mp ON mp.meeting_id = meeting_edge.source_meeting_id
            JOIN people p ON p.id = mp.person_id
            WHERE topic_edge.relation = 'belongs_to'
              AND topic_edge.to_node_id = ?
            ORDER BY p.display_name
            LIMIT ?
            """,
            (topic_id, limit),
        ).fetchall()
        return rows_to_dicts(rows)

    def _graph_edge_rows(
        self,
        conn: sqlite3.Connection,
        topic_id: int,
        linked_entity_ids: list[int],
        limit: int,
    ) -> list[sqlite3.Row]:
        edge_ids: set[int] = set()
        topic_edges = conn.execute(
            """
            SELECT *
            FROM knowledge_edges
            WHERE from_node_id = ? OR to_node_id = ?
            LIMIT ?
            """,
            (topic_id, topic_id, limit),
        ).fetchall()
        edge_ids.update(int(row["id"]) for row in topic_edges)
        if linked_entity_ids:
            placeholders = ",".join("?" for _ in linked_entity_ids)
            expanded_edges_sql = f"""
                SELECT *
                FROM knowledge_edges
                WHERE from_node_id IN ({placeholders})
                   OR to_node_id IN ({placeholders})
                LIMIT ?
                """  # noqa: S608
            expanded_edges = conn.execute(
                expanded_edges_sql,
                (*linked_entity_ids, *linked_entity_ids, limit),
            ).fetchall()
            edge_ids.update(int(row["id"]) for row in expanded_edges)
        if not edge_ids:
            return []
        placeholders = ",".join("?" for _ in edge_ids)
        edge_rows_sql = f"""
            SELECT *
            FROM knowledge_edges
            WHERE id IN ({placeholders})
            ORDER BY relation, id
            LIMIT ?
            """  # noqa: S608
        return list(
            conn.execute(
                edge_rows_sql,
                (*sorted(edge_ids), limit),
            ).fetchall()
        )

    def _knowledge_nodes_by_id(
        self,
        conn: sqlite3.Connection,
        node_ids: list[int],
    ) -> list[dict[str, Any]]:
        if not node_ids:
            return []
        placeholders = ",".join("?" for _ in node_ids)
        nodes_sql = f"""
            SELECT *
            FROM knowledge_nodes
            WHERE id IN ({placeholders})
            ORDER BY type, title
            """  # noqa: S608
        rows = conn.execute(nodes_sql, tuple(node_ids)).fetchall()
        return rows_to_dicts(rows)

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        fts_query = build_fts_query(query)
        if not fts_query:
            return []
        with index_connection(self.index_path) as conn:
            rows = conn.execute(
                """
                SELECT
                  m.id AS meeting_id,
                  m.external_id AS meeting_external_id,
                  m.title AS title,
                  m.created_at AS created_at,
                  m.updated_at AS updated_at,
                  m.folder_path AS folder_path,
                  c.id AS chunk_id,
                  c.external_id AS chunk_external_id,
                  c.kind AS kind,
                  c.text AS text,
                  c.speaker AS speaker,
                  c.starts_at_seconds AS starts_at_seconds,
                  c.ends_at_seconds AS ends_at_seconds,
                  c.timestamp_label AS timestamp_label,
                  f.rank AS rank
                FROM chunks_fts f
                JOIN chunks c ON c.id = f.chunk_id
                JOIN meetings m ON m.id = c.meeting_id
                WHERE chunks_fts MATCH ?
                ORDER BY f.rank
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
            return rows_to_dicts(rows)

    def list_meetings(self, limit: int = 20, person: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        if person:
            fts_query = build_fts_query(person) or NO_MATCH_FTS_QUERY
            person_like = f"%{person.casefold()}%"
            params.extend([person_like, fts_query])
            sql = """
                SELECT
                  m.*,
                  COUNT(c.id) AS chunk_count
                FROM meetings m
                LEFT JOIN chunks c ON c.meeting_id = m.id
                WHERE (
                  EXISTS (
                    SELECT 1
                    FROM meeting_people mp
                    JOIN people p ON p.id = mp.person_id
                    WHERE mp.meeting_id = m.id
                      AND p.normalized_name LIKE ?
                  )
                  OR EXISTS (
                    SELECT 1
                    FROM chunks_fts
                    WHERE chunks_fts.meeting_id = m.id
                      AND chunks_fts MATCH ?
                  )
                )
                GROUP BY m.id
                ORDER BY COALESCE(m.updated_at, m.created_at, m.indexed_at) DESC
                LIMIT ?
                """
        else:
            sql = """
                SELECT
                  m.*,
                  COUNT(c.id) AS chunk_count
                FROM meetings m
                LEFT JOIN chunks c ON c.meeting_id = m.id
                GROUP BY m.id
                ORDER BY COALESCE(m.updated_at, m.created_at, m.indexed_at) DESC
                LIMIT ?
                """

        params.append(limit)
        with index_connection(self.index_path) as conn:
            rows = conn.execute(sql, params).fetchall()
            return rows_to_dicts(rows)

    def get_meeting(self, external_or_internal_id: str) -> dict[str, Any] | None:
        with index_connection(self.index_path) as conn:
            if external_or_internal_id.isdigit():
                row = conn.execute(
                    "SELECT * FROM meetings WHERE id = ?",
                    (int(external_or_internal_id),),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM meetings WHERE external_id = ?",
                    (external_or_internal_id,),
                ).fetchone()
            return row_to_dict(row)

    def record_scan_run(
        self,
        source_id: int,
        started_at: str,
        finished_at: str,
        result: ScanRunStats,
    ) -> None:
        with index_connection(self.index_path) as conn:
            conn.execute(
                """
                INSERT INTO scan_runs (
                  source_id, started_at, finished_at, status, meetings_seen,
                  meetings_inserted, meetings_updated, chunks_seen,
                  chunks_inserted, chunks_updated, errors_json
                )
                VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    source_id,
                    started_at,
                    finished_at,
                    result.meetings_seen,
                    result.meetings_inserted,
                    result.meetings_updated,
                    result.chunks_seen,
                    result.chunks_inserted,
                    result.chunks_updated,
                ),
            )
            conn.commit()

    def stats(self) -> dict[str, int]:
        with index_connection(self.index_path) as conn:
            stats = {
                "meetings": int(conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]),
                "chunks": int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]),
                "sources": int(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]),
            }
            for kind in ENTITY_KINDS:
                stats[kind] = int(conn.execute(ENTITY_COUNT_SQL[kind]).fetchone()[0])
            stats["knowledge_nodes"] = int(
                conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0]
            )
            stats["knowledge_edges"] = int(
                conn.execute("SELECT COUNT(*) FROM knowledge_edges").fetchone()[0]
            )
            return stats


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def structured_entity_sort_key(row: dict[str, Any]) -> tuple[str, int]:
    return (str(row.get("meeting_date") or ""), -int(row.get("ordinal") or 0))


def build_fts_query(text: str) -> str:
    tokens = [token.casefold() for token in FTS_TOKEN_RE.findall(text)]
    unique_tokens = list(
        dict.fromkeys(token for token in tokens if len(token) > 1 and token not in FTS_STOPWORDS)
    )
    return " OR ".join(f'"{token}"' for token in unique_tokens[:MAX_FTS_QUERY_TOKENS])


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_key(value: str) -> str:
    return " ".join(value.casefold().split())


def topic_stable_key(title: str) -> str:
    return f"topic:{normalize_key(title)}"


def entity_stable_key(row: dict[str, Any]) -> str:
    return f"{row['kind']}:{row['id']}"


def optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    message = f"Expected integer-compatible value, got {type(value).__name__}."
    raise TypeError(message)


def row_matches_terms(row: dict[str, Any], terms: Iterable[str]) -> bool:
    haystack = normalize_key(
        " ".join(
            str(row.get(key) or "")
            for key in (
                "text",
                "meeting_title",
                "meeting_external_id",
                "chunk_external_id",
                "chunk_speaker",
            )
        )
    )
    return any(normalize_key(term) in haystack for term in terms if normalize_key(term))


def without_added_aliases(topic: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in topic.items() if key != "added_aliases"}


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


def last_insert_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        message = "SQLite insert did not return lastrowid."
        raise RuntimeError(message)
    return cursor.lastrowid
