from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import Generator

from meetily_memory.db.migrations import CURRENT_SCHEMA_VERSION
from meetily_memory.scanner.meetily_sqlite import validate_meetily_schema
from meetily_memory.user_state import CURRENT_USER_STATE_SCHEMA_VERSION

DatabaseStatus = Literal["missing", "current", "legacy", "incompatible"]
ScanRunPayload = dict[str, object]
ScanDiagnostics = dict[str, ScanRunPayload | None]
SQLITE_ROLLBACK_JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")

INDEX_STAT_QUERIES = {
    "meetings": ("meetings", "SELECT COUNT(*) FROM meetings"),
    "chunks": ("chunks", "SELECT COUNT(*) FROM chunks"),
    "sources": ("sources", "SELECT COUNT(*) FROM sources"),
    "decisions": ("decisions", "SELECT COUNT(*) FROM decisions"),
    "action_items": ("action_items", "SELECT COUNT(*) FROM action_items"),
    "risks": ("risks", "SELECT COUNT(*) FROM risks"),
    "open_questions": ("open_questions", "SELECT COUNT(*) FROM open_questions"),
    "knowledge_nodes": ("knowledge_nodes", "SELECT COUNT(*) FROM knowledge_nodes"),
    "knowledge_edges": ("knowledge_edges", "SELECT COUNT(*) FROM knowledge_edges"),
}
LEGACY_SCAN_RUN_COLUMNS = {
    "id",
    "source_id",
    "started_at",
    "finished_at",
    "status",
    "meetings_seen",
    "meetings_inserted",
    "meetings_updated",
    "chunks_seen",
    "chunks_inserted",
    "chunks_updated",
    "errors_json",
}
CURRENT_SCAN_RUN_COLUMNS = LEGACY_SCAN_RUN_COLUMNS | {"phase", "error_message"}
BASE_INDEX_COLUMNS = {
    "sources": {
        "id",
        "kind",
        "path",
        "label",
        "external_app",
        "external_version",
        "last_seen_at",
        "created_at",
        "updated_at",
    },
    "meetings": {
        "id",
        "source_id",
        "external_id",
        "title",
        "started_at",
        "ended_at",
        "created_at",
        "updated_at",
        "folder_path",
        "source_path",
        "language",
        "summary_text",
        "raw_summary_json",
        "raw_metadata_json",
        "fingerprint",
        "indexed_at",
    },
    "chunks": {
        "id",
        "meeting_id",
        "external_id",
        "kind",
        "ordinal",
        "text",
        "speaker",
        "starts_at_seconds",
        "ends_at_seconds",
        "timestamp_label",
        "token_count",
        "fingerprint",
        "raw_metadata_json",
    },
    "chunks_fts": {"chunk_id", "meeting_id", "title", "text", "speaker"},
    "people": {
        "id",
        "display_name",
        "normalized_name",
        "email",
        "external_ref",
        "raw_metadata_json",
    },
    "meeting_people": {"meeting_id", "person_id", "role", "confidence", "source"},
    "artifacts": {
        "id",
        "meeting_id",
        "kind",
        "format",
        "content",
        "source",
        "created_at",
        "updated_at",
        "fingerprint",
        "raw_metadata_json",
    },
    "scan_runs": LEGACY_SCAN_RUN_COLUMNS,
    "plugin_state": {"plugin_name", "key", "value_json", "updated_at"},
}
ENTITY_COLUMNS = {
    "id",
    "meeting_id",
    "source_chunk_id",
    "ordinal",
    "text",
    "source",
    "confidence",
    "fingerprint",
    "created_at",
    "updated_at",
    "raw_metadata_json",
}
STRUCTURED_INDEX_COLUMNS = dict.fromkeys(
    ("decisions", "action_items", "risks", "open_questions"),
    ENTITY_COLUMNS,
)
KNOWLEDGE_INDEX_COLUMNS = {
    "knowledge_nodes": {
        "id",
        "type",
        "stable_key",
        "title",
        "normalized_title",
        "created_at",
        "updated_at",
        "raw_metadata_json",
    },
    "knowledge_edges": {
        "id",
        "from_node_id",
        "relation",
        "to_node_id",
        "confidence",
        "source_meeting_id",
        "source_chunk_id",
        "extraction_method",
        "created_at",
        "raw_metadata_json",
    },
    "topic_aliases": {"id", "topic_node_id", "alias", "normalized_alias", "created_at"},
}
TASK_STATUS_OVERRIDE_COLUMNS = {
    "action_item_id",
    "status",
    "note",
    "source",
    "created_at",
    "updated_at",
}
CURRENT_BASE_INDEX_COLUMNS = {**BASE_INDEX_COLUMNS, "scan_runs": CURRENT_SCAN_RUN_COLUMNS}
INDEX_COLUMNS_BY_VERSION = {
    1: BASE_INDEX_COLUMNS,
    2: BASE_INDEX_COLUMNS | STRUCTURED_INDEX_COLUMNS,
    3: BASE_INDEX_COLUMNS
    | STRUCTURED_INDEX_COLUMNS
    | KNOWLEDGE_INDEX_COLUMNS
    | {"task_status_overrides": TASK_STATUS_OVERRIDE_COLUMNS},
    4: BASE_INDEX_COLUMNS | STRUCTURED_INDEX_COLUMNS | KNOWLEDGE_INDEX_COLUMNS,
    CURRENT_SCHEMA_VERSION: CURRENT_BASE_INDEX_COLUMNS
    | STRUCTURED_INDEX_COLUMNS
    | KNOWLEDGE_INDEX_COLUMNS,
}
STATE_COLUMNS_V1 = {
    "sources": {"uuid", "kind", "current_path", "created_at", "updated_at"},
    "task_states": {
        "id",
        "source_uuid",
        "meeting_external_id",
        "chunk_external_id",
        "entity_kind",
        "content_fingerprint",
        "status",
        "note",
        "source",
        "orphaned",
        "orphaned_reason",
        "legacy_action_item_id",
        "created_at",
        "updated_at",
    },
    "migration_reports": {"id", "index_path", "migrated", "orphaned", "created_at"},
}
CURRENT_STATE_COLUMNS = STATE_COLUMNS_V1 | {
    "tags": {"id", "normalized_name", "display_name", "created_at"},
    "meeting_tags": {
        "source_uuid",
        "meeting_external_id",
        "tag_id",
        "source",
        "created_at",
    },
}
STATE_COLUMNS_BY_VERSION = {
    1: STATE_COLUMNS_V1,
    CURRENT_USER_STATE_SCHEMA_VERSION: CURRENT_STATE_COLUMNS,
}


