import shlex
import sqlite3
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from meetily_memory.cli import lifecycle_commands
from meetily_memory.cli.app import app
from meetily_memory.diagnostics import SourceDatabaseDiagnostic
from meetily_memory.domain import MeetingRef
from meetily_memory.json_codec import loads_json
from meetily_memory.open_commands import stable_meeting_open_command
from meetily_memory.repositories.index import IndexRepository

TITLE = "[red]literal[/red]"
TAG = "closing [/oops]"
SPEAKER = "[link=https://example.com]literal[/link]"
EXCERPT = "closing [x]"
TIMESTAMP = "[tool.poetry]"


def test_search_prints_source_values_literally_and_keeps_json_unchanged(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    with sqlite3.connect(meetily_db) as conn:
        _ = conn.execute(
            "UPDATE meetings SET title = ? WHERE id = ?",
            (TITLE, "meeting-1"),
        )
        _ = conn.execute(
            """
            UPDATE transcripts
            SET transcript = ?, timestamp = ?, speaker = ?
            WHERE id = ?
            """,
            (EXCERPT, TIMESTAMP, SPEAKER, "transcript-1"),
        )
        conn.commit()

    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()
    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0, scan.output

    meeting = IndexRepository.open_existing(index_path).get_meeting_by_local_id(1)
    assert meeting is not None
    tag = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "tag",
            "add",
            TAG,
            "--source-uuid",
            str(meeting["source_uuid"]),
            "--external-id",
            str(meeting["external_id"]),
        ],
    )
    assert tag.exit_code == 0, tag.output

    text_result = runner.invoke(app, ["--index", str(index_path), "s", "closing"])

    assert text_result.exit_code == 0, text_result.output
    for value in (TITLE, TAG, SPEAKER, EXCERPT, TIMESTAMP):
        assert value in text_result.stdout

    json_result = runner.invoke(
        app,
        ["--index", str(index_path), "s", "closing", "--json"],
    )

    assert json_result.exit_code == 0, json_result.output
    payload = cast("list[dict[str, object]]", loads_json(json_result.stdout))
    search_result = payload[0]
    meeting = cast("dict[str, object]", search_result["meeting"])
    evidence = cast("list[dict[str, object]]", search_result["evidence"])
    excerpt = cast("dict[str, object]", evidence[0]["excerpt"])
    assert meeting["title"] == TITLE
    assert search_result["matched_tags"] == [TAG]
    assert excerpt["speaker"] == SPEAKER
    assert excerpt["text"] == EXCERPT
    assert excerpt["timestamp_label"] == TIMESTAMP


def test_open_command_quotes_canonical_ref_as_one_argument() -> None:
    command = stable_meeting_open_command(MeetingRef("source uuid", "meeting'1"))

    assert shlex.split(command) == ["mm", "open", "source uuid/meeting'1"]


def test_doctor_prints_filesystem_paths_and_diagnostic_errors_literally(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = Path(f"source-{TIMESTAMP}.sqlite")
    diagnostic = f"unsupported source: {TAG}"

    def inspect_source(_path: Path | None) -> SourceDatabaseDiagnostic:
        return SourceDatabaseDiagnostic(
            readable=True,
            schema_valid=False,
            schema_error=diagnostic,
            read_error=None,
        )

    monkeypatch.setattr(lifecycle_commands, "inspect_source_database", inspect_source)

    result = CliRunner().invoke(
        app,
        ["--index", str(tmp_path / "index.sqlite"), "doctor", "--source", str(source_path)],
    )

    assert result.exit_code == 0, result.output
    assert f"source path: {source_path}" in result.stdout
    assert f"source schema error: {diagnostic}" in result.stdout
