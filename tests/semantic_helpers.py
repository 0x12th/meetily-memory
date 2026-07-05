import sqlite3

import pytest

from meetily_memory.semantic_search import load_sqlite_vec


def sqlite_vec_runtime_available() -> bool:
    try:
        with sqlite3.connect(":memory:") as conn:
            load_sqlite_vec(conn)
    except RuntimeError:
        return False
    return True


requires_sqlite_vec = pytest.mark.skipif(
    not sqlite_vec_runtime_available(),
    reason="sqlite-vec requires SQLite extension loading",
)
