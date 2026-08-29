import base64
import hashlib
import sqlite3
import unicodedata
from pathlib import Path

import pytest

import meetily_memory.integrations as integrations_module
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.integrations import (
    MANAGED_MARKER,
    MAX_FILENAME_COMPONENT_BYTES,
    NoteRef,
    PlannedNote,
    apply_obsidian_note_plan,
    build_obsidian_note_plan,
    object_key_from_note_text,
    paths_are_same_case_variant,
    person_stable_key,
    rename_case_variant_exactly,
    render_obsidian_meeting_note,
    stable_content_fingerprint,
    sync_obsidian_vault,
)
from meetily_memory.json_codec import dumps_json, loads_json
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner


def scan_fixture(meetily_db: Path, tmp_path: Path) -> tuple[Path, MeetilySQLiteScanner]:
    index_path = tmp_path / "index.sqlite"
    scanner = MeetilySQLiteScanner(index_path)
    scanner.scan(meetily_db)
    return index_path, scanner


def meeting_refs(index_path: Path) -> dict[str, NoteRef]:
    return {
        meeting.external_id: NoteRef.meeting(
            meeting.ref.source_uuid,
            meeting.external_id,
            meeting.title,
        )
        for meeting in MeetilyMemoryCore(index_path).meetings(limit=100)
    }


def planned_text(ref: NoteRef) -> str:
    return f"# {ref.display_label}\n\n{ref.identity_marker}\n"


def marker_text(payload: object) -> str:
    object_key = dumps_json(payload)
    encoded = base64.urlsafe_b64encode(object_key.encode()).decode().rstrip("=")
    return f"<!-- meetily-memory:managed:v1:{encoded} -->\n"


def managed_paths(root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.glob("*/*.md")
            if MANAGED_MARKER in path.read_text(encoding="utf-8")
        )
    )


