import multiprocessing
import os
import stat
from pathlib import Path
from typing import Protocol, TextIO
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import meetily_memory.config.settings as settings_module
from meetily_memory.cli import lifecycle_commands
from meetily_memory.cli.app import app
from meetily_memory.config.settings import (
    AppSettings,
    ObsidianSettings,
    load_app_settings,
    save_app_settings,
    update_app_settings,
)
from meetily_memory.integrations import ObsidianSyncResult
from meetily_memory.json_codec import loads_json


class Waitable(Protocol):
    def wait(self, timeout: float | None = None) -> object: ...


def update_setting_in_process(
    settings_path: Path,
    key: str,
    value: object,
    start: Waitable,
    stale_reads: Waitable,
) -> None:
    original_load = settings_module.load_app_settings

    def coordinated_load(path: Path | None = None) -> AppSettings:
        settings = original_load(path)
        stale_reads.wait(timeout=10)
        return settings

    with patch.object(settings_module, "load_app_settings", coordinated_load):
        if not start.wait(timeout=10):
            msg = "Timed out waiting to start settings update."
            raise RuntimeError(msg)
        if key == "ui_language":
            settings_module.update_app_settings(
                settings_path=settings_path,
                ui_language=str(value),
            )
        elif key == "source_uuid":
            settings_module.update_app_settings(
                settings_path=settings_path,
                source_uuid=str(value),
            )
        else:
            message = f"Unsupported concurrent settings key: {key}"
            raise ValueError(message)


@pytest.mark.parametrize("failure_stage", ["write", "fsync", "replace"])
def test_save_failure_preserves_existing_file_and_cleans_up_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    settings_path = tmp_path / "settings.json"
    original = b'{"future_key":"keep","ui_language":"en"}\n'
    settings_path.write_bytes(original)

    def fail_write(file: TextIO, contents: str) -> None:
        file.write(contents[:5])
        message = "write failed"
        raise OSError(message)

    def fail_fsync(_file_descriptor: int) -> None:
        message = "fsync failed"
        raise OSError(message)

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        message = "replace failed"
        raise OSError(message)

    if failure_stage == "write":
        monkeypatch.setattr(settings_module, "_write_and_sync", fail_write)
    elif failure_stage == "fsync":
        monkeypatch.setattr(os, "fsync", fail_fsync)
    else:
        monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match=failure_stage):
        save_app_settings(AppSettings(ui_language="ru"), settings_path)

    assert settings_path.read_bytes() == original
    assert list(tmp_path.glob(".settings.json.*.tmp")) == []


@pytest.mark.parametrize("contents", [b"{", b"[]"])
def test_update_rejects_invalid_settings_without_replacing_them(
    tmp_path: Path,
    contents: bytes,
) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_bytes(contents)

    with pytest.raises(ValueError, match=r"Invalid settings file .*settings\.json"):
        update_app_settings(settings_path=settings_path, ui_language="ru")

    assert settings_path.read_bytes() == contents
    assert list(tmp_path.glob(".settings.json.*.tmp")) == []


def test_save_and_update_preserve_unknown_keys(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        """{
  "future_top_level": {"enabled": true},
  "obsidian": {"folder": "Existing", "future_nested": {"mode": "safe"}},
  "semantic": {"future_option": 7}
}\n""",
        encoding="utf-8",
    )

    update_app_settings(settings_path=settings_path, ui_language="en")
    updated_payload = loads_json(settings_path.read_bytes())

    assert updated_payload["future_top_level"] == {"enabled": True}
    assert updated_payload["obsidian"]["future_nested"] == {"mode": "safe"}
    assert updated_payload["semantic"]["future_option"] == 7

    save_app_settings(AppSettings(ui_language="ru"), settings_path)
    saved_payload = loads_json(settings_path.read_bytes())

    assert saved_payload["future_top_level"] == {"enabled": True}
    assert saved_payload["obsidian"]["future_nested"] == {"mode": "safe"}
    assert saved_payload["semantic"]["future_option"] == 7
    assert saved_payload["ui_language"] == "ru"


def test_update_removes_legacy_source_path_without_dropping_future_keys(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        '{"future_key":"keep","source_path":"/legacy/source"}\n',
        encoding="utf-8",
    )

    update_app_settings(
        settings_path=settings_path,
        source_path=None,
        source_uuid="source-uuid",
    )

    payload = loads_json(settings_path.read_bytes())
    assert payload["source_uuid"] == "source-uuid"
    assert payload["future_key"] == "keep"
    assert "source_path" not in payload


def test_concurrent_updates_merge_under_one_interprocess_lock(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    stale_reads = context.Barrier(2)
    processes = [
        context.Process(
            target=update_setting_in_process,
            args=(settings_path, "ui_language", "ru", start, stale_reads),
        ),
        context.Process(
            target=update_setting_in_process,
            args=(settings_path, "source_uuid", "source-uuid", start, stale_reads),
        ),
    ]

    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=15)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

    assert [process.exitcode for process in processes] == [0, 0]
    settings = load_app_settings(settings_path)
    assert settings.ui_language == "ru"
    assert settings.source_uuid == "source-uuid"


