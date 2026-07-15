import sqlite3
from collections.abc import Callable

CURRENT_SCHEMA_VERSION = 4

STRUCTURED_ENTITIES_SQL = """
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY,
  meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  source_chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  source TEXT NOT NULL,
  confidence REAL NOT NULL,
  fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  raw_metadata_json TEXT,
  UNIQUE(meeting_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS action_items (
  id INTEGER PRIMARY KEY,
  meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  source_chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  source TEXT NOT NULL,
  confidence REAL NOT NULL,
  fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  raw_metadata_json TEXT,
  UNIQUE(meeting_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS risks (
  id INTEGER PRIMARY KEY,
  meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  source_chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  source TEXT NOT NULL,
  confidence REAL NOT NULL,
  fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  raw_metadata_json TEXT,
  UNIQUE(meeting_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS open_questions (
  id INTEGER PRIMARY KEY,
  meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  source_chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  source TEXT NOT NULL,
  confidence REAL NOT NULL,
  fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  raw_metadata_json TEXT,
  UNIQUE(meeting_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_decisions_meeting ON decisions(meeting_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_action_items_meeting ON action_items(meeting_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_risks_meeting ON risks(meeting_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_open_questions_meeting ON open_questions(meeting_id, ordinal);
"""

KNOWLEDGE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_nodes (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL,
  stable_key TEXT NOT NULL,
  title TEXT NOT NULL,
  normalized_title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  raw_metadata_json TEXT,
  UNIQUE(type, stable_key)
);

CREATE TABLE IF NOT EXISTS knowledge_edges (
  id INTEGER PRIMARY KEY,
  from_node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
  relation TEXT NOT NULL,
  to_node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
  confidence REAL NOT NULL,
  source_meeting_id INTEGER REFERENCES meetings(id) ON DELETE CASCADE,
  source_chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
  extraction_method TEXT NOT NULL,
  created_at TEXT NOT NULL,
  raw_metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS topic_aliases (
  id INTEGER PRIMARY KEY,
  topic_node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
  alias TEXT NOT NULL,
  normalized_alias TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(normalized_alias)
);

CREATE TABLE IF NOT EXISTS task_status_overrides (
  action_item_id INTEGER PRIMARY KEY REFERENCES action_items(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  note TEXT,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_edges_unique
ON knowledge_edges(
  from_node_id,
  relation,
  to_node_id,
  source_meeting_id,
  IFNULL(source_chunk_id, 0),
  extraction_method
);

CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_type_key
ON knowledge_nodes(type, stable_key);

CREATE INDEX IF NOT EXISTS idx_knowledge_edges_source
ON knowledge_edges(source_meeting_id, source_chunk_id);

CREATE INDEX IF NOT EXISTS idx_topic_aliases_normalized
ON topic_aliases(normalized_alias);
"""

BASE_SCHEMA_SQL = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  label TEXT,
  external_app TEXT,
  external_version TEXT,
  last_seen_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(kind, path)
);

CREATE TABLE IF NOT EXISTS meetings (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
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
  UNIQUE(source_id, external_id)
);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  external_id TEXT,
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

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED,
  meeting_id UNINDEXED,
  title,
  text,
  speaker,
  tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY,
  display_name TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  email TEXT,
  external_ref TEXT,
  raw_metadata_json TEXT,
  UNIQUE(normalized_name, email)
);

CREATE TABLE IF NOT EXISTS meeting_people (
  meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  confidence REAL,
  source TEXT,
  PRIMARY KEY (meeting_id, person_id, role)
);

CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY,
  meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  format TEXT NOT NULL,
  content TEXT NOT NULL,
  source TEXT,
  created_at TEXT,
  updated_at TEXT,
  fingerprint TEXT NOT NULL,
  raw_metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS scan_runs (
  id INTEGER PRIMARY KEY,
  source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  meetings_seen INTEGER DEFAULT 0,
  meetings_inserted INTEGER DEFAULT 0,
  meetings_updated INTEGER DEFAULT 0,
  chunks_seen INTEGER DEFAULT 0,
  chunks_inserted INTEGER DEFAULT 0,
  chunks_updated INTEGER DEFAULT 0,
  errors_json TEXT
);

CREATE TABLE IF NOT EXISTS plugin_state (
  plugin_name TEXT NOT NULL,
  key TEXT NOT NULL,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (plugin_name, key)
);

CREATE INDEX IF NOT EXISTS idx_meetings_updated_at ON meetings(updated_at);
CREATE INDEX IF NOT EXISTS idx_meetings_started_at ON meetings(started_at);
CREATE INDEX IF NOT EXISTS idx_chunks_meeting_ordinal ON chunks(meeting_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_chunks_fingerprint ON chunks(fingerprint);
CREATE INDEX IF NOT EXISTS idx_people_normalized_name ON people(normalized_name);
"""


def migrate_to_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(BASE_SCHEMA_SQL)


def migrate_to_v2(conn: sqlite3.Connection) -> None:
    conn.executescript(STRUCTURED_ENTITIES_SQL)


def migrate_to_v3(conn: sqlite3.Connection) -> None:
    conn.executescript(KNOWLEDGE_SCHEMA_SQL)


def migrate_to_v4(conn: sqlite3.Connection) -> None:
    legacy_status_count = conn.execute("SELECT COUNT(*) FROM task_status_overrides").fetchone()[0]
    migration_ready = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'user_state_migration_ready'
        """
    ).fetchone()
    if legacy_status_count and migration_ready is None:
        message = "User state must be migrated before upgrading the disposable index to v4."
        raise RuntimeError(message)

    conn.execute("DROP TABLE task_status_overrides")
    for table in ("decisions", "action_items", "risks", "open_questions"):
        migrate_entity_table_to_required_chunk(conn, table)
    conn.execute("DROP TABLE IF EXISTS user_state_migration_ready")


def migrate_entity_table_to_required_chunk(conn: sqlite3.Connection, table: str) -> None:
    legacy_table = f"{table}_v3"
    conn.execute(f"ALTER TABLE {table} RENAME TO {legacy_table}")
    conn.execute(
        f"""
        CREATE TABLE {table} (
          id INTEGER PRIMARY KEY,
          meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
          source_chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL,
          text TEXT NOT NULL,
          source TEXT NOT NULL,
          confidence REAL NOT NULL,
          fingerprint TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          raw_metadata_json TEXT,
          UNIQUE(meeting_id, fingerprint)
        )
        """
    )
    conn.execute(
        f"""
        INSERT INTO {table} (
          id, meeting_id, source_chunk_id, ordinal, text, source, confidence,
          fingerprint, created_at, updated_at, raw_metadata_json
        )
        SELECT
          id, meeting_id, source_chunk_id, ordinal, text, source, confidence,
          fingerprint, created_at, updated_at, raw_metadata_json
        FROM {legacy_table}
        WHERE source_chunk_id IS NOT NULL
        """
    )
    conn.execute(f"DROP TABLE {legacy_table}")
    conn.execute(f"CREATE INDEX idx_{table}_meeting ON {table}(meeting_id, ordinal)")


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: migrate_to_v1,
    2: migrate_to_v2,
    3: migrate_to_v3,
    4: migrate_to_v4,
}
