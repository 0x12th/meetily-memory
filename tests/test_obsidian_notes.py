import base64
import sqlite3
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from meetily_memory.db.schema import IndexReadError
from meetily_memory.domain import MeetingRef
from meetily_memory.json_codec import dumps_json, loads_json
from meetily_memory.obsidian_notes import (
    MANAGED_MARKER,
    MAX_FILENAME_COMPONENT_BYTES,
    OBJECT_KEY_VERSION,
    OBSIDIAN_DIRS,
    OBSIDIAN_MARKER_VERSION,
    NoteRef,
    ObsidianMeetingSnapshot,
    PlannedNote,
    apply_obsidian_note_plan,
    build_obsidian_snapshot,
    object_key_from_note_text,
    paths_are_same_case_variant,
    rename_case_variant_exactly,
    render_obsidian_meeting_note,
    sync_obsidian_vault,
)
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.repositories.snapshot import SnapshotRepository
from meetily_memory.tagging import TagRepository
from tests.index_helpers import publish_fresh_index


def scan_fixture(meetily_db: Path, tmp_path: Path) -> Path:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    return index_path


def assign_tags(
    index_path: Path,
    source_uuid: str,
    meeting_ids: tuple[str, ...],
    tags: tuple[str, ...],
) -> None:
    TagRepository(index_path.with_name("state.sqlite")).assign(
        source_uuid,
        meeting_ids,
        tags,
        now="2026-08-31T12:00:00Z",
    )


def planned_text(ref: NoteRef) -> str:
    return f"# {ref.display_label}\n\n{ref.identity_marker}\n"


def marker_text(payload: object, *, version: int) -> str:
    object_key = dumps_json(payload)
    encoded = base64.urlsafe_b64encode(object_key.encode()).decode().rstrip("=")
    return f"<!-- meetily-memory:managed:v{version}:{encoded} -->\n"


def meeting_note_refs(index_path: Path) -> dict[str, NoteRef]:
    snapshot = build_obsidian_snapshot(index_path, 100)
    return {
        meeting.ref.external_id: NoteRef.meeting(
            meeting.ref.source_uuid,
            meeting.ref.external_id,
            meeting.title,
        )
        for meeting in snapshot.meetings
    }


