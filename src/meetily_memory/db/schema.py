from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, closing, contextmanager
from pathlib import Path

from meetily_memory.db.index_snapshot import IndexSnapshotError, validate_index_snapshot_schema

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
    journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
    if journal_mode != "delete":
        message = f"index journal_mode must be DELETE, got {journal_mode!r}"
        raise IndexSnapshotError(message)
    validate_index_snapshot_schema(conn)


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
