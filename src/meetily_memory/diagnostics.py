from __future__ import annotations

import sqlite3
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING, Literal, Never
from urllib.parse import quote

from meetily_memory.db.index_snapshot import IndexSnapshotError, validate_index_snapshot_schema
from meetily_memory.db.row_decode import decode_required_integer, decode_required_text
from meetily_memory.db.schema_family import INDEX_SCHEMA_USER_VERSION, STATE_SCHEMA_USER_VERSION
from meetily_memory.db.state_schema import StateSchemaError, validate_state_schema
from meetily_memory.scanner.meetily_sqlite import validate_meetily_schema

if TYPE_CHECKING:
    from collections.abc import Generator

DatabaseStatus = Literal["missing", "current", "incompatible"]
SQLITE_ROLLBACK_JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")
DATABASE_PAIR_PIN_ATTEMPTS = 3


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


@dataclass(frozen=True)
class LocalDiagnostics:
    index_database: DatabaseDiagnostic
    state_database: DatabaseDiagnostic
    stats: dict[str, int]
    dominant_meeting_language: str | None
    configured_source_path: Path | None
    index_target: PinnedDatabasePath
    state_target: PinnedDatabasePath


@dataclass(frozen=True)
class DatabaseStatusDiagnostics:
    local: LocalDiagnostics
    orphaned_tag_assignments: int | None
    details_error: str | None


@dataclass(frozen=True)
class SourceDatabaseDiagnostic:
    readable: bool
    schema_valid: bool
    schema_error: str | None
    read_error: str | None


class DiagnosticDatabaseUnavailableError(RuntimeError):
    def __init__(self, target: PinnedDatabasePath, message: str) -> None:
        super().__init__(message)
        self.target = target


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
    present, error, physical_path = database_path_state(
        logical_path,
        physical_candidate=physical_parent / logical_path.name,
    )
    return PinnedDatabasePath(logical_path, physical_path, present, error)


def pin_local_database_paths(index_path: Path) -> tuple[PinnedDatabasePath, PinnedDatabasePath]:
    logical_index = Path(index_path)
    logical_state = logical_index.with_name("state.sqlite")
    try:
        physical_parent = logical_index.parent.resolve(strict=True)
    except FileNotFoundError:
        return (
            PinnedDatabasePath(logical_index, logical_index, present=False, error=None),
            PinnedDatabasePath(logical_state, logical_state, present=False, error=None),
        )
    except OSError as exc:
        message = f"Unable to pin database directory {logical_index.parent}: {exc}"
        return (
            PinnedDatabasePath(logical_index, logical_index, present=True, error=message),
            PinnedDatabasePath(logical_state, logical_state, present=True, error=message),
        )
    for _attempt in range(DATABASE_PAIR_PIN_ATTEMPTS):
        first = (
            pin_database_path_from_parent(logical_index, physical_parent),
            pin_database_path_from_parent(logical_state, physical_parent),
        )
        _database_pair_checkpoint("between_pair_snapshots")
        second = (
            pin_database_path_from_parent(logical_index, physical_parent),
            pin_database_path_from_parent(logical_state, physical_parent),
        )
        if first == second:
            return second
    message = "Database path pair changed while the diagnostic snapshot was being pinned."
    return (
        PinnedDatabasePath(logical_index, logical_index, present=True, error=message),
        PinnedDatabasePath(logical_state, logical_state, present=True, error=message),
    )


def open_pinned_database(stack: ExitStack, target: PinnedDatabasePath) -> PinnedDatabaseReader:
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


def inspect_local_databases(index_path: Path, source_uuid: str | None) -> LocalDiagnostics:
    index_target, state_target = pin_local_database_paths(index_path)
    with ExitStack() as stack:
        index_reader = open_pinned_database(stack, index_target)
        state_reader = open_pinned_database(stack, state_target)
        index_database, stats, language = inspect_index_database_reader(index_reader)
        state_database, configured = inspect_state_database_reader(state_reader, source_uuid)
    return LocalDiagnostics(
        index_database=index_database,
        state_database=state_database,
        stats=stats,
        dominant_meeting_language=language,
        configured_source_path=configured,
        index_target=index_target,
        state_target=state_target,
    )


def inspect_database_status(index_path: Path) -> DatabaseStatusDiagnostics:
    index_target, state_target = pin_local_database_paths(index_path)
    with ExitStack() as stack:
        index_reader = open_pinned_database(stack, index_target)
        state_reader = open_pinned_database(stack, state_target)
        index_database, stats, language = inspect_index_database_reader(index_reader)
        state_database, configured = inspect_state_database_reader(state_reader, None)
        local = LocalDiagnostics(
            index_database=index_database,
            state_database=state_database,
            stats=stats,
            dominant_meeting_language=language,
            configured_source_path=configured,
            index_target=index_target,
            state_target=state_target,
        )
        details_error = orphaned_tag_assignments_error(index_database, state_database)
        orphaned = count_orphaned_tag_assignments(
            index_reader,
            state_reader,
            index_database,
            state_database,
        )
    return DatabaseStatusDiagnostics(local, orphaned, details_error)


