import re
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from meetily_memory.db.schema import index_connection

FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
MAX_FTS_QUERY_TOKENS = 16
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

            conn.commit()
            return meeting_id, updated, inserted_chunks

    def _delete_meeting_children(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        conn.execute("DELETE FROM chunks_fts WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM meeting_people WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM chunks WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM artifacts WHERE meeting_id = ?", (meeting_id,))

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

    def get_chunks_for_meeting(self, meeting_id: int) -> list[dict[str, Any]]:
        with index_connection(self.index_path) as conn:
            rows = conn.execute(
                "SELECT * FROM chunks WHERE meeting_id = ? ORDER BY ordinal",
                (meeting_id,),
            ).fetchall()
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
            person_like = f"%{person.casefold()}%"
            params.extend([person_like, person_like, person_like])
            sql = """
                SELECT
                  m.*,
                  COUNT(c.id) AS chunk_count
                FROM meetings m
                LEFT JOIN chunks c ON c.meeting_id = m.id
                WHERE EXISTS (
                  SELECT 1
                  FROM chunks person_chunks
                  LEFT JOIN people p ON p.normalized_name LIKE ?
                  LEFT JOIN meeting_people mp ON mp.person_id = p.id AND mp.meeting_id = m.id
                  WHERE person_chunks.meeting_id = m.id
                    AND (
                      lower(COALESCE(person_chunks.speaker, '')) LIKE ?
                      OR lower(person_chunks.text) LIKE ?
                      OR mp.meeting_id IS NOT NULL
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
            return {
                "meetings": int(conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]),
                "chunks": int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]),
                "sources": int(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]),
            }


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def build_fts_query(text: str) -> str:
    tokens = [token.casefold() for token in FTS_TOKEN_RE.findall(text)]
    unique_tokens = list(
        dict.fromkeys(token for token in tokens if len(token) > 1 and token not in FTS_STOPWORDS)
    )
    return " OR ".join(f'"{token}"' for token in unique_tokens[:MAX_FTS_QUERY_TOKENS])


def last_insert_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        message = "SQLite insert did not return lastrowid."
        raise RuntimeError(message)
    return cursor.lastrowid