@dataclass(frozen=True)
class DatabaseDiagnostic:
    path: Path
    status: DatabaseStatus
    schema_version: int | None
    current_schema_version: int
    error: str | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "status": self.status,
            "schema_version": self.schema_version,
            "current_schema_version": self.current_schema_version,
            "error": self.error,
        }

    def status_label(self) -> str:
        if self.schema_version is None:
            return self.status
        return (
            f"{self.status} (schema {self.schema_version}; current {self.current_schema_version})"
        )


@dataclass(frozen=True)
class LocalDiagnostics:
    index_database: DatabaseDiagnostic
    state_database: DatabaseDiagnostic
    stats: dict[str, int]
    scan_runs: ScanDiagnostics
    dominant_meeting_language: str | None
    configured_source_path: Path | None


@dataclass(frozen=True)
class SourceDatabaseDiagnostic:
    readable: bool
    schema_valid: bool
    schema_error: str | None
    read_error: str | None


def inspect_local_databases(index_path: Path, source_uuid: str | None) -> LocalDiagnostics:
    index_path = Path(index_path)
    state_path = index_path.with_name("state.sqlite")
    index_database, stats, scan_runs, dominant_language = inspect_index_database(index_path)
    state_database, configured_source_path = inspect_state_database(state_path, source_uuid)
    return LocalDiagnostics(
        index_database=index_database,
        state_database=state_database,
        stats=stats,
        scan_runs=scan_runs,
        dominant_meeting_language=dominant_language,
        configured_source_path=configured_source_path,
    )


