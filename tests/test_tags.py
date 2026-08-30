import importlib
import importlib.util
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meetily_memory.cli.app import app
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.db.repository import IndexRepository
from meetily_memory.db.schema import existing_index_connection
from meetily_memory.domain import MeetingSearchFilters, RetrievalSource
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.semantic_search import (
    LocalHashEmbeddingProvider,
    index_semantic_embeddings,
)
from meetily_memory.tagging import TagService
from meetily_memory.user_state import UserStateRepository
from tests.semantic_helpers import requires_sqlite_vec


class TargetBiasedEmbeddingProvider:
    name = "target-biased"
    model = "target-biased-v1"
    dims: int | None = 2

    def embed(self, texts: list[str], *, role: str) -> list[list[float]]:
        assert role in {"query", "document"}
        return [[0.0, 1.0] if text == "other semantic topic" else [1.0, 0.0] for text in texts]


def test_tag_repository_normalizes_assigns_idempotently_and_removes_unused_tags(
    tmp_path: Path,
) -> None:
    assert importlib.util.find_spec("meetily_memory.tagging") is not None
    tagging = importlib.import_module("meetily_memory.tagging")
    repository_class = getattr(tagging, "TagRepository", None)
    assert repository_class is not None

    state_path = tmp_path / "state.sqlite"
    state = UserStateRepository(state_path)
    source_uuid = state.get_or_create_source("meetily_sqlite", "/source.sqlite", now="1")
    repository = repository_class(state_path)

    first = repository.assign(
        source_uuid,
        ("meeting-1", "meeting-2"),
        ("  Сбер  ", "сбер", "System   Design"),
        now="2",
    )
    second = repository.assign(
        source_uuid,
        ("meeting-1", "meeting-2"),
        ("СБЕР", "system design"),
        now="3",
    )

    assert first.added_links == 4
    assert first.existing_links == 0
    assert second.added_links == 0
    assert second.existing_links == 4
    assert [tag.display_name for tag in repository.list_for_meeting(source_uuid, "meeting-1")] == [
        "Сбер",
        "System Design",
    ]

    first_remove = repository.remove(
        source_uuid,
        ("meeting-1",),
        ("сбер",),
    )
    assert first_remove.removed_links == 1
    assert repository.list_for_meeting(source_uuid, "meeting-2")[0].display_name == "Сбер"

    last_remove = repository.remove(
        source_uuid,
        ("meeting-2",),
        ("СБЕР",),
    )
    assert last_remove.removed_links == 1
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT 1 FROM tags WHERE normalized_name = 'сбер'").fetchone() is None


def test_tag_repository_finds_exact_before_token_matches(tmp_path: Path) -> None:
    assert importlib.util.find_spec("meetily_memory.tagging") is not None
    tagging = importlib.import_module("meetily_memory.tagging")
    repository_class = getattr(tagging, "TagRepository", None)
    assert repository_class is not None

    state_path = tmp_path / "state.sqlite"
    state = UserStateRepository(state_path)
    source_uuid = state.get_or_create_source("meetily_sqlite", "/source.sqlite", now="1")
    repository = repository_class(state_path)
    repository.assign(
        source_uuid,
        ("meeting-1",),
        ("Сбер",),
        now="2",
    )
    repository.assign(
        source_uuid,
        ("meeting-2",),
        ("Сбер собес",),
        now="2",
    )

    matches = repository.search("что решили по сбер")

    assert [(match.meeting_external_id, match.kind) for match in matches] == [
        ("meeting-1", "token"),
        ("meeting-2", "token"),
    ]
    exact = repository.search("  СБЕР  ")
    assert [(match.meeting_external_id, match.kind) for match in exact] == [
        ("meeting-1", "exact"),
        ("meeting-2", "token"),
    ]


