import sqlite3
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from itertools import batched
from pathlib import Path
from typing import Any

from meetily_memory.db.fts import NO_MATCH_FTS_QUERY, build_fts_query
from meetily_memory.db.rows import last_insert_id, row_to_dict, rows_to_dicts
from meetily_memory.db.schema import IndexConnectionFactory, index_connection
from meetily_memory.domain import (
    AmbiguousMeetingError,
    MeetingRef,
    MeetingSearchFilters,
    stable_evidence_id,
)
from meetily_memory.json_codec import dumps_json, loads_json
from meetily_memory.meeting_structure import ENTITY_KINDS
from meetily_memory.memory.entities import ENTITY_COUNT_SQL, ENTITY_DELETE_SQL, ENTITY_SELECT_SQL
from meetily_memory.repositories.records import (
    ChunkRecord,
    MeetingRecord,
    PostPublishIssue,
    ScanRunStats,
)
from meetily_memory.repositories.search import meeting_time_predicate

SyncKnowledge = Callable[[sqlite3.Connection, int, str], None]
DeleteKnowledge = Callable[[sqlite3.Connection, int], None]
MEETING_READ_BATCH_SIZE = 200
POST_PUBLISH_PHASES = frozenset(
    {"index_cleanup", "obsidian_sync", "settings_update", "source_path_finalize"}
)
POST_PUBLISH_ERROR_MESSAGE = (
    "Index scan completed; post-publish work failed. "
    "Use the structured status diagnostic for a safe retry action."
)


def post_publish_run_phase(phases: Iterable[str]) -> str:
    phase_names = tuple(phases)
    if len(phase_names) == 1 and phase_names[0] in POST_PUBLISH_PHASES:
        return f"post_publish_{phase_names[0]}_failed"
    return "post_publish_failed"


@dataclass(frozen=True)
class MeetingsContext:
    index_path: Path
    connection: IndexConnectionFactory
    sync_meeting_knowledge: SyncKnowledge
    delete_meeting_knowledge: DeleteKnowledge


class DuplicateEvidenceIdentityError(ValueError):
    pass


