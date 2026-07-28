import importlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meetily_memory.cli.app import app
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.db.repository import IndexRepository
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.user_state import UserStateRepository


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
    identity = service.index_repository.meeting_source_identity("1")
    assert identity is not None
    source_uuid = identity["source_uuid"]
    service.repository.assign(
        str(source_uuid),
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
    service = service_class(core.repo)
    service.assign(("2",), ("Сбер",))

    tag_only = core.search("сбер").data["results"]
    lexical = core.search("migration risks").data["results"]

    assert len(tag_only) == 1
    assert tag_only[0]["meeting_id"] == 2
    assert tag_only[0]["rank"] == 1
    assert tag_only[0]["match_sources"] == ["tag"]
    assert tag_only[0]["matched_tags"] == ["Сбер"]
    assert tag_only[0]["evidence"] == []
    assert len({result["meeting"]["external_id"] for result in lexical}) == len(lexical)
    assert lexical[0]["match_sources"] == ["fts"]
    assert 1 <= len(lexical[0]["evidence"]) <= 2


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
    service = service_class(core.repo)
    service.assign(("2",), ("pricing decision",))

    exact_then_lexical = core.search("pricing decision").data["results"]
    lexical_then_token = core.search("pricing").data["results"]

    assert [result["meeting_id"] for result in exact_then_lexical[:2]] == [2, 1]
    assert exact_then_lexical[0]["match_sources"] == ["tag"]
    assert exact_then_lexical[1]["match_sources"] == ["fts"]
    assert [result["meeting_id"] for result in lexical_then_token[:2]] == [1, 2]
    assert lexical_then_token[1]["match_sources"] == ["tag"]


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
    assert "open: mm open 2" in result.stdout