def inspect_index_database(
    index_path: Path,
) -> tuple[DatabaseDiagnostic, dict[str, int], ScanDiagnostics, str | None]:
    index_path = Path(index_path)
    present, path_error = database_path_state(index_path)
    if not present:
        return (
            DatabaseDiagnostic(index_path, "missing", None, CURRENT_SCHEMA_VERSION),
            empty_stats(),
            empty_scan_diagnostics(),
            None,
        )
    if path_error:
        return (
            DatabaseDiagnostic(
                index_path,
                "incompatible",
                None,
                CURRENT_SCHEMA_VERSION,
                path_error,
            ),
            empty_stats(),
            empty_scan_diagnostics(),
            None,
        )
    journal_error = active_journal_error(index_path)
    if journal_error:
        return (
            DatabaseDiagnostic(
                index_path,
                "incompatible",
                None,
                CURRENT_SCHEMA_VERSION,
                journal_error,
            ),
            empty_stats(),
            empty_scan_diagnostics(),
            None,
        )

    schema_version: int | None = None
    try:
        with readonly_sqlite_connection(index_path) as conn:
            schema_version = read_schema_version(conn)
            database = version_diagnostic(
                index_path,
                schema_version,
                CURRENT_SCHEMA_VERSION,
                database_name="index",
            )
            tables = table_names(conn)
            database = validate_versioned_schema(
                conn,
                database,
                tables,
                INDEX_COLUMNS_BY_VERSION,
                database_name="index",
            )
            if database.status == "incompatible":
                return database, empty_stats(), empty_scan_diagnostics(), None
            return (
                database,
                read_stats(conn, tables),
                read_scan_diagnostics(conn, tables),
                read_dominant_language(conn, tables),
            )
    except (OSError, sqlite3.Error, ValueError) as exc:
        return (
            DatabaseDiagnostic(
                index_path,
                "incompatible",
                schema_version,
                CURRENT_SCHEMA_VERSION,
                f"Unable to inspect index database: {exc}",
            ),
            empty_stats(),
            empty_scan_diagnostics(),
            None,
        )


def inspect_state_database(
    state_path: Path,
    source_uuid: str | None,
) -> tuple[DatabaseDiagnostic, Path | None]:
    state_path = Path(state_path)
    present, path_error = database_path_state(state_path)
    if not present:
        return (
            DatabaseDiagnostic(state_path, "missing", None, CURRENT_USER_STATE_SCHEMA_VERSION),
            None,
        )
    if path_error:
        return (
            DatabaseDiagnostic(
                state_path,
                "incompatible",
                None,
                CURRENT_USER_STATE_SCHEMA_VERSION,
                path_error,
            ),
            None,
        )
    journal_error = active_journal_error(state_path)
    if journal_error:
        return (
            DatabaseDiagnostic(
                state_path,
                "incompatible",
                None,
                CURRENT_USER_STATE_SCHEMA_VERSION,
                journal_error,
            ),
            None,
        )

    schema_version: int | None = None
    try:
        with readonly_sqlite_connection(state_path) as conn:
            schema_version = read_schema_version(conn)
            database = version_diagnostic(
                state_path,
                schema_version,
                CURRENT_USER_STATE_SCHEMA_VERSION,
                database_name="user-state",
            )
            tables = table_names(conn)
            database = validate_versioned_schema(
                conn,
                database,
                tables,
                STATE_COLUMNS_BY_VERSION,
                database_name="user-state",
            )
            if database.status == "incompatible":
                return database, None
            return database, read_configured_source(conn, tables, source_uuid)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return (
            DatabaseDiagnostic(
                state_path,
                "incompatible",
                schema_version,
                CURRENT_USER_STATE_SCHEMA_VERSION,
                f"Unable to inspect user-state database: {exc}",
            ),
            None,
        )


