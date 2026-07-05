import json
import sqlite3
from importlib.metadata import version
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from meetily_memory.cli.app import app
from meetily_memory.cli.common import open_path
from meetily_memory.json_codec import loads_json


def test_cli_help_uses_plain_click_format() -> None:
    runner = CliRunner()

    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "Options:" in help_result.stdout
    assert "Commands:" in help_result.stdout
    assert "--version" in help_result.stdout
    assert "Everyday:" in help_result.stdout
    assert "Advanced:" in help_result.stdout
    assert "--install-completion" not in help_result.stdout
    assert "--show-completion" not in help_result.stdout
    assert "╭" not in help_result.stdout

    assert "init" in help_result.stdout
    assert "status" in help_result.stdout
    assert "llm" in help_result.stdout
    assert "obsidian" in help_result.stdout
    assert "autosync" in help_result.stdout
    assert "export" not in help_result.stdout
    assert "spotlight" not in help_result.stdout
    assert "graph" not in help_result.stdout
    assert "project" not in help_result.stdout
    assert "person" not in help_result.stdout

    open_help = runner.invoke(app, ["open", "--help"])
    assert open_help.exit_code == 0
    assert "--source" in open_help.stdout
    assert "--folder" not in open_help.stdout

    obsidian_init_help = runner.invoke(app, ["obsidian", "init", "--help"])
    assert obsidian_init_help.exit_code == 0
    assert "--sync-after-refresh" in obsidian_init_help.stdout
    assert "--sync-after-update" not in obsidian_init_help.stdout


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
    assert opened.stdout.strip() == str(tmp_path / "Vladimir Follow-up")


def test_cli_topic_shows_structured_memory_with_source_evidence(
    meetily_db: Path, tmp_path: Path
) -> None:
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
    assert "Vladimir Follow-up" in topic.stdout
    assert "Source: meeting-2 / transcript-2" in topic.stdout
    assert "Vladimir agreed to send migration risks by Friday." in topic.stdout


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


def test_cli_open_selects_meeting_folder_by_default(meetily_db: Path, tmp_path: Path) -> None:
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
    assert default_path.stdout.strip() == str(tmp_path / "Launch Planning")

    source_path = runner.invoke(
        app,
        ["--index", str(index_path), "open", "1", "--source", "--print-path"],
    )
    assert source_path.exit_code == 0
    assert source_path.stdout.strip() == str(meetily_db)


def test_cli_scan_can_skip_structured_analysis(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db), "--no-analyze"],
    )
    assert scan.exit_code == 0

    topic = runner.invoke(app, ["--index", str(index_path), "topic", "migration"])
    assert topic.exit_code == 0
    assert "No structured signals." in topic.stdout

    refresh = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
    )
    assert refresh.exit_code == 0
    assert "meetings seen: 2" in refresh.stdout
    assert "meetings analyzed:" in refresh.stdout


def test_cli_refresh_runs_configured_semantic_without_autosync(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    data_dir = tmp_path / "data"
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()

    init = runner.invoke(
        app,
        ["--index", str(index_path), "init", "--source", str(meetily_db), "--no-autosync"],
        env=env,
    )
    assert init.exit_code == 0

    semantic_init = runner.invoke(
        app,
        ["semantic", "init", "--provider", "hash", "--model", "local-hash-v1"],
        env=env,
    )
    assert semantic_init.exit_code == 0

    refresh = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db), "--json"],
        env=env,
    )

    assert refresh.exit_code == 0
    payload = loads_json(refresh.stdout)
    assert payload["embeddings_indexed"] > 0


def test_cli_doctor_reports_meetily_schema_status(tmp_path: Path) -> None:
    source_path = tmp_path / "meeting_minutes.sqlite"
    with sqlite3.connect(source_path) as conn:
        conn.execute("CREATE TABLE meetings (id TEXT PRIMARY KEY)")
        conn.commit()
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    doctor = runner.invoke(
        app,
        ["--index", str(index_path), "doctor", "--source", str(source_path), "--json"],
    )

    assert doctor.exit_code == 0
    payload = loads_json(doctor.stdout)
    assert payload["source_readable"] is True
    assert payload["source_schema_valid"] is False
    assert "Meetily DB schema is unsupported" in payload["source_schema_error"]
    assert "meetings" in payload["source_schema_error"]


def test_open_path_reports_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(typer.BadParameter, match="Path does not exist"):
        open_path(missing)


def test_cli_update_upgrades_homebrew_package(tmp_path: Path) -> None:
    runner = CliRunner()
    brew = tmp_path / "brew"
    calls = tmp_path / "brew-calls.txt"
    brew.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {calls}\n", encoding="utf-8")
    brew.chmod(0o755)

    update = runner.invoke(app, ["update"], env={"PATH": str(tmp_path)})

    assert update.exit_code == 0
    assert calls.read_text(encoding="utf-8") == "upgrade meetily-memory\n"
    assert "updated: meetily-memory" in update.stdout


