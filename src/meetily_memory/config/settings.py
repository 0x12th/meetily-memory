from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meetily_memory.config.paths import default_state_path
from meetily_memory.db.row_decode import decode_nullable_text, decode_required_text
from meetily_memory.db.state_schema import StateSchemaError
from meetily_memory.user_state import UserStateRepository


@dataclass(frozen=True)
class ObsidianSettings:
    vault_path: str | None = None
    folder: str = "Meetily Memory"
    last_sync_at: str | None = None


@dataclass(frozen=True)
class AppSettings:
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
        return payload


def load_app_settings(state_path: Path | None = None) -> AppSettings:
    state_path = Path(state_path) if state_path is not None else default_state_path()
    if not state_path.is_file():
        return AppSettings()
    row = UserStateRepository.open_existing(state_path).read_app_settings()
    return _app_settings_from_state_row(row)


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


def normalize_ui_language(value: str | None) -> str | None:
    if value is None:
        return None
    language = value.casefold().replace("_", "-").split("-", maxsplit=1)[0]
    return language if language in {"en", "ru"} else None
