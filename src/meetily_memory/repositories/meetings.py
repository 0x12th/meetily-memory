import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from meetily_memory.db.fts import NO_MATCH_FTS_QUERY, build_fts_query
from meetily_memory.db.rows import last_insert_id, row_to_dict, rows_to_dicts
from meetily_memory.db.schema import index_connection
from meetily_memory.meeting_structure import ENTITY_KINDS
from meetily_memory.memory.entities import ENTITY_COUNT_SQL, ENTITY_DELETE_SQL, ENTITY_SELECT_SQL
from meetily_memory.repositories.records import ChunkRecord, MeetingRecord, ScanRunStats

SyncKnowledge = Callable[[sqlite3.Connection, int, str], None]
DeleteKnowledge = Callable[[sqlite3.Connection, int], None]


@dataclass(frozen=True)
class MeetingsContext:
    index_path: Path
    sync_meeting_knowledge: SyncKnowledge
    delete_meeting_knowledge: DeleteKnowledge


class MeetingsRepository:
    def __init__(self, context: MeetingsContext) -> None:
        self.context = context
        self.index_path = context.index_path

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
                self.delete_meeting_children(conn, meeting_id)
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

            self.context.sync_meeting_knowledge(conn, meeting_id, meeting.indexed_at)
            conn.commit()
            return meeting_id, updated, inserted_chunks

    def delete_meeting_children(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        self.context.delete_meeting_knowledge(conn, meeting_id)
        self.delete_structured_entities(conn, meeting_id)
        conn.execute("DELETE FROM chunks_fts WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM meeting_people WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM chunks WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM artifacts WHERE meeting_id = ?", (meeting_id,))

    def delete_structured_entities(self, conn: sqlite3.Connection, meeting_id: int) -> None:
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

    def chunk_rows(self, conn: sqlite3.Connection, meeting_id: int) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT * FROM chunks WHERE meeting_id = ? ORDER BY ordinal",
                (meeting_id,),
            ).fetchall()
        )

    def meeting_people_rows(self, conn: sqlite3.Connection, meeting_id: int) -> list[sqlite3.Row]:
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

    def structured_entity_rows(
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
