import json
from importlib.metadata import version
from pathlib import Path

from typer.testing import CliRunner

from meetily_memory.cli.app import app


def test_cli_help_uses_plain_click_format() -> None:
    runner = CliRunner()

    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "Options:" in help_result.stdout
    assert "Commands:" in help_result.stdout
    assert "--version" in help_result.stdout
    assert "--install-completion" not in help_result.stdout
    assert "--show-completion" not in help_result.stdout
    assert "╭" not in help_result.stdout

    spotlight_help = runner.invoke(app, ["spotlight", "--help"])
    assert spotlight_help.exit_code == 0
    assert "Options:" in spotlight_help.stdout
    assert "Commands:" in spotlight_help.stdout
    assert "╭" not in spotlight_help.stdout


def test_cli_version_outputs_package_version() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"meetily-memory {version('meetily-memory')}\n"


def scan_twice(runner: CliRunner, index_path: Path, meetily_db: Path) -> None:
    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0
    assert "meetings seen: 2" in scan.stdout

    force_scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db), "--force"],
    )
    assert force_scan.exit_code == 0
    assert "meetings updated: 2" in force_scan.stdout


def test_cli_v1_scan_search_list_last_person_and_doctor(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    scan_twice(runner, index_path, meetily_db)

    search = runner.invoke(app, ["--index", str(index_path), "s", "pricing decision"])
    assert search.exit_code == 0
    assert "Launch Planning" in search.stdout
    assert "pricing decision" in search.stdout
    assert "chunk #" in search.stdout
    assert "open: mm open 1" in search.stdout

    context = runner.invoke(app, ["--index", str(index_path), "c", "Who owns migration risks?"])
    assert context.exit_code == 0
    assert "# Question" in context.stdout
    assert "# Relevant meetings" in context.stdout
    assert "## Meeting: Vladimir Follow-up" in context.stdout
    assert "Date: 2026-07-02T09:30:00Z" in context.stdout
    assert "Source: meeting-2 / transcript-2" in context.stdout
    assert "### Relevant excerpt" in context.stdout
    assert "Vladimir agreed to send migration risks by Friday." in context.stdout
    assert context.stdout.count("Who owns migration risks?") == 2

    analyze = runner.invoke(app, ["--index", str(index_path), "analyze", "meeting-2"])
    assert analyze.exit_code == 0
    assert "meetings analyzed: 1" in analyze.stdout
    assert "action items:" in analyze.stdout
    assert "risks:" in analyze.stdout

    listing = runner.invoke(app, ["--index", str(index_path), "ls"])
    assert listing.exit_code == 0
    assert "Vladimir Follow-up" in listing.stdout
    assert "Launch Planning" in listing.stdout
    assert "mm open 1" in listing.stdout

    last_for_person = runner.invoke(
        app, ["--index", str(index_path), "last", "--person", "Vladimir"]
    )
    assert last_for_person.exit_code == 0
    assert "Vladimir Follow-up" in last_for_person.stdout

    person = runner.invoke(app, ["--index", str(index_path), "p", "Vladimir"])
    assert person.exit_code == 0
    assert "Vladimir Follow-up" in person.stdout
    assert "mm open 2" in person.stdout

    cyrillic_person = runner.invoke(app, ["--index", str(index_path), "p", "Никита"])
    assert cyrillic_person.exit_code == 0
    assert "Vladimir Follow-up" in cyrillic_person.stdout
    assert "mm open 2" in cyrillic_person.stdout

    doctor = runner.invoke(
        app,
        ["--index", str(index_path), "doctor", "--source", str(meetily_db)],
    )
    assert doctor.exit_code == 0
    assert "source readable: yes" in doctor.stdout
    assert "fts5: yes" in doctor.stdout
    assert "decisions:" in doctor.stdout
    assert "action items:" in doctor.stdout

    opened = runner.invoke(app, ["--index", str(index_path), "open", "meeting-2", "--print-path"])
    assert opened.exit_code == 0
    assert "Vladimir Follow-up" in opened.stdout


def test_cli_lists_structured_entities_with_source_evidence(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0

    decisions = runner.invoke(app, ["--index", str(index_path), "decisions"])
    assert decisions.exit_code == 0
    assert "Launch Planning" in decisions.stdout
    assert "meeting-1" in decisions.stdout
    assert "transcript-1" in decisions.stdout
    assert "confidence" in decisions.stdout
    assert "pricing decision" in decisions.stdout

    tasks = runner.invoke(app, ["--index", str(index_path), "tasks"])
    assert tasks.exit_code == 0
    assert "Vladimir Follow-up" in tasks.stdout
    assert "meeting-2" in tasks.stdout
    assert "transcript-2" in tasks.stdout
    assert "Vladimir agreed to send migration risks by Friday." in tasks.stdout

    risks_json = runner.invoke(app, ["--index", str(index_path), "risks", "--json"])
    assert risks_json.exit_code == 0
    risks_payload = json.loads(risks_json.stdout)
    assert risks_payload[0]["kind"] == "risks"
    assert risks_payload[0]["meeting_external_id"] == "meeting-2"

    questions = runner.invoke(app, ["--index", str(index_path), "questions"])
    assert questions.exit_code == 0
    assert "Open question: who owns partner review?" in questions.stdout


def test_cli_exports_and_cleans_spotlight_markdown(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    output_dir = tmp_path / "Spotlight"
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0

    analyze = runner.invoke(app, ["--index", str(index_path), "analyze"])
    assert analyze.exit_code == 0

    export = runner.invoke(
        app,
        ["--index", str(index_path), "spotlight", "export", "--output", str(output_dir)],
    )
    assert export.exit_code == 0
    assert "meetings exported: 2" in export.stdout

    exported_files = sorted(output_dir.glob("meetily-memory-*.md"))
    assert len(exported_files) == 2
    exported_text = "\n".join(path.read_text() for path in exported_files)
    assert "# Vladimir Follow-up" in exported_text
    assert "Open: mm open 2" in exported_text
    assert "Vladimir agreed to send migration risks by Friday." in exported_text
    assert "## Action Items" in exported_text
    assert "## Transcript" in exported_text

    user_file = output_dir / "notes.md"
    user_file.write_text("keep me")

    clean = runner.invoke(
        app,
        ["--index", str(index_path), "spotlight", "clean", "--output", str(output_dir)],
    )
    assert clean.exit_code == 0
    assert "files removed: 2" in clean.stdout
    assert not list(output_dir.glob("meetily-memory-*.md"))
    assert user_file.read_text() == "keep me"