def inspect_index_database(
    index_path: Path,
) -> tuple[DatabaseDiagnostic, dict[str, int], str | None]:
    target = pin_database_path(index_path)
    with ExitStack() as stack:
        return inspect_index_database_reader(open_pinned_database(stack, target))


def inspect_index_database_reader(
    reader: PinnedDatabaseReader,
) -> tuple[DatabaseDiagnostic, dict[str, int], str | None]:
    target = reader.target
    if not target.present:
        return _missing_index(target.logical_path), empty_stats(), None
    if target.error or reader.error:
        return (
            _bad_index(target.logical_path, None, target.error or reader.error),
            empty_stats(),
            None,
        )
    schema_version: int | None = None
    try:
        conn = _required_connection(reader, "index")
        schema_version = read_schema_version(conn)
        journal_row = conn.execute("PRAGMA journal_mode").fetchone()
        if journal_row is None:
            _raise_index_snapshot("index journal_mode PRAGMA returned no value")
        journal_mode = decode_required_text(
            journal_row[0],
            table="pragma",
            column="journal_mode",
            context="index diagnostics",
            error_type=IndexSnapshotError,
        ).casefold()
        if journal_mode != "delete":
            _raise_index_snapshot(f"index journal_mode must be DELETE, got {journal_mode!r}")
        validate_index_snapshot_schema(conn)
        stats = {
            "meetings": _diagnostic_count(conn, "meetings"),
            "chunks": _diagnostic_count(conn, "chunks"),
            "sources": _diagnostic_count(conn, "index_meta"),
        }
        language = read_dominant_language(conn)
    except (IndexSnapshotError, OSError, sqlite3.Error, ValueError) as exc:
        return _bad_index(target.logical_path, schema_version, str(exc)), empty_stats(), None
    return (
        DatabaseDiagnostic(
            target.logical_path,
            "current",
            schema_version,
            INDEX_SCHEMA_USER_VERSION,
        ),
        stats,
        language,
    )


def inspect_state_database(
    state_path: Path,
    source_uuid: str | None,
) -> tuple[DatabaseDiagnostic, Path | None]:
    target = pin_database_path(state_path)
    with ExitStack() as stack:
        return inspect_state_database_reader(open_pinned_database(stack, target), source_uuid)


def inspect_state_database_reader(
    reader: PinnedDatabaseReader,
    source_uuid: str | None,
) -> tuple[DatabaseDiagnostic, Path | None]:
    target = reader.target
    if not target.present:
        return (
            DatabaseDiagnostic(target.logical_path, "missing", None, STATE_SCHEMA_USER_VERSION),
            None,
        )
    if target.error or reader.error:
        return _bad_state(target.logical_path, None, target.error or reader.error), None
    schema_version: int | None = None
    try:
        conn = _required_connection(reader, "state")
        schema_version = read_schema_version(conn)
        validate_state_schema(conn)
        configured = read_configured_source(conn, source_uuid)
    except (StateSchemaError, OSError, sqlite3.Error, ValueError) as exc:
        return _bad_state(target.logical_path, schema_version, str(exc)), None
    return (
        DatabaseDiagnostic(
            target.logical_path,
            "current",
            schema_version,
            STATE_SCHEMA_USER_VERSION,
        ),
        configured,
    )


def orphaned_tag_assignments_error(
    index_database: DatabaseDiagnostic,
    state_database: DatabaseDiagnostic,
) -> str | None:
    unavailable: list[tuple[str, DatabaseDiagnostic]] = []
    if index_database.status != "current":
        unavailable.append(("index", index_database))
    if state_database.status != "current":
        unavailable.append(("state", state_database))
    if not unavailable:
        return None
    reasons = " and ".join(
        f"the {label} database status is {database.status}" for label, database in unavailable
    )
    return f"Orphaned tag assignments are unavailable because {reasons}."


