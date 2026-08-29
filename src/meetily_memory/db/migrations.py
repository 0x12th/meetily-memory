import secrets
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from meetily_memory.db.migration_identity import (
    MIGRATION_REPORT_SCHEMA_VERSION,
    LegacyTaskState,
    canonical_database_path,
    legacy_state_migration_key,
    main_database_path,
    read_legacy_task_states,
    verified_migration_report,
)

LATEST_IN_PLACE_SCHEMA_VERSION = 5
SOURCE_AWARE_SCHEMA_VERSION = 6
CURRENT_SCHEMA_VERSION = 7
INDEX_ALIAS_OWNER_STATE = "state"
INDEX_ALIAS_OWNER_LEGACY = "legacy"
INDEX_GENERATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS index_generation (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  generation_id TEXT NOT NULL UNIQUE,
  alias_owner TEXT NOT NULL CHECK (alias_owner IN ('state', 'legacy'))
);
"""

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
CREATE INDEX IF NOT EXISTS idx_decisions_source_chunk ON decisions(source_chunk_id);
CREATE INDEX IF NOT EXISTS idx_action_items_source_chunk ON action_items(source_chunk_id);
CREATE INDEX IF NOT EXISTS idx_risks_source_chunk ON risks(source_chunk_id);
CREATE INDEX IF NOT EXISTS idx_open_questions_source_chunk ON open_questions(source_chunk_id);
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

LEGACY_SOURCE_SCHEMA_SQL = """
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
"""

CURRENT_SOURCE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  source_uuid TEXT NOT NULL UNIQUE,
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
"""

