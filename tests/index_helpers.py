from pathlib import Path

from meetily_memory.refresh import PublishedIndex, switch_selected_source_locked
from meetily_memory.refresh_lock import RefreshLock
from meetily_memory.user_state import UserStateRepository


def publish_fresh_index(
    index_path: Path,
    source_path: Path,
    *,
    state_path: Path | None = None,
) -> PublishedIndex:
    canonical_index = Path(index_path)
    with RefreshLock(canonical_index):
        state = UserStateRepository(state_path or canonical_index.with_name("state.sqlite"))
        source_uuid = state.resolve_source(
            "meetily_sqlite",
            Path(source_path).resolve(strict=True),
            now="2026-08-31T00:00:00Z",
        )
        return switch_selected_source_locked(canonical_index, state, source_uuid)