def test_obsidian_sync_creates_stable_managed_note_network(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path, _scanner = scan_fixture(meetily_db, tmp_path)
    vault_path = tmp_path / "vault"

    result = sync_obsidian_vault(index_path, vault_path, "Meetily Memory")

    root = vault_path / "Meetily Memory"
    refs = meeting_refs(index_path)
    assert result.root_dir == root
    assert result.files_written >= 6
    assert all((root / ref.relative_path).exists() for ref in refs.values())
    assert (root / "Tasks").is_dir()
    task_notes = list((root / "Tasks").glob("*.md"))
    assert task_notes
    task_text = task_notes[0].read_text(encoding="utf-8")
    assert MANAGED_MARKER in task_text
    assert any(ref.wikilink in task_text for ref in refs.values())
    assert "/meeting-" in task_text
    assert "Confidence:" not in task_text
    for external_id, ref in refs.items():
        note = (root / ref.relative_path).read_text(encoding="utf-8")
        source_uuid = loads_json(ref.object_key)["source_uuid"]
        command = f"mm open --source-uuid {source_uuid} --external-id {external_id}"
        assert command in note
        assert "mm open 1" not in note
        assert "mm open 2" not in note


def test_duplicate_titles_create_distinct_notes_and_unambiguous_links(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    with sqlite3.connect(meetily_db) as conn:
        conn.execute("UPDATE meetings SET title = 'Duplicate title'")
    index_path, _scanner = scan_fixture(meetily_db, tmp_path)
    vault_path = tmp_path / "vault"

    sync_obsidian_vault(index_path, vault_path)

    root = vault_path / "Meetily Memory"
    refs = meeting_refs(index_path)
    assert len({ref.relative_path for ref in refs.values()}) == 2
    assert all((root / ref.relative_path).exists() for ref in refs.values())
    assert len({ref.wikilink for ref in refs.values()}) == 2
    assert all("|Duplicate title]]" in ref.wikilink for ref in refs.values())


def test_sanitization_collisions_and_same_entity_prefixes_remain_distinct(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    with sqlite3.connect(meetily_db) as conn:
        conn.execute("UPDATE meetings SET title = 'A/B' WHERE id = 'meeting-1'")
        conn.execute("UPDATE meetings SET title = 'A:B' WHERE id = 'meeting-2'")
    index_path, _scanner = scan_fixture(meetily_db, tmp_path)
    refs = meeting_refs(index_path)
    assert len({ref.relative_path for ref in refs.values()}) == 2

    prefix = "same readable prefix " * 8
    first = NoteRef.entity(
        source_uuid="source",
        meeting_external_id="meeting",
        chunk_evidence_id="evidence:one",
        kind="action_items",
        stable_content_fingerprint=stable_content_fingerprint(f"{prefix}alpha"),
        text=f"{prefix}alpha",
        directory="Tasks",
    )
    second = NoteRef.entity(
        source_uuid="source",
        meeting_external_id="meeting",
        chunk_evidence_id="evidence:two",
        kind="action_items",
        stable_content_fingerprint=stable_content_fingerprint(f"{prefix}beta"),
        text=f"{prefix}beta",
        directory="Tasks",
    )
    first_key = loads_json(first.object_key)
    assert first.relative_path != second.relative_path
    assert first.stem[:80] == second.stem[:80]
    assert set(first_key) == {
        "version",
        "kind",
        "source_uuid",
        "meeting_external_id",
        "chunk_evidence_id",
        "entity_kind",
        "stable_content_fingerprint",
    }
    assert "local_id" not in first.object_key


def test_forbidden_filename_and_wikilink_characters_are_safe() -> None:
    ref = NoteRef.meeting("source", "meeting", "Roadmap: A/B | [Q3] #1 ^ \\")

    assert not set('<>:"/\\|?*#^[]').intersection(ref.stem)
    assert ref.wikilink.count("|") == 1
    assert ref.wikilink.startswith(f"[[{ref.stem}|")
    assert ref.wikilink[-2:] == "]]"
    assert not set("|[]#^").intersection(ref.wikilink.split("|", maxsplit=1)[1][:-2])


@pytest.mark.parametrize("title", ["CON", "nul.txt", "LPT9", "name. "])
def test_note_ref_obeys_cross_platform_filename_constraints(title: str) -> None:
    ref = NoteRef.meeting("source", title, title)
    component = ref.relative_path.name

    assert len(component.encode()) <= MAX_FILENAME_COMPONENT_BYTES
    assert not component.removesuffix(".md").endswith((".", " "))
    assert component.casefold().split(".", maxsplit=1)[0] not in {
        "con",
        "nul",
        "lpt9",
    }


def test_composed_and_decomposed_long_unicode_is_normalized_and_byte_bounded() -> None:
    composed = "é" * 300
    decomposed = unicodedata.normalize("NFD", composed)

    first = NoteRef.meeting("source", "meeting", composed)
    second = NoteRef.meeting("source", "meeting", decomposed)

    assert first.relative_path == second.relative_path
    assert first.wikilink == second.wikilink
    assert unicodedata.is_normalized("NFC", first.relative_path.name)
    assert len(first.relative_path.name.encode()) <= MAX_FILENAME_COMPONENT_BYTES
    assert first.relative_path.name.encode().decode() == first.relative_path.name


def test_opaque_identity_values_preserve_canonically_equivalent_bytes() -> None:
    composed = "é"
    decomposed = unicodedata.normalize("NFD", composed)
    pairs = (
        (
            NoteRef.meeting("source", composed, "Same title"),
            NoteRef.meeting("source", decomposed, "Same title"),
            "external_id",
        ),
        (
            NoteRef.entity(
                source_uuid="source",
                meeting_external_id="meeting",
                chunk_evidence_id=composed,
                kind="action_items",
                stable_content_fingerprint="fingerprint",
                text="Same entity",
                directory="Tasks",
            ),
            NoteRef.entity(
                source_uuid="source",
                meeting_external_id="meeting",
                chunk_evidence_id=decomposed,
                kind="action_items",
                stable_content_fingerprint="fingerprint",
                text="Same entity",
                directory="Tasks",
            ),
            "chunk_evidence_id",
        ),
        (
            NoteRef.topic(composed, "Same topic"),
            NoteRef.topic(decomposed, "Same topic"),
            "stable_key",
        ),
        (
            NoteRef.person(composed, "Same person"),
            NoteRef.person(decomposed, "Same person"),
            "stable_key",
        ),
    )

    for first, second, field in pairs:
        assert loads_json(first.object_key)[field] == composed
        assert loads_json(second.object_key)[field] == decomposed
        assert first.object_key != second.object_key
        assert first.relative_path != second.relative_path


def test_rename_reconciles_only_the_same_meeting_identity(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    with sqlite3.connect(meetily_db) as conn:
        conn.execute("UPDATE meetings SET title = 'Shared title'")
    index_path, scanner = scan_fixture(meetily_db, tmp_path)
    vault_path = tmp_path / "vault"
    root = vault_path / "Meetily Memory"
    sync_obsidian_vault(index_path, vault_path)
    before = meeting_refs(index_path)
    renamed_old_path = root / before["meeting-1"].relative_path
    isolated_path = root / before["meeting-2"].relative_path
    isolated_text = isolated_path.read_text(encoding="utf-8")

    with sqlite3.connect(meetily_db) as conn:
        conn.execute(
            "UPDATE meetings SET title = ?, updated_at = ? WHERE id = ?",
            ("Renamed meeting", "2026-07-03T09:30:00Z", "meeting-1"),
        )
    scanner.scan(meetily_db)
    result = sync_obsidian_vault(index_path, vault_path)
    after = meeting_refs(index_path)

    assert result.files_removed >= 1
    assert not renamed_old_path.exists()
    assert (root / after["meeting-1"].relative_path).exists()
    assert isolated_path == root / after["meeting-2"].relative_path
    assert isolated_path.read_text(encoding="utf-8") == isolated_text


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

    names = {path.name for path in destination.parent.iterdir()}
    assert destination.name in names
    assert old_path.name not in names
    assert temporary.name not in names
    assert destination.read_text(encoding="utf-8") == planned_text(new_ref)


def test_case_only_rename_removes_distinct_case_sensitive_old_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault" / "Meetily Memory"
    old_ref = NoteRef.meeting("source", "meeting", "Case title")
    new_ref = NoteRef.meeting("source", "meeting", "case title")
    old_path = root / old_ref.relative_path
    destination = root / new_ref.relative_path
    old_path.parent.mkdir(parents=True)
    old_path.write_text(planned_text(old_ref), encoding="utf-8")
    unlinked: list[Path] = []

    def record_unlink(path: Path) -> None:
        unlinked.append(path)

    monkeypatch.setattr(Path, "samefile", lambda _self, _other: False)
    monkeypatch.setattr(Path, "unlink", record_unlink)

    result = apply_obsidian_note_plan(
        root,
        (PlannedNote(new_ref, planned_text(new_ref)),),
        destructive=True,
    )

    assert result.files_removed == 1
    assert unlinked == [old_path]
    assert destination not in unlinked
    assert destination.read_text(encoding="utf-8") == planned_text(new_ref)
    assert not paths_are_same_case_variant(old_path, destination)


def test_case_only_rename_failure_restores_original_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "Meetings"
    directory.mkdir()
    old_path = directory / "Case title--m-identity.md"
    temporary = directory / ".case-rename-temp.md"
    destination = directory / "case title--m-identity.md"
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

    names = {path.name for path in directory.iterdir()}
    assert old_path.name in names
    assert temporary.name not in names
    assert destination.name not in names
    assert old_path.read_text(encoding="utf-8") == "managed content"


def test_case_only_rename_restore_failure_keeps_note_at_temporary_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "Meetings"
    directory.mkdir()
    old_path = directory / "Case title--m-identity.md"
    temporary = directory / ".case-rename-temp.md"
    destination = directory / "case title--m-identity.md"
    old_path.write_text("managed content", encoding="utf-8")
    original_rename = Path.rename

    def fail_destination_and_restore(path: Path, target: Path) -> Path:
        if path == temporary and target in {destination, old_path}:
            message = f"injected rename failure for {target.name}"
            raise OSError(message)
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_destination_and_restore)
    monkeypatch.setattr(Path, "samefile", lambda _self, _other: True)

    with pytest.raises(OSError, match="injected rename failure"):
        rename_case_variant_exactly(old_path, temporary, destination)

    assert temporary.read_text(encoding="utf-8") == "managed content"
    assert not any(entry.name == old_path.name for entry in directory.iterdir())
    assert not any(entry.name == destination.name for entry in directory.iterdir())


def test_case_only_rename_destination_race_preserves_foreign_and_managed_notes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "Meetings"
    directory.mkdir()
    old_path = directory / "Case title--m-identity.md"
    temporary = directory / ".case-rename-temp.md"
    destination = directory / "case title--m-identity.md"
    old_path.write_text("managed content", encoding="utf-8")
    original_rename = Path.rename

    def occupy_destination_after_first_rename(path: Path, target: Path) -> Path:
        result = original_rename(path, target)
        if path == old_path and target == temporary:
            destination.write_text("personal content", encoding="utf-8")
        return result

    monkeypatch.setattr(Path, "rename", occupy_destination_after_first_rename)
    monkeypatch.setattr(Path, "samefile", lambda _self, _other: True)

    with pytest.raises(FileExistsError, match="destination is no longer free"):
        rename_case_variant_exactly(old_path, temporary, destination)

    assert destination.read_text(encoding="utf-8") == "personal content"
    managed_locations = [
        path
        for path in (old_path, temporary)
        if any(entry.name == path.name for entry in directory.iterdir())
    ]
    assert len(managed_locations) == 1
    assert managed_locations[0].read_text(encoding="utf-8") == "managed content"


def test_case_only_rename_failure_after_write_restores_updated_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault" / "Meetily Memory"
    old_ref = NoteRef.meeting("source", "meeting", "Case title")
    new_ref = NoteRef.meeting("source", "meeting", "case title")
    old_path = root / old_ref.relative_path
    destination = root / new_ref.relative_path
    old_path.parent.mkdir(parents=True)
    old_path.write_text(planned_text(old_ref), encoding="utf-8")
    if not destination.exists():
        pytest.skip("current filesystem does not alias case-only names")
    original_rename = Path.rename

    def fail_exact_destination(path: Path, target: Path) -> Path:
        if path.name.startswith(".meetily-memory-rename-") and target == destination:
            message = "injected exact rename failure after write"
            raise OSError(message)
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_exact_destination)

    with pytest.raises(OSError, match="injected exact rename failure after write"):
        apply_obsidian_note_plan(
            root,
            (PlannedNote(new_ref, planned_text(new_ref)),),
            destructive=True,
        )

    names = {entry.name for entry in old_path.parent.iterdir()}
    assert old_path.name in names
    assert destination.name not in names
    assert old_path.read_text(encoding="utf-8") == planned_text(new_ref)


def test_case_only_rename_matches_real_filesystem_case_semantics(tmp_path: Path) -> None:
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
    wikilink_target = new_ref.wikilink.removeprefix("[[").split("|", maxsplit=1)[0]
    assert result.files_removed == int(case_sensitive)
    assert destination.name in names
    assert old_path.name not in names
    assert destination.read_text(encoding="utf-8") == planned_text(new_ref)
    assert wikilink_target == destination.stem

    second = apply_obsidian_note_plan(
        root,
        (PlannedNote(new_ref, planned_text(new_ref)),),
        destructive=True,
    )

    assert second.files_written == 0
    assert second.files_removed == 0


def test_unicode_spelling_rename_matches_exact_note_ref_filename(tmp_path: Path) -> None:
    root = tmp_path / "vault" / "Meetily Memory"
    ref = NoteRef.meeting("source", "meeting", "Café")
    destination = root / ref.relative_path
    old_name = unicodedata.normalize("NFD", destination.name)
    if old_name == destination.name:
        pytest.skip("test filename has no distinct normalization spelling")
    old_path = destination.with_name(old_name)
    old_path.parent.mkdir(parents=True)
    old_path.write_text(planned_text(ref), encoding="utf-8")
    if not destination.exists():
        pytest.skip("current filesystem does not alias Unicode-normalized names")

    result = apply_obsidian_note_plan(
        root,
        (PlannedNote(ref, planned_text(ref)),),
        destructive=True,
    )

    names = {entry.name for entry in destination.parent.iterdir()}
    wikilink_target = ref.wikilink.removeprefix("[[").split("|", maxsplit=1)[0]
    assert result.files_written == 0
    assert result.files_removed == 0
    assert destination.name in names
    assert old_path.name not in names
    assert wikilink_target == destination.stem

    second = apply_obsidian_note_plan(
        root,
        (PlannedNote(ref, planned_text(ref)),),
        destructive=True,
    )
    assert second.files_written == 0
    assert second.files_removed == 0


def test_rebuild_preserves_all_note_identities(meetily_db: Path, tmp_path: Path) -> None:
    index_path, scanner = scan_fixture(meetily_db, tmp_path)
    vault_path = tmp_path / "vault"
    root = vault_path / "Meetily Memory"
    sync_obsidian_vault(index_path, vault_path)
    paths_before = managed_paths(root)
    markers_before = {
        path.relative_to(root).as_posix(): next(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith(MANAGED_MARKER)
        )
        for path in root.glob("*/*.md")
        if MANAGED_MARKER in path.read_text(encoding="utf-8")
    }

    index_path.unlink()
    scanner.scan(meetily_db)
    sync_obsidian_vault(index_path, vault_path)

    assert managed_paths(root) == paths_before
    assert {
        path.relative_to(root).as_posix(): next(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith(MANAGED_MARKER)
        )
        for path in root.glob("*/*.md")
        if MANAGED_MARKER in path.read_text(encoding="utf-8")
    } == markers_before


@pytest.mark.parametrize("duplicate_kind", ["object", "path"])
def test_duplicate_plan_fails_before_any_write_or_removal(
    tmp_path: Path,
    duplicate_kind: str,
) -> None:
    root = tmp_path / "vault" / "Meetily Memory"
    meetings_dir = root / "Meetings"
    meetings_dir.mkdir(parents=True)
    stale_ref = NoteRef.meeting("stale-source", "stale-meeting", "Stale")
    stale_path = meetings_dir / "stale.md"
    stale_path.write_text(planned_text(stale_ref), encoding="utf-8")
    first = NoteRef.meeting("source", "one", "One")
    second = first if duplicate_kind == "object" else NoteRef.meeting("source", "two", "Two")
    if duplicate_kind == "path":
        object.__setattr__(second, "relative_path", first.relative_path)
    plan = (
        PlannedNote(first, planned_text(first)),
        PlannedNote(second, planned_text(second)),
    )
    before = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*")))

    with pytest.raises(ValueError, match="Duplicate Obsidian note"):
        apply_obsidian_note_plan(root, plan, destructive=True)

    assert stale_path.exists()
    assert tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))) == before


