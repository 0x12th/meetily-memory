from __future__ import annotations

import sqlite3
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import Generator

from meetily_memory.db.migrations import CURRENT_SCHEMA_VERSION, LATEST_IN_PLACE_SCHEMA_VERSION
from meetily_memory.scanner.meetily_sqlite import validate_meetily_schema
from meetily_memory.user_state import (
    CURRENT_USER_STATE_SCHEMA_VERSION,
    INDEX_GENERATION_STATE_SCHEMA_VERSION,
    PENDING_SOURCE_BINDING_SCHEMA_VERSION,
    SOURCE_REVISION_SCHEMA_VERSION,
    TOPIC_ALIAS_STATE_SCHEMA_VERSION,
)

DatabaseStatus = Literal["missing", "current", "legacy", "incompatible"]
ScanRunPayload = dict[str, object]
ScanDiagnostics = dict[str, ScanRunPayload | None]
SQLITE_ROLLBACK_JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")
DATABASE_PAIR_PIN_ATTEMPTS = 3

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
CURRENT_BASE_INDEX_COLUMNS = {
    **BASE_INDEX_COLUMNS,
    "sources": BASE_INDEX_COLUMNS["sources"] | {"source_uuid"},
    "scan_runs": CURRENT_SCAN_RUN_COLUMNS,
}
INDEX_COLUMNS_BY_VERSION = {
    1: BASE_INDEX_COLUMNS,
    2: BASE_INDEX_COLUMNS | STRUCTURED_INDEX_COLUMNS,
    3: BASE_INDEX_COLUMNS
    | STRUCTURED_INDEX_COLUMNS
    | KNOWLEDGE_INDEX_COLUMNS
    | {"task_status_overrides": TASK_STATUS_OVERRIDE_COLUMNS},
    4: BASE_INDEX_COLUMNS | STRUCTURED_INDEX_COLUMNS | KNOWLEDGE_INDEX_COLUMNS,
    LATEST_IN_PLACE_SCHEMA_VERSION: CURRENT_BASE_INDEX_COLUMNS
    | {"sources": BASE_INDEX_COLUMNS["sources"]}
    | STRUCTURED_INDEX_COLUMNS
    | KNOWLEDGE_INDEX_COLUMNS,
    CURRENT_SCHEMA_VERSION: CURRENT_BASE_INDEX_COLUMNS
    | STRUCTURED_INDEX_COLUMNS
    | KNOWLEDGE_INDEX_COLUMNS
    | {"index_generation": {"singleton", "generation_id", "alias_owner"}},
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
STATE_COLUMNS_V2 = STATE_COLUMNS_V1 | {
    "tags": {"id", "normalized_name", "display_name", "created_at"},
    "meeting_tags": {
        "source_uuid",
        "meeting_external_id",
        "tag_id",
        "source",
        "created_at",
    },
}
STATE_COLUMNS_V3 = {
    **STATE_COLUMNS_V2,
    "migration_reports": STATE_COLUMNS_V1["migration_reports"] | {"migration_key"},
    "migration_report_items": {
        "report_id",
        "legacy_action_item_id",
        "task_state_id",
        "legacy_intent_digest",
        "task_identity_digest",
        "outcome",
    },
}
STATE_COLUMNS_V4 = {
    **STATE_COLUMNS_V3,
    "sources": STATE_COLUMNS_V1["sources"] | {"revision"},
}
STATE_COLUMNS_V5 = {
    **STATE_COLUMNS_V4,
    "sources": STATE_COLUMNS_V4["sources"] | {"projected_path", "pending_revision"},
}
STATE_COLUMNS_V6 = {
    **STATE_COLUMNS_V5,
    "topic_alias_topics": {
        "stable_key",
        "title",
        "normalized_title",
        "created_at",
        "updated_at",
        "raw_metadata_json",
    },
    "topic_aliases": {"normalized_alias", "topic_stable_key", "alias", "created_at"},
    "topic_alias_imports": {
        "index_path",
        "source_schema_version",
        "source_alias_count",
        "source_digest",
        "imported_at",
    },
}
CURRENT_STATE_COLUMNS = {
    **STATE_COLUMNS_V6,
    "index_generations": {
        "generation_id",
        "index_path",
        "alias_owner",
        "registered_at",
    },
    "topic_alias_imports": STATE_COLUMNS_V6["topic_alias_imports"] | {"generation_id"},
}
STATE_COLUMNS_BY_VERSION = {
    1: STATE_COLUMNS_V1,
    2: STATE_COLUMNS_V2,
    3: STATE_COLUMNS_V3,
    SOURCE_REVISION_SCHEMA_VERSION: STATE_COLUMNS_V4,
    PENDING_SOURCE_BINDING_SCHEMA_VERSION: STATE_COLUMNS_V5,
    TOPIC_ALIAS_STATE_SCHEMA_VERSION: STATE_COLUMNS_V6,
    INDEX_GENERATION_STATE_SCHEMA_VERSION: CURRENT_STATE_COLUMNS,
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
class PinnedDatabasePath:
    logical_path: Path
    physical_path: Path
    present: bool
    error: str | None


@dataclass
class PinnedDatabaseReader:
    target: PinnedDatabasePath
    connection: sqlite3.Connection | None = None
    error: str | None = None


class DiagnosticDatabaseUnavailableError(RuntimeError):
    target: PinnedDatabasePath

    def __init__(self, target: PinnedDatabasePath, message: str) -> None:
        super().__init__(message)
        self.target = target


@dataclass(frozen=True)
class LocalDiagnostics:
    index_database: DatabaseDiagnostic
    state_database: DatabaseDiagnostic
    stats: dict[str, int]
    scan_runs: ScanDiagnostics
    dominant_meeting_language: str | None
    configured_source_path: Path | None
    index_target: PinnedDatabasePath
    state_target: PinnedDatabasePath


@dataclass(frozen=True)
class DatabaseStatusDiagnostics:
    local: LocalDiagnostics
    migration_report: dict[str, int] | None
    orphaned_tag_assignments: int | None
    details_error: str | None


@dataclass(frozen=True)
class SourceDatabaseDiagnostic:
    readable: bool
    schema_valid: bool
    schema_error: str | None
    read_error: str | None


def _diagnostic_checkpoint(_name: str) -> None:
    return


def _database_pair_checkpoint(_name: str) -> None:
    return


def pin_database_path(path: Path) -> PinnedDatabasePath:
    logical_path = Path(path)
    present, error, physical_path = database_path_state(logical_path)
    return PinnedDatabasePath(logical_path, physical_path, present, error)


def pin_database_path_from_parent(
    logical_path: Path,
    physical_parent: Path,
) -> PinnedDatabasePath:
    physical_candidate = physical_parent / logical_path.name
    present, error, physical_path = database_path_state(
        logical_path,
        physical_candidate=physical_candidate,
    )
    return PinnedDatabasePath(logical_path, physical_path, present, error)


def inaccessible_database_pair(
    logical_index_path: Path,
    logical_state_path: Path,
    message: str,
) -> tuple[PinnedDatabasePath, PinnedDatabasePath]:
    return (
        PinnedDatabasePath(
            logical_index_path,
            logical_index_path,
            present=True,
            error=message,
        ),
        PinnedDatabasePath(
            logical_state_path,
            logical_state_path,
            present=True,
            error=message,
        ),
    )


def pin_local_database_paths(index_path: Path) -> tuple[PinnedDatabasePath, PinnedDatabasePath]:
    logical_index_path = Path(index_path)
    logical_state_path = logical_index_path.with_name("state.sqlite")
    if logical_index_path.parent == logical_state_path.parent:
        try:
            physical_parent = logical_index_path.parent.resolve(strict=True)
        except FileNotFoundError:
            return (
                PinnedDatabasePath(
                    logical_index_path,
                    logical_index_path,
                    present=False,
                    error=None,
                ),
                PinnedDatabasePath(
                    logical_state_path,
                    logical_state_path,
                    present=False,
                    error=None,
                ),
            )
        except OSError as exc:
            message = f"Unable to pin database directory {logical_index_path.parent}: {exc}"
            return inaccessible_database_pair(logical_index_path, logical_state_path, message)
        _database_pair_checkpoint("after_shared_parent")
        for _attempt in range(DATABASE_PAIR_PIN_ATTEMPTS):
            first_index = pin_database_path_from_parent(logical_index_path, physical_parent)
            _database_pair_checkpoint("after_first_child")
            first_state = pin_database_path_from_parent(logical_state_path, physical_parent)
            second_index = pin_database_path_from_parent(logical_index_path, physical_parent)
            second_state = pin_database_path_from_parent(logical_state_path, physical_parent)
            first = (first_index, first_state)
            second = (second_index, second_state)
            if first == second:
                return second
        message = "Database child path pair changed while the diagnostic snapshot was being pinned."
        return inaccessible_database_pair(logical_index_path, logical_state_path, message)

    for _attempt in range(DATABASE_PAIR_PIN_ATTEMPTS):
        first = (pin_database_path(logical_index_path), pin_database_path(logical_state_path))
        _database_pair_checkpoint("between_pair_snapshots")
        second = (pin_database_path(logical_index_path), pin_database_path(logical_state_path))
        if first == second:
            return second
    message = "Database path pair changed while the diagnostic snapshot was being pinned."
    return inaccessible_database_pair(logical_index_path, logical_state_path, message)


def open_pinned_database(
    stack: ExitStack,
    target: PinnedDatabasePath,
) -> PinnedDatabaseReader:
    reader = PinnedDatabaseReader(target)
    if not target.present or target.error is not None:
        return reader
    journal_error = active_journal_error(target.physical_path)
    if journal_error is not None:
        reader.error = journal_error
        return reader
    try:
        reader.connection = stack.enter_context(readonly_sqlite_connection(target.physical_path))
    except (OSError, sqlite3.Error) as exc:
        reader.error = f"Unable to open database {target.logical_path}: {exc}"
    return reader


def guard_database_reader(reader: PinnedDatabaseReader) -> None:
    if reader.connection is None:
        if reader.error is not None:
            raise DiagnosticDatabaseUnavailableError(reader.target, reader.error)
        return
    journal_error = active_journal_error(reader.target.physical_path)
    if journal_error is None:
        return
    reader.error = journal_error
    raise DiagnosticDatabaseUnavailableError(reader.target, journal_error)


def require_database_reader_available(reader: PinnedDatabaseReader) -> None:
    if reader.error is not None:
        raise DiagnosticDatabaseUnavailableError(reader.target, reader.error)
    if reader.connection is not None:
        guard_database_reader(reader)


def collect_database_reader_errors(
    *readers: PinnedDatabaseReader,
) -> tuple[DiagnosticDatabaseUnavailableError, ...]:
    errors: list[DiagnosticDatabaseUnavailableError] = []
    for reader in readers:
        try:
            require_database_reader_available(reader)
        except DiagnosticDatabaseUnavailableError as exc:
            errors.append(exc)
    return tuple(errors)


def merge_database_reader_errors(
    *groups: tuple[DiagnosticDatabaseUnavailableError, ...],
) -> tuple[DiagnosticDatabaseUnavailableError, ...]:
    merged: dict[PinnedDatabasePath, DiagnosticDatabaseUnavailableError] = {}
    for group in groups:
        for error in group:
            merged[error.target] = error
    return tuple(merged.values())


def database_reader_errors_message(
    errors: tuple[DiagnosticDatabaseUnavailableError, ...],
) -> str:
    return "; ".join(str(error) for error in errors)


def database_reader_connection(
    reader: PinnedDatabaseReader,
    database_name: str,
) -> sqlite3.Connection:
    if reader.connection is None:
        message = f"{database_name} database is unavailable: {reader.target.logical_path}"
        raise DiagnosticDatabaseUnavailableError(reader.target, message)
    return reader.connection


def inspect_open_local_databases(
    index_reader: PinnedDatabaseReader,
    state_reader: PinnedDatabaseReader,
    source_uuid: str | None,
) -> LocalDiagnostics:
    index_database, stats, scan_runs, dominant_language = inspect_index_database_reader(
        index_reader
    )
    state_database, configured_source_path = inspect_state_database_reader(
        state_reader, source_uuid
    )
    return LocalDiagnostics(
        index_database=index_database,
        state_database=state_database,
        stats=stats,
        scan_runs=scan_runs,
        dominant_meeting_language=dominant_language,
        configured_source_path=configured_source_path,
        index_target=index_reader.target,
        state_target=state_reader.target,
    )


def inspect_local_databases(index_path: Path, source_uuid: str | None) -> LocalDiagnostics:
    index_target, state_target = pin_local_database_paths(index_path)
    with ExitStack() as stack:
        index_reader = open_pinned_database(stack, index_target)
        state_reader = open_pinned_database(stack, state_target)
        local = inspect_open_local_databases(index_reader, state_reader, source_uuid)
        errors = collect_database_reader_errors(index_reader, state_reader)
        return mark_local_databases_unavailable(local, errors)


def inspect_database_status(index_path: Path) -> DatabaseStatusDiagnostics:
    index_target, state_target = pin_local_database_paths(index_path)
    with ExitStack() as stack:
        index_reader = open_pinned_database(stack, index_target)
        state_reader = open_pinned_database(stack, state_target)
        local = inspect_open_local_databases(index_reader, state_reader, None)
        _diagnostic_checkpoint("db_status:after_initial_inspection")
        errors = collect_database_reader_errors(index_reader, state_reader)
        if errors:
            return DatabaseStatusDiagnostics(
                local=mark_local_databases_unavailable(local, errors),
                migration_report=None,
                orphaned_tag_assignments=None,
                details_error=database_reader_errors_message(errors),
            )
        try:
            migration_report = read_latest_migration_report(state_reader, local.state_database)
            orphaned_tag_assignments = count_orphaned_tag_assignments(
                index_reader,
                state_reader,
                local.index_database,
                local.state_database,
            )
        except DiagnosticDatabaseUnavailableError as exc:
            errors = merge_database_reader_errors(
                (exc,),
                collect_database_reader_errors(index_reader, state_reader),
            )
            return DatabaseStatusDiagnostics(
                local=mark_local_databases_unavailable(local, errors),
                migration_report=None,
                orphaned_tag_assignments=None,
                details_error=database_reader_errors_message(errors),
            )
        errors = collect_database_reader_errors(index_reader, state_reader)
        if errors:
            return DatabaseStatusDiagnostics(
                local=mark_local_databases_unavailable(local, errors),
                migration_report=None,
                orphaned_tag_assignments=None,
                details_error=database_reader_errors_message(errors),
            )
    return DatabaseStatusDiagnostics(
        local=local,
        migration_report=migration_report,
        orphaned_tag_assignments=orphaned_tag_assignments,
        details_error=orphaned_tag_assignments_error(
            local.index_database,
            local.state_database,
        ),
    )


def mark_local_database_unavailable(
    diagnostics: LocalDiagnostics,
    error: DiagnosticDatabaseUnavailableError,
) -> LocalDiagnostics:
    index_database = diagnostics.index_database
    state_database = diagnostics.state_database
    stats = diagnostics.stats
    scan_runs = diagnostics.scan_runs
    dominant_language = diagnostics.dominant_meeting_language
    configured_source_path = diagnostics.configured_source_path
    if error.target == diagnostics.index_target:
        index_database = DatabaseDiagnostic(
            index_database.path,
            "incompatible",
            index_database.schema_version,
            index_database.current_schema_version,
            str(error),
        )
        stats = empty_stats()
        scan_runs = empty_scan_diagnostics()
        dominant_language = None
    elif error.target == diagnostics.state_target:
        state_database = DatabaseDiagnostic(
            state_database.path,
            "incompatible",
            state_database.schema_version,
            state_database.current_schema_version,
            str(error),
        )
        configured_source_path = None
    return LocalDiagnostics(
        index_database=index_database,
        state_database=state_database,
        stats=stats,
        scan_runs=scan_runs,
        dominant_meeting_language=dominant_language,
        configured_source_path=configured_source_path,
        index_target=diagnostics.index_target,
        state_target=diagnostics.state_target,
    )


def mark_local_databases_unavailable(
    diagnostics: LocalDiagnostics,
    errors: tuple[DiagnosticDatabaseUnavailableError, ...],
) -> LocalDiagnostics:
    for error in errors:
        diagnostics = mark_local_database_unavailable(diagnostics, error)
    return diagnostics


def inspect_index_database(
    index_path: Path,
) -> tuple[DatabaseDiagnostic, dict[str, int], ScanDiagnostics, str | None]:
    target = pin_database_path(index_path)
    with ExitStack() as stack:
        return inspect_index_database_reader(open_pinned_database(stack, target))


def inspect_index_database_reader(
    reader: PinnedDatabaseReader,
) -> tuple[DatabaseDiagnostic, dict[str, int], ScanDiagnostics, str | None]:
    target = reader.target
    if not target.present:
        return (
            DatabaseDiagnostic(target.logical_path, "missing", None, CURRENT_SCHEMA_VERSION),
            empty_stats(),
            empty_scan_diagnostics(),
            None,
        )
    if target.error is not None or reader.error is not None:
        return (
            DatabaseDiagnostic(
                target.logical_path,
                "incompatible",
                None,
                CURRENT_SCHEMA_VERSION,
                target.error or reader.error,
            ),
            empty_stats(),
            empty_scan_diagnostics(),
            None,
        )

    schema_version: int | None = None
    try:
        guard_database_reader(reader)
        conn = database_reader_connection(reader, "Index")
        schema_version = read_schema_version(conn)
        database = version_diagnostic(
            target.logical_path,
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
            result = database, empty_stats(), empty_scan_diagnostics(), None
        else:
            result = (
                database,
                read_stats(conn, tables),
                read_scan_diagnostics(conn, tables),
                read_dominant_language(conn, tables),
            )
        guard_database_reader(reader)
    except DiagnosticDatabaseUnavailableError as exc:
        error = str(exc)
    except (OSError, sqlite3.Error, ValueError) as exc:
        error = f"Unable to inspect index database: {exc}"
    else:
        return result
    return (
        DatabaseDiagnostic(
            target.logical_path,
            "incompatible",
            schema_version,
            CURRENT_SCHEMA_VERSION,
            error,
        ),
        empty_stats(),
        empty_scan_diagnostics(),
        None,
    )


def inspect_state_database(
    state_path: Path,
    source_uuid: str | None,
) -> tuple[DatabaseDiagnostic, Path | None]:
    target = pin_database_path(state_path)
    with ExitStack() as stack:
        return inspect_state_database_reader(
            open_pinned_database(stack, target),
            source_uuid,
        )


def inspect_state_database_reader(
    reader: PinnedDatabaseReader,
    source_uuid: str | None,
) -> tuple[DatabaseDiagnostic, Path | None]:
    target = reader.target
    if not target.present:
        return (
            DatabaseDiagnostic(
                target.logical_path,
                "missing",
                None,
                CURRENT_USER_STATE_SCHEMA_VERSION,
            ),
            None,
        )
    if target.error is not None or reader.error is not None:
        return (
            DatabaseDiagnostic(
                target.logical_path,
                "incompatible",
                None,
                CURRENT_USER_STATE_SCHEMA_VERSION,
                target.error or reader.error,
            ),
            None,
        )

    schema_version: int | None = None
    try:
        guard_database_reader(reader)
        conn = database_reader_connection(reader, "User-state")
        schema_version = read_schema_version(conn)
        database = version_diagnostic(
            target.logical_path,
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
        configured_source = (
            None
            if database.status == "incompatible"
            else read_configured_source(conn, tables, source_uuid)
        )
        guard_database_reader(reader)
    except DiagnosticDatabaseUnavailableError as exc:
        error = str(exc)
    except (OSError, sqlite3.Error, ValueError) as exc:
        error = f"Unable to inspect user-state database: {exc}"
    else:
        return database, configured_source
    return (
        DatabaseDiagnostic(
            target.logical_path,
            "incompatible",
            schema_version,
            CURRENT_USER_STATE_SCHEMA_VERSION,
            error,
        ),
        None,
    )


def read_latest_migration_report(
    state_reader: PinnedDatabaseReader,
    database: DatabaseDiagnostic,
) -> dict[str, int] | None:
    if database.status in {"missing", "incompatible"}:
        return None
    guard_database_reader(state_reader)
    conn = database_reader_connection(state_reader, "User-state")
    tables = table_names(conn)
    if "migration_reports" not in tables:
        result = None
    else:
        columns = table_columns(conn, "migration_reports")
        if not {"id", "migrated", "orphaned"}.issubset(columns):
            result = None
        else:
            row = conn.execute(
                """
                SELECT migrated, orphaned
                FROM migration_reports
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            result = (
                None
                if row is None
                else {"migrated": int(row["migrated"]), "orphaned": int(row["orphaned"])}
            )
    guard_database_reader(state_reader)
    return result


def orphaned_tag_assignments_error(
    index_database: DatabaseDiagnostic,
    state_database: DatabaseDiagnostic,
) -> str | None:
    unavailable = [
        ("index", index_database) for database in (index_database,) if database.status != "current"
    ]
    if state_database.status != "current":
        unavailable.append(("user-state", state_database))
    if not unavailable:
        return None
    reasons = " and ".join(
        f"the {label} database status is {database.status}" for label, database in unavailable
    )
    message = (
        f"Orphaned tag assignments are unavailable because {reasons}; "
        "current readable index and user-state databases are required."
    )
    errors = " ".join(
        database.error for _label, database in unavailable if database.error is not None
    )
    return f"{message} {errors}" if errors else message


def count_orphaned_tag_assignments(
    index_reader: PinnedDatabaseReader,
    state_reader: PinnedDatabaseReader,
    index_database: DatabaseDiagnostic,
    state_database: DatabaseDiagnostic,
) -> int | None:
    if index_database.status != "current" or state_database.status != "current":
        return None
    assignments = read_tag_assignment_refs(state_reader, state_database)
    indexed = read_indexed_meeting_refs(index_reader, index_database)
    return sum(assignment not in indexed for assignment in assignments)


def read_tag_assignment_refs(
    state_reader: PinnedDatabaseReader,
    database: DatabaseDiagnostic,
) -> tuple[tuple[str, str], ...]:
    if database.status in {"missing", "incompatible"}:
        return ()
    guard_database_reader(state_reader)
    conn = database_reader_connection(state_reader, "User-state")
    tables = table_names(conn)
    if "meeting_tags" not in tables:
        result: tuple[tuple[str, str], ...] = ()
    else:
        columns = table_columns(conn, "meeting_tags")
        if not {"source_uuid", "meeting_external_id"}.issubset(columns):
            result = ()
        else:
            result = tuple(
                (str(row["source_uuid"]), str(row["meeting_external_id"]))
                for row in conn.execute(
                    "SELECT source_uuid, meeting_external_id FROM meeting_tags ORDER BY tag_id"
                ).fetchall()
            )
    guard_database_reader(state_reader)
    return result


def read_indexed_meeting_refs(
    index_reader: PinnedDatabaseReader,
    database: DatabaseDiagnostic,
) -> set[tuple[str, str]]:
    if database.status != "current":
        return set()
    guard_database_reader(index_reader)
    conn = database_reader_connection(index_reader, "Index")
    result = {
        (str(row["source_uuid"]), str(row["external_id"]))
        for row in conn.execute(
            """
            SELECT s.source_uuid, m.external_id
            FROM meetings m
            JOIN sources s ON s.id = m.source_id
            """
        ).fetchall()
    }
    guard_database_reader(index_reader)
    return result


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
    present, path_error, physical_path = database_path_state(source_path)
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
    journal_error = active_journal_error(physical_path)
    if journal_error:
        return SourceDatabaseDiagnostic(
            readable=False,
            schema_valid=False,
            schema_error=None,
            read_error=journal_error,
        )

    schema_error: str | None = None
    try:
        with readonly_sqlite_connection(physical_path) as conn:
            try:
                validate_meetily_schema(conn)
            except (RuntimeError, sqlite3.Error) as exc:
                schema_error = str(exc)
            journal_error = active_journal_error(physical_path)
            if journal_error is not None:
                return SourceDatabaseDiagnostic(
                    readable=False,
                    schema_valid=False,
                    schema_error=None,
                    read_error=journal_error,
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
        schema_valid=schema_error is None,
        schema_error=schema_error,
        read_error=None,
    )


@contextmanager
def readonly_sqlite_connection(
    physical_path: Path,
) -> Generator[sqlite3.Connection, None, None]:
    uri = f"file:{quote(str(physical_path))}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        _ = conn.execute("PRAGMA query_only=ON")
        yield conn


def database_path_state(
    path: Path,
    *,
    physical_candidate: Path | None = None,
) -> tuple[bool, str | None, Path]:
    candidate = path if physical_candidate is None else physical_candidate
    try:
        physical_path = candidate.resolve(strict=True)
        path_stat = physical_path.stat()
    except FileNotFoundError:
        return False, None, path
    except OSError as exc:
        return True, f"Unable to access database path {path}: {exc}", path
    if not S_ISREG(path_stat.st_mode):
        return True, f"Database path is not a regular file: {path}", path
    return True, None, physical_path


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
