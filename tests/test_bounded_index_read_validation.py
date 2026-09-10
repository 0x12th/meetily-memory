from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from meetily_memory.cli.app import app
from meetily_memory.db.index_snapshot import IndexSnapshotError, validate_index_snapshot_schema
from meetily_memory.db.schema import existing_index_connection
from tests.index_helpers import publish_fresh_index

if TYPE_CHECKING:
    from pathlib import Path


def test_ordinary_reads_do_not_run_full_snapshot_validation(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)

    with sqlite3.connect(index_path) as conn:
        conn.execute("UPDATE index_meta SET chunk_count = chunk_count + 1 WHERE singleton = 1")
        conn.commit()

    with sqlite3.connect(index_path) as conn, pytest.raises(
        IndexSnapshotError,
        match="index_meta counts do not match",
    ):
        validate_index_snapshot_schema(conn)

    with existing_index_connection(index_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 0

    result = CliRunner().invoke(app, ["--index", str(index_path), "s", "migration", "--json"])
    assert result.exit_code == 0, result.output
