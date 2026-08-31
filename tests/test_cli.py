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
from meetily_memory.db.schema_family import INDEX_SCHEMA_USER_VERSION
from meetily_memory.json_codec import loads_json
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.tagging import TagRepository
from tests.index_helpers import publish_fresh_index


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
    assert "mm open SOURCE_UUID/EXTERNAL_ID" in help_result.stdout
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
        "obsidian",
    ):
        assert re.search(rf"\n  {re.escape(command)}(?:\s{{2,}}|\n)", help_result.stdout)
    for command in (
        "scan",
        "c",
        "t",
        "topic",
        "config",
        "db",
        "mcp",
    ):
        assert not re.search(rf"\n  {re.escape(command)}(?:\s{{2,}}|\n)", help_result.stdout)

    open_help = runner.invoke(app, ["open", "--help"])
    assert open_help.exit_code == 0
    assert "MEETING_REF" in open_help.stdout
    for obsolete_option in ("--source-uuid", "--external-id", "--source", "--print-path"):
        assert obsolete_option not in open_help.stdout

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
    assert "--sync-after-refresh" not in obsidian_init_help.stdout
    assert "--no-sync-after-refresh" not in obsidian_init_help.stdout
    assert "--sync-after-update" not in obsidian_init_help.stdout


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (("scan",), "--source"),
        (("config",), "source"),
        (("db",), "status"),
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
    settings_path = index_path.with_name("settings.json")
    state_path = index_path.with_name("state.sqlite")
    assert load_app_settings(settings_path).ui_language == "ru"
    assert not settings_path.exists()
    with sqlite3.connect(state_path) as connection:
        assert connection.execute("SELECT ui_language FROM app_settings").fetchone() == ("ru",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

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
    assert load_app_settings(settings_path).ui_language is None
    assert not settings_path.exists()
    with sqlite3.connect(state_path) as connection:
        assert connection.execute("SELECT ui_language FROM app_settings").fetchone() == (None,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def scan_twice(runner: CliRunner, index_path: Path, meetily_db: Path) -> None:
    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0
    assert "meetings: 2" in scan.stdout

    second_scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert second_scan.exit_code == 0
    assert "meetings: 2" in second_scan.stdout


def test_cli_v1_scan_search_list_last_person_and_doctor(
    meetily_db: Path,
    tmp_path: Path,
    platform_opener: tuple[dict[str, str], Path],
) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    scan_twice(runner, index_path, meetily_db)

    search = runner.invoke(app, ["--index", str(index_path), "s", "pricing decision"])
    assert search.exit_code == 0
    assert "Launch Planning" in search.stdout
    assert "pricing decision" in search.stdout
    assert "chunk #" in search.stdout
    with sqlite3.connect(index_path) as conn:
        source_uuid = str(conn.execute("SELECT source_uuid FROM index_meta").fetchone()[0])
    assert f"open: mm open {source_uuid}/meeting-1" in search.stdout

    doctor = runner.invoke(
        app,
        ["--index", str(index_path), "doctor", "--source", str(meetily_db)],
    )
    assert doctor.exit_code == 0
    assert "source readable: yes" in doctor.stdout
    assert "fts5: yes" in doctor.stdout
    assert "meetings: 2" in doctor.stdout
    assert "chunks: 6" in doctor.stdout

    target = tmp_path / "Dobrynya Follow-up"
    target.mkdir()
    opener_env, opener_calls = platform_opener
    opened = runner.invoke(
        app,
        ["--index", str(index_path), "open", f"{source_uuid}/meeting-2"],
        env=opener_env,
    )
    assert opened.exit_code == 0
    assert opener_calls.read_text(encoding="utf-8").strip() == str(target)


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
    with sqlite3.connect(index_path) as conn:
        source_uuid = str(conn.execute("SELECT source_uuid FROM index_meta").fetchone()[0])
    assert f"open: mm open {source_uuid}/meeting-1" in search.stdout


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


def test_cli_open_resolves_one_canonical_ref_to_the_source_native_folder(
    meetily_db: Path,
    tmp_path: Path,
    platform_opener: tuple[dict[str, str], Path],
) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()
    target = tmp_path / "Launch Planning"
    target.mkdir()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0

    meeting = IndexRepository.open_existing(index_path).get_meeting_by_local_id(1)
    assert meeting is not None
    meeting_ref = f"{meeting['source_uuid']}/{meeting['external_id']}"
    opener_env, opener_calls = platform_opener
    opened = runner.invoke(
        app,
        ["--index", str(index_path), "open", meeting_ref],
        env=opener_env,
    )

    assert opened.exit_code == 0, opened.output
    assert opener_calls.read_text(encoding="utf-8").strip() == str(target)


def test_cli_open_reports_invalid_and_missing_canonical_refs(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    invalid = runner.invoke(app, ["--index", str(index_path), "open", "meeting-1"])
    assert invalid.exit_code == 2
    assert "Expected SOURCE_UUID/EXTERNAL_ID" in invalid.output
    assert not index_path.exists()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0
    meeting = IndexRepository.open_existing(index_path).get_meeting_by_local_id(1)
    assert meeting is not None
    missing_ref = f"{meeting['source_uuid']}/missing"

    missing = runner.invoke(app, ["--index", str(index_path), "open", missing_ref])
    assert missing.exit_code == 2
    assert f"Meeting not found: {missing_ref}" in missing.output


def test_cli_refresh_skips_unproven_structured_analysis(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0

    refresh = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db), "--json"],
    )
    assert refresh.exit_code == 0
    payload = loads_json(refresh.stdout)
    assert payload["meetings_seen"] == 2
    assert "meetings_analyzed" not in payload


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


def test_cli_db_status_reports_missing_schema_without_creating_databases(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    status = runner.invoke(app, ["--index", str(index_path), "db", "status"])

    assert status.exit_code == 0
    assert f"index path: {index_path}" in status.stdout
    assert "schema version: missing" in status.stdout
    assert f"current schema version: {INDEX_SCHEMA_USER_VERSION}" in status.stdout
    assert "schema status: missing" in status.stdout
    assert "orphaned tag assignments: unavailable" in status.stdout
    assert "state database status is missing" in status.stdout
    assert not index_path.exists()
    assert not index_path.with_name("state.sqlite").exists()


def test_cli_db_status_reports_orphaned_tag_assignments(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    published = publish_fresh_index(index_path, meetily_db)
    source_uuid = published.source.source_uuid
    TagRepository(index_path.with_name("state.sqlite")).assign(
        source_uuid,
        ("missing-meeting",),
        ("Сбер", "Собес"),
        now="2",
    )
    runner = CliRunner()

    status = runner.invoke(app, ["--index", str(index_path), "db", "status"])
    json_status = runner.invoke(
        app,
        ["--index", str(index_path), "db", "status", "--json"],
    )

    assert status.exit_code == 0
    assert "orphaned tag assignments: 2" in status.stdout
    assert json.loads(json_status.stdout)["orphaned_tag_assignments"] == 2


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
        "autosync",
        "c",
        "t",
        "topic",
        "mcp",
    )
    for command in removed_commands:
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code != 0


def test_cli_init_status_and_obsidian_sync(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    vault_dir = tmp_path / "vault"
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner = CliRunner()

    init = runner.invoke(
        app,
        ["--index", str(index_path), "init", "--source", str(meetily_db)],
        env=env,
    )
    assert init.exit_code == 0
    assert "initialized: yes" in init.stdout
    assert "meetings: 2" in init.stdout

    status = runner.invoke(app, ["--index", str(index_path), "status"], env=env)
    assert status.exit_code == 0
    assert f"index path: {index_path}" in status.stdout
    assert f"source path: {meetily_db}" in status.stdout
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
        ],
        env=env,
    )
    assert obsidian_init.exit_code == 0
    assert "obsidian vault:" in obsidian_init.stdout
    obsidian_status = runner.invoke(
        app,
        ["obsidian", "status", "--json"],
        env=env,
    )
    assert obsidian_status.exit_code == 0
    assert loads_json(obsidian_status.stdout) == {
        "vault_path": str(vault_dir),
        "folder": "Meetily Memory",
        "last_sync_at": None,
    }

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
    meeting_note = next(
        (vault_dir / "Meetily Memory" / "Meetings").glob("Dobrynya Follow-up--m-*.md")
    )
    assert "<!-- meetily-memory:managed:v2:" in meeting_note.read_text(encoding="utf-8")


def test_cli_obsidian_uses_workspace_settings_scope_and_manual_sync(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
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
    meeting_note = next(
        (workspace_vault / "Meetily Memory" / "Meetings").glob("Launch Planning--m-*.md")
    )
    meeting_note.unlink()
    refresh = runner.invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db), "--json"],
    )
    assert refresh.exit_code == 0
    assert "obsidian_synced" not in loads_json(refresh.stdout)
    assert not meeting_note.exists()

    workspace_config = load_app_settings(workspace_settings)
    global_config = load_app_settings(global_settings)
    assert workspace_config.obsidian.vault_path == str(workspace_vault)
    assert workspace_config.obsidian.last_sync_at is not None
    assert global_config.obsidian == ObsidianSettings(vault_path=str(global_vault))
    assert not global_vault.exists()

    manual_resync = runner.invoke(
        app,
        ["--index", str(index_path), "obsidian", "sync"],
    )
    assert manual_resync.exit_code == 0
    assert meeting_note.exists()
