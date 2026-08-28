import json
import re
import sqlite3
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import typer
from typer.testing import CliRunner

from meetily_memory.cli.app import app
from meetily_memory.cli.common import open_path
from meetily_memory.cli.search_commands import parse_search_filters
from meetily_memory.config.settings import (
    ObsidianSettings,
    load_app_settings,
)
from meetily_memory.db.migrations import CURRENT_SCHEMA_VERSION
from meetily_memory.db.repository import IndexRepository
from meetily_memory.json_codec import loads_json
from meetily_memory.tagging import TagRepository


def test_parse_search_filters_builds_since_window_from_injected_clock() -> None:
    now = datetime(2026, 8, 25, 12, 30, tzinfo=UTC)

    filters = parse_search_filters(since="7d", now=lambda: now)

    assert filters.from_utc == datetime(2026, 8, 18, 12, 30, tzinfo=UTC)
    assert filters.to_utc == now


def test_parse_search_filters_converts_inclusive_local_dates_to_utc() -> None:
    filters = parse_search_filters(
        from_date="2024-02-29",
        to_date="2024-02-29",
        local_timezone=ZoneInfo("Europe/Moscow"),
    )

    assert filters.from_utc == datetime(2024, 2, 28, 21, tzinfo=UTC)
    assert filters.to_utc == datetime(2024, 2, 29, 21, tzinfo=UTC)


@pytest.mark.parametrize(
    ("since", "from_date", "to_date", "message"),
    [
        ("0d", None, None, "positive number of days"),
        ("1w", None, None, "positive number of days"),
        (None, "2026-02-29", None, "Invalid --from date"),
        (None, None, "tomorrow", "Invalid --to date"),
        ("7d", "2026-08-01", None, "mutually exclusive"),
        (
            None,
            "2026-08-23",
            "2026-08-17",
            "must not be earlier",
        ),
    ],
)
def test_parse_search_filters_rejects_invalid_ranges(
    since: str | None,
    from_date: str | None,
    to_date: str | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_search_filters(since=since, from_date=from_date, to_date=to_date)


def test_cli_help_uses_plain_click_format() -> None:
    runner = CliRunner()

    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "Options:" in help_result.stdout
    assert "Commands:" in help_result.stdout
    assert "--version" in help_result.stdout
    assert "Local search over Meetily meeting history." in help_result.stdout
    assert "Main workflow:" in help_result.stdout
    assert "mm s QUERY" in help_result.stdout
    assert "mm open ID" in help_result.stdout
    assert "\n  ask" not in help_result.stdout
    assert "ask answers" not in help_result.stdout
    assert "--install-completion" not in help_result.stdout
    assert "--show-completion" not in help_result.stdout
    assert "╭" not in help_result.stdout

    for command in (
        "init",
        "status",
        "refresh",
        "update",
        "doctor",
        "s",
        "open",
        "tag",
        "autosync",
    ):
        assert re.search(rf"\n  {re.escape(command)}(?:\s{{2,}}|\n)", help_result.stdout)
    for command in (
        "scan",
        "c",
        "t",
        "topic",
        "obsidian",
        "config",
        "db",
        "mcp",
    ):
        assert not re.search(rf"\n  {re.escape(command)}(?:\s{{2,}}|\n)", help_result.stdout)

    open_help = runner.invoke(app, ["open", "--help"])
    assert open_help.exit_code == 0
    assert "--source" in open_help.stdout
    assert "--folder" not in open_help.stdout

    search_help = runner.invoke(app, ["s", "--help"])
    assert search_help.exit_code == 0
    assert "--since" in search_help.stdout
    assert "positive number of days" in search_help.stdout
    assert "--from" in search_help.stdout
    assert "inclusive local date" in search_help.stdout
    assert "--to" in search_help.stdout
    assert "entire local date" in search_help.stdout

    obsidian_init_help = runner.invoke(app, ["obsidian", "init", "--help"])
    assert obsidian_init_help.exit_code == 0
    assert "--sync-after-refresh" in obsidian_init_help.stdout
    assert "--sync-after-update" not in obsidian_init_help.stdout


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (("scan",), "--source"),
        (("c",), "QUESTION"),
        (("t",), "QUERY"),
        (("obsidian",), "sync"),
        (("config",), "source"),
        (("db",), "status"),
        (("mcp",), "serve"),
    ],
)
def test_hidden_commands_remain_directly_accessible(
    command: tuple[str, ...],
    expected: str,
) -> None:
    result = CliRunner().invoke(app, [*command, "--help"])

    assert result.exit_code == 0
    assert expected in result.stdout