def test_tag_service_validates_batch_and_persists_assignments(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    tagging = importlib.import_module("meetily_memory.tagging")
    service_class = getattr(tagging, "TagService", None)
    assert service_class is not None

    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    service = service_class(IndexRepository(index_path, state_path=state_path))

    assigned = service.assign(("1", "2"), ("Сбер", "собес"))
    repeated = service.assign(("1", "2"), ("сбер", "СОБЕС"))

    assert assigned.added_links == 4
    assert repeated.existing_links == 4
    assert [tag.display_name for tag in service.list_for_meeting("1")] == ["Сбер", "собес"]
    assert [(item.display_name, item.active_meetings) for item in service.list_all()] == [
        ("Сбер", 2),
        ("собес", 2),
    ]

    with pytest.raises(ValueError, match="Meetings not found: 999"):
        service.assign(("1", "999"), ("не записывать",))
    assert service.repository.search("не записывать") == ()


def test_tag_list_and_suggest_hydrate_assignments_with_bounded_queries(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    writer = TagService(IndexRepository(index_path, state_path=state_path))
    writer.assign(("2",), tuple(f"batch-only-tag-{index}" for index in range(16)))
    service = TagService(IndexRepository.open_existing(index_path, state_path=state_path))
    counts = {
        "index_connections": 0,
        "index_queries": 0,
        "state_connections": 0,
        "state_queries": 0,
    }

    @contextmanager
    def counted_index_connection(path: Path) -> Generator[sqlite3.Connection, None, None]:
        counts["index_connections"] += 1
        with existing_index_connection(path) as conn:

            def trace(statement: str) -> None:
                if statement.lstrip().upper().startswith(("SELECT", "WITH")):
                    counts["index_queries"] += 1

            conn.set_trace_callback(trace)
            yield conn

    original_state_connection = service.repository._connect  # noqa: SLF001

    @contextmanager
    def counted_state_connection() -> Generator[sqlite3.Connection, None, None]:
        counts["state_connections"] += 1
        with original_state_connection() as conn:

            def trace(statement: str) -> None:
                if statement.lstrip().upper().startswith(("SELECT", "WITH")):
                    counts["state_queries"] += 1

            conn.set_trace_callback(trace)
            yield conn

    monkeypatch.setattr(
        service.index_repository.meetings,
        "context",
        replace(
            service.index_repository.meetings.context,
            connection=counted_index_connection,
        ),
    )
    monkeypatch.setattr(service.index_repository, "connection", counted_index_connection)
    monkeypatch.setattr(service.repository, "_connect", counted_state_connection)

    assert len(service.list_all()) == 16
    assert counts == {
        "index_connections": 1,
        "index_queries": 1,
        "state_connections": 1,
        "state_queries": 1,
    }

    counts.update(
        index_connections=0,
        index_queries=0,
        state_connections=0,
        state_queries=0,
    )
    assert service.suggest("1") == ()
    assert counts == {
        "index_connections": 5,
        "index_queries": 5,
        "state_connections": 2,
        "state_queries": 2,
    }


def test_manual_tags_survive_disposable_index_rebuild(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    tagging = importlib.import_module("meetily_memory.tagging")
    service_class = getattr(tagging, "TagService", None)
    assert service_class is not None

    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    service = service_class(IndexRepository(index_path, state_path=state_path))
    service.assign(("1",), ("Сбер",))

    index_path.unlink()
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    rebuilt = service_class(IndexRepository(index_path, state_path=state_path))

    assert [tag.display_name for tag in rebuilt.list_for_meeting("1")] == ["Сбер"]


def test_orphaned_tag_assignments_are_preserved_but_excluded_from_active_tags(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    tagging = importlib.import_module("meetily_memory.tagging")
    service_class = tagging.TagService

    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    service = service_class(IndexRepository(index_path, state_path=state_path))
    identity = service.index_repository.meeting_ref_for_local_id(1)
    assert identity is not None
    service.repository.assign(
        identity.source_uuid,
        ("missing-meeting",),
        ("Сбер",),
        now="2",
    )

    assert service.list_all() == ()
    assert service.orphaned_assignment_count() == 1
    assert service.repository.search("сбер")[0].meeting_external_id == "missing-meeting"


def test_cli_assigns_lists_and_removes_tags(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()
    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0

    added = runner.invoke(
        app,
        ["--index", str(index_path), "tag", "add", "1", "2", "сбер,собес"],
    )
    repeated = runner.invoke(
        app,
        ["--index", str(index_path), "tag", "add", "1", "2", "СБЕР,собес"],
    )
    meeting_tags = runner.invoke(
        app,
        ["--index", str(index_path), "tag", "list", "1"],
    )
    all_tags = runner.invoke(
        app,
        ["--index", str(index_path), "tag", "list"],
    )
    removed = runner.invoke(
        app,
        ["--index", str(index_path), "tag", "remove", "1", "сбер"],
    )

    assert added.exit_code == 0
    assert "Added: сбер, собес" in added.stdout
    assert repeated.exit_code == 0
    assert "Already assigned: сбер, собес" in repeated.stdout
    assert meeting_tags.exit_code == 0
    assert "Meeting #1" in meeting_tags.stdout
    assert "- сбер" in meeting_tags.stdout
    assert "- собес" in meeting_tags.stdout
    assert all_tags.exit_code == 0
    assert "сбер" in all_tags.stdout
    assert "2 meetings" in all_tags.stdout
    assert removed.exit_code == 0
    assert "Removed: сбер" in removed.stdout


def test_cli_tag_batch_errors_do_not_write_partial_state(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()
    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0

    missing_meeting = runner.invoke(
        app,
        ["--index", str(index_path), "tag", "add", "1", "999", "сбер"],
    )
    no_ids = runner.invoke(
        app,
        ["--index", str(index_path), "tag", "add", "сбер"],
    )
    no_tags = runner.invoke(
        app,
        ["--index", str(index_path), "tag", "add", "1", "2"],
    )
    all_tags = runner.invoke(
        app,
        ["--index", str(index_path), "tag", "list"],
    )

    assert missing_meeting.exit_code == 2
    assert "Meetings not found: 999" in missing_meeting.output
    assert "No tags were changed." in missing_meeting.output
    assert no_ids.exit_code == 2
    assert "No meeting IDs provided." in no_ids.output
    assert no_tags.exit_code == 2
    assert "No tags provided." in no_tags.output
    assert "сбер" not in all_tags.stdout


def test_search_returns_meetings_with_real_or_empty_evidence(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    tagging = importlib.import_module("meetily_memory.tagging")
    service_class = getattr(tagging, "TagService", None)
    assert service_class is not None

    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path, state_path=state_path)
    service = service_class(IndexRepository(index_path, state_path=state_path))
    service.assign(("2",), ("Сбер",))

    tag_only = core.search("сбер").results
    lexical = core.search("migration risks").results

    assert len(tag_only) == 1
    assert tag_only[0].meeting_id == 2
    assert tag_only[0].rank == 1
    assert tag_only[0].match_sources == (RetrievalSource.TAG,)
    assert tag_only[0].matched_tags == ("Сбер",)
    assert tag_only[0].evidence == ()
    assert len({result.meeting.external_id for result in lexical}) == len(lexical)
    assert lexical[0].match_sources == (RetrievalSource.FTS,)
    assert 1 <= len(lexical[0].evidence) <= 2


def test_tag_only_search_uses_the_same_date_filter(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    repository = IndexRepository(index_path, state_path=state_path)
    TagService(repository).assign(("1", "2"), ("product integration",))
    filters = MeetingSearchFilters(
        from_utc=datetime(2026, 7, 2, tzinfo=UTC),
        to_utc=datetime(2026, 7, 3, tzinfo=UTC),
    )

    results = MeetilyMemoryCore(index_path, state_path=state_path).search(
        "product integration",
        filters=filters,
    )

    assert [result.meeting.external_id for result in results.results] == ["meeting-2"]
    assert results.results[0].match_sources == (RetrievalSource.TAG,)


def test_search_orders_exact_tag_before_lexical_before_token_tag(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    tagging = importlib.import_module("meetily_memory.tagging")
    service_class = getattr(tagging, "TagService", None)
    assert service_class is not None

    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path, state_path=state_path)
    service = service_class(IndexRepository(index_path, state_path=state_path))
    service.assign(("2",), ("pricing decision",))

    exact_then_lexical = core.search("pricing decision").results
    lexical_then_token = core.search("pricing").results

    assert [result.meeting_id for result in exact_then_lexical[:2]] == [2, 1]
    assert exact_then_lexical[0].match_sources == (RetrievalSource.TAG,)
    assert exact_then_lexical[1].match_sources == (RetrievalSource.FTS,)
    assert [result.meeting_id for result in lexical_then_token[:2]] == [1, 2]
    assert lexical_then_token[1].match_sources == (RetrievalSource.TAG,)


def test_cli_search_explains_tag_only_match(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()
    assert (
        runner.invoke(
            app,
            ["--index", str(index_path), "scan", "--source", str(meetily_db)],
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            ["--index", str(index_path), "tag", "add", "2", "сбер"],
        ).exit_code
        == 0
    )

    result = runner.invoke(app, ["--index", str(index_path), "s", "сбер"])

    assert result.exit_code == 0
    assert "#2 Dobrynya Follow-up" in result.stdout
    assert "matched tag: сбер" in result.stdout
    assert "chunk #" not in result.stdout
    meeting = IndexRepository.open_existing(index_path).get_meeting_by_local_id(2)
    assert meeting is not None
    assert (
        "open: mm open --source-uuid "
        f"{meeting['source_uuid']} --external-id {meeting['external_id']}" in result.stdout
    )


@requires_sqlite_vec
def test_tag_suggestions_prioritize_title_text_then_similar_meeting(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    provider = LocalHashEmbeddingProvider()
    index_semantic_embeddings(index_path, embedding_provider=provider)
    service = TagService(IndexRepository(index_path, state_path=state_path))
    service.assign(
        ("2",),
        ("Launch Planning", "pricing decision", "migration-team"),
    )

    suggestions = service.suggest("1", embedding_provider=provider)

    assert [
        (item.tag.display_name, item.reason, item.similar_meeting_id) for item in suggestions
    ] == [
        ("Launch Planning", "title match", None),
        ("pricing decision", "text match", None),
        ("migration-team", "similar meeting", 2),
    ]


@requires_sqlite_vec
def test_tag_suggestions_skip_all_target_chunks_to_find_another_meeting(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    with sqlite3.connect(meetily_db) as conn:
        conn.execute("DELETE FROM transcripts")
        conn.execute("DELETE FROM summary_processes")
        conn.execute("DELETE FROM meeting_notes")
        conn.executemany(
            """
            INSERT INTO transcripts (
                id, meeting_id, transcript, timestamp, audio_start_time,
                audio_end_time, duration, speaker
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"target-{ordinal}",
                    "meeting-1",
                    "target semantic topic",
                    f"10:{ordinal:02d}:00",
                    float(ordinal),
                    float(ordinal + 1),
                    1.0,
                    "Alice",
                )
                for ordinal in range(60)
            ]
            + [
                (
                    "other-1",
                    "meeting-2",
                    "other semantic topic",
                    "11:00:00",
                    0.0,
                    1.0,
                    1.0,
                    "Bob",
                )
            ],
        )
        conn.commit()
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    provider = TargetBiasedEmbeddingProvider()
    index_semantic_embeddings(index_path, embedding_provider=provider)
    service = TagService(IndexRepository(index_path, state_path=state_path))
    service.assign(("2",), ("private-label",))

    suggestions = service.suggest("1", embedding_provider=provider)

    assert [(item.tag.display_name, item.similar_meeting_id) for item in suggestions] == [
        ("private-label", 2)
    ]


class ExplodingEmbeddingProvider:
    name = "never"
    model = "never"
    dims: int | None = 128

    def embed(self, texts: list[str], *, role: str) -> list[list[float]]:
        del texts, role
        message = "must not run"
        raise AssertionError(message)


class UnavailableHashProvider:
    name = "hash"
    model = "local-hash-v1"
    dims: int | None = 128

    def embed(self, texts: list[str], *, role: str) -> list[list[float]]:
        del texts, role
        message = "provider unavailable"
        raise RuntimeError(message)


def test_tag_suggestions_fall_back_without_complete_semantic_index(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    service = TagService(IndexRepository(index_path, state_path=state_path))
    service.assign(("2",), ("Launch Planning", "pricing decision", "migration-team"))
    service.assign(("1",), ("pricing decision",))

    suggestions = service.suggest(
        "1",
        embedding_provider=ExplodingEmbeddingProvider(),
    )

    assert [(item.tag.display_name, item.reason) for item in suggestions] == [
        ("Launch Planning", "title match"),
    ]


@requires_sqlite_vec
def test_tag_suggestions_fall_back_when_ready_semantic_provider_fails(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    MeetilySQLiteScanner(index_path, state_path=state_path).scan(meetily_db)
    index_semantic_embeddings(
        index_path,
        embedding_provider=LocalHashEmbeddingProvider(),
    )
    service = TagService(IndexRepository(index_path, state_path=state_path))
    service.assign(("2",), ("Launch Planning", "migration-team"))

    suggestions = service.suggest(
        "1",
        embedding_provider=UnavailableHashProvider(),
    )

    assert [(item.tag.display_name, item.reason) for item in suggestions] == [
        ("Launch Planning", "title match"),
    ]


def test_cli_suggests_existing_tags_without_persisting_suggestions(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    data_dir = tmp_path / "data"
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()
    assert (
        runner.invoke(
            app,
            ["--index", str(index_path), "scan", "--source", str(meetily_db)],
            env=env,
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            [
                "--index",
                str(index_path),
                "tag",
                "add",
                "2",
                "Launch Planning,pricing decision",
            ],
            env=env,
        ).exit_code
        == 0
    )

    suggested = runner.invoke(
        app,
        ["--index", str(index_path), "tag", "suggest", "1"],
        env=env,
    )

    assert suggested.exit_code == 0
    assert "Suggested tags for meeting #1:" in suggested.stdout
    assert "1. Launch Planning — title match" in suggested.stdout
    assert "2. pricing decision — text match" in suggested.stdout
    with sqlite3.connect(index_path.with_name("state.sqlite")) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "meeting_tag_suggestions" not in tables
