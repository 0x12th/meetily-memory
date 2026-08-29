import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

from meetily_memory.db.migrations import (
    CURRENT_SCHEMA_VERSION,
    LATEST_IN_PLACE_SCHEMA_VERSION,
    MIGRATIONS,
    initialize_current_schema,
)


class IndexRebuildRequiredError(RuntimeError):
    pass


@contextmanager
def index_connection(index_path: Path) -> Generator[sqlite3.Connection, None, None]:
    index_path = Path(index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(index_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_schema(conn)
        yield conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > CURRENT_SCHEMA_VERSION:
        message = (
            f"Unsupported index schema version {version}; "
            f"this binary supports {CURRENT_SCHEMA_VERSION}."
        )
        raise RuntimeError(message)

    if version == 0 and not _has_application_tables(conn):
        initialize_current_schema(conn)
        return

    for next_version in range(version + 1, LATEST_IN_PLACE_SCHEMA_VERSION + 1):
        MIGRATIONS[next_version](conn)

    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < CURRENT_SCHEMA_VERSION:
        message = (
            f"Index schema {version} requires a source-aware rebuild to schema "
            f"{CURRENT_SCHEMA_VERSION}. Run refresh or scan with the source database."
        )
        raise IndexRebuildRequiredError(message)


def _has_application_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        LIMIT 1
        """
    ).fetchone()
    return row is not None
