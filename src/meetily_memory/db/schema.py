import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from meetily_memory.db.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS


@contextmanager
def index_connection(index_path: Path) -> Iterator[sqlite3.Connection]:
    index_path = Path(index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(index_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_schema(conn)
        yield conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > CURRENT_SCHEMA_VERSION:
        message = (
            f"Unsupported index schema version {version}; "
            f"this binary supports {CURRENT_SCHEMA_VERSION}."
        )
        raise RuntimeError(message)
    for next_version in range(version + 1, CURRENT_SCHEMA_VERSION + 1):
        migration = MIGRATIONS[next_version]
        migration(conn)
        conn.execute(f"PRAGMA user_version = {next_version}")
        conn.commit()
