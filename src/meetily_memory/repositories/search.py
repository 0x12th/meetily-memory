import sqlite3
from datetime import UTC
from itertools import batched
from pathlib import Path
from typing import Any

from meetily_memory.db.fts import build_fts_query, build_strict_fts_query
from meetily_memory.db.rows import rows_to_dicts
from meetily_memory.db.schema import (
    IndexConnectionFactory,
    index_connection,
    sqlite_read_snapshot,
)
from meetily_memory.domain import MeetingRef, MeetingSearchFilters

MEETING_TIME_EXPRESSION = (
    "COALESCE({alias}.started_at, {alias}.created_at, {alias}.updated_at, {alias}.indexed_at)"
)
SQLITE_READ_BATCH_SIZE = 200
SEARCH_DOMAIN_COLUMNS = """
  m.id AS meeting_id,
  m.external_id AS meeting_external_id,
  s.source_uuid AS source_uuid,
  m.title AS title,
  m.started_at AS started_at,
  m.ended_at AS ended_at,
  m.created_at AS created_at,
  m.updated_at AS updated_at,
  m.folder_path AS folder_path,
  m.source_path AS source_path,
  m.language AS language,
  m.summary_text AS summary_text,
  (SELECT COUNT(*) FROM chunks meeting_chunks WHERE meeting_chunks.meeting_id = m.id)
    AS chunk_count,
  c.id AS chunk_id,
  c.external_id AS chunk_external_id,
  c.evidence_id AS evidence_id,
  c.kind AS kind,
  c.ordinal AS ordinal,
  c.text AS text,
  c.speaker AS speaker,
  c.starts_at_seconds AS starts_at_seconds,
  c.ends_at_seconds AS ends_at_seconds,
  c.timestamp_label AS timestamp_label
"""
EVIDENCE_LOOKUP_SQL = f"""
SELECT
{SEARCH_DOMAIN_COLUMNS},
  NULL AS rank
FROM chunks c
JOIN meetings m ON m.id = c.meeting_id
JOIN sources s ON s.id = m.source_id
WHERE c.evidence_id = ?
"""
EvidenceReference = tuple[str, MeetingRef]


class EvidenceResolutionError(LookupError):
    pass


def meeting_time_predicate(
    filters: MeetingSearchFilters | None,
    *,
    alias: str = "m",
) -> tuple[str, list[object]]:
    if filters is None:
        return "1 = 1", []
    expression = MEETING_TIME_EXPRESSION.format(alias=alias)
    clauses: list[str] = []
    params: list[object] = []
    if filters.from_utc is not None:
        clauses.append(f"datetime({expression}) >= datetime(?)")
        params.append(filters.from_utc.astimezone(UTC).isoformat())
    if filters.to_utc is not None:
        clauses.append(f"datetime({expression}) < datetime(?)")
        params.append(filters.to_utc.astimezone(UTC).isoformat())
    return " AND ".join(clauses) if clauses else "1 = 1", params


