import hashlib
import shutil
import sqlite3
import tempfile
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from meetily_memory.config.paths import canonical_source_path
from meetily_memory.db.migration_identity import canonical_database_path
from meetily_memory.db.migrations import (
    CURRENT_SCHEMA_VERSION,
    INDEX_ALIAS_OWNER_LEGACY,
    INDEX_ALIAS_OWNER_STATE,
    read_index_generation_marker,
)
from meetily_memory.db.schema import IndexProjectionCleanupError, IndexReadError
from meetily_memory.durable_files import durable_replace
from meetily_memory.json_codec import dumps_json, dumps_json_bytes, loads_json
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.repositories.records import ChunkRecord, MeetingRecord, PostPublishIssue
from meetily_memory.scanner.sqlite_source import readonly_sqlite_connection
from meetily_memory.structure_analyzer import StructureAnalyzer
from meetily_memory.user_state import (
    AmbiguousSourceIdentityError,
    SourcePathClaim,
    UserStateFileIdentity,
    UserStateRepository,
    find_existing_source_by_uuid,
    pin_user_state_identity,
    recover_and_validate_index,
)

MEETING_NORMALIZATION_VERSION = 2


def _rebuild_checkpoint(_name: str) -> None:
    return


def _scan_checkpoint(_name: str) -> None:
    return


def previous_index_backup_path(index_path: Path) -> Path:
    path = Path(index_path)
    return path.with_name(f"{path.name}.pre-v{CURRENT_SCHEMA_VERSION}")


@dataclass(frozen=True)
class IndexFileIdentity:
    physical_path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class CurrentIndexPreflight:
    state_reader: UserStateRepository | None
    state_identity: UserStateFileIdentity
    index_identity: IndexFileIdentity


def require_index_identity(
    index_path: Path,
    *,
    expected: IndexFileIdentity | None = None,
) -> IndexFileIdentity:
    try:
        physical_path = Path(index_path).resolve(strict=True)
        path_stat = physical_path.stat()
    except OSError as exc:
        message = f"Meetily Memory index no longer matches the preflighted database: {index_path}."
        raise IndexReadError(message) from exc
    if not physical_path.is_file():
        message = f"Meetily Memory index is not a regular file: {index_path}."
        raise IndexReadError(message)
    identity = IndexFileIdentity(physical_path, path_stat.st_dev, path_stat.st_ino)
    if expected is not None and identity != expected:
        message = f"Meetily Memory index no longer matches the preflighted database: {index_path}."
        raise IndexReadError(message)
    return identity


def preflight_current_index_user_state(
    index_path: Path,
    state_path: Path,
) -> CurrentIndexPreflight:
    index_identity = require_index_identity(index_path)
    with readonly_sqlite_connection(index_identity.physical_path) as conn:
        marker = read_index_generation_marker(conn)
        sources = conn.execute(
            "SELECT source_uuid, kind, path FROM sources ORDER BY source_uuid"
        ).fetchall()
    require_index_identity(index_path, expected=index_identity)

    alias_owner = marker[1] if marker is not None else INDEX_ALIAS_OWNER_LEGACY
    state_identity = pin_user_state_identity(state_path)
    if alias_owner == INDEX_ALIAS_OWNER_STATE:
        state_reader = UserStateRepository.open_existing(
            state_path,
            expected_identity=state_identity,
        )
    else:
        try:
            state_reader = UserStateRepository.open_existing(
                state_path,
                expected_identity=state_identity,
            )
        except IndexReadError:
            state_reader = None

    if marker is not None and alias_owner == INDEX_ALIAS_OWNER_STATE:
        generation_id = marker[0]
        if state_reader is None or not state_reader.has_index_generation(
            generation_id,
            Path(canonical_database_path(index_path)),
            INDEX_ALIAS_OWNER_STATE,
        ):
            message = (
                "Current state-owned index generation does not match the authoritative user "
                f"state at {state_path}: missing ledger for generation {generation_id}, index "
                f"{canonical_database_path(index_path)}. Restore the authoritative `state.sqlite` "
                "from backup; refusing to resolve sources or write the index."
            )
            raise IndexReadError(message)

    for source in sources:
        source_uuid = str(source["source_uuid"])
        kind = str(source["kind"])
        index_path_value = str(source["path"])
        binding = (
            state_reader.get_source_binding(source_uuid)
            if state_reader is not None
            else find_existing_source_by_uuid(
                state_path,
                source_uuid,
                expected_identity=state_identity,
            )
        )
        if binding is not None and str(binding["kind"]) == kind:
            current_path = str(binding["current_path"])
            projected_path = binding["projected_path"]
            pending_revision = binding["pending_revision"]
            if index_path_value == current_path or (
                pending_revision is not None
                and projected_path is not None
                and index_path_value == str(projected_path)
            ):
                continue
        message = (
            "Current index source projection does not match the authoritative user state at "
            f"{state_path}: UUID {source_uuid}, kind {kind}, path {index_path_value}. Restore the "
            "authoritative `state.sqlite` from backup; refusing to resolve sources or write the "
            "index."
        )
        raise IndexReadError(message)
    pin_user_state_identity(state_path, expected=state_identity)
    return CurrentIndexPreflight(state_reader, state_identity, index_identity)


