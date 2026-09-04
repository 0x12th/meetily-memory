from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Never

from meetily_memory.db.index_snapshot import (
    IndexSnapshotError,
    IndexSnapshotMetadata,
    remove_index_snapshot,
    validate_index_snapshot_database,
)
from meetily_memory.durable_files import fsync_directory
from meetily_memory.refresh_lock import RefreshLock
from meetily_memory.scanner.fresh_index import (
    FreshIndexResult,
    build_fresh_index,
    cleanup_fresh_index,
)
from meetily_memory.source_fingerprint import capture_source_fingerprint
from meetily_memory.user_state import UserStateRepository

SOURCE_KIND = "meetily_sqlite"


@dataclass(frozen=True)
class SourceSelection:
    source_uuid: str
    source_path: Path
    source_revision: int
    source_kind: str = SOURCE_KIND


@dataclass(frozen=True)
class PublishedIndex:
    index_path: Path
    source: SourceSelection
    meetings: int
    chunks: int
    fts_rows: int
    bytes: int
    changed: bool = True


@dataclass(frozen=True)
class RelocatedIndex:
    published: PublishedIndex
    previous_path: Path


class StaleSourceSelectionError(RuntimeError):
    pass


class PublicationDurabilityAmbiguousError(RuntimeError):
    """The destination was replaced, but directory durability could not be confirmed."""

    def __init__(self, index_path: Path, source: SourceSelection, cause: OSError) -> None:
        message = (
            f"Index publication at {index_path} contains the expected complete snapshot for "
            f"source {source.source_uuid} revision {source.source_revision}, but durable directory "
            "fsync failed. Do not restore or roll back the previous index. Verify the filesystem "
            "and rerun `mm refresh`; readers may safely use the validated destination."
        )
        super().__init__(message)
        self.index_path: Path = index_path
        self.source: SourceSelection = source
        self.fsync_error: OSError = cause


class PublicationStateUnknownError(RuntimeError):
    """Replacement happened, but the destination could not be proven exact after fsync failure."""


class IndexRemovalDurabilityAmbiguousError(RuntimeError):
    pass


def selected_source_from_state(state: UserStateRepository) -> SourceSelection:
    binding = state.get_selected_source_binding()
    if binding is None:
        message = "No active Meetily source is selected in state; select a source before refresh."
        raise ValueError(message)
    return _selection_from_binding(binding)


def source_from_state(state: UserStateRepository, source_uuid: str) -> SourceSelection:
    binding = state.get_source_binding(source_uuid)
    if binding is None:
        message = f"Source UUID not found in user state: {source_uuid}."
        raise ValueError(message)
    return _selection_from_binding(binding)


def refresh_index(
    index_path: Path,
    *,
    state_path: Path | None = None,
    force: bool = False,
) -> PublishedIndex:
    canonical = Path(index_path)
    with RefreshLock(canonical):
        state = UserStateRepository(state_path or canonical.with_name("state.sqlite"))
        return refresh_index_locked(canonical, state, force=force)


def refresh_index_locked(
    index_path: Path,
    state: UserStateRepository,
    *,
    force: bool = False,
) -> PublishedIndex:
    source = selected_source_from_state(state)
    if not force:
        unchanged = unchanged_published_index(index_path, state, source)
        if unchanged is not None:
            return unchanged
    candidate = build_fresh_index(
        selected_source_uuid=source.source_uuid,
        selected_source_path=source.source_path,
        selected_source_revision=source.source_revision,
        destination_directory=Path(index_path).parent,
    )
    return publish_index_candidate_locked(index_path, state, source, candidate)


