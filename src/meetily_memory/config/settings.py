from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meetily_memory.config.paths import app_config_path
from meetily_memory.json_codec import dumps_json, loads_json


@dataclass(frozen=True)
class ObsidianSettings:
    vault_path: str | None = None
    folder: str = "Meetily Memory"
    sync_after_update: bool = False
    last_sync_at: str | None = None


@dataclass(frozen=True)
class LLMSettings:
    provider: str | None = None
    model: str | None = None
    ollama_url: str | None = None


@dataclass(frozen=True)
class SemanticSettings:
    provider: str | None = None
    model: str | None = None
    ollama_url: str | None = None


@dataclass(frozen=True)
class AppSettings:
    source_path: str | None = None
    source_uuid: str | None = None
    ui_language: str | None = None
    autosync_enabled: bool = False
    last_update_at: str | None = None
    obsidian: ObsidianSettings = ObsidianSettings()
    llm: LLMSettings = LLMSettings()
    semantic: SemanticSettings = SemanticSettings()

    def as_payload(self) -> dict[str, Any]:
        payload = {
            "source_uuid": self.source_uuid,
            "ui_language": self.ui_language,
            "autosync_enabled": self.autosync_enabled,
            "last_update_at": self.last_update_at,
            "obsidian": {
                "vault_path": self.obsidian.vault_path,
                "folder": self.obsidian.folder,
                "sync_after_update": self.obsidian.sync_after_update,
                "last_sync_at": self.obsidian.last_sync_at,
            },
            "llm": {
                "provider": self.llm.provider,
                "model": self.llm.model,
                "ollama_url": self.llm.ollama_url,
            },
            "semantic": {
                "provider": self.semantic.provider,
                "model": self.semantic.model,
                "ollama_url": self.semantic.ollama_url,
            },
        }
        if self.source_path is not None:
            payload["source_path"] = self.source_path
        return payload


def load_app_settings() -> AppSettings:
    path = app_config_path()
    if not path.exists():
        return AppSettings()
    payload = loads_json(path.read_text())
    if not isinstance(payload, dict):
        return AppSettings()
    obsidian_payload = payload.get("obsidian")
    llm_payload = payload.get("llm")
    semantic_payload = payload.get("semantic")
    obsidian = obsidian_from_payload(obsidian_payload if isinstance(obsidian_payload, dict) else {})
    llm = llm_from_payload(llm_payload if isinstance(llm_payload, dict) else {})
    semantic = semantic_from_payload(semantic_payload if isinstance(semantic_payload, dict) else {})
    return AppSettings(
        source_path=optional_str(payload.get("source_path")),
        source_uuid=optional_str(payload.get("source_uuid")),
        ui_language=normalize_ui_language(optional_str(payload.get("ui_language"))),
        autosync_enabled=bool(payload.get("autosync_enabled", False)),
        last_update_at=optional_str(payload.get("last_update_at")),
        obsidian=obsidian,
        llm=llm,
        semantic=semantic,
    )


def save_app_settings(settings: AppSettings) -> Path:
    path = app_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps_json(settings.as_payload()) + "\n", encoding="utf-8")
    return path


def update_app_settings(**changes: object) -> AppSettings:
    settings = load_app_settings()
    updated = AppSettings(
        source_path=string_change(changes, "source_path", settings.source_path),
        source_uuid=string_change(changes, "source_uuid", settings.source_uuid),
        ui_language=normalize_ui_language(
            string_change(changes, "ui_language", settings.ui_language)
        ),
        autosync_enabled=bool_change(
            changes,
            "autosync_enabled",
            current=settings.autosync_enabled,
        ),
        last_update_at=string_change(changes, "last_update_at", settings.last_update_at),
        obsidian=obsidian_change(changes.get("obsidian"), settings.obsidian),
        llm=llm_change(changes.get("llm"), settings.llm),
        semantic=semantic_change(changes.get("semantic"), settings.semantic),
    )
    save_app_settings(updated)
    return updated


def obsidian_from_payload(payload: dict[str, Any]) -> ObsidianSettings:
    return ObsidianSettings(
        vault_path=optional_str(payload.get("vault_path")),
        folder=optional_str(payload.get("folder")) or "Meetily Memory",
        sync_after_update=bool(payload.get("sync_after_update", False)),
        last_sync_at=optional_str(payload.get("last_sync_at")),
    )


def llm_from_payload(payload: dict[str, Any]) -> LLMSettings:
    return LLMSettings(
        provider=optional_str(payload.get("provider")),
        model=optional_str(payload.get("model")),
        ollama_url=optional_str(payload.get("ollama_url")),
    )


def semantic_from_payload(payload: dict[str, Any]) -> SemanticSettings:
    return SemanticSettings(
        provider=optional_str(payload.get("provider")),
        model=optional_str(payload.get("model")),
        ollama_url=optional_str(payload.get("ollama_url")),
    )


def obsidian_change(value: object, current: ObsidianSettings) -> ObsidianSettings:
    if isinstance(value, ObsidianSettings):
        return value
    return current


def llm_change(value: object, current: LLMSettings) -> LLMSettings:
    if isinstance(value, LLMSettings):
        return value
    return current


def semantic_change(value: object, current: SemanticSettings) -> SemanticSettings:
    if isinstance(value, SemanticSettings):
        return value
    return current


def string_change(changes: dict[str, object], key: str, current: str | None) -> str | None:
    value = changes.get(key, current)
    if isinstance(value, Path):
        return str(value)
    return optional_str(value)


def bool_change(changes: dict[str, object], key: str, *, current: bool) -> bool:
    value = changes.get(key, current)
    return bool(value)


def optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def normalize_ui_language(value: str | None) -> str | None:
    if value is None:
        return None
    language = value.casefold().replace("_", "-").split("-", maxsplit=1)[0]
    return language if language in {"en", "ru"} else None