def test_stale_obsidian_sync_does_not_mark_reconfigured_vault_as_synced(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    synced_configuration = ObsidianSettings(
        vault_path="/old-vault",
        folder="Old",
        sync_after_update=True,
    )
    reconfigured = ObsidianSettings(
        vault_path="/new-vault",
        folder="New",
        sync_after_update=False,
    )
    save_app_settings(AppSettings(obsidian=synced_configuration), settings_path)
    update_app_settings(settings_path=settings_path, obsidian=reconfigured)

    update_app_settings(
        settings_path=settings_path,
        expected_obsidian=synced_configuration,
        obsidian_last_sync_at="2026-08-28T12:00:00Z",
    )

    assert load_app_settings(settings_path).obsidian == reconfigured


def test_overlapping_obsidian_syncs_record_the_later_completion(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    synced_configuration = ObsidianSettings(
        vault_path="/vault",
        folder="Meetily Memory",
        sync_after_update=True,
    )
    save_app_settings(AppSettings(obsidian=synced_configuration), settings_path)

    update_app_settings(
        settings_path=settings_path,
        expected_obsidian=synced_configuration,
        obsidian_last_sync_at="2026-08-28T12:00:00Z",
    )
    update_app_settings(
        settings_path=settings_path,
        expected_obsidian=synced_configuration,
        obsidian_last_sync_at="2026-08-28T12:01:00Z",
    )

    assert load_app_settings(settings_path).obsidian.last_sync_at == "2026-08-28T12:01:00Z"


def test_refresh_preserves_obsidian_reconfigured_during_scan(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    settings_path = data_dir / "settings.json"
    index_path = data_dir / "index.sqlite"
    initial = ObsidianSettings(vault_path="/old-vault", folder="Old")
    reconfigured = ObsidianSettings(vault_path="/new-vault", folder="New")
    save_app_settings(AppSettings(obsidian=initial), settings_path)
    original_scan_update = lifecycle_commands.scan_update

    def scan_with_reconfiguration(
        selected_index_path: Path,
        source_path: Path,
        *,
        finalize: bool = True,
    ) -> tuple[dict[str, object], lifecycle_commands.ScanResult]:
        result = original_scan_update(
            selected_index_path,
            source_path,
            finalize=finalize,
        )
        update_app_settings(settings_path=settings_path, obsidian=reconfigured)
        return result

    monkeypatch.setattr(lifecycle_commands, "scan_update", scan_with_reconfiguration)

    result = CliRunner().invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert result.exit_code == 0, result.output
    assert load_app_settings(settings_path).obsidian == reconfigured


def test_refresh_does_not_mark_new_obsidian_configuration_after_stale_sync(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    settings_path = data_dir / "settings.json"
    index_path = data_dir / "index.sqlite"
    initial = ObsidianSettings(
        vault_path="/old-vault",
        folder="Old",
        sync_after_update=True,
    )
    reconfigured = ObsidianSettings(
        vault_path="/new-vault",
        folder="New",
        sync_after_update=False,
    )
    save_app_settings(AppSettings(obsidian=initial), settings_path)

    def sync_with_reconfiguration(
        _index_path: Path,
        _vault_path: Path,
        _folder: str,
    ) -> ObsidianSyncResult:
        update_app_settings(settings_path=settings_path, obsidian=reconfigured)
        return ObsidianSyncResult(
            root_dir=tmp_path / "old-vault" / "Old",
            files_written=0,
            files_skipped=0,
            files_removed=0,
        )

    monkeypatch.setattr(lifecycle_commands, "sync_obsidian_vault", sync_with_reconfiguration)

    result = CliRunner().invoke(
        app,
        ["--index", str(index_path), "refresh", "--source", str(meetily_db)],
        env={"MEETILY_MEMORY_DATA_DIR": str(data_dir)},
    )

    assert result.exit_code == 0, result.output
    assert load_app_settings(settings_path).obsidian == reconfigured


def test_default_and_workspace_settings_remain_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_dir = tmp_path / "global"
    global_settings_path = global_dir / "settings.json"
    workspace_settings_path = tmp_path / "workspace" / "settings.json"
    monkeypatch.setenv("MEETILY_MEMORY_DATA_DIR", str(global_dir))

    update_app_settings(ui_language="ru")
    update_app_settings(settings_path=workspace_settings_path, source_uuid="source-uuid")

    global_settings = load_app_settings(global_settings_path)
    workspace_settings = load_app_settings(workspace_settings_path)
    assert global_settings.ui_language == "ru"
    assert global_settings.source_uuid is None
    assert workspace_settings.ui_language is None
    assert workspace_settings.source_uuid == "source-uuid"


def test_save_fsyncs_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_path = tmp_path / "settings.json"
    synced_modes: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(file_descriptor: int) -> None:
        synced_modes.append(os.fstat(file_descriptor).st_mode)
        real_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)

    save_app_settings(AppSettings(ui_language="ru"), settings_path)

    assert any(stat.S_ISREG(mode) for mode in synced_modes)
    assert any(stat.S_ISDIR(mode) for mode in synced_modes)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write through directory mode bits")
def test_read_only_directory_failure_preserves_existing_file(tmp_path: Path) -> None:
    settings_dir = tmp_path / "settings"
    settings_path = settings_dir / "settings.json"
    save_app_settings(AppSettings(ui_language="en"), settings_path)
    original = settings_path.read_bytes()
    original_mode = stat.S_IMODE(settings_dir.stat().st_mode)
    settings_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)

    try:
        with pytest.raises(PermissionError):
            save_app_settings(AppSettings(ui_language="ru"), settings_path)
    finally:
        settings_dir.chmod(original_mode)

    assert settings_path.read_bytes() == original
    assert list(settings_dir.glob(".settings.json.*.tmp")) == []