@dataclass
class ScanResult:
    run_id: int = 0
    source_id: int = 0
    source_uuid: str = ""
    meetings_seen: int = 0
    meetings_inserted: int = 0
    meetings_updated: int = 0
    meetings_analyzed: int = 0
    chunks_seen: int = 0
    chunks_inserted: int = 0
    chunks_updated: int = 0


@dataclass(frozen=True)
class RebuildSource:
    source_uuid: str
    source_path: Path
    source_revision: int
    projected_path: str
    pending_revision: int | None
    requested: bool


class SourcePathProjectionFinalizeError(RuntimeError):
    """The index committed, but pending source binding finalization did not."""


class MeetilySQLiteScanner:
    source_kind = "meetily_sqlite"

    def __init__(self, index_path: Path, *, state_path: Path | None = None) -> None:
        self.index_path = Path(index_path)
        self.state_path = (
            Path(state_path)
            if state_path is not None
            else self.index_path.with_name("state.sqlite")
        )

    def scan(
        self,
        source_path: Path,
        *,
        force: bool = False,
        analyze: bool = True,
        finalize: bool = True,
    ) -> ScanResult:
        source_path = canonical_source_path(source_path)
        started_at = utc_now()
        index_version = recover_and_validate_index(self.index_path)
        current_preflight = (
            preflight_current_index_user_state(self.index_path, self.state_path)
            if index_version == CURRENT_SCHEMA_VERSION
            else None
        )
        with readonly_sqlite_connection(source_path) as conn:
            validate_meetily_schema(conn)
            if current_preflight is not None:
                pin_user_state_identity(
                    self.state_path,
                    expected=current_preflight.state_identity,
                )
                if current_preflight.state_reader is not None:
                    current_preflight.state_reader.recheck_identity()
                    user_state = current_preflight.state_reader.open_existing_writer()
                else:
                    user_state = UserStateRepository.open_existing_migration_writer(
                        self.state_path,
                        expected_identity=current_preflight.state_identity,
                    )
                user_state.recheck_identity()
            else:
                user_state = UserStateRepository(self.state_path)
            rebuild_sources = self._preflight_legacy_rebuild_sources(source_path, started_at)
            if current_preflight is not None:
                user_state.recheck_identity()
                require_index_identity(
                    self.index_path,
                    expected=current_preflight.index_identity,
                )
            repo = IndexRepository(
                self.index_path,
                state_path=self.state_path,
                _user_state=user_state,
            )
            if repo.requires_rebuild:
                if rebuild_sources is None:
                    rebuild_sources = self._prepare_rebuild_sources(source_path, started_at)
                return self._rebuild_index(
                    conn,
                    started_at,
                    rebuild_sources,
                    force=force,
                )
            source_uuid = repo.user_state.resolve_source(
                self.source_kind,
                source_path,
                now=started_at,
            )
            result = self._scan_repository(
                conn,
                repo,
                source_path,
                source_uuid,
                started_at,
                force=force,
                analyze=analyze,
            )
        if finalize:
            self._discard_confirmed_backup()
        return result

    def _scan_repository(  # noqa: PLR0913, PLR0915
        self,
        conn: Any,
        repo: IndexRepository,
        source_path: Path,
        source_uuid: str,
        started_at: str,
        *,
        force: bool,
        analyze: bool,
        heal_pending_paths: bool = True,
    ) -> ScanResult:
        result = ScanResult(source_uuid=source_uuid)
        source_id, run_id = repo.begin_source_scan(source_uuid, started_at)
        result.source_id = source_id
        result.run_id = run_id
        phase = "source_scan"
        seen_external_ids: set[str] = set()
        structure_analyzer = StructureAnalyzer(repo)
        pending_claims: tuple[SourcePathClaim, ...] = ()

        try:
            _scan_checkpoint("after_running")
            with repo.projection_transaction() as projection:
                if heal_pending_paths:
                    pending_claims = repo.project_pending_source_path_projections(projection)
                    _scan_checkpoint("after_pending_path_projection")
                source_id = repo.upsert_source(
                    source_uuid,
                    self.source_kind,
                    str(source_path),
                    started_at,
                    connection=projection,
                )
                result.source_id = source_id
                for meeting_number, upstream in enumerate(self._read_meetings(conn), start=1):
                    _scan_checkpoint(f"before_meeting:{meeting_number}")
                    result.meetings_seen += 1
                    meeting, chunks = normalize_meeting(
                        source_id,
                        source_path,
                        upstream,
                        utc_now(),
                    )
                    result.chunks_seen += len(chunks)
                    existing = repo.get_meeting_by_source_id(
                        source_id,
                        meeting.external_id,
                        connection=projection,
                    )
                    meeting_id, updated, inserted_chunks = repo.upsert_meeting_with_chunks(
                        meeting,
                        chunks,
                        force=force,
                        connection=projection,
                    )
                    if analyze and (existing is None or updated):
                        structure_analyzer.analyze_meeting(
                            meeting_id,
                            connection=projection,
                        )
                        result.meetings_analyzed += 1
                    result.chunks_inserted += inserted_chunks
                    if existing is None:
                        result.meetings_inserted += 1
                    elif updated:
                        result.meetings_updated += 1
                        result.chunks_updated += inserted_chunks
                    seen_external_ids.add(meeting.external_id)
                phase = "reconciliation"
                _scan_checkpoint("before_reconciliation")
                repo.reconcile_source_meetings(
                    source_id,
                    seen_external_ids,
                    connection=projection,
                )
                phase = "topic_alias_projection"
                _scan_checkpoint("before_topic_alias_projection")
                repo.project_topic_aliases(connection=projection)
                phase = "publishing"
                repo.complete_scan_run(
                    result.run_id,
                    utc_now(),
                    result,
                    connection=projection,
                )
                _scan_checkpoint("before_publish")
        except IndexProjectionCleanupError:
            issues = [self._post_publish_issue("index_cleanup", source_uuid, source_path)]
            claims_finalized = self._try_finalize_published_source_path_claims(
                repo,
                pending_claims,
            )
            if claims_finalized:
                self._resolve_successful_post_publish_phases(
                    repo,
                    source_uuid,
                    pending_claims,
                    index_cleanup=False,
                )
            else:
                issues.append(
                    self._post_publish_issue("source_path_finalize", source_uuid, source_path)
                )
            repo.record_post_publish_failure(result.run_id, tuple(issues))
            raise
        except BaseException as exc:
            repo.fail_scan_run(
                result.run_id,
                utc_now(),
                phase,
                result,
                type(exc).__name__,
            )
            raise
        claims_finalized = self._try_finalize_published_source_path_claims(repo, pending_claims)
        if not claims_finalized:
            repo.record_post_publish_failure(
                result.run_id,
                (self._post_publish_issue("source_path_finalize", source_uuid, source_path),),
            )
            message = (
                "Index projection committed, but source binding finalization failed. "
                "Rerun refresh to retry the state handoff."
            )
            raise SourcePathProjectionFinalizeError(message) from None
        self._resolve_successful_post_publish_phases(
            repo,
            source_uuid,
            pending_claims,
            index_cleanup=True,
        )
        return result

    def _try_finalize_published_source_path_claims(
        self,
        repo: IndexRepository,
        claims: tuple[SourcePathClaim, ...],
    ) -> bool:
        try:
            self._finalize_published_source_path_claims(repo, claims)
        except BaseException:  # noqa: BLE001
            return False
        return True

    def _finalize_published_source_path_claims(
        self,
        repo: IndexRepository,
        claims: tuple[SourcePathClaim, ...],
    ) -> None:
        if claims and not repo.user_state.finalize_source_path_claims(claims):
            message = "Pending source path claims changed after index publication."
            raise RuntimeError(message)

    def _resolve_successful_post_publish_phases(
        self,
        repo: IndexRepository,
        source_uuid: str,
        claims: tuple[SourcePathClaim, ...],
        *,
        index_cleanup: bool,
    ) -> None:
        finalized_source_uuids = {claim.source_uuid for claim in claims}
        finalized_source_uuids.add(source_uuid)
        for finalized_source_uuid in sorted(finalized_source_uuids):
            phases = ["source_path_finalize"]
            if index_cleanup and finalized_source_uuid == source_uuid:
                phases.insert(0, "index_cleanup")
            repo.resolve_post_publish_failures(finalized_source_uuid, tuple(phases))

    def _post_publish_issue(
        self,
        phase: str,
        source_uuid: str,
        source_path: Path,
    ) -> PostPublishIssue:
        error_types = {
            "index_cleanup": "IndexProjectionCleanupError",
            "source_path_finalize": "SourcePathProjectionFinalizeError",
        }
        actions = {
            "index_cleanup": "Retry refresh to clean transient SQLite files.",
            "source_path_finalize": "Retry refresh to finalize pending source bindings.",
        }
        return PostPublishIssue(
            phase=phase,
            error_type=error_types[phase],
            action=actions[phase],
            source_uuid=source_uuid,
            source_path=str(source_path),
            retry_command=(
                "mm",
                "--index",
                str(self.index_path),
                "refresh",
                "--source",
                str(source_path),
            ),
        )

    def _preflight_legacy_rebuild_sources(
        self,
        source_path: Path,
        started_at: str,
    ) -> tuple[RebuildSource, ...] | None:
        if not self.index_path.is_file():
            return None
        with readonly_sqlite_connection(self.index_path) as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version <= 0 or version >= CURRENT_SCHEMA_VERSION:
                return None
            sources_table = conn.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'sources'
                """
            ).fetchone()
            if sources_table is None:
                message = "Legacy index is missing the required sources table."
                raise RuntimeError(message)
            source_count = int(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0])
        if source_count == 0:
            return None
        return self._prepare_rebuild_sources(source_path, started_at)

    def _rebuild_index(
        self,
        source_conn: Any,
        started_at: str,
        rebuild_sources: tuple[RebuildSource, ...],
        *,
        force: bool,
    ) -> ScanResult:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=self.index_path.parent,
            prefix=f".{self.index_path.name}.",
            suffix=".rebuild",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
        temporary_path.unlink()
        try:
            rebuilt_repo = IndexRepository(
                temporary_path,
                state_path=self.state_path,
                generation_ledger_paths=(self.index_path,),
            )
            if rebuilt_repo.requires_rebuild:
                message = "Fresh side-by-side index unexpectedly requires another rebuild."
                raise RuntimeError(message)
            requested_result: ScanResult | None = None
            for rebuild_source in rebuild_sources:
                if rebuild_source.requested:
                    result = self._scan_repository(
                        source_conn,
                        rebuilt_repo,
                        rebuild_source.source_path,
                        rebuild_source.source_uuid,
                        started_at,
                        force=force,
                        analyze=True,
                        heal_pending_paths=False,
                    )
                    requested_result = result
                    continue
                with readonly_sqlite_connection(rebuild_source.source_path) as rebuild_conn:
                    validate_meetily_schema(rebuild_conn)
                    self._scan_repository(
                        rebuild_conn,
                        rebuilt_repo,
                        rebuild_source.source_path,
                        rebuild_source.source_uuid,
                        started_at,
                        force=force,
                        analyze=True,
                        heal_pending_paths=False,
                    )
            if requested_result is None:
                message = "The requested source was not included in the rebuilt index."
                raise RuntimeError(message)
            rebuilt_repo.project_topic_aliases()
            self._verify_rebuilt_index(temporary_path, rebuild_sources)
            _rebuild_checkpoint("before_backup")
            with self._quiesced_index_for_swap() as active_conn:
                self._write_backup(active_conn)
                _rebuild_checkpoint("before_replace")
                rebuilt_repo.project_topic_aliases()
                self._verify_rebuilt_index(temporary_path, rebuild_sources)
                self._verify_quiesced_index_family(active_conn)
                durable_replace(temporary_path, self.index_path)
            self._finalize_rebuilt_source_bindings(rebuild_sources)
            return requested_result
        finally:
            temporary_path.unlink(missing_ok=True)

    def _prepare_rebuild_sources(
        self,
        requested_path: Path,
        started_at: str,
    ) -> tuple[RebuildSource, ...]:
        with readonly_sqlite_connection(self.index_path) as conn:
            legacy_sources = conn.execute(
                "SELECT id, kind, path FROM sources ORDER BY id"
            ).fetchall()

        state = UserStateRepository(self.state_path)
        rebuild_sources: list[RebuildSource] = []
        source_paths: dict[Path, int] = {}
        mapped_source_ids: dict[str, int] = {}
        for row in legacy_sources:
            source_id = int(row["id"])
            kind = str(row["kind"])
            raw_path = str(row["path"])
            if kind != self.source_kind:
                message = f"Legacy source {source_id} has unsupported kind: {kind}."
                raise RuntimeError(message)
            rebuild_source = self._legacy_rebuild_source(
                state,
                source_id,
                kind,
                raw_path,
                requested_path,
            )
            if rebuild_source.source_path in source_paths:
                other_id = source_paths[rebuild_source.source_path]
                message = (
                    "State source paths are ambiguous after canonicalization: "
                    f"sources {other_id} and {source_id} both resolve to "
                    f"{rebuild_source.source_path}."
                )
                raise RuntimeError(message)
            if rebuild_source.source_uuid in mapped_source_ids:
                other_id = mapped_source_ids[rebuild_source.source_uuid]
                message = (
                    f"Legacy sources {other_id} and {source_id} map to the same state UUID "
                    f"{rebuild_source.source_uuid}; automatic source merging is not allowed."
                )
                raise RuntimeError(message)
            source_paths[rebuild_source.source_path] = source_id
            mapped_source_ids[rebuild_source.source_uuid] = source_id
            rebuild_sources.append(rebuild_source)

        if requested_path not in source_paths:
            self._validate_rebuild_source(requested_path, label="requested source")
            requested_uuid = state.resolve_source(
                self.source_kind,
                requested_path,
                now=started_at,
            )
            requested_binding = state.get_source_binding(requested_uuid)
            if requested_binding is None:
                message = "The requested source disappeared after state registration."
                raise RuntimeError(message)
            rebuild_sources.append(
                RebuildSource(
                    source_uuid=requested_uuid,
                    source_path=requested_path,
                    source_revision=int(requested_binding["revision"]),
                    projected_path=str(
                        requested_binding["projected_path"] or requested_binding["current_path"]
                    ),
                    pending_revision=(
                        int(requested_binding["pending_revision"])
                        if requested_binding["pending_revision"] is not None
                        else None
                    ),
                    requested=True,
                )
            )

        requested_sources = [source for source in rebuild_sources if source.requested]
        if len(requested_sources) != 1:
            message = "The requested source is ambiguous in the legacy index snapshot."
            raise RuntimeError(message)

        return tuple(
            [source for source in rebuild_sources if not source.requested] + requested_sources
        )

    def _legacy_rebuild_source(
        self,
        state: UserStateRepository,
        source_id: int,
        kind: str,
        raw_path: str,
        requested_path: Path,
    ) -> RebuildSource:
        state_uuid_hints = self._state_uuid_hints(state, kind, raw_path)
        state_source = state.get_source_for_index_projection(kind, raw_path)
        pending_revision = (
            int(state_source["pending_revision"])
            if state_source is not None and state_source["pending_revision"] is not None
            else None
        )
        if state_source is None or pending_revision is None:
            legacy_path = self._validated_rebuild_source_path(
                source_id,
                raw_path,
                state_uuid_hints,
            )
            canonical_state_source = self._rebuild_state_source(
                state,
                source_id,
                kind,
                legacy_path,
                state_uuid_hints,
            )
            if state_source is not None and str(state_source["uuid"]) != str(
                canonical_state_source["uuid"]
            ):
                message = f"Legacy source {source_id} has conflicting state path bindings."
                raise RuntimeError(message)
            state_source = canonical_state_source

        mapped_uuid = str(state_source["uuid"])
        projected_path = str(state_source["projected_path"] or state_source["current_path"])
        pending_revision = (
            int(state_source["pending_revision"])
            if state_source["pending_revision"] is not None
            else None
        )
        if pending_revision is not None and raw_path not in {
            projected_path,
            str(state_source["current_path"]),
        }:
            message = (
                f"Legacy source {source_id} no longer matches pending state projection "
                f"for UUID {mapped_uuid}."
            )
            raise RuntimeError(message)
        source_path = self._validated_state_source_path(source_id, state_source)
        return RebuildSource(
            source_uuid=mapped_uuid,
            source_path=source_path,
            source_revision=int(state_source["revision"]),
            projected_path=projected_path,
            pending_revision=pending_revision,
            requested=source_path == requested_path,
        )

    def _state_uuid_hints(
        self,
        state: UserStateRepository,
        kind: str,
        raw_path: str,
    ) -> tuple[str, ...]:
        raw_state_sources = state.get_sources_by_path(kind, raw_path)
        projected_state_sources = state.get_sources_by_projected_path(kind, raw_path)
        diagnostic_uuids = [
            str(source["uuid"]) for source in (*raw_state_sources, *projected_state_sources)
        ]
        try:
            resolved_path_hint = canonical_source_path(Path(raw_path))
        except (OSError, RuntimeError):
            return tuple(diagnostic_uuids)
        canonical_string = str(resolved_path_hint)
        canonical_state_sources = state.get_sources_by_path(kind, canonical_string)
        canonical_projected_sources = state.get_sources_by_projected_path(kind, canonical_string)
        diagnostic_uuids.extend(
            str(source["uuid"])
            for source in (*canonical_state_sources, *canonical_projected_sources)
        )
        return tuple(dict.fromkeys(diagnostic_uuids))

    def _rebuild_state_source(
        self,
        state: UserStateRepository,
        source_id: int,
        kind: str,
        legacy_path: Path,
        state_uuid_hints: tuple[str, ...],
    ) -> dict[str, Any]:
        try:
            state_source = state.get_source_by_canonical_path(kind, legacy_path)
        except AmbiguousSourceIdentityError as exc:
            claim_uuids = exc.source_uuids or state_uuid_hints
            uuid_label = ", ".join(claim_uuids) or "unmapped"
            message = (
                f"Legacy source {source_id} path {legacy_path} has unsafe state claims "
                f"for UUID(s): {uuid_label}. {exc} "
                f"{self._source_rebind_guidance(claim_uuids, legacy_path)}"
            )
            raise AmbiguousSourceIdentityError(
                message,
                source_uuids=claim_uuids,
            ) from exc
        if state_source is None:
            message = (
                f"Legacy source {source_id} path {legacy_path} is not mapped by an exact "
                "canonical state-owned UUID binding. "
                f"{self._source_rebind_guidance(state_uuid_hints, legacy_path)}"
            )
            raise RuntimeError(message)
        return state_source

    def _validated_state_source_path(
        self,
        source_id: int,
        state_source: dict[str, Any],
    ) -> Path:
        raw_path = str(state_source["current_path"])
        try:
            source_path = canonical_source_path(Path(raw_path))
        except (OSError, RuntimeError) as exc:
            message = (
                f"State source UUID {state_source['uuid']} for legacy source {source_id} "
                f"is unavailable: {raw_path}."
            )
            raise RuntimeError(message) from exc
        if raw_path != str(source_path):
            message = (
                f"State source UUID {state_source['uuid']} has a non-canonical current path: "
                f"{raw_path!r}."
            )
            raise RuntimeError(message)
        self._validate_rebuild_source(source_path, label=f"state source {source_id}")
        return source_path

    def _validated_rebuild_source_path(
        self,
        source_id: int,
        raw_path: str,
        state_uuids: tuple[str, ...],
    ) -> Path:
        uuid_label = ", ".join(state_uuids) or "unmapped"
        try:
            source_path = canonical_source_path(Path(raw_path))
        except (OSError, RuntimeError) as exc:
            message = (
                f"Legacy source {source_id} path {raw_path!r} is unavailable; "
                f"state UUID(s): {uuid_label}. "
                f"{self._source_rebind_guidance(state_uuids, None)}"
            )
            raise RuntimeError(message) from exc
        if raw_path != str(source_path):
            message = (
                f"Legacy source {source_id} path {raw_path!r} is non-canonical and currently "
                f"resolves to {source_path}; state UUID(s): {uuid_label}. "
                f"{self._source_rebind_guidance(state_uuids, source_path)}"
            )
            raise RuntimeError(message)
        self._validate_rebuild_source(source_path, label=f"legacy source {source_id}")
        return source_path

    def _source_rebind_guidance(
        self,
        source_uuids: tuple[str, ...],
        source_path: Path | None,
    ) -> str:
        path_argument = f'"{source_path}"' if source_path is not None else "NEW_CANONICAL_PATH"
        if not source_uuids:
            return (
                f"Run `mm config source {path_argument}` to register a new source identity, "
                "or use explicit `--rebind --source-uuid UUID` to repair an existing identity."
            )
        source_uuid = source_uuids[0] if len(source_uuids) == 1 else "UUID"
        return f"Run `mm config source {path_argument} --rebind --source-uuid {source_uuid}`."

    def _validate_rebuild_source(self, source_path: Path, *, label: str) -> None:
        try:
            with readonly_sqlite_connection(source_path) as conn:
                validate_meetily_schema(conn)
        except (FileNotFoundError, RuntimeError, sqlite3.Error) as exc:
            message = f"{label.capitalize()} is unavailable or invalid: {source_path}."
            raise RuntimeError(message) from exc

    def _verify_state_topic_aliases(self, rebuilt_path: Path) -> None:
        expected = [
            (
                alias.topic_stable_key,
                alias.topic_title,
                alias.topic_normalized_title,
                alias.topic_created_at,
                alias.topic_updated_at,
                alias.topic_raw_metadata_json,
                alias.alias,
                alias.normalized_alias,
                alias.alias_created_at,
            )
            for alias in UserStateRepository(self.state_path).list_topic_aliases()
        ]
        with sqlite3.connect(rebuilt_path) as conn:
            actual = conn.execute(
                """
                SELECT
                  n.stable_key, n.title, n.normalized_title,
                  n.created_at, n.updated_at, n.raw_metadata_json,
                  a.alias, a.normalized_alias, a.created_at
                FROM topic_aliases a
                JOIN knowledge_nodes n ON n.id = a.topic_node_id
                ORDER BY a.normalized_alias
                """
            ).fetchall()
        if actual != expected:
            message = "Rebuilt index does not exactly project state-owned topic aliases."
            raise RuntimeError(message)

    def _finalize_rebuilt_source_bindings(
        self,
        rebuild_sources: tuple[RebuildSource, ...],
    ) -> None:
        state = UserStateRepository(self.state_path)
        claims = []
        for source in rebuild_sources:
            if source.pending_revision is None:
                continue
            claim = state.get_pending_source_path_claim(source.source_uuid)
            if (
                claim is None
                or claim.claimed_revision != source.pending_revision
                or claim.claimed_path != str(source.source_path)
                or claim.projected_path != source.projected_path
            ):
                message = (
                    f"Pending source binding for UUID {source.source_uuid} changed before "
                    "rebuilt projection finalization."
                )
                raise RuntimeError(message)
            claims.append(claim)
        if claims and not state.finalize_source_path_claims(tuple(claims)):
            message = "Pending source bindings changed before atomic rebuilt finalization."
            raise RuntimeError(message)

    def _verify_rebuilt_index(
        self,
        rebuilt_path: Path,
        rebuild_sources: tuple[RebuildSource, ...],
    ) -> None:
        expected_sources = sorted(
            (
                source.source_uuid,
                self.source_kind,
                str(source.source_path),
            )
            for source in rebuild_sources
        )
        with sqlite3.connect(rebuilt_path) as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            actual_sources = conn.execute(
                "SELECT source_uuid, kind, path FROM sources ORDER BY source_uuid"
            ).fetchall()
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if version != CURRENT_SCHEMA_VERSION:
            message = f"Rebuilt index has schema {version}, expected {CURRENT_SCHEMA_VERSION}."
            raise RuntimeError(message)
        if actual_sources != expected_sources:
            message = "Rebuilt index does not contain the complete state-owned source snapshot."
            raise RuntimeError(message)
        state = UserStateRepository(self.state_path)
        for source in rebuild_sources:
            state_source = state.get_source_binding(source.source_uuid)
            if (
                state_source is None
                or str(state_source["current_path"]) != str(source.source_path)
                or int(state_source["revision"]) != source.source_revision
                or str(state_source["projected_path"] or state_source["current_path"])
                != source.projected_path
                or (
                    int(state_source["pending_revision"])
                    if state_source["pending_revision"] is not None
                    else None
                )
                != source.pending_revision
            ):
                message = "Registered source changed while rebuilding the index."
                raise RuntimeError(message)
        self._verify_state_topic_aliases(rebuilt_path)
        if integrity != "ok" or foreign_key_errors:
            message = "Rebuilt index failed SQLite integrity verification."
            raise RuntimeError(message)

    @contextmanager
    def _quiesced_index_for_swap(self) -> Generator[sqlite3.Connection, None, None]:
        try:
            source_conn = sqlite3.connect(self.index_path, timeout=0)
        except sqlite3.Error as exc:
            message = "Active index could not be opened for a safe side-by-side replacement."
            raise RuntimeError(message) from exc
        try:
            try:
                source_conn.execute("PRAGMA busy_timeout=0")
                journal_mode = str(source_conn.execute("PRAGMA journal_mode").fetchone()[0])
                if journal_mode.casefold() == "wal":
                    checkpoint = source_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if (
                        checkpoint is None
                        or int(checkpoint[0]) != 0
                        or int(checkpoint[1]) != int(checkpoint[2])
                    ):
                        message = (
                            "Active index WAL could not be checkpointed completely; "
                            "side-by-side replacement was aborted."
                        )
                        raise RuntimeError(message)
                selected_mode = str(source_conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0])
                if selected_mode.casefold() != "delete":
                    message = (
                        "Active index did not enter DELETE journal mode; side-by-side "
                        "replacement was aborted."
                    )
                    raise RuntimeError(message)
                source_conn.execute("BEGIN EXCLUSIVE")
                self._verify_quiesced_index_family(source_conn)
            except sqlite3.Error as exc:
                message = (
                    "Active index could not be quiesced under exclusive SQLite control; "
                    "side-by-side replacement was aborted."
                )
                raise RuntimeError(message) from exc
            yield source_conn
        finally:
            if source_conn.in_transaction:
                source_conn.rollback()
            source_conn.close()

    def _verify_quiesced_index_family(self, source_conn: sqlite3.Connection) -> None:
        if not source_conn.in_transaction:
            message = "Active index replacement lost its exclusive SQLite transaction."
            raise RuntimeError(message)
        journal_mode = str(source_conn.execute("PRAGMA journal_mode").fetchone()[0])
        if journal_mode.casefold() != "delete":
            message = "Active index left DELETE journal mode before side-by-side replacement."
            raise RuntimeError(message)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = self.index_path.with_name(self.index_path.name + suffix)
            try:
                sidecar.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                message = f"Unable to verify active SQLite sidecar before replacement: {sidecar}."
                raise RuntimeError(message) from exc
            message = (
                f"Active SQLite sidecar remained before replacement: {sidecar}. "
                "Side-by-side replacement was aborted."
            )
            raise RuntimeError(message)

    def _write_backup(self, source_conn: sqlite3.Connection) -> None:
        self._verify_quiesced_index_family(source_conn)
        with tempfile.NamedTemporaryFile(
            dir=self.index_path.parent,
            prefix=f".{self.index_path.name}.",
            suffix=".backup",
            delete=False,
        ) as backup_file:
            temporary_backup = Path(backup_file.name)
        try:
            shutil.copyfile(self.index_path, temporary_backup)
            durable_replace(temporary_backup, self._backup_path())
        finally:
            temporary_backup.unlink(missing_ok=True)

    def _backup_path(self) -> Path:
        return previous_index_backup_path(self.index_path)

    def _discard_confirmed_backup(self) -> None:
        self._backup_path().unlink(missing_ok=True)

    def _read_meetings(self, conn: Any) -> Iterator[dict[str, Any]]:
        meetings = (
            dict(row)
            for row in conn.execute(
                """
                SELECT id, title, created_at, updated_at, folder_path
                FROM meetings
                ORDER BY created_at ASC
                """
            )
        )
        for meeting in meetings:
            meeting_id = meeting["id"]
            meeting["transcripts"] = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM transcripts
                    WHERE meeting_id = ?
                    ORDER BY COALESCE(audio_start_time, 0), timestamp, id
                    """,
                    (meeting_id,),
                ).fetchall()
            ]
            meeting["summary_process"] = optional_row(
                conn,
                "SELECT * FROM summary_processes WHERE meeting_id = ?",
                (meeting_id,),
            )
            meeting["notes"] = optional_row(
                conn,
                "SELECT * FROM meeting_notes WHERE meeting_id = ?",
                (meeting_id,),
            )
            yield meeting


