from __future__ import annotations

import multiprocessing
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from meetily_memory.config.settings import (
    AppSettings,
    ObsidianSettings,
    load_app_settings,
    save_app_settings,
    update_app_settings,
)
from meetily_memory.db.state_schema import StateSchemaError
from meetily_memory.user_state import UserStateRepository


def _update_setting_in_process(settings_path: Path, key: str, value: str) -> None:
    if key == "ui_language":
        update_app_settings(settings_path=settings_path, ui_language=value)
    elif key == "source_uuid":
        update_app_settings(settings_path=settings_path, source_uuid=value)
    else:
        message = f"Unsupported concurrent settings key: {key}"
        raise ValueError(message)


def test_save_and_update_persist_settings_in_state_database(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"

    save_app_settings(
        AppSettings(
            ui_language="en",
            obsidian=ObsidianSettings(vault_path="/vault", folder="Notes"),
        ),
        settings_path,
    )
    updated = update_app_settings(settings_path=settings_path, ui_language="ru")

    assert updated.ui_language == "ru"
    assert load_app_settings(settings_path) == updated
    assert updated.as_payload()["obsidian"] == {
        "vault_path": "/vault",
        "folder": "Notes",
        "last_sync_at": None,
    }
    assert not settings_path.exists()
    state_path = tmp_path / "state.sqlite"
    with sqlite3.connect(state_path) as connection:
        assert connection.execute(
            """
            SELECT ui_language, obsidian_vault_path, obsidian_folder
            FROM app_settings
            """
        ).fetchone() == ("ru", "/vault", "Notes")


def test_settings_row_decoder_rejects_wrong_sqlite_storage_type(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    save_app_settings(AppSettings(), settings_path)
    with sqlite3.connect(tmp_path / "state.sqlite") as connection:
        connection.execute(
            "UPDATE app_settings SET source_path = ? WHERE singleton = 1",
            (sqlite3.Binary(b"/not-text"),),
        )
        connection.commit()

    with pytest.raises(
        StateSchemaError,
        match=r"app_settings\.source_path must be TEXT, got BLOB",
    ):
        load_app_settings(settings_path)


def test_settings_preserve_database_empty_strings_but_input_empty_string_clears(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "settings.json"
    save_app_settings(AppSettings(source_path=""), settings_path)

    stored = load_app_settings(settings_path)
    assert stored.source_path == ""

    cleared = update_app_settings(settings_path=settings_path, source_path="")
    assert cleared.source_path is None
    with pytest.raises(TypeError, match="source_path must be a string, path, or None"):
        update_app_settings(settings_path=settings_path, source_path=42)


def test_invalid_selected_source_rolls_back_settings_transaction(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    save_app_settings(AppSettings(ui_language="en"), settings_path)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        update_app_settings(settings_path=settings_path, source_uuid="missing-source")

    assert load_app_settings(settings_path) == AppSettings(ui_language="en")


def test_concurrent_updates_merge_under_one_interprocess_lock(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    state = UserStateRepository(tmp_path / "state.sqlite")
    source_uuid = state.get_or_create_source("meetily_sqlite", "/source.sqlite", now="created")
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_update_setting_in_process,
            args=(settings_path, "ui_language", "ru"),
        ),
        context.Process(
            target=_update_setting_in_process,
            args=(settings_path, "source_uuid", source_uuid),
        ),
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

    assert [process.exitcode for process in processes] == [0, 0]
    settings = load_app_settings(settings_path)
    assert settings.ui_language == "ru"
    assert settings.source_uuid == source_uuid


def test_stale_obsidian_sync_does_not_mark_reconfigured_vault_as_synced(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    synced_configuration = ObsidianSettings(
        vault_path="/old-vault",
        folder="Old",
    )
    reconfigured = ObsidianSettings(
        vault_path="/new-vault",
        folder="New",
    )
    save_app_settings(AppSettings(obsidian=synced_configuration), settings_path)
    update_app_settings(settings_path=settings_path, obsidian=reconfigured)

    update_app_settings(
        settings_path=settings_path,
        expected_obsidian=synced_configuration,
        obsidian_last_sync_at="2026-08-28T12:00:00Z",
    )

    assert load_app_settings(settings_path).obsidian == reconfigured


def test_distinct_state_settings_paths_remain_isolated(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    global_settings_path = global_dir / "settings.json"
    workspace_settings_path = tmp_path / "workspace" / "settings.json"
    workspace_state = UserStateRepository(workspace_settings_path.with_name("state.sqlite"))
    source_uuid = workspace_state.get_or_create_source(
        "meetily_sqlite",
        "/workspace.sqlite",
        now="created",
    )
    update_app_settings(settings_path=global_settings_path, ui_language="ru")
    update_app_settings(settings_path=workspace_settings_path, source_uuid=source_uuid)

    global_settings = load_app_settings(global_settings_path)
    workspace_settings = load_app_settings(workspace_settings_path)
    assert global_settings.ui_language == "ru"
    assert global_settings.source_uuid is None
    assert workspace_settings.ui_language is None
    assert workspace_settings.source_uuid == source_uuid


def test_legacy_settings_file_is_not_auto_imported_or_modified(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    legacy = b'{"source_path":"/legacy.sqlite","ui_language":"ru"}\n'
    settings_path.write_bytes(legacy)

    assert load_app_settings(settings_path) == AppSettings()
    assert settings_path.read_bytes() == legacy
    assert not settings_path.with_name("state.sqlite").exists()