def test_cli_update_reports_homebrew_failure(tmp_path: Path) -> None:
    runner = CliRunner()

    update = runner.invoke(app, ["update"], env={"PATH": str(tmp_path)})

    assert update.exit_code != 0
    assert "Homebrew was not found" in update.output


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
            "init",
            "--provider",
            "hash",
            "--model",
            "local-hash-v1",
        ],
        env=semantic_env,
    )
    assert setup.exit_code == 0
    assert "semantic provider: hash" in setup.stdout

    config_path = data_dir / "settings.json"
    config = loads_json(config_path.read_text())
    assert config["semantic"]["provider"] == "hash"
    assert config["semantic"]["model"] == "local-hash-v1"
    assert config["semantic"]["ollama_url"] is None

    shown = runner.invoke(
        app,
        ["--index", str(index_path), "semantic", "init", "--show"],
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
        ["--index", str(index_path), "semantic", "init", "--provider", "ollama"],
        env=semantic_env,
    )
    assert switch.exit_code == 0
    assert "semantic provider: ollama" in switch.stdout
    assert "model: nomic-embed-text" in switch.stdout

    config = loads_json(config_path.read_text())
    assert config["semantic"]["provider"] == "ollama"
    assert config["semantic"]["model"] == "nomic-embed-text"
    assert config["semantic"]["ollama_url"] == "http://localhost:11434"


def test_cli_semantic_init_is_real_subcommand() -> None:
    runner = CliRunner()

    semantic_help = runner.invoke(app, ["semantic", "--help"])
    assert semantic_help.exit_code == 0
    assert "Commands:" in semantic_help.stdout
    assert "init" in semantic_help.stdout
    assert "setup" not in semantic_help.stdout

    init_help = runner.invoke(app, ["semantic", "init", "--help"])
    assert init_help.exit_code == 0
    assert "Usage: root semantic init" in init_help.stdout
    assert "--provider" in init_help.stdout
    assert "--show" in init_help.stdout


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


def test_cli_removed_public_commands_are_not_available() -> None:
    runner = CliRunner()

    removed_commands = (
        "export",
        "spotlight",
        "graph",
        "project",
        "person",
        "ls",
        "last",
        "p",
        "summary",
        "timeline",
        "decisions",
        "tasks",
        "risks",
        "questions",
        "task-status",
    )
    for command in removed_commands:
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code != 0


def test_cli_init_status_ask_and_obsidian_sync(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    data_dir = tmp_path / "data"
    vault_dir = tmp_path / "vault"
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()

    init = runner.invoke(
        app,
        ["--index", str(index_path), "init", "--source", str(meetily_db), "--no-autosync"],
        env=env,
    )
    assert init.exit_code == 0
    assert "initialized: yes" in init.stdout
    assert "meetings seen: 2" in init.stdout

    status = runner.invoke(app, ["--index", str(index_path), "status"], env=env)
    assert status.exit_code == 0
    assert f"index path: {index_path}" in status.stdout
    assert f"source path: {meetily_db}" in status.stdout
    assert "autosync: disabled" in status.stdout
    assert "llm: not configured" in status.stdout
    assert "obsidian: not configured" in status.stdout

    llm = runner.invoke(app, ["llm", "init", "--provider", "manual"], env=env)
    assert llm.exit_code == 0
    assert "llm provider: manual" in llm.stdout

    ask = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "ask",
            "--topic",
            "migration",
            "what is still open?",
        ],
        env=env,
    )
    assert ask.exit_code == 0
    assert "# Manual LLM Prompt" in ask.stdout
    assert "# Topic Memory" in ask.stdout
    assert "what is still open?" in ask.stdout
    assert "Source: meeting-2 / transcript-2" in ask.stdout

    scoped_ask = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "ask",
            "--meeting",
            "meeting-2",
            "pricing decision",
        ],
        env=env,
    )
    assert scoped_ask.exit_code == 0
    assert "Meeting: meeting-2" in scoped_ask.stdout
    assert "No relevant excerpts found." in scoped_ask.stdout
    assert "Launch Planning" not in scoped_ask.stdout

    obsidian_init = runner.invoke(
        app,
        [
            "obsidian",
            "init",
            "--vault",
            str(vault_dir),
            "--folder",
            "Meetily Memory",
            "--sync-after-refresh",
        ],
        env=env,
    )
    assert obsidian_init.exit_code == 0
    assert "obsidian vault:" in obsidian_init.stdout

    obsidian_sync = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "obsidian",
            "sync",
        ],
        env=env,
    )
    assert obsidian_sync.exit_code == 0
    assert "obsidian files synced:" in obsidian_sync.stdout
    assert (vault_dir / "Meetily Memory" / "Topics" / "migration.md").exists()
    assert (vault_dir / "Meetily Memory" / "Meetings" / "Vladimir Follow-up.md").exists()
    assert "<!-- meetily-memory:managed -->" in (
        vault_dir / "Meetily Memory" / "Topics" / "migration.md"
    ).read_text(encoding="utf-8")


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