def count_orphaned_tag_assignments(
    index_reader: PinnedDatabaseReader,
    state_reader: PinnedDatabaseReader,
    index_database: DatabaseDiagnostic,
    state_database: DatabaseDiagnostic,
) -> int | None:
    if index_database.status != "current" or state_database.status != "current":
        return None
    index_conn = _required_connection(index_reader, "index")
    state_conn = _required_connection(state_reader, "state")
    indexed = {
        (
            decode_required_text(
                row[0],
                table="meetings",
                column="source_uuid",
                context="orphaned tag diagnostics",
                error_type=IndexSnapshotError,
            ),
            decode_required_text(
                row[1],
                table="meetings",
                column="external_id",
                context="orphaned tag diagnostics",
                error_type=IndexSnapshotError,
            ),
        )
        for row in index_conn.execute("SELECT source_uuid, external_id FROM meetings").fetchall()
    }
    assignments = (
        (
            decode_required_text(
                row[0],
                table="meeting_tags",
                column="source_uuid",
                context="orphaned tag diagnostics",
                error_type=StateSchemaError,
            ),
            decode_required_text(
                row[1],
                table="meeting_tags",
                column="meeting_external_id",
                context="orphaned tag diagnostics",
                error_type=StateSchemaError,
            ),
        )
        for row in state_conn.execute(
            "SELECT source_uuid, meeting_external_id FROM meeting_tags"
        ).fetchall()
    )
    return sum(assignment not in indexed for assignment in assignments)


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
    logical_path = Path(source_path)
    present, path_error, physical_path = database_path_state(logical_path)
    if not present:
        return SourceDatabaseDiagnostic(
            readable=False,
            schema_valid=False,
            schema_error=None,
            read_error=f"Meetily DB not found: {logical_path}",
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
    try:
        with readonly_sqlite_connection(physical_path) as conn:
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
def readonly_sqlite_connection(physical_path: Path) -> Generator[sqlite3.Connection, None, None]:
    uri = f"file:{quote(str(physical_path))}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
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
        f"SQLite database has an active {label} sidecar and cannot be inspected without risking "
        "sidecar changes. Retry after the writer closes."
    )


def read_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    if row is None:
        _raise_value_error("SQLite did not return a schema version.")
    return decode_required_integer(
        row[0],
        table="pragma",
        column="user_version",
        context="database diagnostics",
        error_type=ValueError,
    )


def _raise_index_snapshot(message: str) -> Never:
    raise IndexSnapshotError(message)


def _raise_value_error(message: str) -> Never:
    raise ValueError(message)


def read_dominant_language(conn: sqlite3.Connection) -> str | None:
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
    if row is None:
        return None
    return decode_required_text(
        row[0],
        table="meetings",
        column="language",
        context="dominant language diagnostics",
        error_type=IndexSnapshotError,
    )


def read_configured_source(conn: sqlite3.Connection, source_uuid: str | None) -> Path | None:
    if source_uuid is None:
        row = conn.execute(
            """
            SELECT s.current_path
            FROM app_settings a
            JOIN sources s ON s.uuid = a.source_uuid
            WHERE a.singleton = 1
            """
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT current_path FROM sources WHERE uuid = ?",
            (source_uuid,),
        ).fetchone()
    if row is None:
        return None
    return Path(
        decode_required_text(
            row[0],
            table="sources",
            column="current_path",
            context="configured source diagnostics",
            error_type=StateSchemaError,
        )
    ).expanduser()


def _diagnostic_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
    if row is None:
        _raise_index_snapshot(f"{table} count query returned no value")
    return decode_required_integer(
        row[0],
        table=table,
        column="row_count",
        context="index diagnostics",
        error_type=IndexSnapshotError,
    )


def empty_stats() -> dict[str, int]:
    return {"meetings": 0, "chunks": 0, "sources": 0}


def _required_connection(reader: PinnedDatabaseReader, label: str) -> sqlite3.Connection:
    if reader.connection is None:
        raise DiagnosticDatabaseUnavailableError(
            reader.target,
            reader.error or f"{label} database is unavailable: {reader.target.logical_path}",
        )
    return reader.connection


def _missing_index(path: Path) -> DatabaseDiagnostic:
    return DatabaseDiagnostic(path, "missing", None, INDEX_SCHEMA_USER_VERSION)


def _bad_index(path: Path, version: int | None, reason: str | None) -> DatabaseDiagnostic:
    error = (
        f"Unsupported, foreign, or damaged index database: {reason or 'unavailable'}. "
        "Delete the disposable `index.sqlite` or run `mm refresh --source PATH` to rebuild it."
    )
    return DatabaseDiagnostic(path, "incompatible", version, INDEX_SCHEMA_USER_VERSION, error)


def _bad_state(path: Path, version: int | None, reason: str | None) -> DatabaseDiagnostic:
    error = (
        f"Unsupported, foreign, or damaged state database: {reason or 'unavailable'}. "
        "Delete `state.sqlite` together with the disposable `index.sqlite`, then reinitialize. "
        "Deleting state permanently loses manual tags and application settings."
    )
    return DatabaseDiagnostic(path, "incompatible", version, STATE_SCHEMA_USER_VERSION, error)
