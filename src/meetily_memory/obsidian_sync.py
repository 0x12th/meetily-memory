from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from meetily_memory.config.settings import load_app_settings
from meetily_memory.obsidian_notes import ObsidianSyncResult, sync_obsidian_vault
from meetily_memory.user_state import UserStateRepository


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sync_configured_obsidian_locked(
    index_path: Path,
    state_path: Path,
    *,
    required: bool = False,
) -> ObsidianSyncResult | None:
    settings = load_app_settings(state_path)
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
    UserStateRepository(state_path).record_obsidian_sync(
        configured.vault_path,
        configured.folder,
        utc_now_iso(),
    )
    return result
