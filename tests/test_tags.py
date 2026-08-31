import importlib
import importlib.util
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meetily_memory.cli.app import app
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.db.state_schema import StateSchemaError
from meetily_memory.domain import MeetingRef, MeetingSearchFilters, RetrievalSource
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.tagging import TagRepository, TagService
from meetily_memory.user_state import UserStateRepository
from tests.index_helpers import publish_fresh_index


def indexed_ref(repository: IndexRepository, local_id: int) -> MeetingRef:
    meeting_ref = repository.meeting_ref_for_local_id(local_id)
    assert meeting_ref is not None
    return meeting_ref


def indexed_refs(repository: IndexRepository, *local_ids: int) -> tuple[MeetingRef, ...]:
    return tuple(indexed_ref(repository, local_id) for local_id in local_ids)


def test_tag_repository_rejects_preexisting_empty_database_without_modifying_it(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.sqlite"
    state_path.write_bytes(b"")
    before = (state_path.read_bytes(), state_path.stat().st_mtime_ns)

    with pytest.raises(
        StateSchemaError,
        match=r"Deleting state permanently loses manual tags and application settings",
    ):
        TagRepository(state_path)

    assert (state_path.read_bytes(), state_path.stat().st_mtime_ns) == before


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
    with sqlite3.connect(state_path) as connection:
        assert [row[1] for row in connection.execute("PRAGMA table_info(meeting_tags)")] == [
            "source_uuid",
            "meeting_external_id",
            "manual_tag_id",
            "created_at",
        ]
        assert connection.execute(
            "SELECT source_uuid, meeting_external_id, manual_tag_id, created_at "
            "FROM meeting_tags ORDER BY meeting_external_id, manual_tag_id"
        ).fetchall() == [
            (source_uuid, "meeting-1", 1, "2"),
            (source_uuid, "meeting-1", 2, "2"),
            (source_uuid, "meeting-2", 1, "2"),
            (source_uuid, "meeting-2", 2, "2"),
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
        assert (
            conn.execute("SELECT 1 FROM manual_tags WHERE normalized_name = 'сбер'").fetchone()
            is None
        )


def test_tag_repository_strictly_rejects_wrong_stored_tag_type(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    state = UserStateRepository(state_path)
    source_uuid = state.get_or_create_source("meetily_sqlite", "/source.sqlite", now="1")
    repository = TagRepository(state_path)
    repository.assign(source_uuid, ("meeting-1",), ("Manual",), now="2")
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "UPDATE manual_tags SET display_name = ? WHERE normalized_name = 'manual'",
            (sqlite3.Binary(b"not-text"),),
        )
        connection.commit()

    with pytest.raises(
        StateSchemaError,
        match=r"manual_tags\.display_name must be TEXT, got BLOB",
    ):
        repository.list_for_meeting(source_uuid, "meeting-1")


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

    assert [(match.meeting_ref.external_id, match.kind) for match in matches] == [
        ("meeting-1", "token"),
        ("meeting-2", "token"),
    ]
    exact = repository.search("  СБЕР  ")
    assert [(match.meeting_ref.external_id, match.kind) for match in exact] == [
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
    publish_fresh_index(index_path, meetily_db, state_path=state_path)
    service = service_class(IndexRepository(index_path, state_path=state_path))

    refs = indexed_refs(service.index_repository, 1, 2)
    assigned = service.assign(refs, ("Сбер", "собес"))
    repeated = service.assign(refs, ("сбер", "СОБЕС"))

    assert assigned.added_links == 4
    assert repeated.existing_links == 4
    assert [tag.display_name for tag in service.list_for_meeting(refs[0])] == ["Сбер", "собес"]
    assert [(item.display_name, item.active_meetings) for item in service.list_all()] == [
        ("Сбер", 2),
        ("собес", 2),
    ]

    missing_ref = MeetingRef(refs[0].source_uuid, "missing")
    with pytest.raises(ValueError, match=f"Meetings not found: {missing_ref.source_uuid}/missing"):
        service.assign((refs[0], missing_ref), ("не записывать",))
    assert service.repository.search("не записывать") == ()


def test_tag_list_and_suggest_reopen_exact_persisted_assignments(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    publish_fresh_index(index_path, meetily_db, state_path=state_path)
    writer = TagService(IndexRepository(index_path, state_path=state_path))
    writer.assign(
        (indexed_ref(writer.index_repository, 2),),
        tuple(f"batch-only-tag-{index}" for index in range(16)),
    )
    service = TagService(IndexRepository.open_existing(index_path, state_path=state_path))
    listed = service.list_all()
    assert len(listed) == 16
    assert {item.display_name for item in listed} == {
        f"batch-only-tag-{index}" for index in range(16)
    }

    meeting_ref = indexed_ref(service.index_repository, 1)
    assert service.suggest(meeting_ref) == ()

    reopened = TagService(IndexRepository.open_existing(index_path, state_path=state_path))
    assert reopened.list_all() == listed
    assert reopened.suggest(meeting_ref) == ()
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM manual_tags").fetchone() == (16,)
        assert conn.execute("SELECT COUNT(*) FROM meeting_tags").fetchone() == (16,)
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_manual_tags_survive_disposable_index_rebuild(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    tagging = importlib.import_module("meetily_memory.tagging")
    service_class = getattr(tagging, "TagService", None)
    assert service_class is not None

    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    publish_fresh_index(index_path, meetily_db, state_path=state_path)
    service = service_class(IndexRepository(index_path, state_path=state_path))
    meeting_ref = indexed_ref(service.index_repository, 1)
    service.assign((meeting_ref,), ("Сбер",))

    index_path.unlink()
    publish_fresh_index(index_path, meetily_db, state_path=state_path)
    rebuilt = service_class(IndexRepository(index_path, state_path=state_path))

    assert [tag.display_name for tag in rebuilt.list_for_meeting(meeting_ref)] == ["Сбер"]


def test_orphaned_tag_assignments_are_preserved_but_excluded_from_active_tags(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    tagging = importlib.import_module("meetily_memory.tagging")
    service_class = tagging.TagService

    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    publish_fresh_index(index_path, meetily_db, state_path=state_path)
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
    assert service.repository.search("сбер")[0].meeting_ref.external_id == "missing-meeting"


def test_cli_assigns_lists_and_removes_tags(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()
    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0
    repository = IndexRepository.open_existing(index_path)
    first_ref, second_ref = indexed_refs(repository, 1, 2)

    added = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "tag",
            "add",
            "сбер,собес",
            "--source-uuid",
            first_ref.source_uuid,
            "--external-id",
            first_ref.external_id,
            "--external-id",
            second_ref.external_id,
        ],
    )
    repeated = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "tag",
            "add",
            "СБЕР,собес",
            "--source-uuid",
            first_ref.source_uuid,
            "--external-id",
            first_ref.external_id,
            "--external-id",
            second_ref.external_id,
        ],
    )
    meeting_tags = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "tag",
            "list",
            "--source-uuid",
            first_ref.source_uuid,
            "--external-id",
            first_ref.external_id,
        ],
    )
    all_tags = runner.invoke(
        app,
        ["--index", str(index_path), "tag", "list"],
    )
    removed = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "tag",
            "remove",
            "сбер",
            "--source-uuid",
            first_ref.source_uuid,
            "--external-id",
            first_ref.external_id,
        ],
    )

    assert added.exit_code == 0
    assert "Added: сбер, собес" in added.stdout
    assert repeated.exit_code == 0
    assert "Already assigned: сбер, собес" in repeated.stdout
    assert meeting_tags.exit_code == 0
    assert f"Meeting {first_ref.source_uuid}/{first_ref.external_id}" in meeting_tags.stdout
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
    meeting_ref = indexed_ref(IndexRepository.open_existing(index_path), 1)

    missing_meeting = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "tag",
            "add",
            "сбер",
            "--source-uuid",
            meeting_ref.source_uuid,
            "--external-id",
            meeting_ref.external_id,
            "--external-id",
            "missing",
        ],
    )
    no_ids = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "tag",
            "add",
            "сбер",
            "--source-uuid",
            meeting_ref.source_uuid,
        ],
    )
    no_tags = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "tag",
            "add",
            "--source-uuid",
            meeting_ref.source_uuid,
            "--external-id",
            meeting_ref.external_id,
        ],
    )
    all_tags = runner.invoke(
        app,
        ["--index", str(index_path), "tag", "list"],
    )

    assert missing_meeting.exit_code == 2
    assert f"Meetings not found: {meeting_ref.source_uuid}/missing" in missing_meeting.output
    assert "No tags were changed." in missing_meeting.output
    assert no_ids.exit_code == 2
    assert "Provide at least one --external-id." in no_ids.output
    assert no_tags.exit_code == 2
    assert "Missing argument 'TAGS'" in no_tags.output
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
    publish_fresh_index(index_path, meetily_db, state_path=state_path)
    core = MeetilyMemoryCore(index_path, state_path=state_path)
    service = service_class(IndexRepository(index_path, state_path=state_path))
    service.assign((indexed_ref(service.index_repository, 2),), ("Сбер",))

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
    publish_fresh_index(index_path, meetily_db, state_path=state_path)
    repository = IndexRepository(index_path, state_path=state_path)
    TagService(repository).assign(indexed_refs(repository, 1, 2), ("product integration",))
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
    publish_fresh_index(index_path, meetily_db, state_path=state_path)
    core = MeetilyMemoryCore(index_path, state_path=state_path)
    service = service_class(IndexRepository(index_path, state_path=state_path))
    service.assign((indexed_ref(service.index_repository, 2),), ("pricing decision",))

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
    meeting_ref = indexed_ref(IndexRepository.open_existing(index_path), 2)
    assert (
        runner.invoke(
            app,
            [
                "--index",
                str(index_path),
                "tag",
                "add",
                "сбер",
                "--source-uuid",
                meeting_ref.source_uuid,
                "--external-id",
                meeting_ref.external_id,
            ],
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
    assert f"open: mm open {meeting['source_uuid']}/{meeting['external_id']}" in result.stdout


def test_tag_suggestions_use_existing_title_and_text_matches(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    publish_fresh_index(index_path, meetily_db, state_path=state_path)
    service = TagService(IndexRepository(index_path, state_path=state_path))
    service.assign(
        (indexed_ref(service.index_repository, 2),),
        ("Launch Planning", "pricing decision", "migration-team"),
    )

    suggestions = service.suggest(indexed_ref(service.index_repository, 1))

    assert [(item.tag.display_name, item.reason) for item in suggestions] == [
        ("Launch Planning", "title match"),
        ("pricing decision", "text match"),
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
    repository = IndexRepository.open_existing(index_path)
    first_ref, second_ref = indexed_refs(repository, 1, 2)
    assert (
        runner.invoke(
            app,
            [
                "--index",
                str(index_path),
                "tag",
                "add",
                "Launch Planning,pricing decision",
                "--source-uuid",
                second_ref.source_uuid,
                "--external-id",
                second_ref.external_id,
            ],
            env=env,
        ).exit_code
        == 0
    )

    suggested = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "tag",
            "suggest",
            "--source-uuid",
            first_ref.source_uuid,
            "--external-id",
            first_ref.external_id,
        ],
        env=env,
    )

    assert suggested.exit_code == 0
    expected_header = f"Suggested tags for meeting {first_ref.source_uuid}/{first_ref.external_id}:"
    assert expected_header in suggested.stdout
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
