from pathlib import Path
from typing import Any

import pytest

from meetily_memory import spotlight_export
from meetily_memory.db.repository import IndexRepository
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner


def test_spotlight_export_keeps_existing_files_when_render_fails(
    meetily_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index_path = tmp_path / "index.sqlite"
    output_dir = tmp_path / "spotlight"
    output_dir.mkdir()
    stale_file = output_dir / "meetily-memory-stale.md"
    stale_file.write_text("old export\n", encoding="utf-8")
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    repo = IndexRepository(index_path)

    def fail_render(
        _meeting: dict[str, Any],
        _chunks: list[dict[str, Any]],
        _entities: list[dict[str, Any]],
        *,
        include_transcript: bool,
    ) -> str:
        del include_transcript
        message = "render failed"
        raise RuntimeError(message)

    monkeypatch.setattr(spotlight_export, "render_meeting_markdown", fail_render)

    with pytest.raises(RuntimeError, match="render failed"):
        spotlight_export.export_spotlight_markdown(repo, output_dir)

    assert stale_file.read_text(encoding="utf-8") == "old export\n"