def test_case_rename_temp_collision_fails_preflight_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault" / "Meetily Memory"
    old_ref = NoteRef.meeting("source", "meeting", "Case title")
    new_ref = NoteRef.meeting("source", "meeting", "case title")
    old_path = root / old_ref.relative_path
    old_path.parent.mkdir(parents=True)
    old_path.write_text(planned_text(old_ref), encoding="utf-8")
    digest = hashlib.sha256(old_ref.object_key.encode()).hexdigest()[
        : integrations_module.SUFFIX_DIGEST_HEX_LENGTH
    ]
    occupied_temp = old_path.parent / f".meetily-memory-rename-{digest}-0.md"
    occupied_temp.write_text("personal temp", encoding="utf-8")
    monkeypatch.setattr(integrations_module, "TEMP_PATH_ATTEMPTS", 1)
    monkeypatch.setattr(Path, "samefile", lambda _self, _other: True)

    with pytest.raises(ValueError, match="reserve a safe Obsidian rename path"):
        apply_obsidian_note_plan(
            root,
            (PlannedNote(new_ref, planned_text(new_ref)),),
            destructive=True,
        )

    names = {path.name for path in old_path.parent.iterdir()}
    assert names == {old_path.name, occupied_temp.name}
    assert old_path.read_text(encoding="utf-8") == planned_text(old_ref)
    assert occupied_temp.read_text(encoding="utf-8") == "personal temp"


