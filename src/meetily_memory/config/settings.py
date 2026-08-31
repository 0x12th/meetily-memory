from __future__ import annotations

import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meetily_memory.config.paths import app_config_path
from meetily_memory.db.row_decode import decode_nullable_text, decode_required_text
from meetily_memory.db.state_schema import StateSchemaError
from meetily_memory.user_state import UserStateRepository

if TYPE_CHECKING:
    from collections.abc import Generator


@dataclass(frozen=True)
class ObsidianSettings:
    vault_path: str | None = None
    folder: str = "Meetily Memory"
    last_sync_at: str | None = None


@dataclass(frozen=True)
class AppSettings:
    source_path: str | None = None
    source_uuid: str | None = None
    ui_language: str | None = None
    last_update_at: str | None = None
    obsidian: ObsidianSettings = ObsidianSettings()

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_uuid": self.source_uuid,
            "ui_language": self.ui_language,
            "last_update_at": self.last_update_at,
            "obsidian": {
                "vault_path": self.obsidian.vault_path,
                "folder": self.obsidian.folder,
                "last_sync_at": self.obsidian.last_sync_at,
            },
        }
        if self.source_path is not None:
            payload["source_path"] = self.source_path
        return payload


def load_app_settings(path: Path | None = None) -> AppSettings:
    state_path = _state_path(path)
    if not state_path.is_file():
        return AppSettings()
    row = UserStateRepository.open_existing(state_path).read_app_settings()
    return _app_settings_from_state_row(row)


def save_app_settings(settings: AppSettings, path: Path | None = None) -> Path:
    settings_path = path or app_config_path()
    with _settings_lock(settings_path):
        _write_settings(settings_path, settings)
    return settings_path


def update_app_settings(
    *,
    settings_path: Path | None = None,
    expected_obsidian: ObsidianSettings | None = None,
    obsidian_last_sync_at: str | None = None,
    **changes: object,
) -> AppSettings:
    path = settings_path or app_config_path()
    with _settings_lock(path):
        settings = load_app_settings(path)
        updated = AppSettings(
            source_path=normalize_setting_text_change(changes, "source_path", settings.source_path),
            source_uuid=normalize_setting_text_change(changes, "source_uuid", settings.source_uuid),
            ui_language=normalize_ui_language(
                normalize_setting_text_change(changes, "ui_language", settings.ui_language)
            ),
            last_update_at=normalize_setting_text_change(
                changes, "last_update_at", settings.last_update_at
            ),
            obsidian=obsidian_change(
                changes.get("obsidian"),
                settings.obsidian,
                expected_for_sync=expected_obsidian,
                last_sync_at=obsidian_last_sync_at,
            ),
        )
        _write_settings(path, updated)
    return updated


def _write_settings(settings_path: Path, settings: AppSettings) -> None:
    repository = UserStateRepository(_state_path(settings_path))
    repository.replace_app_settings(_state_values(settings))


def _state_path(settings_path: Path | None) -> Path:
    logical_path = settings_path or app_config_path()
    return Path(logical_path).with_name("state.sqlite")


@contextmanager
def _settings_lock(path: Path) -> Generator[None, None, None]:
    state_path = _state_path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _state_values(settings: AppSettings) -> dict[str, object]:
    return {
        "source_uuid": settings.source_uuid,
        "source_path": settings.source_path,
        "ui_language": settings.ui_language,
        "last_update_at": settings.last_update_at,
        "obsidian_vault_path": settings.obsidian.vault_path,
        "obsidian_folder": settings.obsidian.folder,
        "obsidian_last_sync_at": settings.obsidian.last_sync_at,
    }


def _app_settings_from_state_row(row: dict[str, object]) -> AppSettings:
    context = "state app settings"

    def nullable_text(column: str) -> str | None:
        return decode_nullable_text(
            row[column],
            table="app_settings",
            column=column,
            context=context,
            error_type=StateSchemaError,
        )

    return AppSettings(
        source_path=nullable_text("source_path"),
        source_uuid=nullable_text("source_uuid"),
        ui_language=nullable_text("ui_language"),
        last_update_at=nullable_text("last_update_at"),
        obsidian=ObsidianSettings(
            vault_path=nullable_text("obsidian_vault_path"),
            folder=decode_required_text(
                row["obsidian_folder"],
                table="app_settings",
                column="obsidian_folder",
                context=context,
                error_type=StateSchemaError,
            ),
            last_sync_at=nullable_text("obsidian_last_sync_at"),
        ),
    )


def obsidian_change(
    value: object,
    current: ObsidianSettings,
    *,
    expected_for_sync: ObsidianSettings | None = None,
    last_sync_at: str | None = None,
) -> ObsidianSettings:
    if last_sync_at is not None:
        if isinstance(value, ObsidianSettings):
            message = "Cannot replace Obsidian settings and record a sync in one update."
            raise ValueError(message)
        if expected_for_sync is None:
            message = "Recording an Obsidian sync requires the configuration that was synced."
            raise ValueError(message)
        current_target = (current.vault_path, current.folder)
        expected_target = (expected_for_sync.vault_path, expected_for_sync.folder)
        if current_target != expected_target:
            return current
        return ObsidianSettings(
            vault_path=current.vault_path,
            folder=current.folder,
            last_sync_at=last_sync_at,
        )
    if isinstance(value, ObsidianSettings):
        return value
    return current


def normalize_setting_text_change(
    changes: dict[str, object],
    key: str,
    current: str | None,
) -> str | None:
    """Normalize setting input; an explicit empty string clears the setting."""
    value = changes.get(key, current)
    if value is None:
        return None
    if isinstance(value, Path):
        return str(value)
    if type(value) is str:
        return value or None
    message = f"Setting {key} must be a string, path, or None; got {type(value).__name__}."
    raise TypeError(message)


def normalize_ui_language(value: str | None) -> str | None:
    if value is None:
        return None
    language = value.casefold().replace("_", "-").split("-", maxsplit=1)[0]
    return language if language in {"en", "ru"} else None
