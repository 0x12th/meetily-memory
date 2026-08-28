import sqlite3
from pathlib import Path

import pytest

from meetily_memory.integrations import MANAGED_MARKER, sync_obsidian_vault
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner


def test_obsidian_sync_creates_managed_note_network(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    vault_path = tmp_path / "vault"
    MeetilySQLiteScanner(index_path).scan(meetily_db)

    result = sync_obsidian_vault(index_path, vault_path, "Meetily Memory")

    root = vault_path / "Meetily Memory"
    assert result.root_dir == root
    assert result.files_written >= 6
    assert (root / "Meetings" / "Dobrynya Follow-up.md").exists()
    assert (root / "Tasks").is_dir()
    task_notes = list((root / "Tasks").glob("*.md"))
    assert task_notes
    task_text = task_notes[0].read_text(encoding="utf-8")
    assert MANAGED_MARKER in task_text
    assert "[[Dobrynya Follow-up]]" in task_text
    assert "Source: meeting-2 /" in task_text
    assert "Confidence:" not in task_text


def test_obsidian_sync_does_not_overwrite_unmanaged_notes(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    vault_path = tmp_path / "vault"
    root = vault_path / "Meetily Memory"
    unmanaged = root / "Meetings" / "Dobrynya Follow-up.md"
    unmanaged.parent.mkdir(parents=True)
    unmanaged.write_text("personal note", encoding="utf-8")
    MeetilySQLiteScanner(index_path).scan(meetily_db)

    result = sync_obsidian_vault(index_path, vault_path, "Meetily Memory")

    assert result.files_skipped >= 1
    assert unmanaged.read_text(encoding="utf-8") == "personal note"


def test_obsidian_sync_removes_only_stale_managed_notes(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    vault_path = tmp_path / "vault"
    root = vault_path / "Meetily Memory"
    scanner = MeetilySQLiteScanner(index_path)
    scanner.scan(meetily_db)
    sync_obsidian_vault(index_path, vault_path)
    stale_managed = root / "Meetings" / "Dobrynya Follow-up.md"
    unmanaged = root / "Meetings" / "Personal.md"
    changed_marker = root / "Meetings" / "Changed marker.md"
    unmanaged.write_text("personal note", encoding="utf-8")
    changed_marker.write_text("<!-- meetily-memory:managed-v2 -->\n", encoding="utf-8")

    with sqlite3.connect(meetily_db) as conn:
        conn.execute("DELETE FROM meetings WHERE id = 'meeting-2'")
    scanner.scan(meetily_db)

    result = sync_obsidian_vault(index_path, vault_path)

    assert result.files_removed >= 1
    assert not stale_managed.exists()
    assert unmanaged.read_text(encoding="utf-8") == "personal note"
    assert changed_marker.read_text(encoding="utf-8") == "<!-- meetily-memory:managed-v2 -->\n"


def test_obsidian_sync_reconciles_renamed_meeting_path(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    vault_path = tmp_path / "vault"
    root = vault_path / "Meetily Memory" / "Meetings"
    scanner = MeetilySQLiteScanner(index_path)
    scanner.scan(meetily_db)
    sync_obsidian_vault(index_path, vault_path)
    old_path = root / "Dobrynya Follow-up.md"

    with sqlite3.connect(meetily_db) as conn:
        conn.execute(
            "UPDATE meetings SET title = ?, updated_at = ? WHERE id = ?",
            ("Dobrynya Retrospective", "2026-07-03T09:30:00Z", "meeting-2"),
        )
    scanner.scan(meetily_db)

    result = sync_obsidian_vault(index_path, vault_path)

    assert result.files_removed >= 1
    assert not old_path.exists()
    assert (root / "Dobrynya Retrospective.md").exists()


@pytest.mark.parametrize("folder", ["../outside", "/absolute-outside"])
def test_obsidian_sync_rejects_folders_outside_vault(
    meetily_db: Path, tmp_path: Path, folder: str
) -> None:
    index_path = tmp_path / "index.sqlite"
    vault_path = tmp_path / "vault"
    outside = tmp_path / "outside" / "Meetings"
    outside.mkdir(parents=True)
    protected = outside / "Protected.md"
    protected.write_text(f"{MANAGED_MARKER}\n", encoding="utf-8")
    MeetilySQLiteScanner(index_path).scan(meetily_db)

    with pytest.raises(ValueError, match="configured vault"):
        sync_obsidian_vault(index_path, vault_path, folder)

    assert protected.exists()


def test_obsidian_sync_rejects_managed_directory_symlink_escape(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    vault_path = tmp_path / "vault"
    root = vault_path / "Meetily Memory"
    outside = tmp_path / "outside"
    root.mkdir(parents=True)
    outside.mkdir()
    (root / "Meetings").symlink_to(outside, target_is_directory=True)
    MeetilySQLiteScanner(index_path).scan(meetily_db)

    with pytest.raises(ValueError, match="managed directory escapes"):
        sync_obsidian_vault(index_path, vault_path)

    assert not list(outside.iterdir())
