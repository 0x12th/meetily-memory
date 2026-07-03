import sqlite3
from pathlib import Path

import pytest

from meetily_memory.context_builder import build_context_markdown
from meetily_memory.db.repository import IndexRepository, build_fts_query
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.scanner.sqlite_source import readonly_sqlite_connection

EMPTY_ENTITY_COUNT_SQL = (
    "SELECT COUNT(*) FROM decisions",
    "SELECT COUNT(*) FROM action_items",
    "SELECT COUNT(*) FROM risks",
    "SELECT COUNT(*) FROM open_questions",
)


def test_readonly_meetily_connection_is_context_managed(meetily_db: Path) -> None:
    with readonly_sqlite_connection(meetily_db) as conn:
        row = conn.execute(
            "SELECT title FROM meetings WHERE id = ?",
            ("meeting-1",),
        ).fetchone()
        title = row[0]
        assert title == "Launch Planning"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE should_not_write (id INTEGER)")

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_index_schema_uses_builtin_sqlite_migration(tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"

    repo = IndexRepository(index_path)

    with sqlite3.connect(index_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
        for sql in EMPTY_ENTITY_COUNT_SQL:
            assert conn.execute(sql).fetchone()[0] == 0
        repo.stats()


def test_scan_indexes_meetily_rows_with_upstream_ids(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"

    result = MeetilySQLiteScanner(index_path).scan(meetily_db)

    assert result.meetings_seen == 2
    assert result.meetings_inserted == 2
    assert result.chunks_inserted >= 4

    repo = IndexRepository(index_path)
    source = repo.get_source("meetily_sqlite", str(meetily_db))
    assert source is not None

    meeting = repo.get_meeting_by_external_id(source["id"], "meeting-1")
    assert meeting is not None
    assert meeting["title"] == "Launch Planning"

    chunks = repo.get_chunks_for_meeting(meeting["id"])
    assert {chunk["external_id"] for chunk in chunks} >= {
        "transcript-1",
        "summary:meeting-1",
    }

    structured_entities = repo.list_structured_entities(meeting["id"])
    assert {entity["kind"] for entity in structured_entities} >= {
        "decisions",
        "open_questions",
    }

    stats = repo.stats()
    assert stats["decisions"] >= 1
    assert stats["action_items"] >= 1
    assert stats["risks"] >= 1
    assert stats["open_questions"] >= 1

    search_results = repo.search("pricing decision")
    assert search_results[0]["meeting_external_id"] == "meeting-1"
    assert "pricing decision" in search_results[0]["text"]

    question_results = repo.search("What was the pricing decision?")
    assert question_results[0]["meeting_external_id"] == "meeting-1"

    context = build_context_markdown("What was the pricing decision?", question_results)
    assert context.startswith("# Question\n\nWhat was the pricing decision?")
    assert "## Meeting: Launch Planning" in context
    assert "### Relevant excerpt" in context
    assert context.endswith("# Question\n\nWhat was the pricing decision?\n")


def test_fts_query_filters_natural_language_noise() -> None:
    assert build_fts_query("What was the pricing decision?") == '"pricing" OR "decision"'
    assert (
        build_fts_query("что решили про migration risks?") == '"решили" OR "migration" OR "risks"'
    )


def test_scan_is_incremental_and_replaces_changed_meeting_chunks(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    scanner = MeetilySQLiteScanner(index_path)

    first = scanner.scan(meetily_db)
    second = scanner.scan(meetily_db)

    assert first.meetings_inserted == 2
    assert second.meetings_inserted == 0
    assert second.meetings_updated == 0

    conn = sqlite3.connect(meetily_db)
    conn.execute(
        "UPDATE transcripts SET transcript = ? WHERE id = ?",
        ("Vladimir agreed to send migration risks and budget notes by Friday.", "transcript-2"),
    )
    conn.execute(
        "UPDATE meetings SET updated_at = ? WHERE id = ?",
        ("2026-07-02T10:00:00Z", "meeting-2"),
    )
    conn.commit()
    conn.close()

    third = scanner.scan(meetily_db)

    assert third.meetings_inserted == 0
    assert third.meetings_updated == 1

    repo = IndexRepository(index_path)
    results = repo.search("budget notes")
    assert len(results) == 1
    assert results[0]["meeting_external_id"] == "meeting-2"


def test_force_scan_reindexes_unchanged_meetings(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    scanner = MeetilySQLiteScanner(index_path)

    scanner.scan(meetily_db)
    result = scanner.scan(meetily_db, force=True)

    assert result.meetings_inserted == 0
    assert result.meetings_updated == 2
    assert result.chunks_updated >= 4
