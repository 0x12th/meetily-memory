from pathlib import Path

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
