from __future__ import annotations

from typing import Never


def decode_required_text(
    value: object,
    *,
    table: str,
    column: str,
    context: str,
    error_type: type[Exception],
) -> str:
    if type(value) is str:
        return value
    return _raise_decode_error(value, "TEXT", (table, column, context), error_type)


def decode_nullable_text(
    value: object,
    *,
    table: str,
    column: str,
    context: str,
    error_type: type[Exception],
) -> str | None:
    if value is None:
        return None
    return decode_required_text(
        value,
        table=table,
        column=column,
        context=context,
        error_type=error_type,
    )


def decode_required_integer(
    value: object,
    *,
    table: str,
    column: str,
    context: str,
    error_type: type[Exception],
) -> int:
    if type(value) is int:
        return value
    return _raise_decode_error(value, "INTEGER", (table, column, context), error_type)


def decode_nullable_integer(
    value: object,
    *,
    table: str,
    column: str,
    context: str,
    error_type: type[Exception],
) -> int | None:
    if value is None:
        return None
    return decode_required_integer(
        value,
        table=table,
        column=column,
        context=context,
        error_type=error_type,
    )


def decode_required_real(
    value: object,
    *,
    table: str,
    column: str,
    context: str,
    error_type: type[Exception],
) -> float:
    if type(value) is float:
        return value
    return _raise_decode_error(value, "REAL", (table, column, context), error_type)


def decode_nullable_real(
    value: object,
    *,
    table: str,
    column: str,
    context: str,
    error_type: type[Exception],
) -> float | None:
    if value is None:
        return None
    return decode_required_real(
        value,
        table=table,
        column=column,
        context=context,
        error_type=error_type,
    )


def _raise_decode_error(
    value: object,
    expected: str,
    location: tuple[str, str, str],
    error_type: type[Exception],
) -> Never:
    table, column, context = location
    actual = _sqlite_storage_type(value)
    message = (
        f"Invalid SQLite row for {context}: {table}.{column} must be {expected}, "
        f"got {actual} ({value!r})."
    )
    raise error_type(message)


def _sqlite_storage_type(value: object) -> str:
    if value is None:
        return "NULL"
    if type(value) is str:
        return "TEXT"
    if type(value) is int:
        return "INTEGER"
    if type(value) is float:
        return "REAL"
    if type(value) is bytes:
        return "BLOB"
    return type(value).__name__