def test_snapshot_is_immutable_and_contains_only_active_manual_assignments(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = scan_fixture(meetily_db, tmp_path)
    initial = build_obsidian_snapshot(index_path, 100)
    source_uuid = initial.meetings[0].ref.source_uuid
    assign_tags(index_path, source_uuid, ("meeting-1",), (" Project X ",))
    state_path = index_path.with_name("state.sqlite")
    with sqlite3.connect(state_path) as conn:
        orphan_tag_id = conn.execute(
            """
            INSERT INTO manual_tags (normalized_name, display_name, created_at)
            VALUES ('orphan', 'Orphan', '2026-08-31T12:00:00Z')
            RETURNING id
            """
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO meeting_tags (
              source_uuid, meeting_external_id, manual_tag_id, created_at
            ) VALUES (?, 'missing-meeting', ?, '2026-08-31T12:00:00Z')
            """,
            (source_uuid, orphan_tag_id),
        )

    snapshot = build_obsidian_snapshot(index_path, 100)

    meeting = next(item for item in snapshot.meetings if item.ref.external_id == "meeting-1")
    assert tuple(tag.display_name for tag in meeting.manual_tags) == ("Project X",)
    assert tuple(tag.tag.display_name for tag in snapshot.tags) == ("Project X",)
    assert snapshot.tags[0].meetings == (meeting,)
    attribute = "title"
    with pytest.raises(FrozenInstanceError):
        setattr(meeting, attribute, "changed")


def test_snapshot_uses_one_pinned_index_and_state_read_transaction(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = scan_fixture(meetily_db, tmp_path)
    seed = build_obsidian_snapshot(index_path, 100)
    source_uuid = seed.meetings[0].ref.source_uuid
    state_path = index_path.with_name("state.sqlite")
    with sqlite3.connect(state_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    assign_tags(index_path, source_uuid, ("meeting-1",), ("Before",))

    index_repository = IndexRepository.open_existing(index_path)
    snapshot_repository = SnapshotRepository(index_path)
    with index_repository.operation_snapshot() as pinned:
        assert pinned.execute("SELECT COUNT(*) FROM meetings").fetchone()[0] == 2
        with ThreadPoolExecutor(max_workers=1) as executor:
            writer = executor.submit(
                assign_tags,
                index_path,
                source_uuid,
                ("meeting-1",),
                ("After",),
            )
            writer.result(timeout=5)
        pinned_snapshot = snapshot_repository.read_in_snapshot(pinned, 100)
        pinned_meeting = next(
            item for item in pinned_snapshot.meetings if item.ref.external_id == "meeting-1"
        )
        assert tuple(tag.display_name for tag in pinned_meeting.manual_tags) == ("Before",)

    refreshed = snapshot_repository.read(100)
    refreshed_meeting = next(
        item for item in refreshed.meetings if item.ref.external_id == "meeting-1"
    )
    assert tuple(tag.display_name for tag in refreshed_meeting.manual_tags) == (
        "After",
        "Before",
    )


def test_obsidian_snapshot_strictly_decodes_nullable_meeting_text(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = scan_fixture(meetily_db, tmp_path)
    with sqlite3.connect(index_path) as conn:
        conn.execute(
            "UPDATE meetings SET started_at = NULL, summary_text = '' "
            "WHERE external_id = 'meeting-1'"
        )
        conn.commit()

    snapshot = build_obsidian_snapshot(index_path, 100)
    meeting = next(item for item in snapshot.meetings if item.ref.external_id == "meeting-1")
    assert meeting.started_at is None
    assert meeting.source_summary == ""

    with sqlite3.connect(index_path) as conn:
        conn.execute(
            "UPDATE meetings SET summary_text = ? WHERE external_id = 'meeting-1'",
            (sqlite3.Binary(b"not-text"),),
        )
        conn.commit()
    with pytest.raises(
        IndexReadError,
        match=r"meetings\.summary_text must be TEXT, got BLOB",
    ):
        build_obsidian_snapshot(index_path, 100)


def test_sync_creates_only_meetings_and_tags_snapshot(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = scan_fixture(meetily_db, tmp_path)
    snapshot = build_obsidian_snapshot(index_path, 100)
    source_uuid = snapshot.meetings[0].ref.source_uuid
    assign_tags(index_path, source_uuid, ("meeting-1", "meeting-2"), ("Roadmap",))
    assign_tags(index_path, source_uuid, ("meeting-1",), ("Launch",))
    vault_path = tmp_path / "vault"

    result = sync_obsidian_vault(index_path, vault_path)

    root = vault_path / "Meetily Memory"
    assert result.root_dir == root
    assert {path.name for path in root.iterdir()} == set(OBSIDIAN_DIRS)
    assert len(list((root / "Meetings").glob("*.md"))) == 2
    assert len(list((root / "Tags").glob("*.md"))) == 2
    assert {path.name for path in root.iterdir()} == {"Meetings", "Tags"}


def test_meeting_notes_include_ref_dates_summary_open_command_and_manual_tags(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = scan_fixture(meetily_db, tmp_path)
    snapshot = build_obsidian_snapshot(index_path, 100)
    source_uuid = snapshot.meetings[0].ref.source_uuid
    assign_tags(index_path, source_uuid, ("meeting-1",), ("Launch", "Roadmap"))

    sync_obsidian_vault(index_path, tmp_path / "vault")

    root = tmp_path / "vault" / "Meetily Memory"
    refs = meeting_note_refs(index_path)
    note = (root / refs["meeting-1"].relative_path).read_text(encoding="utf-8")
    assert f"- MeetingRef: `{source_uuid}/meeting-1`" in note
    assert "- Title: Launch Planning" in note
    assert "- Created at: 2026-07-01T10:00:00Z" in note
    assert "- Updated at: 2026-07-01T11:00:00Z" in note
    assert f"`mm open {source_uuid}/meeting-1`" in note
    assert "Launch checklist approved." in note
    assert "## Source summary" in note
    assert NoteRef.tag("launch", "Launch").wikilink in note
    assert NoteRef.tag("roadmap", "Roadmap").wikilink in note
    open_commands = [
        line.removeprefix("- Open: `").removesuffix("`")
        for line in note.splitlines()
        if line.startswith("- Open: ")
    ]
    assert open_commands == [f"mm open {source_uuid}/meeting-1"]
    assert not any(command.removeprefix("mm open ").isdigit() for command in open_commands)

    note_without_source_summary = (root / refs["meeting-2"].relative_path).read_text(
        encoding="utf-8"
    )
    assert "## Source summary" not in note_without_source_summary


def test_tag_notes_list_meetings_deterministically(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = scan_fixture(meetily_db, tmp_path)
    snapshot = build_obsidian_snapshot(index_path, 100)
    source_uuid = snapshot.meetings[0].ref.source_uuid
    assign_tags(index_path, source_uuid, ("meeting-1", "meeting-2"), ("Roadmap",))

    sync_obsidian_vault(index_path, tmp_path / "vault")

    root = tmp_path / "vault" / "Meetily Memory"
    tag_ref = NoteRef.tag("roadmap", "Roadmap")
    tag_note = (root / tag_ref.relative_path).read_text(encoding="utf-8")
    meeting_links = [
        line.removeprefix("- ") for line in tag_note.splitlines() if line.startswith("- [[")
    ]
    refs = meeting_note_refs(index_path)
    assert meeting_links == [refs["meeting-2"].wikilink, refs["meeting-1"].wikilink]

    second = sync_obsidian_vault(index_path, tmp_path / "vault")
    assert second.files_written == 0
    assert second.files_removed == 0


def test_source_summary_cannot_spoof_a_second_managed_marker() -> None:
    fake_marker = "<!-- meetily-memory:managed:v2:Zm9yZWlnbg -->"
    meeting = ObsidianMeetingSnapshot(
        ref=MeetingRef("source", "meeting"),
        title="Marker safety",
        started_at=None,
        ended_at=None,
        created_at="2026-08-31",
        updated_at="2026-08-31",
        source_summary=f"Summary\n{fake_marker}",
        manual_tags=(),
    )

    note = render_obsidian_meeting_note(meeting)

    assert f"> {fake_marker}" in note
    assert (
        object_key_from_note_text(note)
        == NoteRef.meeting("source", "meeting", "Marker safety").object_key
    )


def test_marker_v2_and_object_key_v2_are_explicit_and_strict() -> None:
    meeting = NoteRef.meeting("source", "meeting", "Meeting")
    tag = NoteRef.tag("project x", "Project X")

    assert OBSIDIAN_MARKER_VERSION == 2
    assert OBJECT_KEY_VERSION == 2

    assert MANAGED_MARKER.startswith("<!-- meetily-memory:managed:v2:")
    assert loads_json(meeting.object_key) == {
        "external_id": "meeting",
        "kind": "meeting",
        "source_uuid": "source",
        "version": 2,
    }
    assert loads_json(tag.object_key) == {
        "kind": "tag",
        "normalized_name": "project x",
        "version": 2,
    }
    assert object_key_from_note_text(meeting.identity_marker) == meeting.object_key
    assert object_key_from_note_text(tag.identity_marker) == tag.object_key


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 2, "kind": "meeting", "source_uuid": "source"},
        {
            "version": 2,
            "kind": "meeting",
            "source_uuid": "source",
            "external_id": "meeting",
            "extra": "foreign",
        },
        {"version": 2, "kind": "tag", "normalized_name": ""},
        {"version": 2, "kind": "topic", "stable_key": "legacy"},
        {"version": "2", "kind": "tag", "normalized_name": "tag"},
    ],
)
def test_malformed_v2_markers_are_foreign(payload: object) -> None:
    text = marker_text(payload, version=2)

    assert object_key_from_note_text(text) is None


def test_v1_and_retired_directory_notes_are_foreign_and_untouched(tmp_path: Path) -> None:
    root = tmp_path / "vault" / "Meetily Memory"
    protected = {
        root / "Meetings" / "Personal.md": "personal\n",
        root / "Meetings" / "Legacy.md": marker_text(
            {
                "version": 1,
                "kind": "meeting",
                "source_uuid": "source",
                "external_id": "meeting",
            },
            version=1,
        ),
        root / "Topics" / "Retired.md": "retired content\n",
    }
    for path, text in protected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    result = apply_obsidian_note_plan(root, (), destructive=True)

    assert result.files_removed == 0
    assert {path: path.read_text(encoding="utf-8") for path in protected} == protected


def test_stale_current_owned_notes_are_removed(tmp_path: Path) -> None:
    root = tmp_path / "vault" / "Meetily Memory"
    stale = NoteRef.meeting("old-source", "old-meeting", "Stale")
    stale_path = root / stale.relative_path
    stale_path.parent.mkdir(parents=True)
    stale_path.write_text(planned_text(stale), encoding="utf-8")

    result = apply_obsidian_note_plan(root, (), destructive=True)

    assert result.files_removed == 1
    assert not stale_path.exists()


def test_unmanaged_expected_destination_is_skipped_without_removing_owned_old_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault" / "Meetily Memory"
    ref = NoteRef.meeting("source", "meeting", "New title")
    old_path = root / "Meetings" / "Old title.md"
    destination = root / ref.relative_path
    old_path.parent.mkdir(parents=True)
    old_path.write_text(planned_text(ref), encoding="utf-8")
    destination.write_text("personal content\n", encoding="utf-8")

    result = apply_obsidian_note_plan(
        root,
        (PlannedNote(ref, planned_text(ref)),),
        destructive=True,
    )

    assert result.files_skipped == 1
    assert result.files_removed == 0
    assert old_path.exists()
    assert destination.read_text(encoding="utf-8") == "personal content\n"


def test_limited_sync_is_non_destructive(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = scan_fixture(meetily_db, tmp_path)
    root = tmp_path / "vault" / "Meetily Memory"
    stale = NoteRef.meeting("old", "old", "Old")
    stale_path = root / stale.relative_path
    stale_path.parent.mkdir(parents=True)
    stale_path.write_text(planned_text(stale), encoding="utf-8")

    result = sync_obsidian_vault(index_path, tmp_path / "vault", limit=1)

    assert result.files_removed == 0
    assert stale_path.exists()
    assert len(list((root / "Meetings").glob("*.md"))) == 2


def test_duplicate_plan_fails_before_any_write_or_removal(tmp_path: Path) -> None:
    root = tmp_path / "vault" / "Meetily Memory"
    stale = NoteRef.meeting("old", "old", "Old")
    stale_path = root / stale.relative_path
    stale_path.parent.mkdir(parents=True)
    stale_path.write_text(planned_text(stale), encoding="utf-8")
    duplicate = NoteRef.meeting("source", "meeting", "Meeting")
    plan = (
        PlannedNote(duplicate, planned_text(duplicate)),
        PlannedNote(duplicate, planned_text(duplicate)),
    )

    with pytest.raises(ValueError, match="Duplicate Obsidian note object identity"):
        apply_obsidian_note_plan(root, plan, destructive=True)

    assert stale_path.exists()
    assert not (root / duplicate.relative_path).exists()


@pytest.mark.parametrize("folder", ["../outside", "/absolute-outside"])
def test_sync_rejects_folder_outside_vault(
    meetily_db: Path,
    tmp_path: Path,
    folder: str,
) -> None:
    index_path = scan_fixture(meetily_db, tmp_path)
    protected = tmp_path / "outside" / "Protected.md"
    protected.parent.mkdir()
    protected.write_text("protected\n", encoding="utf-8")

    with pytest.raises(ValueError, match="configured vault"):
        sync_obsidian_vault(index_path, tmp_path / "vault", folder)

    assert protected.read_text(encoding="utf-8") == "protected\n"


@pytest.mark.parametrize("directory", ["Meetings", "Tags"])
def test_sync_rejects_snapshot_directory_symlinks_before_mutation(
    meetily_db: Path,
    tmp_path: Path,
    directory: str,
) -> None:
    index_path = scan_fixture(meetily_db, tmp_path)
    root = tmp_path / "vault" / "Meetily Memory"
    root.mkdir(parents=True)
    outside = tmp_path / f"outside-{directory}"
    outside.mkdir()
    protected = outside / "Protected.md"
    protected.write_text("protected\n", encoding="utf-8")
    (root / directory).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        sync_obsidian_vault(index_path, tmp_path / "vault")

    assert protected.read_text(encoding="utf-8") == "protected\n"
    assert not any((root / managed).exists() for managed in OBSIDIAN_DIRS if managed != directory)


def test_case_only_rename_matches_real_filesystem_semantics(tmp_path: Path) -> None:
    probe = tmp_path / "case-probe"
    probe.write_text("probe", encoding="utf-8")
    case_sensitive = not (tmp_path / "CASE-PROBE").exists()
    probe.unlink()
    root = tmp_path / "vault" / "Meetily Memory"
    old_ref = NoteRef.meeting("source", "meeting", "Case title")
    new_ref = NoteRef.meeting("source", "meeting", "case title")
    old_path = root / old_ref.relative_path
    destination = root / new_ref.relative_path
    old_path.parent.mkdir(parents=True)
    old_path.write_text(planned_text(old_ref), encoding="utf-8")

    result = apply_obsidian_note_plan(
        root,
        (PlannedNote(new_ref, planned_text(new_ref)),),
        destructive=True,
    )

    names = {path.name for path in destination.parent.iterdir()}
    assert result.files_removed == int(case_sensitive)
    assert destination.name in names
    assert old_path.name not in names
    assert destination.read_text(encoding="utf-8") == planned_text(new_ref)


def test_case_only_rename_failure_restores_original_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "Meetings"
    directory.mkdir()
    old_path = directory / "Case title.md"
    temporary = directory / ".case-rename-temp.md"
    destination = directory / "case title.md"
    old_path.write_text("managed content", encoding="utf-8")
    original_rename = Path.rename

    def fail_exact_destination(path: Path, target: Path) -> Path:
        if path == temporary and target == destination:
            message = "injected exact rename failure"
            raise OSError(message)
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_exact_destination)
    monkeypatch.setattr(Path, "samefile", lambda _self, _other: True)

    with pytest.raises(OSError, match="injected exact rename failure"):
        rename_case_variant_exactly(old_path, temporary, destination)

    names = {entry.name for entry in directory.iterdir()}
    assert old_path.read_text(encoding="utf-8") == "managed content"
    assert temporary.name not in names
    assert old_path.name in names
    assert destination.name not in names


def test_case_only_rename_never_unlinks_same_filesystem_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault" / "Meetily Memory"
    old_ref = NoteRef.meeting("source", "meeting", "Case title")
    new_ref = NoteRef.meeting("source", "meeting", "case title")
    old_path = root / old_ref.relative_path
    destination = root / new_ref.relative_path
    temporary = old_path.parent / ".case-rename-temp.md"
    old_path.parent.mkdir(parents=True)
    old_path.write_text(planned_text(new_ref), encoding="utf-8")
    monkeypatch.setattr(Path, "samefile", lambda _self, _other: True)

    assert paths_are_same_case_variant(old_path, destination)
    rename_case_variant_exactly(old_path, temporary, destination)

    names = {entry.name for entry in destination.parent.iterdir()}
    assert destination.read_text(encoding="utf-8") == planned_text(new_ref)
    assert destination.name in names
    assert old_path.name not in names
    assert temporary.name not in names


def test_note_refs_preserve_collision_resistance_and_portable_names() -> None:
    first = NoteRef.meeting("source", "one", "A/B")
    second = NoteRef.meeting("source", "two", "A:B")
    reserved = NoteRef.meeting("source", "three", "CON")
    long_unicode = NoteRef.meeting("source", "four", "é" * 300)

    assert first.relative_path != second.relative_path
    assert not set('<>:"/\\|?*#^[]').intersection(first.stem)
    assert reserved.relative_path.name.startswith("_CON")
    assert len(long_unicode.relative_path.name.encode()) <= MAX_FILENAME_COMPONENT_BYTES
    assert unicodedata.is_normalized("NFC", long_unicode.relative_path.name)


def test_canonically_equivalent_opaque_ids_remain_distinct() -> None:
    composed = "é"
    decomposed = unicodedata.normalize("NFD", composed)
    first = NoteRef.meeting("source", composed, "Same title")
    second = NoteRef.meeting("source", decomposed, "Same title")

    assert first.object_key != second.object_key
    assert first.relative_path != second.relative_path
