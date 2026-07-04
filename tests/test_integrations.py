import json
from pathlib import Path

from meetily_memory.integrations import (
    export_gbrain_bundle,
    export_markdown_bundle,
    export_obsidian_topic,
    export_task_tracker_draft,
)
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner


def test_obsidian_topic_export_contains_links_summary_and_evidence(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    output_dir = tmp_path / "vault"
    MeetilySQLiteScanner(index_path).scan(meetily_db)

    result = export_obsidian_topic(index_path, "migration", output_dir)

    topic_note = result.files[0]
    text = topic_note.read_text(encoding="utf-8")
    assert topic_note.name == "migration.md"
    assert "[[Vladimir Follow-up]]" in text
    assert "## Unresolved Tasks" in text
    assert "Vladimir agreed to send migration risks by Friday." in text
    assert "Source: meeting-2 / transcript-2" in text


def test_gbrain_and_markdown_exports_are_core_backed(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    output_dir = tmp_path / "exports"
    MeetilySQLiteScanner(index_path).scan(meetily_db)

    gbrain = export_gbrain_bundle(index_path, "migration", output_dir / "gbrain.jsonl")
    lines = [json.loads(line) for line in gbrain.path.read_text(encoding="utf-8").splitlines()]
    assert {line["kind"] for line in lines} >= {"topic", "context", "graph"}
    assert all(line["contract_version"] == "meetily-memory.core.v1" for line in lines)
    assert any("meeting-2" in json.dumps(line) for line in lines)

    markdown = export_markdown_bundle(index_path, "migration", output_dir / "bundle.md")
    text = markdown.path.read_text(encoding="utf-8")
    assert "# Meetily Memory: migration" in text
    assert "## Topic Summary" in text
    assert "Source: meeting-2 / transcript-2" in text


def test_task_tracker_draft_is_write_back_free(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    output_path = tmp_path / "draft.md"
    MeetilySQLiteScanner(index_path).scan(meetily_db)

    result = export_task_tracker_draft(
        index_path, task_query="migration risks", output_path=output_path
    )

    text = result.path.read_text(encoding="utf-8")
    assert "# Task Draft" in text
    assert "Tracker: generic" in text
    assert "Vladimir agreed to send migration risks by Friday." in text
    assert "Source: meeting-2 / transcript-2" in text
    assert "Status: open" in text
