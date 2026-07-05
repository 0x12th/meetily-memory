import sqlite3
from collections.abc import Iterable
from typing import Any


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def last_insert_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        message = "SQLite insert did not return lastrowid."
        raise RuntimeError(message)
    return cursor.lastrowid
