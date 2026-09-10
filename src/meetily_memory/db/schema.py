from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, closing, contextmanager
from pathlib import Path

from meetily_memory.db.index_snapshot import IndexSnapshotError
from meetily_memory.db.schema_family import (
    INDEX_APPLICATION_ID,
    INDEX_SCHEMA_EPOCH,
    INDEX_SCHEMA_FAMILY,
    INDEX_SCHEMA_USER_VERSION,
)

IndexConnectionFactory = Callable[[Path], AbstractContextManager[sqlite3.Connection]]
OPERATION_STATE_SCHEMA = "operation_state"


class IndexReadError(RuntimeError):
    pass


def missing_user_state_message(state_path: Path) -> str:
    return (
        f"Meetily Memory state database not found: {state_path}. Remove the disposable "
        "`index.sqlite` and run `mm init --source PATH` or `mm refresh --source PATH` to "
        "reinitialize local data. Deleting or replacing `state.sqlite` permanently loses "
        "manual tags and application settings."
    )


@contextmanager
def sqlite_read_snapshot(conn: sqlite3.Connection) -> Generator[None, None, None]:
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if owns_transaction and conn.in_transaction:
            conn.rollback()


@contextmanager
def index_connection(index_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Open an existing exact-epoch index for bounded current-schema writes."""
    physical_path = _require_index_file(index_path)
    try:
        with closing(sqlite3.connect(physical_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            validate_existing_index_schema(conn)
            yield conn
    except IndexReadError:
        raise
    except (IndexSnapshotError, sqlite3.Error) as exc:
        raise IndexReadError(_invalid_index_message(index_path, exc)) from exc


@contextmanager
def existing_index_connection(index_path: Path) -> Generator[sqlite3.Connection, None, None]:
    physical_path = _require_index_file(index_path)
    uri = f"{physical_path.as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA foreign_keys=ON")
            validate_existing_index_schema(conn)
            yield conn
    except IndexReadError:
        raise
    except (IndexSnapshotError, sqlite3.Error) as exc:
        raise IndexReadError(_invalid_index_message(index_path, exc)) from exc


def validate_existing_index_schema(conn: sqlite3.Connection) -> None:
    """Validate only bounded compatibility invariants required by ordinary reads."""
    journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
    if journal_mode != "delete":
        message = f"index journal_mode must be DELETE, got {journal_mode!r}"
        raise IndexSnapshotError(message)

    application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
    if application_id != INDEX_APPLICATION_ID:
        raise IndexSnapshotError(f"foreign application_id 0x{application_id:08X}")

    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if user_version != INDEX_SCHEMA_USER_VERSION:
        relation = "future" if user_version > INDEX_SCHEMA_USER_VERSION else "unsupported"
        raise IndexSnapshotError(
            f"{relation} index user_version {user_version}; exact version "
            f"{INDEX_SCHEMA_USER_VERSION} is required"
        )

    row = conn.execute(
        """
        SELECT singleton, schema_family, schema_epoch, source_uuid, source_path, source_revision
        FROM index_meta
        WHERE singleton = 1
        """
    ).fetchone()
    if row is None:
        raise IndexSnapshotError("index_meta singleton row is missing")
    if tuple(row[:3]) != (1, INDEX_SCHEMA_FAMILY, INDEX_SCHEMA_EPOCH):
        raise IndexSnapshotError("index_meta family/epoch identity is invalid")
    if not str(row[3]).strip() or not str(row[4]):
        raise IndexSnapshotError("index_meta source identity is empty")
    if int(row[5]) < 0:
        raise IndexSnapshotError("index_meta source revision/token is invalid")


def _require_index_file(index_path: Path) -> Path:
    logical_path = Path(index_path)
    if not logical_path.is_file():
        message = (
            f"Meetily Memory index not found: {logical_path}. "
            "Run `mm refresh` or `mm scan --source PATH` to build it."
        )
        raise IndexReadError(message)
    try:
        physical_path = logical_path.resolve(strict=True)
    except OSError as exc:
        message = f"Meetily Memory index cannot be opened: {logical_path}."
        raise IndexReadError(message) from exc
    if not physical_path.is_file():
        message = f"Meetily Memory index is not a regular file: {logical_path}."
        raise IndexReadError(message)
    return physical_path


def _invalid_index_message(index_path: Path, error: BaseException) -> str:
    return (
        f"Meetily Memory index at {index_path} is unsupported, foreign, or damaged: {error} "
        "Delete the disposable `index.sqlite` or run `mm refresh --source PATH` to rebuild it. "
        "in-place migration is not supported."
    )
