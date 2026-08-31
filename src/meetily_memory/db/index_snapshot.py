# ruff: noqa: S608

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Never

from meetily_memory.db._schema_utils import (
    application_objects,
    execute_sql_statements,
    pragma_int,
    quote_identifier,
    schema_manifest,
)
from meetily_memory.db.schema_family import (
    INDEX_APPLICATION_ID,
    INDEX_SCHEMA_EPOCH,
    INDEX_SCHEMA_FAMILY,
    INDEX_SCHEMA_USER_VERSION,
)

INDEX_APPLICATION_TABLES = frozenset({"index_meta", "meetings", "chunks", "chunks_fts"})
INDEX_SCHEMA_SQL = f"""
CREATE TABLE index_meta (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  schema_family TEXT NOT NULL CHECK (schema_family = '{INDEX_SCHEMA_FAMILY}'),
  schema_epoch INTEGER NOT NULL CHECK (schema_epoch = {INDEX_SCHEMA_EPOCH}),
  source_uuid TEXT NOT NULL CHECK (length(trim(source_uuid)) > 0),
  source_path TEXT NOT NULL CHECK (length(source_path) > 0),
  source_revision INTEGER NOT NULL CHECK (source_revision >= 0),
  meeting_count INTEGER NOT NULL DEFAULT 0 CHECK (meeting_count >= 0),
  chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (chunk_count >= 0)
);

CREATE TABLE meetings (
  id INTEGER PRIMARY KEY,
  source_uuid TEXT NOT NULL CHECK (length(trim(source_uuid)) > 0),
  external_id TEXT NOT NULL,
  title TEXT NOT NULL,
  started_at TEXT,
  ended_at TEXT,
  created_at TEXT,
  updated_at TEXT,
  folder_path TEXT,
  source_path TEXT,
  language TEXT,
  summary_text TEXT,
  raw_summary_json TEXT,
  raw_metadata_json TEXT,
  fingerprint TEXT NOT NULL,
  indexed_at TEXT NOT NULL,
  UNIQUE(source_uuid, external_id)
);

CREATE TABLE chunks (
  id INTEGER PRIMARY KEY,
  meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  external_id TEXT,
  evidence_id TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  speaker TEXT,
  starts_at_seconds REAL,
  ends_at_seconds REAL,
  timestamp_label TEXT,
  token_count INTEGER,
  fingerprint TEXT NOT NULL,
  raw_metadata_json TEXT,
  UNIQUE(meeting_id, kind, ordinal)
);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
  chunk_id UNINDEXED,
  meeting_id UNINDEXED,
  title,
  text,
  speaker,
  tokenize='unicode61'
);

CREATE INDEX idx_meetings_updated_at ON meetings(updated_at);
CREATE INDEX idx_meetings_started_at ON meetings(started_at);
CREATE INDEX idx_chunks_meeting_ordinal ON chunks(meeting_id, ordinal);
CREATE INDEX idx_chunks_fingerprint ON chunks(fingerprint);
"""


class IndexSnapshotError(RuntimeError):
    """A fresh index candidate is not the exact supported index snapshot family."""


