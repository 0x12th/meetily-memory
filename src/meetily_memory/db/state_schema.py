# ruff: noqa: S608

from __future__ import annotations

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
    STATE_APPLICATION_ID,
    STATE_SCHEMA_EPOCH,
    STATE_SCHEMA_FAMILY,
    STATE_SCHEMA_USER_VERSION,
)

STATE_REINITIALIZE_INSTRUCTION = (
    "Delete the unsupported `state.sqlite` together with the disposable `index.sqlite`, then "
    "run `mm init --source PATH` or `mm refresh --source PATH`. Deleting state permanently "
    "loses manual tags and application settings."
)

STATE_SCHEMA_SQL = f"""
CREATE TABLE state_meta (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  schema_family TEXT NOT NULL CHECK (schema_family = '{STATE_SCHEMA_FAMILY}'),
  schema_epoch INTEGER NOT NULL CHECK (schema_epoch = {STATE_SCHEMA_EPOCH})
);

CREATE TABLE sources (
  uuid TEXT PRIMARY KEY CHECK (length(trim(uuid)) > 0),
  kind TEXT NOT NULL CHECK (length(trim(kind)) > 0),
  current_path TEXT NOT NULL CHECK (length(current_path) > 0),
  created_at TEXT NOT NULL CHECK (length(created_at) > 0),
  updated_at TEXT NOT NULL CHECK (length(updated_at) > 0),
  revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
  UNIQUE (kind, current_path)
);

CREATE TABLE app_settings (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  source_uuid TEXT REFERENCES sources(uuid) ON DELETE RESTRICT,
  ui_language TEXT CHECK (ui_language IS NULL OR ui_language IN ('en', 'ru')),
  last_update_at TEXT,
  obsidian_vault_path TEXT,
  obsidian_folder TEXT NOT NULL DEFAULT 'Meetily Memory'
    CHECK (length(trim(obsidian_folder)) > 0),
  obsidian_last_sync_at TEXT
);
CREATE INDEX idx_app_settings_source_uuid ON app_settings(source_uuid);

CREATE TABLE manual_tags (
  id INTEGER PRIMARY KEY,
  normalized_name TEXT NOT NULL UNIQUE CHECK (length(trim(normalized_name)) > 0),
  display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
  created_at TEXT NOT NULL CHECK (length(created_at) > 0)
);

CREATE TABLE meeting_tags (
  source_uuid TEXT NOT NULL REFERENCES sources(uuid) ON DELETE RESTRICT,
  meeting_external_id TEXT NOT NULL CHECK (length(meeting_external_id) > 0),
  manual_tag_id INTEGER NOT NULL REFERENCES manual_tags(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL CHECK (length(created_at) > 0),
  PRIMARY KEY (source_uuid, meeting_external_id, manual_tag_id)
);
CREATE INDEX idx_meeting_tags_meeting
ON meeting_tags(source_uuid, meeting_external_id);
CREATE INDEX idx_meeting_tags_manual_tag_id
ON meeting_tags(manual_tag_id);
"""

APPLICATION_TABLES = frozenset(
    {"state_meta", "sources", "app_settings", "manual_tags", "meeting_tags"}
)


class StateSchemaError(RuntimeError):
    """An existing state database is not the exact supported state family."""


def create_state_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        message = "State schema creation requires a clean SQLite connection."
        raise RuntimeError(message)
    if application_objects(conn):
        reason = "State schema creation is fresh-only; the database is not empty."
        message = f"{reason} {STATE_REINITIALIZE_INSTRUCTION}"
        raise StateSchemaError(message)

    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        execute_sql_statements(conn, STATE_SCHEMA_SQL, context="State schema")
        conn.execute(
            "INSERT INTO state_meta (singleton, schema_family, schema_epoch) VALUES (1, ?, ?)",
            (STATE_SCHEMA_FAMILY, STATE_SCHEMA_EPOCH),
        )
        conn.execute("INSERT INTO app_settings (singleton) VALUES (1)")
        conn.execute(f"PRAGMA application_id = {STATE_APPLICATION_ID}")
        conn.execute(f"PRAGMA user_version = {STATE_SCHEMA_USER_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def create_state_database(path: Path) -> None:
    state_path = Path(path)
    if state_path.exists():
        reason = f"Refusing to initialize existing state database: {state_path}."
        message = f"{reason} {STATE_REINITIALIZE_INSTRUCTION}"
        raise StateSchemaError(message)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(state_path)) as conn:
            create_state_schema(conn)
            validate_state_schema(conn)
    except BaseException:
        state_path.unlink(missing_ok=True)
        raise


