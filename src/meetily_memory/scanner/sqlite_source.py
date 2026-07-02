import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from pathlib import Path
from urllib.parse import quote


@contextmanager
def readonly_sqlite_connection(path: Path) -> Iterator[sqlite3.Connection]:
    path = Path(path)
    if not path.is_file():
        message = f"Meetily DB not found: {path}"
        raise FileNotFoundError(message)

    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        yield conn


def can_open_readonly_sqlite(path: Path) -> bool:
    with suppress(FileNotFoundError, sqlite3.Error), readonly_sqlite_connection(path):
        return True
    return False
