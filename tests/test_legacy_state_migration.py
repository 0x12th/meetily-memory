from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from meetily_memory.cli.app import app
from meetily_memory.config.settings import load_app_settings
from meetily_memory.db.schema_family import STATE_APPLICATION_ID, STATE_SCHEMA_USER_VERSION
from meetily_memory.user_state import UserStateRepository

LEGACY_SCHEMA = """
CREATE TABLE sources (
  uuid TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  current_path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0,
  projected_path TEXT,
  pending_revision INTEGER
);
CREATE TABLE tags (
  id INTEGER PRIMARY KEY,
  normalized_name TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE meeting_tags (
  source_uuid TEXT NOT NULL,
  meeting_external_id TEXT NOT NULL,
  tag_id INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL,
  PRIMARY KEY (source_uuid, meeting_external_id, tag_id)
);
"""


def test_cli_migrates_v07_state_and_preserves_tags_and_settings(tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    source_path = tmp_path / "meeting_minutes.sqlite"
    source_path.touch()
    source_uuid = "source-1"

    with sqlite3.connect(state_path) as conn:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            """
            INSERT INTO sources (
              uuid, kind, current_path, created_at, updated_at, revision,
              projected_path, pending_revision
            ) VALUES (?, 'meetily_sqlite', ?, '2026-08-01', '2026-08-29', 3, ?, NULL)
            """,
            (source_uuid, str(source_path), str(source_path)),
        )
        conn.execute(
            "INSERT INTO tags VALUES (7, 'agent', 'Agent', '2026-08-20')"
        )
        conn.execute(
            """
            INSERT INTO meeting_tags
            VALUES (?, 'meeting-1', 7, 'manual', '2026-08-21')
            """,
            (source_uuid,),
        )
        conn.execute("PRAGMA user_version = 7")
        conn.commit()

    (tmp_path / "settings.json").write_text(
        """{
          "source_uuid": "source-1",
          "ui_language": "ru",
          "last_update_at": "2026-08-29T10:00:00Z",
          "obsidian": {
            "vault_path": "/tmp/vault",
            "folder": "Meetings",
            "last_sync_at": "2026-08-29T09:00:00Z"
          }
        }""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["--index", str(index_path), "config", "language", "en"],
    )
    assert result.exit_code == 0, result.output

    with sqlite3.connect(state_path) as conn:
        assert int(conn.execute("PRAGMA application_id").fetchone()[0]) == STATE_APPLICATION_ID
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == STATE_SCHEMA_USER_VERSION
        assert conn.execute(
            "SELECT id, normalized_name, display_name FROM manual_tags"
        ).fetchall() == [(7, "agent", "Agent")]
        assert conn.execute(
            "SELECT source_uuid, meeting_external_id, manual_tag_id FROM meeting_tags"
        ).fetchall() == [(source_uuid, "meeting-1", 7)]
        assert conn.execute("SELECT revision FROM sources").fetchone()[0] == 3

    settings = load_app_settings(state_path)
    assert settings.source_uuid == source_uuid
    assert settings.ui_language == "en"
    assert settings.last_update_at == "2026-08-29T10:00:00Z"
    assert settings.obsidian.vault_path == "/tmp/vault"
    assert settings.obsidian.folder == "Meetings"
    assert settings.obsidian.last_sync_at == "2026-08-29T09:00:00Z"
    assert state_path.with_name("state.sqlite.v0.7.bak").is_file()
    assert UserStateRepository.open_existing(state_path).get_source_binding(source_uuid) is not None


def test_unknown_application_id_zero_database_is_not_migrated(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    with sqlite3.connect(state_path) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        conn.execute("PRAGMA user_version = 7")
        conn.commit()

    before = state_path.read_bytes()
    result = CliRunner().invoke(
        app,
        ["--index", str(tmp_path / "index.sqlite"), "status"],
    )
    assert result.exit_code != 0
    assert state_path.read_bytes() == before
    assert not state_path.with_name("state.sqlite.v0.7.bak").exists()
