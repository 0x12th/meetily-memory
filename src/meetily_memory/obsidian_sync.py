from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from meetily_memory.config.settings import load_app_settings, update_app_settings
from meetily_memory.integrations import ObsidianSyncResult, sync_obsidian_vault


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sync_configured_obsidian_locked(
    index_path: Path,
    settings_path: Path,
    *,
    required: bool = False,
) -> ObsidianSyncResult | None:
    settings = load_app_settings(settings_path)
    configured = settings.obsidian
    if not configured.vault_path:
        if required:
            message = "Obsidian is not configured. Run `mm obsidian init`."
            raise ValueError(message)
        return None
    result = sync_obsidian_vault(
        Path(index_path),
        Path(configured.vault_path),
        configured.folder,
    )
    update_app_settings(
        settings_path=Path(settings_path),
        expected_obsidian=configured,
        obsidian_last_sync_at=utc_now_iso(),
    )
    return result