def test_foreign_managed_destination_fails_preflight_without_mutation(tmp_path: Path) -> None:
    root = tmp_path / "vault" / "Meetily Memory"
    meetings_dir = root / "Meetings"
    meetings_dir.mkdir(parents=True)
    expected = NoteRef.meeting("source", "expected", "Expected")
    foreign = NoteRef.meeting("source", "foreign", "Foreign")
    destination = root / expected.relative_path
    destination.write_text(planned_text(foreign), encoding="utf-8")
    stale = meetings_dir / "stale.md"
    stale.write_text(planned_text(NoteRef.meeting("old", "old", "Old")), encoding="utf-8")
    original_destination = destination.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="belongs to another managed object"):
        apply_obsidian_note_plan(
            root,
            (PlannedNote(expected, planned_text(expected)),),
            destructive=True,
        )

    assert destination.read_text(encoding="utf-8") == original_destination
    assert stale.exists()
    assert not (root / "People").exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "kind": "meeting", "source_uuid": "source"},
        {
            "version": 1,
            "kind": "meeting",
            "source_uuid": "source",
            "external_id": "meeting",
            "extra": "foreign",
        },
        {
            "version": 1,
            "kind": "meeting",
            "source_uuid": 7,
            "external_id": "meeting",
        },
        {
            "version": 1,
            "kind": "meeting",
            "source_uuid": "source",
            "external_id": "",
        },
        {
            "version": 1,
            "kind": "entity",
            "source_uuid": "source",
            "meeting_external_id": "meeting",
            "source_evidence_id": "legacy-field",
            "entity_kind": "action_items",
            "stable_content_fingerprint": "fingerprint",
        },
        {
            "version": 1,
            "kind": "entity",
            "source_uuid": "source",
            "meeting_external_id": "meeting",
            "chunk_evidence_id": "evidence",
            "entity_kind": "unknown",
            "stable_content_fingerprint": "fingerprint",
        },
        {"version": 1, "kind": "topic", "stable_key": "topic:key", "id": 12},
        {"version": 1, "kind": "person", "stable_key": None},
        {"version": "1", "kind": "topic", "stable_key": "topic:key"},
    ],
)
def test_malformed_kind_specific_v1_markers_are_foreign_and_untouched(
    tmp_path: Path,
    payload: object,
) -> None:
    root = tmp_path / "vault" / "Meetily Memory"
    meetings = root / "Meetings"
    meetings.mkdir(parents=True)
    protected = meetings / "Protected.md"
    text = marker_text(payload)
    protected.write_text(text, encoding="utf-8")

    assert object_key_from_note_text(text) is None
    result = apply_obsidian_note_plan(root, (), destructive=True)

    assert result.files_removed == 0
    assert protected.read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    "ref",
    [
        NoteRef.meeting("source", "meeting", "Meeting"),
        NoteRef.entity(
            source_uuid="source",
            meeting_external_id="meeting",
            chunk_evidence_id="evidence",
            kind="action_items",
            stable_content_fingerprint="fingerprint",
            text="Entity",
            directory="Tasks",
        ),
        NoteRef.topic("topic:key", "Topic"),
        NoteRef.person("person:key", "Person"),
    ],
)
def test_exact_kind_specific_v1_markers_are_owned(ref: NoteRef) -> None:
    assert object_key_from_note_text(ref.identity_marker) == ref.object_key


