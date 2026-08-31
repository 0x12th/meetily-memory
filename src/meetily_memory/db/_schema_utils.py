# ruff: noqa: S608

from __future__ import annotations

import sqlite3

from meetily_memory.db.row_decode import decode_required_integer, decode_required_text

type SchemaManifest = tuple[tuple[str, str, str, str], ...]


def schema_manifest(conn: sqlite3.Connection, schema: str) -> SchemaManifest:
    rows = conn.execute(
        f"""
        SELECT type, name, tbl_name, sql
        FROM {quote_identifier(schema)}.sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    context = f"{schema} schema manifest"
    return tuple(
        (
            decode_required_text(
                row[0],
                table="sqlite_master",
                column="type",
                context=context,
                error_type=sqlite3.DatabaseError,
            ),
            decode_required_text(
                row[1],
                table="sqlite_master",
                column="name",
                context=context,
                error_type=sqlite3.DatabaseError,
            ),
            decode_required_text(
                row[2],
                table="sqlite_master",
                column="tbl_name",
                context=context,
                error_type=sqlite3.DatabaseError,
            ),
            normalize_sql(
                decode_required_text(
                    row[3],
                    table="sqlite_master",
                    column="sql",
                    context=context,
                    error_type=sqlite3.DatabaseError,
                )
            ),
        )
        for row in rows
    )


def application_objects(conn: sqlite3.Connection) -> set[str]:
    return {
        decode_required_text(
            row[0],
            table="sqlite_master",
            column="name",
            context="application object discovery",
            error_type=sqlite3.DatabaseError,
        )
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def pragma_int(conn: sqlite3.Connection, schema: str, pragma: str) -> int:
    row = conn.execute(f"PRAGMA {quote_identifier(schema)}.{pragma}").fetchone()
    if row is None:
        message = f"PRAGMA {pragma} returned no value"
        raise sqlite3.DatabaseError(message)
    return decode_required_integer(
        row[0],
        table="pragma",
        column=pragma,
        context=f"{schema} database header",
        error_type=sqlite3.DatabaseError,
    )


def normalize_sql(sql: str) -> str:
    return " ".join(sql.split())


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def execute_sql_statements(
    conn: sqlite3.Connection,
    script: str,
    *,
    context: str,
) -> None:
    statement = ""
    for line in script.splitlines():
        statement = f"{statement}{line}\n"
        if not sqlite3.complete_statement(statement):
            continue
        if statement.strip():
            conn.execute(statement)
        statement = ""
    if statement.strip():
        message = f"{context} SQL contains an incomplete statement."
        raise ValueError(message)
