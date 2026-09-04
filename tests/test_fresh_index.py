import hashlib
import sqlite3
from pathlib import Path

import pytest

from meetily_memory.db.index_snapshot import (
    INDEX_APPLICATION_TABLES,
    IndexSnapshotError,
    validate_index_snapshot_database,
)
from meetily_memory.db.schema_family import (
    INDEX_APPLICATION_ID,
    INDEX_SCHEMA_EPOCH,
    INDEX_SCHEMA_FAMILY,
    INDEX_SCHEMA_USER_VERSION,
)
from meetily_memory.domain import stable_evidence_id
from meetily_memory.scanner.fresh_index import (
    build_fresh_index,
    iter_meetily_meetings,
    pinned_meetily_snapshot,
)

FTS_INTERNAL_TABLES = {
    "chunks_fts_config",
    "chunks_fts_content",
    "chunks_fts_data",
    "chunks_fts_docsize",
    "chunks_fts_idx",
}


def test_fresh_index_has_exact_schema_one_source_and_no_sidecars(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    source_uuid = "source-fresh-1"
    result = build_fresh_index(
        selected_source_uuid=source_uuid,
        selected_source_path=meetily_db,
        destination_directory=tmp_path / "candidates",
    )

    validated = validate_index_snapshot_database(result.candidate_path)

    assert validated == {
        "source_uuid": source_uuid,
        "source_path": str(meetily_db.resolve()),
        "source_revision": 0,
        "meetings": 2,
        "chunks": 6,
        "fts_rows": 6,
        "source_fingerprint": result.source_fingerprint,
    }
    assert result.meetings == 2
    assert result.chunks == 6
    assert result.fts_rows == 6
    assert result.bytes > 0
    assert result.timings.total_seconds >= result.timings.populate_seconds
    with sqlite3.connect(result.candidate_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == INDEX_APPLICATION_TABLES | FTS_INTERNAL_TABLES
        assert conn.execute("PRAGMA application_id").fetchone()[0] == INDEX_APPLICATION_ID
        assert conn.execute("PRAGMA user_version").fetchone()[0] == INDEX_SCHEMA_USER_VERSION
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).casefold() == "delete"
        assert conn.execute(
            """
            SELECT schema_family, schema_epoch, source_uuid, source_revision,
                   meeting_count, chunk_count, source_fingerprint
            FROM index_meta
            """
        ).fetchone() == (
            INDEX_SCHEMA_FAMILY,
            INDEX_SCHEMA_EPOCH,
            source_uuid,
            0,
            2,
            6,
            result.source_fingerprint,
        )
        assert [row[1] for row in conn.execute("PRAGMA table_info(meetings)")] == [
            "id",
            "source_uuid",
            "external_id",
            "title",
            "started_at",
            "ended_at",
            "created_at",
            "updated_at",
            "folder_path",
            "source_path",
            "language",
            "summary_text",
            "indexed_at",
        ]
        assert [row[1] for row in conn.execute("PRAGMA table_info(chunks)")] == [
            "id",
            "meeting_id",
            "external_id",
            "evidence_id",
            "kind",
            "ordinal",
            "text",
            "speaker",
            "starts_at_seconds",
            "ends_at_seconds",
            "timestamp_label",
        ]
        assert conn.execute("SELECT DISTINCT source_uuid FROM meetings").fetchall() == [
            (source_uuid,)
        ]
        evidence_id = conn.execute(
            "SELECT evidence_id FROM chunks WHERE external_id='transcript-1'"
        ).fetchone()[0]
        assert evidence_id == stable_evidence_id(
            source_uuid,
            "meeting-1",
            "transcript-1",
            kind="transcript",
            ordinal=0,
            text="Alice confirmed the launch checklist and pricing decision.",
        )
    for suffix in ("-wal", "-shm", "-journal"):
        assert not result.candidate_path.with_name(result.candidate_path.name + suffix).exists()


def test_fresh_index_rebuild_is_byte_deterministic(meetily_db: Path, tmp_path: Path) -> None:
    first = build_fresh_index(
        selected_source_uuid="deterministic-source",
        selected_source_path=meetily_db,
        destination_directory=tmp_path,
    )
    second = build_fresh_index(
        selected_source_uuid="deterministic-source",
        selected_source_path=meetily_db,
        destination_directory=tmp_path,
    )

    assert _file_digest(first.candidate_path) == _file_digest(second.candidate_path)


def test_pinned_source_snapshot_does_not_observe_concurrent_writer(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    with sqlite3.connect(meetily_db) as setup:
        assert str(setup.execute("PRAGMA journal_mode=WAL").fetchone()[0]).casefold() == "wal"

    with pinned_meetily_snapshot(meetily_db) as snapshot:
        with sqlite3.connect(meetily_db) as writer:
            writer.execute(
                """
                INSERT INTO meetings (id, title, created_at, updated_at, folder_path)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "meeting-concurrent",
                    "Concurrent meeting",
                    "2026-07-03T10:00:00Z",
                    "2026-07-03T10:30:00Z",
                    str(tmp_path / "Concurrent meeting"),
                ),
            )
            writer.commit()
        assert [meeting["id"] for meeting in iter_meetily_meetings(snapshot)] == [
            "meeting-1",
            "meeting-2",
        ]

    with sqlite3.connect(meetily_db) as current:
        assert (
            current.execute(
                "SELECT COUNT(*) FROM meetings WHERE id='meeting-concurrent'"
            ).fetchone()[0]
            == 1
        )


def test_source_row_decoders_reject_wrong_storage_type_and_preserve_null_empty_values(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    with sqlite3.connect(meetily_db) as conn:
        conn.execute(
            "UPDATE summary_processes SET result = '', metadata = NULL "
            "WHERE meeting_id = 'meeting-1'"
        )
        conn.execute("UPDATE meeting_notes SET notes_markdown = '' WHERE meeting_id = 'meeting-2'")
        conn.commit()

    with pinned_meetily_snapshot(meetily_db) as source:
        meetings = list(iter_meetily_meetings(source))
    first = next(meeting for meeting in meetings if meeting["id"] == "meeting-1")
    second = next(meeting for meeting in meetings if meeting["id"] == "meeting-2")
    assert first["summary_process"] == {"result": "", "metadata": None}
    assert second["notes"] == {"notes_markdown": ""}

    with sqlite3.connect(meetily_db) as conn:
        conn.execute(
            "UPDATE transcripts SET audio_start_time = ? WHERE id = 'transcript-1'",
            (sqlite3.Binary(b"not-real"),),
        )
        conn.commit()
    with pytest.raises(
        RuntimeError,
        match=r"transcripts\.audio_start_time must be REAL, got BLOB",
    ):
        build_fresh_index(
            selected_source_uuid="wrong-source-type",
            selected_source_path=meetily_db,
            destination_directory=tmp_path,
        )


def test_source_summary_projection_supports_schema_without_optional_metadata(
    meetily_db: Path,
) -> None:
    with sqlite3.connect(meetily_db) as conn:
        conn.execute("ALTER TABLE summary_processes DROP COLUMN metadata")
        conn.commit()

    with pinned_meetily_snapshot(meetily_db) as source:
        meetings = list(iter_meetily_meetings(source))

    first = next(meeting for meeting in meetings if meeting["id"] == "meeting-1")
    assert first["summary_process"] == {"result": '{"markdown":"Launch checklist approved."}'}


def test_index_snapshot_validator_rejects_fts_corruption(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    result = build_fresh_index(
        selected_source_uuid="corrupt-source",
        selected_source_path=meetily_db,
        destination_directory=tmp_path,
    )
    with sqlite3.connect(result.candidate_path) as conn:
        conn.execute(
            "UPDATE chunks_fts SET text='corrupted text' "
            "WHERE chunk_id=(SELECT MIN(id) FROM chunks)"
        )
        conn.commit()

    with pytest.raises(IndexSnapshotError, match="FTS content is inconsistent"):
        validate_index_snapshot_database(result.candidate_path)


def test_index_snapshot_validator_rejects_foreign_and_extra_schema(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    foreign = tmp_path / "foreign.sqlite"
    with sqlite3.connect(foreign) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        conn.commit()
    with pytest.raises(IndexSnapshotError, match="foreign application_id"):
        validate_index_snapshot_database(foreign)

    result = build_fresh_index(
        selected_source_uuid="extra-schema-source",
        selected_source_path=meetily_db,
        destination_directory=tmp_path,
    )
    with sqlite3.connect(result.candidate_path) as conn:
        conn.execute("CREATE TABLE unexpected (id INTEGER PRIMARY KEY)")
        conn.commit()
    with pytest.raises(IndexSnapshotError, match="schema objects do not exactly match"):
        validate_index_snapshot_database(result.candidate_path)


def test_index_snapshot_validator_rejects_count_and_foreign_key_corruption(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    result = build_fresh_index(
        selected_source_uuid="fk-source",
        selected_source_path=meetily_db,
        destination_directory=tmp_path,
    )
    with sqlite3.connect(result.candidate_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("UPDATE chunks SET meeting_id=999999 WHERE id=(SELECT MIN(id) FROM chunks)")
        conn.commit()

    with pytest.raises(IndexSnapshotError, match=r"FTS content is inconsistent|foreign_key_check"):
        validate_index_snapshot_database(result.candidate_path)


def test_index_snapshot_validator_rejects_sidecar(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    result = build_fresh_index(
        selected_source_uuid="sidecar-source",
        selected_source_path=meetily_db,
        destination_directory=tmp_path,
    )
    sidecar = result.candidate_path.with_name(result.candidate_path.name + "-wal")
    sidecar.touch()

    with pytest.raises(IndexSnapshotError, match="SQLite sidecars remain"):
        validate_index_snapshot_database(result.candidate_path)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(65536):
            digest.update(block)
    return digest.hexdigest()