def optional_row(conn: Any, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


REQUIRED_MEETILY_SCHEMA = {
    "meetings": {"id", "title", "created_at", "updated_at", "folder_path"},
    "transcripts": {
        "id",
        "meeting_id",
        "transcript",
        "timestamp",
        "audio_start_time",
        "audio_end_time",
        "speaker",
    },
    "summary_processes": {"meeting_id", "result"},
    "meeting_notes": {"meeting_id", "notes_markdown"},
}


def inspect_meetily_schema(source_path: Path) -> tuple[bool, str | None]:
    try:
        with readonly_sqlite_connection(source_path) as conn:
            validate_meetily_schema(conn)
    except (RuntimeError, sqlite3.Error) as exc:
        return False, str(exc)
    return True, None


def validate_meetily_schema(conn: Any) -> None:
    for table, required_columns in REQUIRED_MEETILY_SCHEMA.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            message = f"Meetily DB schema is unsupported: missing table {table}"
            raise RuntimeError(message)
        actual_columns = {str(row["name"]) for row in rows}
        missing_columns = sorted(required_columns - actual_columns)
        if missing_columns:
            message = (
                "Meetily DB schema is unsupported: "
                f"missing columns {table}.{', '.join(missing_columns)}"
            )
            raise RuntimeError(message)


def meeting_external_ids(source_path: Path) -> set[str]:
    with readonly_sqlite_connection(source_path) as conn:
        validate_meetily_schema(conn)
        return {str(row["id"]) for row in conn.execute("SELECT id FROM meetings")}


def normalize_meeting(
    source_id: int,
    source_path: Path,
    upstream: dict[str, Any],
    indexed_at: str,
) -> tuple[MeetingRecord, list[ChunkRecord]]:
    chunks: list[ChunkRecord] = []
    summary_text = extract_summary_text(upstream.get("summary_process"))
    language = extract_language(upstream.get("summary_process"))

    for transcript in upstream["transcripts"]:
        text = normalize_text(transcript.get("transcript") or "")
        if not text:
            continue
        ordinal = len(chunks)
        chunks.append(
            ChunkRecord(
                external_id=transcript.get("id"),
                kind="transcript",
                ordinal=ordinal,
                text=text,
                speaker=clean_optional(transcript.get("speaker")),
                starts_at_seconds=transcript.get("audio_start_time"),
                ends_at_seconds=transcript.get("audio_end_time"),
                timestamp_label=clean_optional(transcript.get("timestamp")),
                token_count=len(text.split()),
                fingerprint=fingerprint_json(transcript),
                raw_metadata_json=dumps_json(transcript),
            )
        )

    if summary_text:
        summary_payload = upstream.get("summary_process") or {}
        ordinal = len(chunks)
        chunks.append(
            ChunkRecord(
                external_id=f"summary:{upstream['id']}",
                kind="summary",
                ordinal=ordinal,
                text=summary_text,
                speaker=None,
                starts_at_seconds=None,
                ends_at_seconds=None,
                timestamp_label=None,
                token_count=len(summary_text.split()),
                fingerprint=fingerprint_json({"kind": "summary", "payload": summary_payload}),
                raw_metadata_json=dumps_json(summary_payload),
            )
        )

    notes = upstream.get("notes")
    notes_text = normalize_text((notes or {}).get("notes_markdown") or "")
    if notes_text:
        ordinal = len(chunks)
        chunks.append(
            ChunkRecord(
                external_id=f"note:{upstream['id']}",
                kind="note",
                ordinal=ordinal,
                text=notes_text,
                speaker=None,
                starts_at_seconds=None,
                ends_at_seconds=None,
                timestamp_label=None,
                token_count=len(notes_text.split()),
                fingerprint=fingerprint_json({"kind": "note", "payload": notes}),
                raw_metadata_json=dumps_json(notes),
            )
        )

    meeting_fingerprint_payload = {
        "normalization_version": MEETING_NORMALIZATION_VERSION,
        "meeting": {
            "id": upstream.get("id"),
            "title": upstream.get("title"),
            "created_at": upstream.get("created_at"),
            "updated_at": upstream.get("updated_at"),
            "folder_path": upstream.get("folder_path"),
        },
        "chunks": [chunk.fingerprint for chunk in chunks],
        "summary": upstream.get("summary_process"),
        "notes": upstream.get("notes"),
    }
    meeting = MeetingRecord(
        source_id=source_id,
        external_id=upstream["id"],
        title=upstream["title"],
        started_at=upstream.get("created_at"),
        ended_at=None,
        created_at=upstream.get("created_at"),
        updated_at=upstream.get("updated_at"),
        folder_path=upstream.get("folder_path"),
        source_path=str(source_path),
        language=language,
        summary_text=summary_text,
        raw_summary_json=dumps_json(upstream.get("summary_process"))
        if upstream.get("summary_process")
        else None,
        raw_metadata_json=dumps_json({"source_kind": MeetilySQLiteScanner.source_kind}),
        fingerprint=fingerprint_json(meeting_fingerprint_payload),
        indexed_at=indexed_at,
    )
    return meeting, chunks


def extract_summary_text(summary_process: dict[str, Any] | None) -> str | None:
    if not summary_process or not summary_process.get("result"):
        return None
    raw = summary_process["result"]
    try:
        parsed = loads_json(raw)
    except ValueError:
        return normalize_text(raw)
    if isinstance(parsed, dict):
        for key in ("markdown", "summary", "raw_summary", "MeetingName"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return normalize_text(value)
        return normalize_text(dumps_json(parsed))
    if isinstance(parsed, str):
        return normalize_text(parsed)
    return normalize_text(dumps_json(parsed))


def extract_language(summary_process: dict[str, Any] | None) -> str | None:
    if not summary_process or not summary_process.get("metadata"):
        return None
    try:
        metadata = loads_json(summary_process["metadata"])
    except ValueError:
        return None
    if not isinstance(metadata, dict):
        return None
    language = metadata.get("language")
    return language if isinstance(language, str) and language.strip() else None


def normalize_text(value: str) -> str:
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized_lines: list[str] = []
    blank = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if not blank:
                normalized_lines.append("")
            blank = True
        else:
            normalized_lines.append(stripped)
            blank = False
    return "\n".join(normalized_lines).strip()


def clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def fingerprint_json(payload: Any) -> str:
    return hashlib.sha256(dumps_json_bytes(payload)).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