class SearchRepository:
    def __init__(
        self,
        index_path: Path,
        connection: IndexConnectionFactory = index_connection,
    ) -> None:
        self.index_path = index_path
        self._connection = connection

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        meeting_id: int | None = None,
        context: int = 0,
        filters: MeetingSearchFilters | None = None,
    ) -> list[dict[str, Any]]:
        fts_query = build_fts_query(query)
        if not fts_query:
            return []
        strict_fts_query = build_strict_fts_query(query)
        context = max(context, 0)
        with self._connection(self.index_path) as conn, sqlite_read_snapshot(conn):
            rows = self._search_with_fallback(
                conn,
                fts_query,
                strict_fts_query,
                limit,
                meeting_id=meeting_id,
                filters=filters,
            )
            if context == 0:
                return rows
            return self._expand_context(conn, rows, context)

    def _search_with_fallback(  # noqa: PLR0913
        self,
        conn: sqlite3.Connection,
        fts_query: str,
        strict_fts_query: str,
        limit: int,
        *,
        meeting_id: int | None = None,
        filters: MeetingSearchFilters | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if strict_fts_query:
            rows = rows_to_dicts(
                self._execute_search(
                    conn,
                    strict_fts_query,
                    limit,
                    meeting_id=meeting_id,
                    filters=filters,
                )
            )
            if len(rows) >= limit:
                return rows
        fallback_rows = rows_to_dicts(
            self._execute_search(
                conn,
                fts_query,
                limit,
                meeting_id=meeting_id,
                filters=filters,
            )
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
        filters: MeetingSearchFilters | None = None,
    ) -> list[Any]:
        time_sql, time_params = meeting_time_predicate(filters)
        params = (fts_query, meeting_id, meeting_id, *time_params, limit)
        return conn.execute(
            f"""
            SELECT
            {SEARCH_DOMAIN_COLUMNS},
              f.rank AS rank
            FROM chunks_fts f
            JOIN chunks c ON c.id = f.chunk_id
            JOIN meetings m ON m.id = c.meeting_id
            JOIN sources s ON s.id = m.source_id
            WHERE chunks_fts MATCH ?
              AND (? IS NULL OR m.id = ?)
              AND {time_sql}
            ORDER BY f.rank
            LIMIT ?
            """,
            params,
        ).fetchall()

    def _expand_context(
        self,
        conn: sqlite3.Connection,
        rows: list[dict[str, Any]],
        context: int,
    ) -> list[dict[str, Any]]:
        if context <= 0 or not rows:
            return rows
        expanded: list[dict[str, Any]] = []
        matched_chunk_ids = {int(row["chunk_id"]) for row in rows}
        ranks_by_chunk_id = {int(row["chunk_id"]): row.get("rank") for row in rows}
        for row in rows:
            matched = dict(row)
            matched["matched_chunk_id"] = int(row["chunk_id"])
            matched["is_context"] = False
            expanded.append(matched)

        seen_chunk_ids = set(matched_chunk_ids)
        for context_row in self._context_rows(conn, rows, context):
            chunk_id = int(context_row["chunk_id"])
            if chunk_id in seen_chunk_ids:
                continue
            matched_chunk_id = int(context_row["matched_chunk_id"])
            context_row["rank"] = ranks_by_chunk_id.get(matched_chunk_id)
            context_row["is_context"] = True
            expanded.append(context_row)
            seen_chunk_ids.add(chunk_id)
        return expanded

    def _context_rows(
        self,
        conn: sqlite3.Connection,
        rows: list[dict[str, Any]],
        context: int,
    ) -> list[dict[str, Any]]:
        context_rows: list[dict[str, Any]] = []
        indexed_rows = tuple(enumerate(rows))
        for row_batch in batched(indexed_rows, SQLITE_READ_BATCH_SIZE):
            values_sql = ", ".join("(?, ?, ?, ?)" for _ in row_batch)
            params = tuple(
                value
                for match_order, row in row_batch
                for value in (
                    match_order,
                    int(row["chunk_id"]),
                    int(row["meeting_id"]),
                    int(row["ordinal"]),
                )
            )
            batch_rows = conn.execute(
                f"""
                WITH matched(match_order, matched_chunk_id, meeting_id, ordinal) AS (
                  VALUES {values_sql}
                )
                SELECT
                {SEARCH_DOMAIN_COLUMNS},
                  NULL AS rank,
                  matched.matched_chunk_id AS matched_chunk_id,
                  matched.match_order AS match_order,
                  ABS(c.ordinal - matched.ordinal) AS context_distance
                FROM matched
                JOIN chunks c
                  ON c.meeting_id = matched.meeting_id
                 AND c.ordinal BETWEEN matched.ordinal - ? AND matched.ordinal + ?
                JOIN meetings m ON m.id = c.meeting_id
                JOIN sources s ON s.id = m.source_id
                ORDER BY matched.match_order, context_distance, c.ordinal
                """,
                (*params, context, context),
            ).fetchall()
            context_rows.extend(rows_to_dicts(batch_rows))
        return context_rows

    def expand_evidence_refs(
        self,
        evidence_refs: tuple[EvidenceReference, ...],
        context: int,
    ) -> list[dict[str, Any]]:
        if not evidence_refs:
            return []
        with self._connection(self.index_path) as conn, sqlite_read_snapshot(conn):
            rows = self.resolve_evidence_rows(conn, evidence_refs)
            if context <= 0:
                return rows
            return self._expand_context(conn, rows, context)

    def resolve_evidence_rows(
        self,
        conn: sqlite3.Connection,
        evidence_refs: tuple[EvidenceReference, ...],
    ) -> list[dict[str, Any]]:
        unique_evidence_ids = tuple(dict.fromkeys(evidence_id for evidence_id, _ in evidence_refs))
        rows_by_evidence_id: dict[str, dict[str, Any]] = {}
        for evidence_id_batch in batched(unique_evidence_ids, SQLITE_READ_BATCH_SIZE):
            placeholders = ", ".join("?" for _ in evidence_id_batch)
            rows = conn.execute(
                f"""
                SELECT
                {SEARCH_DOMAIN_COLUMNS},
                  NULL AS rank
                FROM chunks c
                JOIN meetings m ON m.id = c.meeting_id
                JOIN sources s ON s.id = m.source_id
                WHERE c.evidence_id IN ({placeholders})
                """,
                evidence_id_batch,
            ).fetchall()
            rows_by_evidence_id.update((str(row["evidence_id"]), dict(row)) for row in rows)

        missing = [
            evidence_id
            for evidence_id in unique_evidence_ids
            if evidence_id not in rows_by_evidence_id
        ]
        if missing:
            message = (
                "Evidence no longer exists in the current index generation: "
                f"{', '.join(missing)}. Re-run the search before requesting context or entities."
            )
            raise EvidenceResolutionError(message)

        for evidence_id, expected_ref in evidence_refs:
            row = rows_by_evidence_id[evidence_id]
            actual_ref = MeetingRef(
                source_uuid=str(row["source_uuid"]),
                external_id=str(row["meeting_external_id"]),
            )
            if actual_ref == expected_ref:
                continue
            message = (
                f"Evidence identity mismatch for {evidence_id}: expected "
                f"{expected_ref.source_uuid}/{expected_ref.external_id}, found "
                f"{actual_ref.source_uuid}/{actual_ref.external_id}. Refusing to reuse a "
                "generation-local chunk ID; re-run the search."
            )
            raise EvidenceResolutionError(message)
        return [rows_by_evidence_id[evidence_id] for evidence_id, _ in evidence_refs]

    def evidence_by_id(self, evidence_id: str) -> dict[str, Any] | None:
        with self._connection(self.index_path) as conn:
            row = conn.execute(EVIDENCE_LOOKUP_SQL, (evidence_id,)).fetchone()
        return dict(row) if row is not None else None
