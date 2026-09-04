from __future__ import annotations

import hashlib
import shutil
import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Never

import pytest
from typer.testing import CliRunner

from meetily_memory.cli.app import app
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.db.schema import IndexReadError, existing_index_connection, sqlite_read_snapshot
from meetily_memory.repositories.index import IndexRepository, meeting_from_row, search_hit_from_row
from meetily_memory.scanner.fresh_index import DuplicateEvidenceIdentityError
from meetily_memory.tagging import TagService
from tests.index_helpers import publish_fresh_index

if TYPE_CHECKING:
    from pathlib import Path

INDEX_ROW_QUERIES = (
    ("index_meta", "SELECT COUNT(*) FROM index_meta"),
    ("meetings", "SELECT COUNT(*) FROM meetings"),
    ("chunks", "SELECT COUNT(*) FROM chunks"),
    ("chunks_fts", "SELECT COUNT(*) FROM chunks_fts"),
)
STATE_ROW_QUERIES = (
    ("state_meta", "SELECT COUNT(*) FROM state_meta"),
    ("sources", "SELECT COUNT(*) FROM sources"),
    ("app_settings", "SELECT COUNT(*) FROM app_settings"),
    ("manual_tags", "SELECT COUNT(*) FROM manual_tags"),
    ("meeting_tags", "SELECT COUNT(*) FROM meeting_tags"),
)


class SnapshotFailureError(RuntimeError):
    pass


@dataclass(frozen=True)
class DatabaseSnapshot:
    digest: str
    mtime_ns: int
    size: int
    rows: tuple[tuple[str, int], ...]


def database_snapshot(
    path: Path,
    row_queries: tuple[tuple[str, str], ...],
) -> DatabaseSnapshot:
    physical = path.resolve(strict=True)
    with sqlite3.connect(f"{physical.as_uri()}?mode=ro", uri=True) as conn:
        rows = tuple((name, int(conn.execute(sql).fetchone()[0])) for name, sql in row_queries)
    path_stat = physical.stat()
    return DatabaseSnapshot(
        hashlib.sha256(physical.read_bytes()).hexdigest(),
        path_stat.st_mtime_ns,
        path_stat.st_size,
        rows,
    )