def create_index_snapshot_schema(
    conn: sqlite3.Connection,
    *,
    source_uuid: str,
    source_path: Path,
    source_revision: int,
) -> None:
    source_uuid = source_uuid.strip()
    if not source_uuid:
        message = "Selected source UUID/token must not be empty."
        raise ValueError(message)
    if source_revision < 0:
        message = "Selected source revision/token must not be negative."
        raise ValueError(message)
    if conn.in_transaction:
        message = "Index snapshot schema creation requires a clean SQLite connection."
        raise RuntimeError(message)
    if application_objects(conn):
        _raise_invalid("schema creation is fresh-only; the database is not empty")

    conn.execute("PRAGMA foreign_keys=ON")
    selected_mode = str(conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).casefold()
    if selected_mode != "delete":
        _raise_invalid(f"journal_mode DELETE could not be selected (got {selected_mode!r})")
    conn.execute("BEGIN IMMEDIATE")
    try:
        execute_sql_statements(conn, INDEX_SCHEMA_SQL, context="Index snapshot schema")
        conn.execute(
            """
            INSERT INTO index_meta (
              singleton, schema_family, schema_epoch, source_uuid, source_path, source_revision
            )
            VALUES (1, ?, ?, ?, ?, ?)
            """,
            (
                INDEX_SCHEMA_FAMILY,
                INDEX_SCHEMA_EPOCH,
                source_uuid,
                str(source_path),
                source_revision,
            ),
        )
        conn.execute(f"PRAGMA application_id={INDEX_APPLICATION_ID}")
        conn.execute(f"PRAGMA user_version={INDEX_SCHEMA_USER_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def update_index_snapshot_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    meeting_count = int(conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0])
    chunk_count = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
    conn.execute(
        "UPDATE index_meta SET meeting_count=?, chunk_count=? WHERE singleton=1",
        (meeting_count, chunk_count),
    )
    return meeting_count, chunk_count


def validate_index_snapshot_schema(  # noqa: C901, PLR0912, PLR0915
    conn: sqlite3.Connection,
    *,
    schema: str = "main",
) -> dict[str, int | str]:
    try:
        application_id = pragma_int(conn, schema, "application_id")
        user_version = pragma_int(conn, schema, "user_version")
    except sqlite3.Error as exc:
        _raise_invalid("database header or PRAGMAs are unreadable", cause=exc)

    if application_id != INDEX_APPLICATION_ID:
        _raise_invalid(f"foreign application_id 0x{application_id:08X}")
    if user_version != INDEX_SCHEMA_USER_VERSION:
        relation = "future" if user_version > INDEX_SCHEMA_USER_VERSION else "unsupported"
        _raise_invalid(
            f"{relation} index user_version {user_version}; exact version "
            f"{INDEX_SCHEMA_USER_VERSION} is required"
        )

    try:
        meta_rows = conn.execute(
            f"""
            SELECT singleton, schema_family, schema_epoch, source_uuid, source_path,
                   source_revision, meeting_count, chunk_count
            FROM {quote_identifier(schema)}.index_meta
            ORDER BY singleton
            """
        ).fetchall()
    except sqlite3.Error as exc:
        _raise_invalid("index_meta is missing or unreadable", cause=exc)
    if len(meta_rows) != 1:
        _raise_invalid("index_meta must contain exactly one row")
    meta = meta_rows[0]
    if tuple(meta[:3]) != (1, INDEX_SCHEMA_FAMILY, INDEX_SCHEMA_EPOCH):
        _raise_invalid("index_meta family/epoch identity is invalid")
    source_uuid = str(meta[3])
    source_path = str(meta[4])
    if not source_uuid.strip() or not source_path:
        _raise_invalid("index_meta source identity is empty")
    source_revision = int(meta[5])
    if source_revision < 0:
        _raise_invalid("index_meta source revision/token is invalid")

    try:
        actual_manifest = schema_manifest(conn, schema)
        expected_manifest = _expected_schema_manifest()
    except sqlite3.Error as exc:
        _raise_invalid("schema manifest is unreadable", cause=exc)
    if actual_manifest != expected_manifest:
        _raise_invalid("schema objects do not exactly match the supported index epoch")

    try:
        meeting_count = _count(conn, schema, "meetings")
        chunk_count = _count(conn, schema, "chunks")
        fts_count = _count(conn, schema, "chunks_fts")
        if meeting_count != int(meta[6]) or chunk_count != int(meta[7]):
            _raise_invalid(
                "index_meta counts do not match meetings/chunks "
                f"({meta[6]}/{meta[7]} != {meeting_count}/{chunk_count})"
            )
        if fts_count != chunk_count:
            _raise_invalid(f"FTS row count {fts_count} does not match chunk count {chunk_count}")

        foreign_sources = conn.execute(
            f"""
            SELECT DISTINCT source_uuid
            FROM {quote_identifier(schema)}.meetings
            WHERE source_uuid != ?
            LIMIT 10
            """,
            (source_uuid,),
        ).fetchall()
        if foreign_sources:
            _raise_invalid("meetings contain data outside the selected source UUID/token")

        fts_mismatches = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM {quote_identifier(schema)}.chunks_fts AS f
                LEFT JOIN {quote_identifier(schema)}.chunks AS c
                  ON c.id = CAST(f.chunk_id AS INTEGER)
                LEFT JOIN {quote_identifier(schema)}.meetings AS m ON m.id = c.meeting_id
                WHERE c.id IS NULL
                   OR CAST(f.meeting_id AS INTEGER) != c.meeting_id
                   OR f.title != m.title
                   OR f.text != c.text
                   OR COALESCE(f.speaker, '') != COALESCE(c.speaker, '')
                """
            ).fetchone()[0]
        )
        missing_fts = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM {quote_identifier(schema)}.chunks AS c
                WHERE NOT EXISTS (
                  SELECT 1
                  FROM {quote_identifier(schema)}.chunks_fts AS f
                  WHERE CAST(f.chunk_id AS INTEGER) = c.id
                )
                """
            ).fetchone()[0]
        )
        duplicate_fts = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM (
                  SELECT chunk_id
                  FROM {quote_identifier(schema)}.chunks_fts
                  GROUP BY chunk_id
                  HAVING COUNT(*) != 1
                )
                """
            ).fetchone()[0]
        )
        if fts_mismatches or missing_fts or duplicate_fts:
            _raise_invalid(
                "FTS content is inconsistent with chunks/meetings "
                f"(mismatched={fts_mismatches}, missing={missing_fts}, "
                f"duplicate={duplicate_fts})"
            )

        integrity = [
            str(row[0])
            for row in conn.execute(f"PRAGMA {quote_identifier(schema)}.integrity_check")
        ]
        if integrity != ["ok"]:
            _raise_invalid(f"SQLite integrity_check failed: {integrity!r}")
        foreign_keys = conn.execute(
            f"PRAGMA {quote_identifier(schema)}.foreign_key_check"
        ).fetchall()
        if foreign_keys:
            _raise_invalid(f"SQLite foreign_key_check failed: {foreign_keys[:10]!r}")
    except IndexSnapshotError:
        raise
    except sqlite3.Error as exc:
        _raise_invalid("integrity/FTS validation could not be completed", cause=exc)

    return {
        "source_uuid": source_uuid,
        "source_path": source_path,
        "source_revision": source_revision,
        "meetings": meeting_count,
        "chunks": chunk_count,
        "fts_rows": fts_count,
    }


def validate_index_snapshot_database(path: Path) -> dict[str, int | str]:
    index_path = Path(path)
    try:
        physical_path = index_path.resolve(strict=True)
    except OSError as exc:
        _raise_invalid(f"database does not exist or cannot be resolved: {index_path}", cause=exc)
    if not physical_path.is_file():
        _raise_invalid(f"database is not a regular file: {index_path}")
    _require_no_sidecars(physical_path)
    try:
        uri = f"{physical_path.as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA foreign_keys=ON")
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
            if journal_mode != "delete":
                _raise_invalid(f"journal_mode must be DELETE, got {journal_mode!r}")
            result = validate_index_snapshot_schema(conn)
    except IndexSnapshotError:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        _raise_invalid(f"database cannot be read: {exc}", cause=exc)
    _require_no_sidecars(physical_path)
    return result


def fsync_index_snapshot(path: Path) -> None:
    with Path(path).open("rb") as stream:
        os.fsync(stream.fileno())


def remove_index_snapshot(path: Path) -> None:
    index_path = Path(path)
    index_path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm", "-journal"):
        index_path.with_name(index_path.name + suffix).unlink(missing_ok=True)


def _expected_schema_manifest() -> tuple[tuple[str, str, str, str], ...]:
    with closing(sqlite3.connect(":memory:")) as conn:
        execute_sql_statements(conn, INDEX_SCHEMA_SQL, context="Index snapshot schema")
        return schema_manifest(conn, "main")


def _count(conn: sqlite3.Connection, schema: str, table: str) -> int:
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {quote_identifier(schema)}.{quote_identifier(table)}"
        ).fetchone()[0]
    )


def _require_no_sidecars(path: Path) -> None:
    sidecars = [
        sidecar
        for suffix in ("-wal", "-shm", "-journal")
        if (sidecar := path.with_name(path.name + suffix)).exists()
    ]
    if sidecars:
        _raise_invalid("SQLite sidecars remain: " + ", ".join(sidecar.name for sidecar in sidecars))


def _raise_invalid(reason: str, *, cause: BaseException | None = None) -> Never:
    message = f"Unsupported or damaged Meetily Memory index snapshot: {reason}."
    if cause is None:
        raise IndexSnapshotError(message)
    raise IndexSnapshotError(message) from cause