class MeetingsRepository:
    def __init__(self, context: MeetingsContext) -> None:
        self.context = context
        self.index_path = context.index_path

    def upsert_source(  # noqa: PLR0913
        self,
        source_uuid: str,
        kind: str,
        path: str,
        now: str,
        label: str | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        connection_context = (
            index_connection(self.index_path) if connection is None else nullcontext(connection)
        )
        with connection_context as conn:
            source_id = self._upsert_source(conn, source_uuid, kind, path, now, label)
            if connection is None:
                conn.commit()
            return source_id

    def _upsert_source(  # noqa: PLR0913
        self,
        conn: sqlite3.Connection,
        source_uuid: str,
        kind: str,
        path: str,
        now: str,
        label: str | None = None,
    ) -> int:
        existing = conn.execute(
            "SELECT * FROM sources WHERE source_uuid = ?",
            (source_uuid,),
        ).fetchone()
        path_owner = conn.execute(
            "SELECT source_uuid FROM sources WHERE kind = ? AND path = ?",
            (kind, path),
        ).fetchone()
        if path_owner is not None and str(path_owner["source_uuid"]) != source_uuid:
            message = "The canonical source path is already projected with a different UUID."
            raise ValueError(message)
        if existing:
            if str(existing["kind"]) != kind:
                message = f"Source UUID {source_uuid} is registered with a different kind."
                raise ValueError(message)
            source_id = int(existing["id"])
            conn.execute(
                """
                UPDATE sources
                SET path = ?, last_seen_at = ?, updated_at = ?, label = ?
                WHERE id = ?
                """,
                (path, now, now, label or existing["label"], source_id),
            )
            conn.execute(
                """
                UPDATE meetings
                SET source_path = ?
                WHERE source_id = ? AND (source_path IS NULL OR source_path != ?)
                """,
                (path, source_id, path),
            )
            return source_id

        cursor = conn.execute(
            """
            INSERT INTO sources (
              source_uuid, kind, path, label, external_app, external_version,
              last_seen_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'Meetily', NULL, ?, ?, ?)
            """,
            (source_uuid, kind, path, label, now, now, now),
        )
        return last_insert_id(cursor)

    def get_source(self, kind: str, path: str) -> dict[str, Any] | None:
        with self.context.connection(self.index_path) as conn:
            row = conn.execute(
                "SELECT * FROM sources WHERE kind = ? AND path = ?",
                (kind, path),
            ).fetchone()
            return row_to_dict(row)

    def get_source_by_uuid(self, source_uuid: str) -> dict[str, Any] | None:
        with self.context.connection(self.index_path) as conn:
            row = conn.execute(
                "SELECT * FROM sources WHERE source_uuid = ?",
                (source_uuid,),
            ).fetchone()
            return row_to_dict(row)

    def get_meeting_by_source_id(
        self,
        source_id: int,
        external_id: str,
        *,
        filters: MeetingSearchFilters | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        time_sql, time_params = meeting_time_predicate(filters)
        connection_context = (
            self.context.connection(self.index_path)
            if connection is None
            else nullcontext(connection)
        )
        with connection_context as conn:
            sql = f"""
                SELECT m.*, s.source_uuid
                FROM meetings m
                JOIN sources s ON s.id = m.source_id
                WHERE m.source_id = ? AND m.external_id = ? AND {time_sql}
            """
            row = conn.execute(
                sql,
                (source_id, external_id, *time_params),
            ).fetchone()
            return row_to_dict(row)

    def upsert_meeting_with_chunks(  # noqa: C901
        self,
        meeting: MeetingRecord,
        chunks: Iterable[ChunkRecord],
        *,
        force: bool = False,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[int, bool, int]:
        chunk_list = list(chunks)
        connection_context = (
            index_connection(self.index_path) if connection is None else nullcontext(connection)
        )
        with connection_context as conn:
            existing = conn.execute(
                "SELECT * FROM meetings WHERE source_id = ? AND external_id = ?",
                (meeting.source_id, meeting.external_id),
            ).fetchone()

            if existing and existing["fingerprint"] == meeting.fingerprint and not force:
                return int(existing["id"]), False, 0

            source = conn.execute(
                "SELECT source_uuid FROM sources WHERE id = ?",
                (meeting.source_id,),
            ).fetchone()
            if source is None:
                message = f"Source not found while indexing meeting: {meeting.source_id}."
                raise ValueError(message)
            source_uuid = str(source["source_uuid"])
            evidence_chunks: list[tuple[ChunkRecord, str]] = []
            evidence_owners: dict[str, ChunkRecord] = {}
            for chunk in chunk_list:
                evidence_id = stable_evidence_id(
                    source_uuid,
                    meeting.external_id,
                    chunk.external_id,
                    kind=chunk.kind,
                    ordinal=chunk.ordinal,
                    text=chunk.text,
                )
                previous = evidence_owners.get(evidence_id)
                if previous is not None:
                    first_identity = previous.external_id or f"{previous.kind}#{previous.ordinal}"
                    second_identity = chunk.external_id or f"{chunk.kind}#{chunk.ordinal}"
                    message = (
                        "Duplicate upstream chunk identity while indexing "
                        f"source {source_uuid}, meeting {meeting.external_id}: "
                        f"{first_identity!r} and {second_identity!r} both map to {evidence_id}."
                    )
                    raise DuplicateEvidenceIdentityError(message)
                evidence_owners[evidence_id] = chunk
                evidence_chunks.append((chunk, evidence_id))

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
            for chunk, evidence_id in evidence_chunks:
                chunk_values = asdict(chunk)
                try:
                    cursor = conn.execute(
                        """
                        INSERT INTO chunks (
                          meeting_id, external_id, evidence_id, kind, ordinal, text, speaker,
                          starts_at_seconds, ends_at_seconds, timestamp_label,
                          token_count, fingerprint, raw_metadata_json
                        )
                        VALUES (
                          :meeting_id, :external_id, :evidence_id, :kind, :ordinal, :text,
                          :speaker, :starts_at_seconds, :ends_at_seconds,
                          :timestamp_label, :token_count, :fingerprint,
                          :raw_metadata_json
                        )
                        """,
                        {
                            "meeting_id": meeting_id,
                            "evidence_id": evidence_id,
                            **chunk_values,
                        },
                    )
                except sqlite3.IntegrityError as exc:
                    if "chunks.evidence_id" not in str(exc):
                        raise
                    message = (
                        "Duplicate evidence identity while indexing "
                        f"source {source_uuid}, meeting {meeting.external_id}: {evidence_id}."
                    )
                    raise DuplicateEvidenceIdentityError(message) from exc
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
            if connection is None:
                conn.commit()
            return meeting_id, updated, inserted_chunks

    def reconcile_source_meetings(
        self,
        source_id: int,
        external_ids: set[str],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        connection_context = (
            index_connection(self.index_path) if connection is None else nullcontext(connection)
        )
        with connection_context as conn:
            rows = conn.execute(
                "SELECT id, external_id FROM meetings WHERE source_id = ?",
                (source_id,),
            ).fetchall()
            meeting_ids = [
                int(row["id"]) for row in rows if str(row["external_id"]) not in external_ids
            ]
            for meeting_id in meeting_ids:
                self.delete_meeting_children(conn, meeting_id)
                conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
            if connection is None:
                conn.commit()
            return len(meeting_ids)

    def delete_meeting_children(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        self.delete_meeting_vectors(conn, meeting_id)
        self.context.delete_meeting_knowledge(conn, meeting_id)
        self.delete_structured_entities(conn, meeting_id)
        conn.execute("DELETE FROM chunks_fts WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM meeting_people WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM chunks WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM artifacts WHERE meeting_id = ?", (meeting_id,))

    def delete_meeting_vectors(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        vector_tables = [
            str(row["name"])
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name GLOB 'chunk_embeddings_vec_*'
                  AND sql LIKE 'CREATE VIRTUAL TABLE%USING vec0%'
                """
            ).fetchall()
        ]
        if not vector_tables:
            return
        from meetily_memory.semantic_search import (  # noqa: PLC0415
            assert_safe_identifier,
            load_sqlite_vec,
        )

        load_sqlite_vec(conn)
        for table in vector_tables:
            safe_table = assert_safe_identifier(table)
            conn.execute(
                f"""
                DELETE FROM {safe_table}
                WHERE rowid IN (SELECT id FROM chunks WHERE meeting_id = ?)
                """,
                (meeting_id,),
            )

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
        with self.context.connection(self.index_path) as conn:
            rows = conn.execute(
                "SELECT * FROM chunks WHERE meeting_id = ? ORDER BY ordinal",
                (meeting_id,),
            ).fetchall()
            return rows_to_dicts(rows)

    def list_meeting_ids(self) -> list[int]:
        with self.context.connection(self.index_path) as conn:
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
                  s.source_uuid,
                  COUNT(c.id) AS chunk_count
                FROM meetings m
                JOIN sources s ON s.id = m.source_id
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
                  s.source_uuid,
                  COUNT(c.id) AS chunk_count
                FROM meetings m
                JOIN sources s ON s.id = m.source_id
                LEFT JOIN chunks c ON c.meeting_id = m.id
                GROUP BY m.id
                ORDER BY COALESCE(m.updated_at, m.created_at, m.indexed_at) DESC
                LIMIT ?
                """

        params.append(limit)
        with self.context.connection(self.index_path) as conn:
            rows = conn.execute(sql, params).fetchall()
            return rows_to_dicts(rows)

    def get_meeting_by_local_id(
        self,
        meeting_id: int,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> dict[str, Any] | None:
        time_sql, time_params = meeting_time_predicate(filters)
        with self.context.connection(self.index_path) as conn:
            row = conn.execute(
                f"""
                SELECT m.*, s.source_uuid
                FROM meetings m
                JOIN sources s ON s.id = m.source_id
                WHERE m.id = ? AND {time_sql}
                """,
                (meeting_id, *time_params),
            ).fetchone()
            return row_to_dict(row)

    def get_meeting_by_ref(
        self,
        ref: MeetingRef,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> dict[str, Any] | None:
        return self.get_meetings_by_refs((ref,), filters=filters).get(ref)

    def get_meetings_by_refs(
        self,
        refs: tuple[MeetingRef, ...],
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> dict[MeetingRef, dict[str, Any]]:
        unique_refs = tuple(dict.fromkeys(refs))
        if not unique_refs:
            return {}
        time_sql, time_params = meeting_time_predicate(filters)
        meetings: dict[MeetingRef, dict[str, Any]] = {}
        with self.context.connection(self.index_path) as conn:
            for ref_batch in batched(unique_refs, MEETING_READ_BATCH_SIZE):
                values_sql = ", ".join("(?, ?, ?)" for _ in ref_batch)
                requested_params = tuple(
                    value
                    for request_order, ref in enumerate(ref_batch)
                    for value in (request_order, ref.source_uuid, ref.external_id)
                )
                rows = conn.execute(
                    f"""
                    WITH requested(request_order, source_uuid, external_id) AS (
                      VALUES {values_sql}
                    )
                    SELECT requested.request_order, m.*, s.source_uuid
                    FROM requested
                    JOIN sources s ON s.source_uuid = requested.source_uuid
                    JOIN meetings m
                      ON m.source_id = s.id
                     AND m.external_id = requested.external_id
                    WHERE {time_sql}
                    ORDER BY requested.request_order
                    """,
                    (*requested_params, *time_params),
                ).fetchall()
                for row in rows:
                    ref = MeetingRef(str(row["source_uuid"]), str(row["external_id"]))
                    meetings[ref] = dict(row)
        return meetings

    def get_meetings_by_local_ids(
        self,
        meeting_ids: tuple[int, ...],
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> dict[int, dict[str, Any]]:
        unique_ids = tuple(dict.fromkeys(meeting_ids))
        if not unique_ids:
            return {}
        time_sql, time_params = meeting_time_predicate(filters)
        meetings: dict[int, dict[str, Any]] = {}
        with self.context.connection(self.index_path) as conn:
            for meeting_id_batch in batched(unique_ids, MEETING_READ_BATCH_SIZE):
                placeholders = ", ".join("?" for _ in meeting_id_batch)
                rows = conn.execute(
                    f"""
                    SELECT m.*, s.source_uuid
                    FROM meetings m
                    JOIN sources s ON s.id = m.source_id
                    WHERE m.id IN ({placeholders}) AND {time_sql}
                    """,
                    (*meeting_id_batch, *time_params),
                ).fetchall()
                meetings.update((int(row["id"]), dict(row)) for row in rows)
        return meetings

    def get_meeting_by_external_id(
        self,
        external_id: str,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> dict[str, Any] | None:
        time_sql, time_params = meeting_time_predicate(filters)
        with self.context.connection(self.index_path) as conn:
            rows = conn.execute(
                f"""
                SELECT m.*, s.source_uuid
                FROM meetings m
                JOIN sources s ON s.id = m.source_id
                WHERE m.external_id = ? AND {time_sql}
                ORDER BY s.source_uuid
                LIMIT 2
                """,
                (external_id, *time_params),
            ).fetchall()
        if len(rows) > 1:
            source_uuids = ", ".join(str(row["source_uuid"]) for row in rows)
            message = (
                f"Meeting external ID is ambiguous across sources: {external_id}. "
                f"Use MeetingRef with one of these source UUIDs: {source_uuids}."
            )
            raise AmbiguousMeetingError(message)
        return row_to_dict(rows[0]) if rows else None

    def dominant_meeting_language(self) -> str | None:
        with self.context.connection(self.index_path) as conn:
            row = conn.execute(
                """
                SELECT language
                FROM meetings
                WHERE language IS NOT NULL AND language != ''
                GROUP BY language
                ORDER BY COUNT(*) DESC, language ASC
                LIMIT 1
                """
            ).fetchone()
            return str(row["language"]) if row else None

    def begin_source_scan(self, source_uuid: str, started_at: str) -> tuple[int, int]:
        with index_connection(self.index_path) as conn:
            source = conn.execute(
                "SELECT id FROM sources WHERE source_uuid = ?",
                (source_uuid,),
            ).fetchone()
            source_id = int(source["id"]) if source is not None else 0
            self._fail_running_scan_runs(conn, started_at)
            cursor = conn.execute(
                """
                INSERT INTO scan_runs (source_id, started_at, status, phase)
                VALUES (?, ?, 'running', 'source_scan')
                """,
                (source_id or None, started_at),
            )
            conn.commit()
            return source_id, last_insert_id(cursor)

    def fail_abandoned_scan_runs(self, finished_at: str) -> None:
        with index_connection(self.index_path) as conn:
            self._fail_running_scan_runs(conn, finished_at)
            conn.commit()

    def _fail_running_scan_runs(self, conn: sqlite3.Connection, finished_at: str) -> None:
        interrupted_message = "Previous refresh ended before completion."
        conn.execute(
            """
            UPDATE scan_runs
            SET finished_at = ?, status = 'failed', phase = 'interrupted',
                error_message = ?, errors_json = ?
            WHERE status = 'running'
            """,
            (
                finished_at,
                interrupted_message,
                dumps_json(
                    {
                        "phase": "interrupted",
                        "message": interrupted_message,
                        "action": "Rerun refresh; the previous projection was not published.",
                    }
                ),
            ),
        )

    def update_scan_run_phase(self, run_id: int, phase: str) -> None:
        with index_connection(self.index_path) as conn:
            conn.execute(
                "UPDATE scan_runs SET phase = ? WHERE id = ? AND status = 'running'",
                (phase, run_id),
            )
            conn.commit()

    def complete_scan_run(
        self,
        run_id: int,
        finished_at: str,
        result: ScanRunStats,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        connection_context = (
            index_connection(self.index_path) if connection is None else nullcontext(connection)
        )
        with connection_context as conn:
            cursor = conn.execute(
                """
                UPDATE scan_runs
                SET source_id = ?, finished_at = ?, status = 'completed', phase = 'completed',
                    meetings_seen = ?, meetings_inserted = ?, meetings_updated = ?,
                    chunks_seen = ?, chunks_inserted = ?, chunks_updated = ?,
                    errors_json = NULL, error_message = NULL
                WHERE id = ? AND status = 'running'
                """,
                (
                    result.source_id,
                    finished_at,
                    result.meetings_seen,
                    result.meetings_inserted,
                    result.meetings_updated,
                    result.chunks_seen,
                    result.chunks_inserted,
                    result.chunks_updated,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                message = f"Running scan row disappeared before atomic publish: {run_id}."
                raise RuntimeError(message)
            if connection is None:
                conn.commit()

    def fail_scan_run(
        self,
        run_id: int,
        finished_at: str,
        phase: str,
        result: ScanRunStats,
        error_type: str,
    ) -> None:
        message = f"{error_type} during {phase}."
        with index_connection(self.index_path) as conn:
            conn.execute(
                """
                UPDATE scan_runs
                SET finished_at = ?, status = 'failed', phase = ?,
                    meetings_seen = ?, meetings_inserted = ?, meetings_updated = ?,
                    chunks_seen = ?, chunks_inserted = ?, chunks_updated = ?,
                    errors_json = ?, error_message = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    finished_at,
                    phase,
                    result.meetings_seen,
                    result.meetings_inserted,
                    result.meetings_updated,
                    result.chunks_seen,
                    result.chunks_inserted,
                    result.chunks_updated,
                    dumps_json({"phase": phase, "message": message}),
                    message,
                    run_id,
                ),
            )
            conn.commit()

    def record_post_publish_failure(
        self,
        run_id: int,
        issues: tuple[PostPublishIssue, ...],
    ) -> None:
        if not issues:
            return
        if any(issue.phase not in POST_PUBLISH_PHASES for issue in issues):
            message = "Unsupported post-publish diagnostic phase."
            raise ValueError(message)
        source_uuids = {issue.source_uuid for issue in issues}
        if len(source_uuids) != 1 or not next(iter(source_uuids)):
            message = "Post-publish diagnostics require one source UUID."
            raise ValueError(message)
        source_uuid = issues[0].source_uuid
        source_paths = {issue.source_path for issue in issues if issue.source_path is not None}
        payload = {
            "index_status": "completed",
            "post_publish_status": "failed",
            "source_uuid": source_uuid,
            "source_path": next(iter(source_paths)) if len(source_paths) == 1 else None,
            "issues": [asdict(issue) for issue in issues],
        }
        phase = post_publish_run_phase(issue.phase for issue in issues)
        with index_connection(self.index_path) as conn:
            cursor = conn.execute(
                """
                UPDATE scan_runs
                SET phase = ?, errors_json = ?, error_message = ?
                WHERE id = ? AND status = 'completed'
                """,
                (phase, dumps_json(payload), POST_PUBLISH_ERROR_MESSAGE, run_id),
            )
            if cursor.rowcount != 1:
                error = f"Completed scan row not found for post-publish diagnostics: {run_id}."
                raise RuntimeError(error)
            conn.commit()

    def resolve_post_publish_failures(  # noqa: C901, PLR0912, PLR0915
        self,
        source_uuid: str,
        phases: tuple[str, ...],
    ) -> None:
        resolved_phases = frozenset(phases)
        if not resolved_phases:
            return
        if not resolved_phases <= POST_PUBLISH_PHASES:
            message = "Unsupported post-publish resolution phase."
            raise ValueError(message)
        with index_connection(self.index_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT r.id, r.errors_json, s.source_uuid AS run_source_uuid
                FROM scan_runs r
                LEFT JOIN sources s ON s.id = r.source_id
                WHERE r.status = 'completed'
                  AND r.phase LIKE 'post_publish%_failed'
                ORDER BY r.id
                """
            ).fetchall()
            for row in rows:
                raw_payload = row["errors_json"]
                if not isinstance(raw_payload, str):
                    continue
                try:
                    payload = loads_json(raw_payload)
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict) or not isinstance(payload.get("issues"), list):
                    continue
                payload_source = payload.get("source_uuid")
                run_source = row["run_source_uuid"]
                remaining: list[object] = []
                resolved: list[object] = []
                for issue in payload["issues"]:
                    if not isinstance(issue, dict):
                        remaining.append(issue)
                        continue
                    issue_source = issue.get("source_uuid") or payload_source or run_source
                    issue_phase = issue.get("phase")
                    if issue_source == source_uuid and issue_phase in resolved_phases:
                        resolved.append(issue)
                    else:
                        remaining.append(issue)
                if not resolved:
                    continue
                previous_resolved = payload.get("resolved_issues")
                resolved_issues = previous_resolved if isinstance(previous_resolved, list) else []
                payload["resolved_issues"] = [*resolved_issues, *resolved]
                payload["issues"] = remaining
                if remaining:
                    payload["post_publish_status"] = "failed"
                    remaining_phases: list[str] = []
                    for remaining_issue in remaining:
                        if not isinstance(remaining_issue, dict):
                            continue
                        remaining_phase = remaining_issue.get("phase")
                        if (
                            isinstance(remaining_phase, str)
                            and remaining_phase in POST_PUBLISH_PHASES
                        ):
                            remaining_phases.append(remaining_phase)
                    phase = post_publish_run_phase(remaining_phases)
                    error_message = POST_PUBLISH_ERROR_MESSAGE
                else:
                    payload["post_publish_status"] = "resolved"
                    phase = "completed"
                    error_message = None
                conn.execute(
                    """
                    UPDATE scan_runs
                    SET phase = ?, errors_json = ?, error_message = ?
                    WHERE id = ? AND status = 'completed'
                    """,
                    (phase, dumps_json(payload), error_message, int(row["id"])),
                )
            conn.commit()

    def scan_run_diagnostics(self) -> dict[str, dict[str, Any] | None]:
        with self.context.connection(self.index_path) as conn:
            completed = conn.execute(
                "SELECT * FROM scan_runs WHERE status = 'completed' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            failed = conn.execute(
                "SELECT * FROM scan_runs WHERE status = 'failed' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            post_publish = conn.execute(
                """
                SELECT *
                FROM scan_runs
                WHERE status = 'completed' AND phase LIKE 'post_publish%_failed'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        completed_payload = row_to_dict(completed)
        failed_payload = row_to_dict(failed)
        post_publish_payload = row_to_dict(post_publish)
        if (
            failed_payload
            and completed_payload
            and int(failed_payload["id"]) < int(completed_payload["id"])
        ):
            failed_payload = None
        return {
            "last_completed_run": completed_payload,
            "last_failed_run": failed_payload,
            "last_post_publish_error": post_publish_payload,
        }

    def stats(self) -> dict[str, int]:
        with self.context.connection(self.index_path) as conn:
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
