import json
from importlib.metadata import version
from pathlib import Path

from typer.testing import CliRunner

from meetily_memory.cli.app import app
from meetily_memory.json_codec import loads_json


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
    assert opened.stdout.strip() == str(meetily_db)


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


def test_cli_semantic_search_requires_explicit_index(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0

    semantic = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "semantic",
            "search",
            "migration risks",
            "--provider",
            "hash",
        ],
    )
    assert semantic.exit_code != 0
    assert "Semantic index is empty. Run: mm semantic index" in semantic.output

    index = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "semantic",
            "index",
            "--provider",
            "hash",
        ],
    )
    assert index.exit_code == 0
    assert "embeddings indexed:" in index.stdout

    semantic = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "semantic",
            "search",
            "migration risks",
            "--provider",
            "hash",
        ],
    )
    assert semantic.exit_code == 0
    assert "Vladimir Follow-up" in semantic.stdout
    assert "semantic distance:" in semantic.stdout
    assert "embedding: hash/local-hash-v1/128d" in semantic.stdout
    assert "open: mm open 2" in semantic.stdout

    semantic_json = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "semantic",
            "search",
            "migration risks",
            "--provider",
            "hash",
            "--json",
        ],
    )
    assert semantic_json.exit_code == 0
    payload = json.loads(semantic_json.stdout)
    assert payload[0]["meeting_external_id"] == "meeting-2"
    assert payload[0]["embedding_provider"] == "hash"
    assert payload[0]["embedding_model"] == "local-hash-v1"
    assert payload[0]["embedding_dimensions"] == 128
    assert isinstance(payload[0]["distance"], float)


def test_cli_open_folder_selects_meeting_folder(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0

    default_path = runner.invoke(
        app,
        ["--index", str(index_path), "open", "1", "--print-path"],
    )
    assert default_path.exit_code == 0
    assert default_path.stdout.strip() == str(meetily_db)

    folder_path = runner.invoke(
        app,
        ["--index", str(index_path), "open", "1", "--folder", "--print-path"],
    )
    assert folder_path.exit_code == 0
    assert folder_path.stdout.strip() == str(tmp_path / "Launch Planning")


def test_cli_scan_can_skip_structured_analysis(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db), "--no-analyze"],
    )
    assert scan.exit_code == 0

    decisions = runner.invoke(app, ["--index", str(index_path), "decisions"])
    assert decisions.exit_code == 0
    expected_message = "No heuristic structured signals found. Run `mm analyze` after scanning."
    assert expected_message in decisions.stdout

    update = runner.invoke(
        app,
        ["--index", str(index_path), "update", "--source", str(meetily_db)],
    )
    assert update.exit_code == 0
    assert "meetings seen: 2" in update.stdout
    assert "meetings analyzed:" in update.stdout


def test_cli_db_status_reports_schema_version(tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    status = runner.invoke(app, ["--index", str(index_path), "db", "status"])

    assert status.exit_code == 0
    assert f"index path: {index_path}" in status.stdout
    assert "schema version: 3" in status.stdout
    assert "current schema version: 3" in status.stdout


def test_cli_semantic_setup_persists_provider_config(meetily_db: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    semantic_env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    setup = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "semantic",
            "setup",
            "--provider",
            "hash",
            "--model",
            "local-hash-v1",
        ],
        env=semantic_env,
    )
    assert setup.exit_code == 0
    assert "semantic provider: hash" in setup.stdout

    config_path = data_dir / "config.json"
    config = loads_json(config_path.read_text())
    assert config["provider"] == "hash"
    assert config["model"] == "local-hash-v1"
    assert "ollama_url" not in config

    shown = runner.invoke(
        app,
        ["--index", str(index_path), "semantic", "setup", "--show"],
        env=semantic_env,
    )
    assert shown.exit_code == 0
    assert "semantic provider: hash" in shown.stdout
    assert "model: local-hash-v1" in shown.stdout

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0

    semantic_index = runner.invoke(
        app,
        ["--index", str(index_path), "semantic", "index"],
        env=semantic_env,
    )
    assert semantic_index.exit_code == 0
    assert "embeddings indexed:" in semantic_index.stdout

    semantic_alias = runner.invoke(
        app,
        ["--index", str(index_path), "sem", "migration risks"],
        env=semantic_env,
    )
    assert semantic_alias.exit_code == 0
    assert "Vladimir Follow-up" in semantic_alias.stdout
    assert "embedding: hash/local-hash-v1/128d" in semantic_alias.stdout

    switch = runner.invoke(
        app,
        ["--index", str(index_path), "semantic", "setup", "--provider", "ollama"],
        env=semantic_env,
    )
    assert switch.exit_code == 0
    assert "semantic provider: ollama" in switch.stdout
    assert "model: nomic-embed-text" in switch.stdout

    config = loads_json(config_path.read_text())
    assert config["provider"] == "ollama"
    assert config["model"] == "nomic-embed-text"
    assert config["ollama_url"] == "http://localhost:11434"


