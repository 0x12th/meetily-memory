import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

from meetily_memory.db.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS


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

    # All formats through v5 are compatible in-place. The first incompatible future
    # format must introduce a tested side-by-side rebuild instead of extending this loop.
    for next_version in range(version + 1, CURRENT_SCHEMA_VERSION + 1):
        MIGRATIONS[next_version](conn)