BASE_SCHEMA_SQL = f"""
{LEGACY_SOURCE_SCHEMA_SQL}
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
  errors_json TEXT,
  phase TEXT NOT NULL DEFAULT 'source_scan',
  error_message TEXT
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

CURRENT_STRUCTURED_ENTITIES_SQL = STRUCTURED_ENTITIES_SQL.replace(
    "source_chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL",
    "source_chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE",
)
CURRENT_KNOWLEDGE_SCHEMA_SQL = KNOWLEDGE_SCHEMA_SQL.replace(
    """CREATE TABLE IF NOT EXISTS task_status_overrides (
  action_item_id INTEGER PRIMARY KEY REFERENCES action_items(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  note TEXT,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

""",
    "",
)
SOURCE_AWARE_BASE_SCHEMA_SQL = BASE_SCHEMA_SQL.replace(
    LEGACY_SOURCE_SCHEMA_SQL,
    CURRENT_SOURCE_SCHEMA_SQL,
)
CURRENT_BASE_SCHEMA_SQL = SOURCE_AWARE_BASE_SCHEMA_SQL.replace(
    "  external_id TEXT,\n  kind TEXT NOT NULL,",
    "  external_id TEXT,\n  evidence_id TEXT NOT NULL UNIQUE,\n  kind TEXT NOT NULL,",
)
SOURCE_AWARE_INDEX_SCHEMA_SQL = (
    SOURCE_AWARE_BASE_SCHEMA_SQL
    + CURRENT_STRUCTURED_ENTITIES_SQL
    + CURRENT_KNOWLEDGE_SCHEMA_SQL
    + INDEX_GENERATION_SCHEMA_SQL
)
CURRENT_INDEX_SCHEMA_SQL = (
    CURRENT_BASE_SCHEMA_SQL
    + CURRENT_STRUCTURED_ENTITIES_SQL
    + CURRENT_KNOWLEDGE_SCHEMA_SQL
    + INDEX_GENERATION_SCHEMA_SQL
)


def execute_sql_statements(conn: sqlite3.Connection, script: str) -> None:
    """Execute a static SQL script without sqlite3's implicit executescript commit."""
    statement = ""
    for line in script.splitlines():
        statement = f"{statement}{line}\n"
        if not sqlite3.complete_statement(statement):
            continue
        if statement.strip():
            conn.execute(statement)
        statement = ""
    if statement.strip():
        msg = "SQL migration contains an incomplete statement."
        raise ValueError(msg)


def _migration_checkpoint(_name: str) -> None:
    # A narrow no-op seam lets tests interrupt real public repository upgrades.
    return


def _require_migration_predecessor(current_version: int, target_version: int) -> None:
    if current_version != target_version - 1:
        msg = (
            f"Cannot migrate index schema from version {current_version} "
            f"directly to version {target_version}."
        )
        raise RuntimeError(msg)


def _run_atomic_migration(
    conn: sqlite3.Connection,
    target_version: int,
    migration: Callable[[sqlite3.Connection], None],
) -> None:
    if conn.in_transaction:
        msg = f"Index migration to version {target_version} requires a clean connection."
        raise RuntimeError(msg)

    conn.execute("PRAGMA foreign_keys=ON")
    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        msg = "Index migrations require SQLite foreign-key enforcement."
        raise RuntimeError(msg)

    conn.execute("BEGIN IMMEDIATE")
    try:
        current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current_version >= target_version:
            conn.commit()
            return
        _require_migration_predecessor(current_version, target_version)
        migration(conn)
        _migration_checkpoint(f"v{target_version}:before_user_version")
        conn.execute(f"PRAGMA user_version = {target_version}")
        _migration_checkpoint(f"v{target_version}:after_user_version")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def ensure_index_generation_marker(
    conn: sqlite3.Connection,
    *,
    alias_owner: str,
    generation_id: str | None = None,
) -> str:
    if alias_owner not in {INDEX_ALIAS_OWNER_STATE, INDEX_ALIAS_OWNER_LEGACY}:
        message = f"Unsupported index alias owner: {alias_owner}."
        raise ValueError(message)
    execute_sql_statements(conn, INDEX_GENERATION_SCHEMA_SQL)
    rows = conn.execute(
        "SELECT generation_id, alias_owner FROM index_generation ORDER BY singleton"
    ).fetchall()
    if len(rows) > 1:
        message = "Index generation marker must contain at most one row."
        raise RuntimeError(message)
    if rows:
        stored_generation_id = str(rows[0][0])
        stored_owner = str(rows[0][1])
        if not stored_generation_id or stored_owner not in {
            INDEX_ALIAS_OWNER_STATE,
            INDEX_ALIAS_OWNER_LEGACY,
        }:
            message = "Index generation marker is invalid."
            raise RuntimeError(message)
        if generation_id is not None and generation_id != stored_generation_id:
            message = "Index generation marker conflicts with the requested generation ID."
            raise RuntimeError(message)
        return stored_generation_id
    generation_id = generation_id or f"gen-{secrets.token_hex(16)}"
    conn.execute(
        """
        INSERT INTO index_generation (singleton, generation_id, alias_owner)
        VALUES (1, ?, ?)
        """,
        (generation_id, alias_owner),
    )
    return generation_id


def read_index_generation_marker(conn: sqlite3.Connection) -> tuple[str, str] | None:
    table = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'index_generation'
        """
    ).fetchone()
    if table is None:
        return None
    try:
        rows = conn.execute(
            "SELECT singleton, generation_id, alias_owner FROM index_generation"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        message = "Index generation marker is not readable."
        raise RuntimeError(message) from exc
    if len(rows) != 1 or int(rows[0][0]) != 1:
        message = "Index generation marker must contain exactly one row."
        raise RuntimeError(message)
    generation_id = str(rows[0][1])
    alias_owner = str(rows[0][2])
    if not generation_id or alias_owner not in {
        INDEX_ALIAS_OWNER_STATE,
        INDEX_ALIAS_OWNER_LEGACY,
    }:
        message = "Index generation marker is invalid."
        raise RuntimeError(message)
    return generation_id, alias_owner


def initialize_current_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        msg = "Index schema initialization requires a clean connection."
        raise RuntimeError(msg)

    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        execute_sql_statements(conn, CURRENT_INDEX_SCHEMA_SQL)
        ensure_index_generation_marker(conn, alias_owner=INDEX_ALIAS_OWNER_STATE)
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    execute_sql_statements(conn, BASE_SCHEMA_SQL)


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    execute_sql_statements(conn, STRUCTURED_ENTITIES_SQL)


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    execute_sql_statements(conn, KNOWLEDGE_SCHEMA_SQL)


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    _require_verified_state_transfer(conn)

    conn.execute("DROP TABLE task_status_overrides")
    _migration_checkpoint("v4:task_status_overrides:dropped")
    for table in ("decisions", "action_items", "risks", "open_questions"):
        migrate_entity_table_to_required_chunk(conn, table)
    conn.execute("DROP TABLE IF EXISTS user_state_migration_ready")
    _migration_checkpoint("v4:user_state_migration_ready:dropped")


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(scan_runs)").fetchall()}
    if "phase" not in columns:
        conn.execute("ALTER TABLE scan_runs ADD COLUMN phase TEXT NOT NULL DEFAULT 'source_scan'")
        _migration_checkpoint("v5:scan_runs:phase_added")
    if "error_message" not in columns:
        conn.execute("ALTER TABLE scan_runs ADD COLUMN error_message TEXT")
        _migration_checkpoint("v5:scan_runs:error_message_added")


def _require_verified_state_transfer(conn: sqlite3.Connection) -> None:
    rows = read_legacy_task_states(conn)
    legacy_status_count = int(
        conn.execute("SELECT COUNT(*) FROM task_status_overrides").fetchone()[0]
    )
    if len(rows) != legacy_status_count:
        msg = "Not every legacy task status is represented in the v4 transfer snapshot."
        raise RuntimeError(msg)

    marker_exists = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'user_state_migration_ready'
        """
    ).fetchone()
    if marker_exists is None:
        if legacy_status_count == 0:
            return
        msg = "User state must be migrated before upgrading the disposable index to v4."
        raise RuntimeError(msg)

    try:
        markers = conn.execute(
            """
            SELECT
              migration_key, report_id, index_path, state_path,
              state_schema_version, expected, migrated, orphaned
            FROM user_state_migration_ready
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        msg = "The legacy user-state transfer marker is not verifiable."
        raise RuntimeError(msg) from exc

    if len(markers) != 1:
        msg = "The legacy user-state transfer marker must contain exactly one row."
        raise RuntimeError(msg)
    marker = markers[0]
    migration_key = str(marker[0])
    report_id = int(marker[1])
    index_path = str(marker[2])
    state_path = str(marker[3])
    state_schema_version = int(marker[4])
    expected = int(marker[5])
    migrated = int(marker[6])
    orphaned = int(marker[7])
    actual_index_path = main_database_path(conn)
    actual_migration_key = legacy_state_migration_key(actual_index_path, rows)
    if (
        not migration_key
        or report_id <= 0
        or index_path != actual_index_path
        or state_path != canonical_database_path(state_path)
        or state_path == actual_index_path
        or state_schema_version != MIGRATION_REPORT_SCHEMA_VERSION
        or migration_key != actual_migration_key
        or expected != legacy_status_count
        or expected != migrated + orphaned
    ):
        msg = "The legacy user-state transfer marker does not match the locked legacy rows."
        raise RuntimeError(msg)

    _require_durable_state_report(
        state_path=state_path,
        state_schema_version=state_schema_version,
        migration_key=migration_key,
        report_id=report_id,
        index_path=index_path,
        rows=rows,
        migrated=migrated,
        orphaned=orphaned,
    )


def _require_durable_state_report(  # noqa: PLR0913
    *,
    state_path: str,
    state_schema_version: int,
    migration_key: str,
    report_id: int,
    index_path: str,
    rows: list[LegacyTaskState],
    migrated: int,
    orphaned: int,
) -> None:
    path = Path(state_path)
    if not path.is_file():
        msg = "The persistent state database for the legacy transfer is missing."
        raise RuntimeError(msg)
    state_uri = f"{path.as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(state_uri, uri=True)) as state_conn:
            actual_version = int(state_conn.execute("PRAGMA user_version").fetchone()[0])
            report = verified_migration_report(
                state_conn,
                migration_key,
                index_path,
                rows,
            )
    except sqlite3.Error as exc:
        msg = "The persistent legacy migration report could not be read."
        raise RuntimeError(msg) from exc
    # The marker pins the migration-report contract; later additive state schemas remain valid.
    if (
        actual_version < state_schema_version
        or report is None
        or report.report_id != report_id
        or report.migrated != migrated
        or report.orphaned != orphaned
    ):
        msg = "The persistent legacy migration report does not match the index marker."
        raise RuntimeError(msg)


def migrate_to_v1(conn: sqlite3.Connection) -> None:
    _run_atomic_migration(conn, 1, _migrate_to_v1)


def migrate_to_v2(conn: sqlite3.Connection) -> None:
    _run_atomic_migration(conn, 2, _migrate_to_v2)


def migrate_to_v3(conn: sqlite3.Connection) -> None:
    _run_atomic_migration(conn, 3, _migrate_to_v3)


def migrate_to_v4(conn: sqlite3.Connection) -> None:
    _run_atomic_migration(conn, 4, _migrate_to_v4)


def migrate_to_v5(conn: sqlite3.Connection) -> None:
    _run_atomic_migration(conn, 5, _migrate_to_v5)


def migrate_entity_table_to_required_chunk(conn: sqlite3.Connection, table: str) -> None:
    legacy_table = f"{table}_v3"
    conn.execute(f"ALTER TABLE {table} RENAME TO {legacy_table}")
    _migration_checkpoint(f"v4:{table}:renamed")
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
    _migration_checkpoint(f"v4:{table}:created")
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
    _migration_checkpoint(f"v4:{table}:copied")
    conn.execute(f"DROP TABLE {legacy_table}")
    _migration_checkpoint(f"v4:{table}:legacy_dropped")
    conn.execute(f"CREATE INDEX idx_{table}_meeting ON {table}(meeting_id, ordinal)")
    conn.execute(f"CREATE INDEX idx_{table}_source_chunk ON {table}(source_chunk_id)")
    _migration_checkpoint(f"v4:{table}:index_created")


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: migrate_to_v1,
    2: migrate_to_v2,
    3: migrate_to_v3,
    4: migrate_to_v4,
    5: migrate_to_v5,
}