def test_cli_semantic_setup_is_real_subcommand() -> None:
    runner = CliRunner()

    semantic_help = runner.invoke(app, ["semantic", "--help"])
    assert semantic_help.exit_code == 0
    assert "Commands:" in semantic_help.stdout
    assert "setup" in semantic_help.stdout
    assert "Search query, or `setup`" not in semantic_help.stdout

    setup_help = runner.invoke(app, ["semantic", "setup", "--help"])
    assert setup_help.exit_code == 0
    assert "Usage: root semantic setup" in setup_help.stdout
    assert "--provider" in setup_help.stdout
    assert "--show" in setup_help.stdout


def test_cli_mcp_serve_is_real_subcommand() -> None:
    runner = CliRunner()

    mcp_help = runner.invoke(app, ["mcp", "--help"])
    assert mcp_help.exit_code == 0
    assert "Commands:" in mcp_help.stdout
    assert "serve" in mcp_help.stdout

    serve_help = runner.invoke(app, ["mcp", "serve", "--help"])
    assert serve_help.exit_code == 0
    assert "Usage: root mcp serve" in serve_help.stdout
    assert "--transport" in serve_help.stdout


def test_cli_export_commands_are_real_subcommands() -> None:
    runner = CliRunner()

    export_help = runner.invoke(app, ["export", "--help"])
    assert export_help.exit_code == 0
    assert "Commands:" in export_help.stdout
    assert "obsidian" in export_help.stdout
    assert "gbrain" in export_help.stdout
    assert "markdown" in export_help.stdout
    assert "task-draft" in export_help.stdout

    obsidian_help = runner.invoke(app, ["export", "obsidian", "--help"])
    assert obsidian_help.exit_code == 0
    assert "Usage: root export obsidian" in obsidian_help.stdout
    assert "--output" in obsidian_help.stdout


def test_cli_exports_core_backed_integration_files(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    export_dir = tmp_path / "exports"
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0

    obsidian = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "export",
            "obsidian",
            "migration",
            "--output",
            str(export_dir / "obsidian"),
        ],
    )
    assert obsidian.exit_code == 0
    obsidian_note = export_dir / "obsidian" / "migration.md"
    assert obsidian_note.exists()
    assert "[[Vladimir Follow-up]]" in obsidian_note.read_text(encoding="utf-8")

    gbrain = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "export",
            "gbrain",
            "migration",
            "--output",
            str(export_dir / "gbrain.jsonl"),
            "--json",
        ],
    )
    assert gbrain.exit_code == 0
    gbrain_payload = json.loads(gbrain.stdout)
    assert gbrain_payload["format"] == "gbrain-jsonl"
    assert (export_dir / "gbrain.jsonl").exists()

    task_draft = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "export",
            "task-draft",
            "migration risks",
            "--output",
            str(export_dir / "task.md"),
        ],
    )
    assert task_draft.exit_code == 0
    task_text = (export_dir / "task.md").read_text(encoding="utf-8")
    assert "Tracker: generic" in task_text
    assert "Draft only; no tracker write-back was performed." in task_text


