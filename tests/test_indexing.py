from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from meetily_memory.db.fts import build_fts_query
from meetily_memory.db.index_snapshot import INDEX_APPLICATION_TABLES
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.scanner.sqlite_source import readonly_sqlite_connection
from tests.index_helpers import publish_fresh_index

if TYPE_CHECKING:
    from pathlib import Path


def _application_tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }


def test_readonly_meetily_connection_is_context_managed(meetily_db: Path) -> None:
    with readonly_sqlite_connection(meetily_db) as connection:
        row = connection.execute(
            "SELECT title FROM meetings WHERE id = ?",
            ("meeting-1",),
        ).fetchone()
        assert row[0] == "Launch Planning"
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE should_not_write (id INTEGER)")

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_scan_builds_exact_fresh_index_with_upstream_ids(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"

    result = publish_fresh_index(index_path, meetily_db)
    repository = IndexRepository.open_existing(index_path)

    assert result.meetings == 2
    assert result.chunks >= 4
    meeting_ref = repository.meeting_ref_for_local_id(1)
    assert meeting_ref is not None
    meeting = repository.get_meeting_by_ref(meeting_ref)
    assert meeting is not None
    assert meeting["title"] == "Launch Planning"
    chunks = repository.get_chunks_for_meeting(int(meeting["id"]))
    assert {chunk["external_id"] for chunk in chunks} >= {
        "transcript-1",
        "summary:meeting-1",
    }
    assert repository.stats() == {
        "meetings": 2,
        "chunks": result.chunks,
        "sources": 1,
    }
    assert _application_tables(index_path) >= INDEX_APPLICATION_TABLES
    assert {
        "sources",
        "scan_runs",
        "index_generation",
        "decisions",
        "action_items",
        "risks",
        "open_questions",
        "knowledge_nodes",
        "knowledge_edges",
        "topic_aliases",
    }.isdisjoint(_application_tables(index_path))

    search_results = repository.search("pricing decision")
    assert search_results[0]["meeting_external_id"] == "meeting-1"
    assert "pricing decision" in search_results[0]["text"]


def test_fts_query_filters_natural_language_noise() -> None:
    assert build_fts_query("What was the pricing decision?") == '"pricing" OR "decision"'
    assert (
        build_fts_query("что решили про migration risks?") == '"решили" OR "migration" OR "risks"'
    )


def test_search_prefers_strict_token_matches_before_or_fallback(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    with sqlite3.connect(meetily_db) as connection:
        connection.execute(
            """
            INSERT INTO meetings (id, title, created_at, updated_at, folder_path)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "meeting-3",
                "Migration Only",
                "2026-07-03T10:00:00Z",
                "2026-07-03T10:30:00Z",
                str(tmp_path / "Migration Only"),
            ),
        )
        connection.execute(
            """
            INSERT INTO transcripts (
                id, meeting_id, transcript, timestamp, audio_start_time,
                audio_end_time, duration, speaker
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "transcript-5",
                "meeting-3",
                "Migration notes were reviewed.",
                "10:05:00",
                300.0,
                310.0,
                10.0,
                "Alice",
            ),
        )
        connection.commit()
    publish_fresh_index(index_path, meetily_db)

    results = IndexRepository.open_existing(index_path).search("migration risks")

    assert results[0]["meeting_external_id"] == "meeting-2"
    assert "migration risks" in str(results[0]["text"])


def test_neighbor_context_keeps_lexical_match_before_adjacent_chunks(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)

    results = IndexRepository.open_existing(index_path).search(
        "partner review",
        limit=1,
        context=1,
    )

    assert results[0]["chunk_external_id"] == "transcript-3"
    assert results[0]["is_context"] is False
    assert results[1]["chunk_external_id"] == "transcript-1"
    assert results[1]["is_context"] is True


def test_scan_reports_unsupported_meetily_schema(tmp_path: Path) -> None:
    source_path = tmp_path / "meeting_minutes.sqlite"
    with sqlite3.connect(source_path) as connection:
        connection.execute("CREATE TABLE meetings (id TEXT PRIMARY KEY)")
        connection.commit()

    with pytest.raises(RuntimeError, match="Meetily DB schema is unsupported"):
        publish_fresh_index(tmp_path / "index.sqlite", source_path)


def test_second_scan_publishes_fresh_snapshot_with_changed_source_data(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    first = publish_fresh_index(index_path, meetily_db)
    first_inode = index_path.stat().st_ino

    with sqlite3.connect(meetily_db) as connection:
        connection.execute(
            "UPDATE transcripts SET transcript = ? WHERE id = ?",
            (
                "Dobrynya agreed to send migration risks and budget notes by Friday.",
                "transcript-2",
            ),
        )
        connection.commit()

    second = publish_fresh_index(index_path, meetily_db)
    results = IndexRepository.open_existing(index_path).search("budget notes")

    assert second.source.source_uuid == first.source.source_uuid
    assert second.meetings == 2
    assert index_path.stat().st_ino != first_inode
    assert len(results) == 1
    assert results[0]["meeting_external_id"] == "meeting-2"