def unchanged_published_index(
    index_path: Path,
    state: UserStateRepository,
    source: SourceSelection,
) -> PublishedIndex | None:
    canonical = Path(index_path)
    try:
        metadata = validate_index_snapshot_database(canonical)
    except (IndexSnapshotError, OSError, ValueError):
        return None
    _require_expected_snapshot(metadata, source)
    _require_current_source(state, source)
    stored_fingerprint = metadata["source_fingerprint"]
    if not isinstance(stored_fingerprint, str):
        return None
    if capture_source_fingerprint(source.source_path) != stored_fingerprint:
        return None
    return PublishedIndex(
        index_path=canonical,
        source=source,
        meetings=metadata["meetings"],
        chunks=metadata["chunks"],
        fts_rows=metadata["fts_rows"],
        bytes=canonical.stat().st_size,
        changed=False,
    )


def switch_selected_source_locked(
    index_path: Path,
    state: UserStateRepository,
    source_uuid: str,
) -> PublishedIndex:
    canonical = Path(index_path)
    source = source_from_state(state, source_uuid)
    candidate = build_fresh_index(
        selected_source_uuid=source.source_uuid,
        selected_source_path=source.source_path,
        selected_source_revision=source.source_revision,
        destination_directory=canonical.parent,
    )
    try:
        current = state.get_selected_source_binding()
        changing_source = (
            current is None or _selection_from_binding(current).source_uuid != source.source_uuid
        )
        if changing_source:
            durably_remove_index_locked(canonical)
            state.select_source(source.source_uuid)
        return publish_index_candidate_locked(canonical, state, source, candidate)
    except BaseException:
        cleanup_fresh_index(candidate)
        raise


def relocate_selected_source_locked(
    index_path: Path,
    state: UserStateRepository,
    source_uuid: str,
    new_path: Path,
    *,
    now: str,
) -> RelocatedIndex:
    canonical = Path(index_path)
    previous = source_from_state(state, source_uuid)
    if previous.source_path == Path(new_path).resolve(strict=True):
        published = refresh_index_locked(canonical, state)
        return RelocatedIndex(published=published, previous_path=previous.source_path)

    state.validate_source_path_claim(source_uuid, previous.source_kind, new_path)
    next_source = SourceSelection(
        source_uuid=source_uuid,
        source_path=Path(new_path).resolve(strict=True),
        source_revision=previous.source_revision + 1,
        source_kind=previous.source_kind,
    )
    durably_remove_index_locked(canonical)
    revision = state.relocate_selected_source(
        source_uuid,
        previous.source_kind,
        str(previous.source_path),
        previous.source_revision,
        next_source.source_path,
        now=now,
    )
    if revision != next_source.source_revision:
        _raise_stale("State returned an unexpected source revision after relocation.")

    candidate = build_fresh_index(
        selected_source_uuid=next_source.source_uuid,
        selected_source_path=next_source.source_path,
        selected_source_revision=next_source.source_revision,
        destination_directory=canonical.parent,
    )
    try:
        published = publish_index_candidate_locked(canonical, state, next_source, candidate)
    except BaseException:
        cleanup_fresh_index(candidate)
        raise
    return RelocatedIndex(published=published, previous_path=previous.source_path)


def publish_index_candidate_locked(
    index_path: Path,
    state: UserStateRepository,
    source: SourceSelection,
    candidate: FreshIndexResult | Path,
) -> PublishedIndex:
    canonical = Path(index_path)
    candidate_path = (
        candidate.candidate_path if isinstance(candidate, FreshIndexResult) else Path(candidate)
    )
    if candidate_path.parent.resolve() != canonical.parent.resolve():
        message = "Index candidate must be created in the canonical index directory."
        raise ValueError(message)

    replaced = False
    try:
        validated = validate_index_snapshot_database(candidate_path)
        _require_expected_snapshot(validated, source)
        _require_current_source(state, source)
        _require_clean_canonical_sidecars(canonical)
        os.replace(candidate_path, canonical)  # noqa: PTH105
        replaced = True
        try:
            fsync_directory(canonical.parent)
        except OSError as exc:
            try:
                destination = validate_index_snapshot_database(canonical)
                _require_expected_snapshot(destination, source)
            except (IndexSnapshotError, StaleSourceSelectionError, OSError, ValueError) as check:
                message = (
                    f"Index replacement at {canonical} completed, directory fsync failed, and the "
                    "destination could not be proven to be the expected snapshot. Do not restore "
                    "the previous index; inspect the destination and rerun `mm refresh`."
                )
                raise PublicationStateUnknownError(message) from check
            raise PublicationDurabilityAmbiguousError(canonical, source, exc) from exc
    finally:
        if not replaced and candidate_path.exists():
            cleanup_fresh_index(candidate_path)

    final = validate_index_snapshot_database(canonical)
    _require_expected_snapshot(final, source)
    return PublishedIndex(
        index_path=canonical,
        source=source,
        meetings=final["meetings"],
        chunks=final["chunks"],
        fts_rows=final["fts_rows"],
        bytes=canonical.stat().st_size,
    )


