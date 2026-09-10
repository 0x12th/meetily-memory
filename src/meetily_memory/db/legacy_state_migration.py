from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from meetily_memory.db.state_schema import create_state_schema, validate_state_database
from meetily_memory.json_codec import loads_json

LEGACY_STATE_USER_VERSION = 7
LEGACY_STATE_APPLICATION_ID = 0
LEGACY_REQUIRED_TABLES = frozenset({"sources", "tags", "meeting_tags"})


def migrate_v07_state_if_needed(state_path: Path) -> bool:
    state_path = Path(state_path)
    if not state_path.is_file():
        return False

    with closing(sqlite3.connect(f"{state_path.resolve(strict=True).as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if application_id != LEGACY_STATE_APPLICATION_ID or user_version != LEGACY_STATE_USER_VERSION:
            return False
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if not LEGACY_REQUIRED_TABLES.issubset(tables):
            return False
        sources = [dict(row) for row in conn.execute("SELECT * FROM sources ORDER BY uuid")]
        tags = [dict(row) for row in conn.execute("SELECT * FROM tags ORDER BY id")]
        meeting_tags = [
            dict(row)
            for row in conn.execute(
                "SELECT source_uuid, meeting_external_id, tag_id, created_at FROM meeting_tags "
                "ORDER BY source_uuid, meeting_external_id, tag_id"
            )
        ]

    settings = _load_legacy_settings(state_path.with_name("settings.json"))
    temp_path = _temporary_state_path(state_path)
    try:
        with closing(sqlite3.connect(temp_path)) as conn:
            conn.row_factory = sqlite3.Row
            create_state_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                _copy_sources(conn, sources)
                _copy_tags(conn, tags)
                _copy_meeting_tags(conn, meeting_tags)
                _copy_settings(conn, settings, {str(row["uuid"]) for row in sources})
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        validate_state_database(temp_path)
        backup_path = state_path.with_name(f"{state_path.name}.v0.7.bak")
        if not backup_path.exists():
            shutil.copy2(state_path, backup_path)
        os.replace(temp_path, state_path)
        _fsync_directory(state_path.parent)
    finally:
        temp_path.unlink(missing_ok=True)
    return True


def _temporary_state_path(state_path: Path) -> Path:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(
        prefix=f".{state_path.name}.migration-",
        suffix=".tmp",
        dir=state_path.parent,
    )
    os.close(fd)
    path = Path(raw_path)
    path.unlink()
    return path


def _copy_sources(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    conn.executemany(
        """
        INSERT INTO sources (
          uuid, kind, current_path, created_at, updated_at, revision
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["uuid"],
                row["kind"],
                row["current_path"],
                row["created_at"],
                row["updated_at"],
                row.get("revision", 0),
            )
            for row in rows
        ],
    )


def _copy_tags(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    conn.executemany(
        """
        INSERT INTO manual_tags (id, normalized_name, display_name, created_at)
        VALUES (?, ?, ?, ?)
        """,
        [
            (row["id"], row["normalized_name"], row["display_name"], row["created_at"])
            for row in rows
        ],
    )


def _copy_meeting_tags(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    conn.executemany(
        """
        INSERT INTO meeting_tags (
          source_uuid, meeting_external_id, manual_tag_id, created_at
        ) VALUES (?, ?, ?, ?)
        """,
        [
            (row["source_uuid"], row["meeting_external_id"], row["tag_id"], row["created_at"])
            for row in rows
        ],
    )


def _copy_settings(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    source_uuids: set[str],
) -> None:
    source_uuid = _optional_string(payload.get("source_uuid"))
    if source_uuid not in source_uuids:
        source_uuid = None
    ui_language = _optional_string(payload.get("ui_language"))
    if ui_language not in {"en", "ru"}:
        ui_language = None
    obsidian = payload.get("obsidian")
    obsidian_payload = obsidian if isinstance(obsidian, dict) else {}
    folder = _optional_string(obsidian_payload.get("folder")) or "Meetily Memory"
    conn.execute(
        """
        UPDATE app_settings
        SET source_uuid = ?, ui_language = ?, last_update_at = ?,
            obsidian_vault_path = ?, obsidian_folder = ?, obsidian_last_sync_at = ?
        WHERE singleton = 1
        """,
        (
            source_uuid,
            ui_language,
            _optional_string(payload.get("last_update_at")),
            _optional_string(obsidian_payload.get("vault_path")),
            folder,
            _optional_string(obsidian_payload.get("last_sync_at")),
        ),
    )


def _load_legacy_settings(path: Path) -> dict[str, Any]:
    try:
        payload = loads_json(path.read_bytes())
    except FileNotFoundError:
        return {}
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