def inspect_source_database(  # noqa: PLR0911
    source_path: Path | None,
) -> SourceDatabaseDiagnostic:
    if source_path is None:
        return SourceDatabaseDiagnostic(
            readable=False,
            schema_valid=False,
            schema_error=None,
            read_error=None,
        )

    source_path = Path(source_path)
    present, path_error = database_path_state(source_path)
    if not present:
        return SourceDatabaseDiagnostic(
            readable=False,
            schema_valid=False,
            schema_error=None,
            read_error=f"Meetily DB not found: {source_path}",
        )
    if path_error:
        return SourceDatabaseDiagnostic(
            readable=False,
            schema_valid=False,
            schema_error=None,
            read_error=path_error,
        )
    journal_error = active_journal_error(source_path)
    if journal_error:
        return SourceDatabaseDiagnostic(
            readable=False,
            schema_valid=False,
            schema_error=None,
            read_error=journal_error,
        )

    try:
        with readonly_sqlite_connection(source_path) as conn:
            try:
                validate_meetily_schema(conn)
            except (RuntimeError, sqlite3.Error) as exc:
                return SourceDatabaseDiagnostic(
                    readable=True,
                    schema_valid=False,
                    schema_error=str(exc),
                    read_error=None,
                )
    except (OSError, sqlite3.Error) as exc:
        return SourceDatabaseDiagnostic(
            readable=False,
            schema_valid=False,
            schema_error=None,
            read_error=str(exc),
        )
    return SourceDatabaseDiagnostic(
        readable=True,
        schema_valid=True,
        schema_error=None,
        read_error=None,
    )


@contextmanager
def readonly_sqlite_connection(path: Path) -> Generator[sqlite3.Connection, None, None]:
    uri = f"file:{quote(str(Path(path).resolve()))}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        _ = conn.execute("PRAGMA query_only=ON")
        yield conn


def database_path_state(path: Path) -> tuple[bool, str | None]:
    try:
        path_stat = path.stat()
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        return True, f"Unable to access database path {path}: {exc}"
    if not S_ISREG(path_stat.st_mode):
        return True, f"Database path is not a regular file: {path}"
    return True, None


def active_journal_error(path: Path) -> str | None:  # noqa: PLR0911
    wal_path = path.with_name(path.name + "-wal")
    try:
        if wal_path.stat().st_size > 0:
            return active_sidecar_message("WAL")
    except FileNotFoundError:
        pass
    except OSError as exc:
        return f"Unable to inspect SQLite WAL sidecar {wal_path}: {exc}"

    journal_path = path.with_name(path.name + "-journal")
    try:
        if journal_path.stat().st_size == 0:
            return None
        with journal_path.open("rb") as journal_file:
            header = journal_file.read(len(SQLITE_ROLLBACK_JOURNAL_MAGIC))
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"Unable to inspect SQLite rollback journal sidecar {journal_path}: {exc}"
    if header == bytes(len(SQLITE_ROLLBACK_JOURNAL_MAGIC)):
        return None
    return active_sidecar_message("rollback journal")


def active_sidecar_message(label: str) -> str:
    return (
        f"SQLite database has an active {label} sidecar and cannot be inspected "
        "without risking sidecar changes. Retry after the writer closes."
    )


def read_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    if row is None:
        msg = "SQLite did not return a schema version."
        raise ValueError(msg)
    return int(row[0])


def version_diagnostic(
    path: Path,
    schema_version: int,
    current_schema_version: int,
    *,
    database_name: str,
) -> DatabaseDiagnostic:
    if schema_version < current_schema_version:
        status: DatabaseStatus = "legacy"
        error = None
    elif schema_version == current_schema_version:
        status = "current"
        error = None
    else:
        status = "incompatible"
        error = (
            f"Unsupported {database_name} schema version {schema_version}; "
            f"this binary supports {current_schema_version}."
        )
    return DatabaseDiagnostic(path, status, schema_version, current_schema_version, error)


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute("SELECT name FROM pragma_table_info(?)", (table,)).fetchall()
    return {str(row[0]) for row in rows}


