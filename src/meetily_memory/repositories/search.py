import sqlite3
from pathlib import Path
from typing import Any

from meetily_memory.db.fts import build_fts_query, build_strict_fts_query
from meetily_memory.db.rows import rows_to_dicts
from meetily_memory.db.schema import index_connection


class SearchRepository:
    def __init__(self, index_path: Path) -> None:
        self.index_path = index_path

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        meeting_id: int | None = None,
    ) -> list[dict[str, Any]]:
        fts_query = build_fts_query(query)
        if not fts_query:
            return []
        strict_fts_query = build_strict_fts_query(query)
        with index_connection(self.index_path) as conn:
            if meeting_id is not None:
                return self._search_with_fallback(
                    conn,
                    fts_query,
                    strict_fts_query,
                    limit,
                    meeting_id=meeting_id,
                )
            return self._search_with_fallback(conn, fts_query, strict_fts_query, limit)

    def _search_with_fallback(
        self,
        conn: sqlite3.Connection,
        fts_query: str,
        strict_fts_query: str,
        limit: int,
        *,
        meeting_id: int | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if strict_fts_query:
            rows = rows_to_dicts(
                self._execute_search(conn, strict_fts_query, limit, meeting_id=meeting_id)
            )
            if len(rows) >= limit:
                return rows
        fallback_rows = rows_to_dicts(
            self._execute_search(conn, fts_query, limit, meeting_id=meeting_id)
        )
        seen_chunk_ids = {row["chunk_id"] for row in rows}
        for row in fallback_rows:
            if row["chunk_id"] in seen_chunk_ids:
                continue
            rows.append(row)
            seen_chunk_ids.add(row["chunk_id"])
            if len(rows) >= limit:
                break
        return rows

    def _execute_search(
        self,
        conn: sqlite3.Connection,
        fts_query: str,
        limit: int,
        *,
        meeting_id: int | None = None,
    ) -> list[Any]:
        params = (fts_query, meeting_id, meeting_id, limit)
        return conn.execute(
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
              AND (? IS NULL OR m.id = ?)
            ORDER BY f.rank
            LIMIT ?
            """,
            params,
        ).fetchall()