def validate_state_schema(  # noqa: C901, PLR0912
    conn: sqlite3.Connection,
    *,
    schema: str = "main",
) -> None:
    try:
        application_id = pragma_int(conn, schema, "application_id")
        user_version = pragma_int(conn, schema, "user_version")
    except sqlite3.Error as exc:
        _raise_invalid("database header or PRAGMAs are unreadable", cause=exc)

    if application_id != STATE_APPLICATION_ID:
        if application_id == INDEX_APPLICATION_ID:
            reason = "database belongs to the index schema family, not the state family"
        else:
            reason = f"foreign application_id 0x{application_id:08X}"
        _raise_invalid(reason)
    if user_version != STATE_SCHEMA_USER_VERSION:
        if user_version > STATE_SCHEMA_USER_VERSION:
            reason = (
                f"future state user_version {user_version}; this binary supports "
                f"{STATE_SCHEMA_USER_VERSION}"
            )
        else:
            reason = (
                f"unsupported state user_version {user_version}; exact version "
                f"{STATE_SCHEMA_USER_VERSION} is required"
            )
        _raise_invalid(reason)

    try:
        meta = conn.execute(
            f"SELECT singleton, schema_family, schema_epoch "
            f"FROM {quote_identifier(schema)}.state_meta"
        ).fetchall()
    except sqlite3.Error as exc:
        _raise_invalid("state_meta is missing or unreadable", cause=exc)
    if len(meta) != 1 or tuple(meta[0]) != (1, STATE_SCHEMA_FAMILY, STATE_SCHEMA_EPOCH):
        if len(meta) == 1 and int(meta[0][2]) > STATE_SCHEMA_EPOCH:
            _raise_invalid(
                f"future state epoch {meta[0][2]}; this binary supports {STATE_SCHEMA_EPOCH}"
            )
        _raise_invalid("state_meta family/epoch identity is invalid")

    try:
        settings_singletons = conn.execute(
            f"SELECT singleton FROM {quote_identifier(schema)}.app_settings ORDER BY singleton"
        ).fetchall()
    except sqlite3.Error as exc:
        _raise_invalid("app_settings is missing or unreadable", cause=exc)
    if [tuple(row) for row in settings_singletons] != [(1,)]:
        _raise_invalid("app_settings singleton row is missing or invalid")

    try:
        actual_manifest = schema_manifest(conn, schema)
        expected_manifest = _expected_schema_manifest()
    except sqlite3.Error as exc:
        _raise_invalid("schema manifest is unreadable", cause=exc)
    if actual_manifest != expected_manifest:
        _raise_invalid("schema objects do not exactly match the supported state epoch")

    try:
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
    except StateSchemaError:
        raise
    except sqlite3.Error as exc:
        _raise_invalid("integrity validation could not be completed", cause=exc)


def validate_state_database(path: Path) -> None:
    state_path = Path(path)
    try:
        physical_path = state_path.resolve(strict=True)
    except OSError as exc:
        reason = f"State database does not exist or cannot be resolved: {state_path}."
        message = f"{reason} {STATE_REINITIALIZE_INSTRUCTION}"
        raise StateSchemaError(message) from exc
    try:
        with closing(sqlite3.connect(f"{physical_path.as_uri()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA foreign_keys=ON")
            validate_state_schema(conn)
    except StateSchemaError:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        _raise_invalid(f"database cannot be read: {exc}", cause=exc)


def _expected_schema_manifest() -> tuple[tuple[str, str, str, str], ...]:
    with closing(sqlite3.connect(":memory:")) as conn:
        create_state_schema(conn)
        return schema_manifest(conn, "main")


def _raise_invalid(reason: str, *, cause: BaseException | None = None) -> Never:
    message = (
        f"Unsupported or damaged Meetily Memory state database: {reason}. "
        f"{STATE_REINITIALIZE_INSTRUCTION}"
    )
    if cause is None:
        raise StateSchemaError(message)
    raise StateSchemaError(message) from cause