def validate_versioned_schema(
    conn: sqlite3.Connection,
    database: DatabaseDiagnostic,
    tables: set[str],
    columns_by_version: dict[int, dict[str, set[str]]],
    *,
    database_name: str,
) -> DatabaseDiagnostic:
    if database.schema_version is None or database.status == "incompatible":
        return database
    required_columns = columns_by_version.get(database.schema_version)
    if required_columns is None:
        return DatabaseDiagnostic(
            database.path,
            "incompatible",
            database.schema_version,
            database.current_schema_version,
            f"Unsupported legacy {database_name} schema version {database.schema_version}.",
        )
    problems: list[str] = []
    missing_tables = sorted(set(required_columns) - tables)
    if missing_tables:
        problems.append("missing tables: " + ", ".join(missing_tables))
    for table, expected_columns in required_columns.items():
        if table not in tables:
            continue
        missing_columns = sorted(expected_columns - table_columns(conn, table))
        if missing_columns:
            problems.append(f"{table} missing columns: {', '.join(missing_columns)}")
    if not problems:
        return database
    return DatabaseDiagnostic(
        database.path,
        "incompatible",
        database.schema_version,
        database.current_schema_version,
        f"{database_name.capitalize()} schema is incomplete: " + "; ".join(problems),
    )


def empty_stats() -> dict[str, int]:
    return dict.fromkeys(INDEX_STAT_QUERIES, 0)


def read_stats(conn: sqlite3.Connection, tables: set[str]) -> dict[str, int]:
    stats = empty_stats()
    for key, (table, query) in INDEX_STAT_QUERIES.items():
        if table not in tables:
            continue
        row = conn.execute(query).fetchone()
        if row is not None:
            stats[key] = int(row[0])
    return stats


def empty_scan_diagnostics() -> ScanDiagnostics:
    return {
        "last_completed_run": None,
        "last_failed_run": None,
        "last_running_run": None,
    }


def read_scan_diagnostics(conn: sqlite3.Connection, tables: set[str]) -> ScanDiagnostics:
    diagnostics = empty_scan_diagnostics()
    if "scan_runs" not in tables:
        return diagnostics
    columns = table_columns(conn, "scan_runs")
    if not {"id", "status"}.issubset(columns):
        return diagnostics

    completed = scan_run_for_status(conn, "completed")
    failed = scan_run_for_status(conn, "failed")
    running = scan_run_for_status(conn, "running")
    if failed and completed:
        failed_id = failed["id"]
        completed_id = completed["id"]
        if (
            isinstance(failed_id, int)
            and isinstance(completed_id, int)
            and failed_id < completed_id
        ):
            failed = None
    diagnostics["last_completed_run"] = completed
    diagnostics["last_failed_run"] = failed
    diagnostics["last_running_run"] = running
    return diagnostics


def scan_run_for_status(conn: sqlite3.Connection, status: str) -> ScanRunPayload | None:
    row = conn.execute(
        "SELECT * FROM scan_runs WHERE status = ? ORDER BY id DESC LIMIT 1",
        (status,),
    ).fetchone()
    if row is None:
        return None
    payload = {str(key): value for key, value in zip(row.keys(), row, strict=True)}
    payload.setdefault("phase", "source_scan")
    payload.setdefault("error_message", None)
    return payload


def read_dominant_language(conn: sqlite3.Connection, tables: set[str]) -> str | None:
    if "meetings" not in tables or "language" not in table_columns(conn, "meetings"):
        return None
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
    return str(row[0]) if row else None


def read_configured_source(
    conn: sqlite3.Connection,
    tables: set[str],
    source_uuid: str | None,
) -> Path | None:
    if source_uuid is None or "sources" not in tables:
        return None
    if not {"uuid", "current_path"}.issubset(table_columns(conn, "sources")):
        return None
    row = conn.execute(
        "SELECT current_path FROM sources WHERE uuid = ?",
        (source_uuid,),
    ).fetchone()
    if row is None:
        return None
    return Path(str(row[0])).expanduser()
