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

from meetily_memory.cli import obsidian_commands
from meetily_memory.cli.app import app
from meetily_memory.cli.common import open_path
from meetily_memory.cli.search_commands import parse_search_filters
from meetily_memory.config.settings import (
    ObsidianSettings,
    load_app_settings,
)
from meetily_memory.db.migrations import CURRENT_SCHEMA_VERSION
from meetily_memory.db.repository import IndexRepository
from meetily_memory.integrations import ObsidianSyncResult
from meetily_memory.json_codec import loads_json
from meetily_memory.refresh_lock import RefreshLock, RefreshLockBusyError
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
    assert "mm open --source-uuid UUID --external-id ID" in help_result.stdout
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
        (("obsidian",), "sync"),
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
    with sqlite3.connect(index_path) as conn:
        source_uuid = str(conn.execute("SELECT source_uuid FROM sources").fetchone()[0])
    assert f"open: mm open --source-uuid {source_uuid} --external-id meeting-1" in search.stdout

    doctor = runner.invoke(
        app,
        ["--index", str(index_path), "doctor", "--source", str(meetily_db)],
    )
    assert doctor.exit_code == 0
    assert "source readable: yes" in doctor.stdout
    assert "fts5: yes" in doctor.stdout
    assert "decisions:" in doctor.stdout
    assert "action items:" in doctor.stdout

    opened = runner.invoke(
        app,
        [
            "--index",
            str(index_path),
            "open",
            "--source-uuid",
            source_uuid,
            "--external-id",
            "meeting-2",
            "--print-path",
        ],
    )
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
    with sqlite3.connect(index_path) as conn:
        source_uuid = str(conn.execute("SELECT source_uuid FROM sources").fetchone()[0])
    assert f"open: mm open --source-uuid {source_uuid} --external-id meeting-1" in search.stdout


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


def test_cli_open_selects_meeting_folder_by_default(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0

    meeting = IndexRepository.open_existing(index_path).get_meeting_by_local_id(1)
    assert meeting is not None
    open_args = [
        "--source-uuid",
        str(meeting["source_uuid"]),
        "--external-id",
        str(meeting["external_id"]),
    ]
    default_path = runner.invoke(
        app,
        ["--index", str(index_path), "open", *open_args, "--print-path"],
    )
    assert default_path.exit_code == 0
    assert default_path.stdout.strip() == str(tmp_path / "Launch Planning")

    source_path = runner.invoke(
        app,
        ["--index", str(index_path), "open", *open_args, "--source", "--print-path"],
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


def test_cli_db_status_reports_missing_schema_without_creating_databases(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    status = runner.invoke(app, ["--index", str(index_path), "db", "status"])

    assert status.exit_code == 0
    assert f"index path: {index_path}" in status.stdout
    assert "schema version: missing" in status.stdout
    assert f"current schema version: {CURRENT_SCHEMA_VERSION}" in status.stdout
    assert "schema status: missing" in status.stdout
    assert "orphaned tag assignments: unavailable" in status.stdout
    assert "user-state database status is missing" in status.stdout
    assert not index_path.exists()
    assert not index_path.with_name("state.sqlite").exists()


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    data_dir = tmp_path / "data"
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
    assert "meetings seen: 2" in init.stdout

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
            "--sync-after-refresh",
        ],
        env=env,
    )
    assert obsidian_init.exit_code == 0
    assert "obsidian vault:" in obsidian_init.stdout

    original_sync = obsidian_commands.sync_obsidian_vault
    lock_checked = False

    def sync_while_lock_is_held(
        index: Path,
        vault: Path,
        folder: str,
    ) -> ObsidianSyncResult:
        nonlocal lock_checked
        with pytest.raises(RefreshLockBusyError), RefreshLock(index_path):
            pass
        lock_checked = True
        return original_sync(index, vault, folder)

    monkeypatch.setattr(obsidian_commands, "sync_obsidian_vault", sync_while_lock_is_held)
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
    assert lock_checked
    assert "obsidian files synced:" in obsidian_sync.stdout
    meeting_note = next(
        (vault_dir / "Meetily Memory" / "Meetings").glob("Dobrynya Follow-up--m-*.md")
    )
    assert "<!-- meetily-memory:managed:v1:" in meeting_note.read_text(encoding="utf-8")


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
    meeting_note = next(
        (workspace_vault / "Meetily Memory" / "Meetings").glob("Launch Planning--m-*.md")
    )
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