def test_core_repository_and_cli_reads_leave_exact_index_and_state_unchanged(
    meetily_db: Path,
    tmp_path: Path,
    platform_opener: tuple[dict[str, str], Path],
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    state_path = index_path.with_name("state.sqlite")
    before = (
        database_snapshot(index_path, INDEX_ROW_QUERIES),
        database_snapshot(state_path, STATE_ROW_QUERIES),
    )

    core = MeetilyMemoryCore(index_path)
    search = core.search("migration risks", limit=5, context=1)
    assert search.results
    assert core.meetings()
    ref = search.results[0].meeting.ref

    target = tmp_path / "Dobrynya Follow-up"
    target.mkdir()
    opener_env, _ = platform_opener
    runner = CliRunner()
    for command in (
        ("s", "migration risks", "--json"),
        ("tag", "list"),
        ("open", str(ref)),
    ):
        result = runner.invoke(
            app,
            ["--index", str(index_path), *command],
            env=opener_env,
        )
        assert result.exit_code == 0, result.output

    assert (
        database_snapshot(index_path, INDEX_ROW_QUERIES),
        database_snapshot(state_path, STATE_ROW_QUERIES),
    ) == before


def raise_snapshot_failure(conn: sqlite3.Connection) -> Never:
    assert conn.in_transaction
    raise SnapshotFailureError


def test_sqlite_read_snapshot_owns_only_the_transaction_it_started(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)

    with existing_index_connection(index_path) as conn:
        with pytest.raises(SnapshotFailureError), sqlite_read_snapshot(conn):
            raise_snapshot_failure(conn)
        assert not conn.in_transaction

        conn.execute("BEGIN")
        try:
            with pytest.raises(SnapshotFailureError), sqlite_read_snapshot(conn):
                raise_snapshot_failure(conn)
            assert conn.in_transaction
        finally:
            conn.rollback()


def test_search_mapping_context_and_tag_round_trip_through_current_sqlite(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    repository = IndexRepository(index_path)

    row = repository.search("pricing decision", limit=1)[0]
    original = dict(row)
    hit = search_hit_from_row(MappingProxyType(row))
    assert row == original
    assert hit.id == row["evidence_id"]
    assert repository.get_search_hit(hit.id) == hit
    assert hit in repository.expand_search_hits((hit,), context=1)

    TagService(repository).assign((hit.meeting.ref,), ("launch",))
    assert MeetilyMemoryCore(index_path).search("launch").results[0].meeting.ref == hit.meeting.ref


def test_index_row_decoder_rejects_wrong_storage_type_and_preserves_nullable_empty_text(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    with sqlite3.connect(index_path) as conn:
        conn.execute(
            "UPDATE meetings SET started_at = ?, summary_text = '' WHERE external_id = 'meeting-1'",
            (sqlite3.Binary(b"not-text"),),
        )
        conn.commit()

    repository = IndexRepository.open_existing(index_path)
    with pytest.raises(
        IndexReadError,
        match=r"meetings\.started_at must be TEXT, got BLOB",
    ):
        repository.list_meetings()

    with sqlite3.connect(index_path) as conn:
        conn.execute("UPDATE meetings SET started_at = NULL WHERE external_id = 'meeting-1'")
        conn.commit()
    repaired_repository = IndexRepository.open_existing(index_path)
    empty_row = next(
        item for item in repaired_repository.list_meetings() if item["external_id"] == "meeting-1"
    )
    mapped = meeting_from_row(empty_row)
    assert mapped.started_at is None
    assert mapped.summary_text == ""


def test_search_repository_strictly_rejects_wrong_stored_chunk_type(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    with sqlite3.connect(index_path) as connection:
        connection.execute(
            "UPDATE chunks SET timestamp_label = ?",
            (sqlite3.Binary(b"not-text"),),
        )
        connection.commit()

    repository = IndexRepository.open_existing(index_path)
    with pytest.raises(
        IndexReadError,
        match=r"chunks\.timestamp_label must be TEXT, got BLOB",
    ):
        repository.search("pricing decision", limit=1)


def test_operation_snapshot_validates_exact_attached_state_and_rejects_replacement(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    state_path = index_path.with_name("state.sqlite")
    repository = IndexRepository.open_existing(index_path)

    with repository.operation_snapshot() as snapshot:
        assert snapshot.in_transaction
        assert tuple(
            snapshot.execute(
                "SELECT schema_family, schema_epoch FROM operation_state.state_meta"
            ).fetchone()
        ) == ("meetily-memory-state", 2)
        assert tuple(
            snapshot.execute(
                """
                SELECT COUNT(*)
                FROM main.index_meta i
                JOIN operation_state.sources s ON s.uuid = i.source_uuid
                """
            ).fetchone()
        ) == (1,)

    replacement = tmp_path / "replacement-state.sqlite"
    shutil.copy2(state_path, replacement)
    replacement.replace(state_path)
    with pytest.raises(IndexReadError, match="no longer matches"), repository.operation_snapshot():
        pass


def test_fresh_refresh_preserves_stable_evidence_and_never_updates_in_place(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    repository = IndexRepository.open_existing(index_path)
    old_hit = repository.search_hits("migration risks", limit=1)[0]
    old_inode = index_path.stat().st_ino

    with sqlite3.connect(meetily_db) as conn:
        conn.execute("UPDATE meetings SET title = 'Renamed meeting' WHERE id = 'meeting-2'")
        conn.commit()
    publish_fresh_index(index_path, meetily_db)

    assert index_path.stat().st_ino != old_inode
    reopened = IndexRepository.open_existing(index_path)
    assert reopened.get_search_hit(old_hit.id) is not None
    renamed = reopened.get_meeting_by_ref(old_hit.meeting.ref)
    assert renamed is not None
    assert renamed["title"] == "Renamed meeting"


def test_failed_fresh_build_keeps_the_last_published_snapshot(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    before = database_snapshot(index_path, INDEX_ROW_QUERIES)
    with sqlite3.connect(meetily_db) as conn:
        conn.execute("UPDATE transcripts SET id = 'summary:meeting-1' WHERE id = 'transcript-1'")
        conn.commit()

    with pytest.raises(DuplicateEvidenceIdentityError, match="Duplicate upstream chunk identity"):
        publish_fresh_index(index_path, meetily_db)

    assert database_snapshot(index_path, INDEX_ROW_QUERIES) == before


def test_missing_and_unsupported_reads_create_nothing_and_give_reinitialize_guidance(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing" / "index.sqlite"
    with pytest.raises(IndexReadError, match=r"index not found.*mm refresh"):
        MeetilyMemoryCore(missing)
    assert not missing.parent.exists()

    foreign = tmp_path / "foreign.sqlite"
    with sqlite3.connect(foreign) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        conn.commit()
    before = foreign.read_bytes()
    with pytest.raises(
        IndexReadError,
        match=r"Delete the disposable.*mm refresh.*in-place migration is not supported",
    ):
        MeetilyMemoryCore(foreign)
    assert foreign.read_bytes() == before
    assert not foreign.with_name("state.sqlite").exists()
