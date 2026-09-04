from __future__ import annotations

import multiprocessing
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from meetily_memory.config.settings import AppSettings, ObsidianSettings, load_app_settings
from meetily_memory.db.state_schema import StateSchemaError
from meetily_memory.user_state import UserStateRepository


def _update_setting_in_process(state_path: Path, key: str, value: str) -> None:
    state = UserStateRepository(state_path)
    if key == "ui_language":
        state.set_ui_language(value)
    elif key == "source_uuid":
        state.select_source(value)
    else:
        message = f"Unsupported concurrent settings key: {key}"
        raise ValueError(message)


def test_narrow_operations_persist_settings_in_state_database(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    state = UserStateRepository(state_path)
    state.set_ui_language("en")
    state.set_obsidian_target("/vault", "Notes")
    state.set_ui_language("ru")
    updated = load_app_settings(state_path)

    assert updated.ui_language == "ru"

    assert updated.as_payload()["obsidian"] == {
        "vault_path": "/vault",
        "folder": "Notes",
        "last_sync_at": None,
    }

    with sqlite3.connect(state_path) as connection:
        assert connection.execute(
            """
            SELECT ui_language, obsidian_vault_path, obsidian_folder
            FROM app_settings
            """
        ).fetchone() == ("ru", "/vault", "Notes")


def test_settings_row_decoder_rejects_wrong_sqlite_storage_type(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    UserStateRepository(state_path)
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "UPDATE app_settings SET obsidian_vault_path = ? WHERE singleton = 1",
            (sqlite3.Binary(b"/not-text"),),
        )
        connection.commit()

    with pytest.raises(
        StateSchemaError,
        match=r"app_settings\.obsidian_vault_path must be TEXT, got BLOB",
    ):
        load_app_settings(state_path)


def test_invalid_selected_source_rolls_back_settings_transaction(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    state = UserStateRepository(state_path)
    state.set_ui_language("en")

    with pytest.raises(ValueError, match="Source UUID is missing"):
        state.select_source("missing-source")

    assert load_app_settings(state_path) == AppSettings(ui_language="en")


def test_concurrent_narrow_updates_do_not_overwrite_other_columns(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    state = UserStateRepository(state_path)
    source_uuid = state.get_or_create_source("meetily_sqlite", "/source.sqlite", now="created")
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_update_setting_in_process,
            args=(state_path, "ui_language", "ru"),
        ),
        context.Process(
            target=_update_setting_in_process,
            args=(state_path, "source_uuid", source_uuid),
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
    settings = load_app_settings(state_path)
    assert settings.ui_language == "ru"
    assert settings.source_uuid == source_uuid


def test_stale_obsidian_sync_does_not_mark_reconfigured_vault_as_synced(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"
    state = UserStateRepository(state_path)
    synced_configuration = ObsidianSettings(
        vault_path="/old-vault",
        folder="Old",
    )
    reconfigured = ObsidianSettings(
        vault_path="/new-vault",
        folder="New",
    )
    state.set_obsidian_target(synced_configuration.vault_path, synced_configuration.folder)
    state.set_obsidian_target(reconfigured.vault_path, reconfigured.folder)
    state.record_obsidian_sync(
        synced_configuration.vault_path,
        synced_configuration.folder,
        "2026-08-28T12:00:00Z",
    )

    assert load_app_settings(state_path).obsidian == reconfigured


def test_distinct_state_settings_paths_remain_isolated(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    global_state_path = global_dir / "state.sqlite"
    workspace_state_path = tmp_path / "workspace" / "state.sqlite"
    workspace_state = UserStateRepository(workspace_state_path)
    source_uuid = workspace_state.get_or_create_source(
        "meetily_sqlite",
        "/workspace.sqlite",
        now="created",
    )
    UserStateRepository(global_state_path).set_ui_language("ru")
    workspace_state.select_source(source_uuid)

    global_settings = load_app_settings(global_state_path)
    workspace_settings = load_app_settings(workspace_state_path)
    assert global_settings.ui_language == "ru"
    assert global_settings.source_uuid is None
    assert workspace_settings.ui_language is None
    assert workspace_settings.source_uuid == source_uuid


def test_missing_state_returns_default_settings_without_creating_database(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite"

    assert load_app_settings(state_path) == AppSettings()
    assert not state_path.exists()
