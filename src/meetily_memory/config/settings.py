import fcntl
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from meetily_memory.config.paths import app_config_path
from meetily_memory.durable_files import fsync_directory
from meetily_memory.json_codec import dumps_json, loads_json


class _SyncableTextFile(Protocol):
    def write(self, value: str, /) -> int: ...

    def flush(self) -> None: ...

    def fileno(self) -> int: ...


@dataclass(frozen=True)
class ObsidianSettings:
    vault_path: str | None = None
    folder: str = "Meetily Memory"
    sync_after_update: bool = False
    last_sync_at: str | None = None


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
            "semantic": {
                "provider": self.semantic.provider,
                "model": self.semantic.model,
                "ollama_url": self.semantic.ollama_url,
            },
        }
        if self.source_path is not None:
            payload["source_path"] = self.source_path
        return payload


def load_app_settings(path: Path | None = None) -> AppSettings:
    settings_path = path or app_config_path()
    return _app_settings_from_payload(_load_settings_payload(settings_path))


def save_app_settings(settings: AppSettings, path: Path | None = None) -> Path:
    settings_path = path or app_config_path()
    with _settings_lock(settings_path):
        current_payload = _load_settings_payload(settings_path)
        _save_app_settings_unlocked(settings, settings_path, current_payload)
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
        current_payload = _load_settings_payload(path)
        settings = _app_settings_from_payload(current_payload)
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
            obsidian=obsidian_change(
                changes.get("obsidian"),
                settings.obsidian,
                expected_for_sync=expected_obsidian,
                last_sync_at=obsidian_last_sync_at,
            ),
            semantic=semantic_change(changes.get("semantic"), settings.semantic),
        )
        _save_app_settings_unlocked(updated, path, current_payload)
    return updated


@contextmanager
def _settings_lock(path: Path) -> Generator[None, None, None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load_settings_payload(path: Path) -> dict[str, Any]:
    try:
        raw_payload = path.read_bytes()
    except FileNotFoundError:
        return {}
    try:
        payload = loads_json(raw_payload)
    except ValueError as exc:
        message = f"Invalid settings file {path}: malformed JSON."
        raise ValueError(message) from exc
    if not isinstance(payload, dict):
        message = f"Invalid settings file {path}: expected a JSON object."
        raise ValueError(message)  # noqa: TRY004
    return payload


def _app_settings_from_payload(payload: dict[str, Any]) -> AppSettings:
    obsidian_payload = payload.get("obsidian")
    semantic_payload = payload.get("semantic")
    obsidian = obsidian_from_payload(obsidian_payload if isinstance(obsidian_payload, dict) else {})
    semantic = semantic_from_payload(semantic_payload if isinstance(semantic_payload, dict) else {})
    return AppSettings(
        source_path=optional_str(payload.get("source_path")),
        source_uuid=optional_str(payload.get("source_uuid")),
        ui_language=normalize_ui_language(optional_str(payload.get("ui_language"))),
        autosync_enabled=bool(payload.get("autosync_enabled", False)),
        last_update_at=optional_str(payload.get("last_update_at")),
        obsidian=obsidian,
        semantic=semantic,
    )


def _save_app_settings_unlocked(
    settings: AppSettings,
    path: Path,
    current_payload: dict[str, Any],
) -> None:
    payload = _merge_settings_payload(current_payload, settings.as_payload())
    _atomic_write_settings(path, payload)


def _merge_settings_payload(
    current_payload: dict[str, Any],
    settings_payload: dict[str, Any],
) -> dict[str, Any]:
    merged_payload = dict(current_payload)
    for key, value in settings_payload.items():
        if key in ("obsidian", "semantic") and isinstance(value, dict):
            current_section = current_payload.get(key)
            merged_section = dict(current_section) if isinstance(current_section, dict) else {}
            merged_section.update(value)
            merged_payload[key] = merged_section
        else:
            merged_payload[key] = value
    if "source_path" not in settings_payload:
        merged_payload.pop("source_path", None)
    return merged_payload


def _atomic_write_settings(path: Path, payload: dict[str, Any]) -> None:
    contents = dumps_json(payload) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            _write_and_sync(temp_file, contents)
        os.replace(temp_path, path)  # noqa: PTH105
        fsync_directory(path.parent)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _write_and_sync(file: _SyncableTextFile, contents: str) -> None:
    file.write(contents)
    file.flush()
    os.fsync(file.fileno())


def obsidian_from_payload(payload: dict[str, Any]) -> ObsidianSettings:
    return ObsidianSettings(
        vault_path=optional_str(payload.get("vault_path")),
        folder=optional_str(payload.get("folder")) or "Meetily Memory",
        sync_after_update=bool(payload.get("sync_after_update", False)),
        last_sync_at=optional_str(payload.get("last_sync_at")),
    )


def semantic_from_payload(payload: dict[str, Any]) -> SemanticSettings:
    return SemanticSettings(
        provider=optional_str(payload.get("provider")),
        model=optional_str(payload.get("model")),
        ollama_url=optional_str(payload.get("ollama_url")),
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
            sync_after_update=current.sync_after_update,
            last_sync_at=last_sync_at,
        )
    if isinstance(value, ObsidianSettings):
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