def test_cli_version_outputs_package_version() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"meetily-memory {version('meetily-memory')}\n"


def test_cli_config_language_persists_ui_language(tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    data_dir = tmp_path / "data"
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()

    language = runner.invoke(
        app,
        ["--index", str(index_path), "config", "language", "ru"],
        env=env,
    )

    assert language.exit_code == 0
    assert "ui language: ru" in language.stdout
    config = loads_json((data_dir / "settings.json").read_text())
    assert config["ui_language"] == "ru"

    status = runner.invoke(app, ["--index", str(index_path), "status"], env=env)
    assert status.exit_code == 0
    assert "language: ru (configured)" in status.stdout

    auto = runner.invoke(
        app,
        ["--index", str(index_path), "config", "language", "auto"],
        env=env,
    )
    assert auto.exit_code == 0
    assert "ui language: auto" in auto.stdout
    config = loads_json((data_dir / "settings.json").read_text())
    assert config["ui_language"] is None


def test_cli_autosync_status_reports_missing_scheduler(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "settings.json").write_text(
        json.dumps({"autosync_enabled": True}) + "\n",
        encoding="utf-8",
    )
    runner = CliRunner()

    status = runner.invoke(
        app,
        ["autosync", "status", "--json"],
        env={"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert status.exit_code == 0
    payload = loads_json(status.stdout)
    assert payload["configured"] is True
    assert payload["installed"] is False
    assert payload["active"] is False
    assert payload["enabled"] is False

    main_status = runner.invoke(
        app,
        ["--index", str(tmp_path / "index.sqlite"), "status"],
        env={"HOME": str(tmp_path / "home"), "MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )
    assert main_status.exit_code == 0
    assert "autosync: misconfigured" in main_status.stdout


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
    assert "Evidence role: neighboring context" not in context.stdout

    expanded_context = runner.invoke(
        app,
        ["--index", str(index_path), "c", "Who owns migration risks?", "--context", "2"],
    )
    assert expanded_context.exit_code == 0
    assert "Evidence role: neighboring context" in expanded_context.stdout
    assert "# Relevant meetings" in context.stdout
    assert "## Meeting: Dobrynya Follow-up" in context.stdout
    assert "Date: 2026-07-02T09:30:00Z" in context.stdout
    assert "Source: meeting-2 / transcript-2" in context.stdout
    assert "### Relevant excerpt" in context.stdout
    assert "Dobrynya agreed to send migration risks by Friday." in context.stdout
    assert "Evidence role: neighboring context" in expanded_context.stdout
    assert context.stdout.count("Who owns migration risks?") == 2

    exact_context = runner.invoke(
        app,
        ["--index", str(index_path), "c", "Who owns migration risks?", "--context", "0"],
    )
    assert exact_context.exit_code == 0
    assert "Evidence role: neighboring context" not in exact_context.stdout

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
    assert opened.stdout.strip() == str(tmp_path / "Dobrynya Follow-up")


def test_cli_search_can_include_neighboring_context(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0

    search = runner.invoke(
        app,
        ["--index", str(index_path), "s", "pricing decision", "--context", "1"],
    )

    assert search.exit_code == 0
    assert "Launch Planning" in search.stdout
    assert "Alice confirmed the launch checklist and pricing decision." in search.stdout
    assert "Open question: who owns partner review?" in search.stdout
    assert "context" in search.stdout
    assert "open: mm open 1" in search.stdout


def test_cli_search_filters_text_and_json_results_by_inclusive_dates(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()
    scan_twice(runner, index_path, meetily_db)

    text = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "s",
            "migration risks",
            "--from",
            "2026-07-02",
            "--to",
            "2026-07-02",
        ],
    )
    json_result = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "s",
            "migration risks",
            "--from",
            "2026-07-02",
            "--to",
            "2026-07-02",
            "--json",
        ],
    )
    excluded = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "s",
            "migration risks",
            "--to",
            "2026-07-01",
        ],
    )

    assert text.exit_code == 0
    assert json_result.exit_code == 0
    assert excluded.exit_code == 0
    assert "Dobrynya Follow-up" in text.stdout
    assert [result["meeting"]["title"] for result in loads_json(json_result.stdout)] == [
        "Dobrynya Follow-up"
    ]
    assert excluded.stdout == ""


def test_cli_search_rejects_invalid_filters_before_opening_database(tmp_path: Path) -> None:
    missing_index = tmp_path / "missing" / "index.sqlite"

    result = CliRunner().invoke(
        app,
        ["--index", str(missing_index), "s", "query", "--since", "0d"],
    )

    assert result.exit_code == 2
    assert "positive number of days" in result.output
    assert not missing_index.exists()


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

    topic = runner.invoke(app, ["--index", str(index_path), "t", "migration"])
    assert topic.exit_code == 0
    assert "What we know: migration" in topic.stdout
    assert "Dobrynya Follow-up" in topic.stdout
    assert "Source: meeting-2 / transcript-2" in topic.stdout
    assert "Dobrynya agreed to send migration risks by Friday." in topic.stdout


def test_cli_topic_uses_configured_ui_language(meetily_db: Path, tmp_path: Path) -> None:
    with sqlite3.connect(meetily_db) as conn:
        conn.execute(
            """
            INSERT INTO summary_processes (
                meeting_id, status, created_at, updated_at, result, metadata
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "meeting-2",
                "completed",
                "2026-07-02T09:33:00Z",
                "2026-07-02T09:34:00Z",
                '{"markdown":"Добрыня подтвердил план миграции."}',
                '{"language":"ru"}',
            ),
        )
        conn.commit()
    index_path = tmp_path / "index.sqlite"
    data_dir = tmp_path / "data"
    config_path = data_dir / "settings.json"
    data_dir.mkdir()
    config_path.write_text('{"ui_language":"ru"}\n', encoding="utf-8")
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0

    topic = runner.invoke(app, ["--index", str(index_path), "t", "миграция"], env=env)

    assert topic.exit_code == 0
    assert "Что известно: миграция" in topic.stdout
    assert "Связанные встречи" in topic.stdout
    assert "Dobrynya Follow-up" in topic.stdout
    assert "Добрыня подтвердил план миграции." in topic.stdout


def test_cli_topic_uses_russian_output_and_cautious_sections(
    meetily_db: Path, tmp_path: Path
) -> None:
    with sqlite3.connect(meetily_db) as conn:
        insert_kafka_meeting(conn, tmp_path)
        conn.commit()
    index_path = tmp_path / "index.sqlite"
    data_dir = tmp_path / "data"
    config_path = data_dir / "settings.json"
    data_dir.mkdir()
    config_path.write_text('{"ui_language":"ru"}\n', encoding="utf-8")
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db), "--no-analyze"],
    )
    assert scan.exit_code == 0

    topic = runner.invoke(app, ["--index", str(index_path), "t", "Kafka"], env=env)

    assert topic.exit_code == 0
    assert "Что известно: Kafka" in topic.stdout
    assert "Связанные встречи" in topic.stdout
    assert "Возможные решения" in topic.stdout
    assert "Подтвержденные решения не найдены." in topic.stdout
    assert "Возможные риски" in topic.stdout
    assert "Проблема: нельзя гарантировать запись в БД" in topic.stdout
    assert "Подтверждающие фрагменты" in topic.stdout
    assert "What we know" not in topic.stdout
    assert "(heuristic)" not in topic.stdout


def test_cli_topic_uses_indexed_alias_terms(meetily_db: Path, tmp_path: Path) -> None:
    with sqlite3.connect(meetily_db) as conn:
        insert_kafka_meeting(conn, tmp_path)
        conn.commit()
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db), "--no-analyze"],
    )
    assert scan.exit_code == 0

    without_alias = runner.invoke(app, ["--index", str(index_path), "t", "кафка"])
    assert without_alias.exit_code == 0
    assert "Kafka Architecture" not in without_alias.stdout

    alias = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "t",
            "kafka",
            "--alias",
            "кафка",
            "--alias",
            "broker",
            "--alias",
            "брокер",
            "--alias",
            "outbox",
        ],
    )
    assert alias.exit_code == 0

    for query in ("kafka", "кафка", "broker", "брокер", "outbox"):
        topic = runner.invoke(app, ["--index", str(index_path), "t", query])
        assert topic.exit_code == 0
        assert "Kafka Architecture" in topic.stdout
        assert "Kafka как брокер событий" in topic.stdout
        assert "Pattern outbox." in topic.stdout


def test_cli_topic_keeps_configured_english_for_russian_content(
    meetily_db: Path, tmp_path: Path
) -> None:
    with sqlite3.connect(meetily_db) as conn:
        insert_kafka_meeting(conn, tmp_path)
        conn.commit()
    index_path = tmp_path / "index.sqlite"
    data_dir = tmp_path / "data"
    config_path = data_dir / "settings.json"
    data_dir.mkdir()
    config_path.write_text('{"ui_language":"en"}\n', encoding="utf-8")
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db), "--no-analyze"],
    )
    assert scan.exit_code == 0

    topic = runner.invoke(app, ["--index", str(index_path), "t", "брокер"], env=env)

    assert topic.exit_code == 0
    assert "What we know: брокер" in topic.stdout
    assert "Related meetings" in topic.stdout
    assert "Что известно" not in topic.stdout


def insert_kafka_meeting(conn: sqlite3.Connection, tmp_path: Path) -> None:
    conn.execute(
        """
        INSERT INTO meetings (id, title, created_at, updated_at, folder_path)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "meeting-3",
            "Kafka Architecture",
            "2026-07-06T12:50:00Z",
            "2026-07-06T13:00:00Z",
            str(tmp_path / "Kafka Architecture"),
        ),
    )
    conn.executemany(
        """
        INSERT INTO transcripts (
            id, meeting_id, transcript, timestamp, audio_start_time,
            audio_end_time, duration, speaker
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "transcript-5",
                "meeting-3",
                "Обсуждали Kafka как брокер событий.",
                "12:56:20",
                3380.0,
                3390.0,
                10.0,
                "Alice",
            ),
            (
                "transcript-6",
                "meeting-3",
                "Проблема: нельзя гарантировать запись в БД "
                "и отправку в Kafka без рассинхронизации.",
                "12:56:36",
                3396.0,
                3402.0,
                6.0,
                "Alice",
            ),
            (
                "transcript-7",
                "meeting-3",
                "Pattern outbox.",
                "12:56:42",
                3402.0,
                3405.0,
                3.0,
                "Bob",
            ),
        ],
    )


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


def test_cli_refresh_skips_unproven_structured_analysis(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db), "--no-analyze"],
    )
    assert scan.exit_code == 0

    topic = runner.invoke(app, ["--index", str(index_path), "t", "migration"])
    assert topic.exit_code == 0
    assert "Supporting excerpts" in topic.stdout
    assert "Dobrynya agreed to send migration risks by Friday." in topic.stdout
    assert "Source: meeting-2 / transcript-2" in topic.stdout

    refresh = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db), "--json"],
    )
    assert refresh.exit_code == 0
    assert loads_json(refresh.stdout)["meetings_analyzed"] == 0


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
    assert f"schema version: {CURRENT_SCHEMA_VERSION}" in status.stdout
    assert f"current schema version: {CURRENT_SCHEMA_VERSION}" in status.stdout
    assert "orphaned tag assignments: 0" in status.stdout


def test_cli_db_status_reports_orphaned_tag_assignments(tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    repo = IndexRepository(index_path)
    source_uuid = repo.user_state.get_or_create_source(
        "meetily_sqlite",
        "/missing.sqlite",
        now="1",
    )
    TagRepository(repo.state_path).assign(
        source_uuid,
        ("missing-meeting",),
        ("Сбер",),
        now="2",
    )
    runner = CliRunner()

    status = runner.invoke(app, ["--index", str(index_path), "db", "status"])
    json_status = runner.invoke(
        app,
        ["--index", str(index_path), "db", "status", "--json"],
    )

    assert status.exit_code == 0
    assert "orphaned tag assignments: 1" in status.stdout
    assert json.loads(json_status.stdout)["orphaned_tag_assignments"] == 1


def test_cli_mcp_serve_is_real_subcommand() -> None:
    runner = CliRunner()

    mcp_help = runner.invoke(app, ["mcp", "--help"])
    assert mcp_help.exit_code == 0
    assert "Commands:" in mcp_help.stdout
    assert "serve" in mcp_help.stdout

    serve_help = runner.invoke(app, ["mcp", "serve", "--help"])
    assert serve_help.exit_code == 0
    assert "Usage: root mcp serve" in serve_help.stdout
    assert "--transport" not in serve_help.stdout
    assert "streamable-http" not in serve_help.stdout
    assert "sse" not in serve_help.stdout.casefold()


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
        "analyze",
        "sem",
        "semantic",
        "ask",
        "llm",
    )
    for command in removed_commands:
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code != 0


def test_cli_init_status_and_obsidian_sync(meetily_db: Path, tmp_path: Path) -> None:
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
    assert "obsidian: not configured" in status.stdout

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
    assert (vault_dir / "Meetily Memory" / "Meetings" / "Dobrynya Follow-up.md").exists()
    assert "<!-- meetily-memory:managed -->" in (
        vault_dir / "Meetily Memory" / "Meetings" / "Dobrynya Follow-up.md"
    ).read_text(encoding="utf-8")


def test_cli_obsidian_uses_workspace_settings_scope(meetily_db: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    index_path = workspace / "index.sqlite"
    workspace_settings = workspace / "settings.json"
    global_data_dir = tmp_path / "global"
    global_settings = global_data_dir / "settings.json"
    workspace_vault = tmp_path / "workspace-vault"
    global_vault = tmp_path / "global-vault"
    runner = CliRunner()

    global_configured = runner.invoke(
        app,
        ["obsidian", "init", "--vault", str(global_vault)],
        env={"MEETILY_MEMORY_DATA_DIR": str(global_data_dir)},
    )
    assert global_configured.exit_code == 0

    init = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "init",
            "--source",
            str(meetily_db),
            "--no-autosync",
        ],
    )
    assert init.exit_code == 0
    configured = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "obsidian",
            "init",
            "--vault",
            str(workspace_vault),
            "--sync-after-refresh",
        ],
    )
    assert configured.exit_code == 0

    synced = runner.invoke(
        app,
        ["--index", str(index_path), "obsidian", "sync"],
    )
    assert synced.exit_code == 0
    assert "obsidian files removed: 0" in synced.stdout
    status = runner.invoke(
        app,
        ["--index", str(index_path), "obsidian", "status"],
    )
    assert status.exit_code == 0
    assert f"vault: {workspace_vault}" in status.stdout
    assert "last sync: never" not in status.stdout
    meeting_note = workspace_vault / "Meetily Memory" / "Meetings" / "Launch Planning.md"
    meeting_note.unlink()
    refresh = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
    )
    assert refresh.exit_code == 0
    assert "obsidian sync: yes" in refresh.stdout

    workspace_config = load_app_settings(workspace_settings)
    global_config = load_app_settings(global_settings)
    assert workspace_config.obsidian.vault_path == str(workspace_vault)
    assert workspace_config.obsidian.last_sync_at is not None
    assert global_config.obsidian == ObsidianSettings(vault_path=str(global_vault))
    assert meeting_note.exists()
    assert not global_vault.exists()


def test_cli_v5_topic_graph_alias_and_task_status_memory(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0

    topic = runner.invoke(app, ["--index", str(index_path), "t", "migration"])
    assert topic.exit_code == 0
    assert "What we know: migration" in topic.stdout
    assert "Possible tasks" in topic.stdout
    assert "Dobrynya agreed to send migration risks by Friday." in topic.stdout
    assert "Source: meeting-2 / transcript-2" in topic.stdout

    alias = runner.invoke(
        app,
        ["--index", str(index_path), "t", "migration", "--alias", "миграция"],
    )
    assert alias.exit_code == 0
    assert "alias added: миграция -> migration" in alias.stdout

    alias_lookup = runner.invoke(app, ["--index", str(index_path), "t", "миграция"])
    assert alias_lookup.exit_code == 0
    assert "What we know: migration" in alias_lookup.stdout
    assert "alias: миграция" in alias_lookup.stdout