def durably_remove_index_locked(index_path: Path) -> None:
    canonical = Path(index_path)
    remove_index_snapshot(canonical)
    try:
        fsync_directory(canonical.parent)
    except OSError as exc:
        message = (
            f"The old canonical index at {canonical} was unlinked, but directory fsync failed. "
            "The source state was not changed. Do not recreate the old index in place; verify the "
            "filesystem and retry the source operation."
        )
        raise IndexRemovalDurabilityAmbiguousError(message) from exc


def _selection_from_binding(binding: dict[str, object]) -> SourceSelection:
    current_path = str(binding["current_path"])
    kind = str(binding["kind"])
    if kind != SOURCE_KIND:
        message = f"Source UUID {binding['uuid']} has unsupported source kind {kind!r}."
        raise ValueError(message)
    return SourceSelection(
        source_uuid=str(binding["uuid"]),
        source_path=Path(current_path).resolve(strict=True),
        source_revision=_required_int(binding, "revision"),
        source_kind=kind,
    )


def _require_current_source(state: UserStateRepository, source: SourceSelection) -> None:
    if state.source_binding_is_current(
        source.source_uuid,
        source.source_kind,
        str(source.source_path),
        source.source_revision,
    ):
        selected = state.get_selected_source_binding()
        if (
            selected is not None
            and _selection_from_binding(selected).source_uuid == source.source_uuid
        ):
            return
    message = " ".join(
        (
            f"Selected source {source.source_uuid} revision {source.source_revision} changed while",
            "the fresh index candidate was being built; the candidate was not published.",
        )
    )
    _raise_stale(message)


def _require_expected_snapshot(
    metadata: IndexSnapshotMetadata,
    source: SourceSelection,
) -> None:
    actual = (
        str(metadata["source_uuid"]),
        str(Path(str(metadata["source_path"])).resolve()),
        metadata["source_revision"],
    )
    expected = (
        source.source_uuid,
        str(source.source_path.resolve()),
        source.source_revision,
    )
    if actual != expected:
        message = " ".join(
            (
                "Candidate source metadata does not match the selected state token",
                f"(candidate={actual!r}, selected={expected!r}).",
            )
        )
        _raise_stale(message)


def _require_clean_canonical_sidecars(canonical: Path) -> None:
    sidecars = tuple(
        canonical.with_name(canonical.name + suffix)
        for suffix in ("-wal", "-shm", "-journal")
        if canonical.with_name(canonical.name + suffix).exists()
    )
    if not sidecars:
        return
    names = ", ".join(sidecar.name for sidecar in sidecars)
    message = (
        f"Refusing to replace canonical index while SQLite sidecars exist: {names}. "
        "Close writers and rerun `mm refresh`; the current index was not changed."
    )
    raise RuntimeError(message)


def _required_int(values: dict[str, object], key: str) -> int:
    value = values[key]
    if not isinstance(value, int):
        message = f"State source {key} is not an integer."
        raise TypeError(message)
    return value


def _raise_stale(message: str) -> Never:
    raise StaleSourceSelectionError(message)