def test_noncanonical_json_marker_is_foreign() -> None:
    object_key = '{"version":1,"kind":"meeting","source_uuid":"source","external_id":"meeting"}'
    encoded = base64.urlsafe_b64encode(object_key.encode()).decode().rstrip("=")
    marker = f"<!-- meetily-memory:managed:v1:{encoded} -->"

    assert object_key_from_note_text(marker) is None


def test_invalid_base64_marker_and_direct_invalid_note_ref_are_rejected() -> None:
    marker = "<!-- meetily-memory:managed:v1:a -->"

    assert object_key_from_note_text(marker) is None
    with pytest.raises(ValueError, match="canonical kind-specific v1 schema"):
        NoteRef(
            object_key="not-json",
            directory="Meetings",
            display_label="Invalid",
            suffix_kind="m",
        )


def test_unmanaged_and_foreign_marker_notes_are_never_changed(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path, _scanner = scan_fixture(meetily_db, tmp_path)
    vault_path = tmp_path / "vault"
    root = vault_path / "Meetily Memory"
    plan = build_obsidian_note_plan(MeetilyMemoryCore(index_path), 100)
    meeting_note = next(note for note in plan if note.ref.directory == "Meetings")
    destination = root / meeting_note.ref.relative_path
    destination.parent.mkdir(parents=True)
    destination.write_text("personal note", encoding="utf-8")
    generic = destination.parent / "Old generic marker.md"
    generic.write_text("<!-- meetily-memory:managed -->\n", encoding="utf-8")
    foreign = destination.parent / "Foreign marker.md"
    foreign.write_text("<!-- meetily-memory:managed:v2:foreign -->\n", encoding="utf-8")

    result = sync_obsidian_vault(index_path, vault_path)

    assert result.files_skipped == 1
    assert destination.read_text(encoding="utf-8") == "personal note"
    assert generic.read_text(encoding="utf-8") == "<!-- meetily-memory:managed -->\n"
    assert foreign.read_text(encoding="utf-8") == "<!-- meetily-memory:managed:v2:foreign -->\n"


def test_limited_sync_is_non_destructive_and_plan_order_is_deterministic(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path, _scanner = scan_fixture(meetily_db, tmp_path)
    vault_path = tmp_path / "vault"
    root = vault_path / "Meetily Memory"
    stale_ref = NoteRef.person(person_stable_key("Stale Person"), "Stale Person")
    stale_path = root / stale_ref.relative_path
    stale_path.parent.mkdir(parents=True)
    stale_path.write_text(planned_text(stale_ref), encoding="utf-8")

    first_plan = build_obsidian_note_plan(MeetilyMemoryCore(index_path), 100)
    second_plan = build_obsidian_note_plan(MeetilyMemoryCore(index_path), 100)
    result = sync_obsidian_vault(index_path, vault_path, limit=1)

    first_order = tuple(note.ref.relative_path.as_posix() for note in first_plan)
    assert first_order == tuple(note.ref.relative_path.as_posix() for note in second_plan)
    assert first_order == tuple(sorted(first_order, key=str.casefold))
    assert result.files_removed == 0
    assert stale_path.exists()


def test_meeting_renderer_keeps_source_aware_persistent_open_command() -> None:
    first = render_obsidian_meeting_note(
        {
            "local_id": 41,
            "ref": {"source_uuid": "source-a", "external_id": "meeting-a"},
            "title": "Duplicate title",
        }
    )
    second = render_obsidian_meeting_note(
        {
            "local_id": 99,
            "ref": {"source_uuid": "source-b", "external_id": "meeting-b"},
            "title": "Duplicate title",
        }
    )

    assert "`mm open --source-uuid source-a --external-id meeting-a`" in first
    assert "`mm open --source-uuid source-b --external-id meeting-b`" in second
    assert "mm open 41" not in first
    assert "mm open 99" not in second


@pytest.mark.parametrize("folder", ["../outside", "/absolute-outside"])
def test_obsidian_sync_rejects_folders_outside_vault(
    meetily_db: Path,
    tmp_path: Path,
    folder: str,
) -> None:
    index_path, _scanner = scan_fixture(meetily_db, tmp_path)
    vault_path = tmp_path / "vault"
    outside = tmp_path / "outside" / "Meetings"
    outside.mkdir(parents=True)
    protected = outside / "Protected.md"
    protected.write_text("protected\n", encoding="utf-8")

    with pytest.raises(ValueError, match="configured vault"):
        sync_obsidian_vault(index_path, vault_path, folder)

    assert protected.exists()


@pytest.mark.parametrize("target_location", ["outside", "inside"])
def test_obsidian_sync_rejects_any_managed_directory_symlink_before_mutation(
    meetily_db: Path,
    tmp_path: Path,
    target_location: str,
) -> None:
    index_path, _scanner = scan_fixture(meetily_db, tmp_path)
    vault_path = tmp_path / "vault"
    root = vault_path / "Meetily Memory"
    root.mkdir(parents=True)
    target = root / "Real Meetings" if target_location == "inside" else tmp_path / "outside"
    target.mkdir()
    protected = target / "Protected.md"
    protected.write_text("protected\n", encoding="utf-8")
    (root / "Meetings").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="managed directory must not be a symlink"):
        sync_obsidian_vault(index_path, vault_path)

    assert protected.read_text(encoding="utf-8") == "protected\n"
    assert not (root / "Topics").exists()
    assert not (root / "People").exists()