def test_cli_local_memory_commands_aggregate_across_meetings(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0

    summary = runner.invoke(app, ["--index", str(index_path), "summary"])
    assert summary.exit_code == 0
    assert "Local memory summary" in summary.stdout
    assert "meetings: 2" in summary.stdout
    assert "latest meeting: #2 Vladimir Follow-up" in summary.stdout
    assert "action items:" in summary.stdout

    timeline = runner.invoke(app, ["--index", str(index_path), "timeline", "migration"])
    assert timeline.exit_code == 0
    assert "Vladimir Follow-up" in timeline.stdout
    assert "Vladimir agreed to send migration risks by Friday." in timeline.stdout
    assert "Source: meeting-2 / transcript-2" in timeline.stdout

    project = runner.invoke(app, ["--index", str(index_path), "project", "migration"])
    assert project.exit_code == 0
    assert "Project memory: migration" in project.stdout
    assert "Meetings" in project.stdout
    assert "Vladimir Follow-up" in project.stdout
    assert "Structured signals" in project.stdout

    person = runner.invoke(app, ["--index", str(index_path), "person", "Vladimir"])
    assert person.exit_code == 0
    assert "Person memory: Vladimir" in person.stdout
    assert "Latest meetings" in person.stdout
    assert "Vladimir Follow-up" in person.stdout
    assert "Action items" in person.stdout
    assert "Vladimir agreed to send migration risks by Friday." in person.stdout


def test_cli_v5_topic_graph_alias_and_task_status_memory(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0

    topic = runner.invoke(app, ["--index", str(index_path), "topic", "migration"])
    assert topic.exit_code == 0
    assert "Topic memory: migration" in topic.stdout
    assert "Unresolved tasks" in topic.stdout
    assert "Vladimir agreed to send migration risks by Friday." in topic.stdout
    assert "Source: meeting-2 / transcript-2" in topic.stdout

    alias = runner.invoke(
        app,
        ["--index", str(index_path), "topic", "migration", "--alias", "миграция"],
    )
    assert alias.exit_code == 0
    assert "alias added: миграция -> migration" in alias.stdout

    alias_lookup = runner.invoke(app, ["--index", str(index_path), "topic", "миграция"])
    assert alias_lookup.exit_code == 0
    assert "Topic memory: migration" in alias_lookup.stdout
    assert "alias: миграция" in alias_lookup.stdout

    graph = runner.invoke(app, ["--index", str(index_path), "graph", "migration", "--json"])
    assert graph.exit_code == 0
    graph_payload = json.loads(graph.stdout)
    assert graph_payload["topic"]["title"] == "migration"
    assert {node["type"] for node in graph_payload["nodes"]} >= {"Topic", "Meeting", "Task"}
    assert {edge["relation"] for edge in graph_payload["edges"]} >= {"contains", "belongs_to"}
    assert all(edge.get("source_meeting_id") for edge in graph_payload["edges"])

    tasks_payload = json.loads(
        runner.invoke(app, ["--index", str(index_path), "tasks", "--json"]).stdout
    )
    task_id = tasks_payload[0]["id"]
    done = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "task-status",
            str(task_id),
            "done",
            "--note",
            "confirmed manually",
        ],
    )
    assert done.exit_code == 0
    assert "task status: done" in done.stdout

    open_tasks = runner.invoke(app, ["--index", str(index_path), "tasks", "--status", "open"])
    assert open_tasks.exit_code == 0
    assert "Vladimir agreed to send migration risks by Friday." not in open_tasks.stdout

    all_tasks = runner.invoke(app, ["--index", str(index_path), "tasks", "--status", "all"])
    assert all_tasks.exit_code == 0
    assert "status: done" in all_tasks.stdout
    assert "confirmed manually" in all_tasks.stdout


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
