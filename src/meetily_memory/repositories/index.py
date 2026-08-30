import sqlite3
from collections.abc import Generator, Iterable, Mapping
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path
from typing import Any

from meetily_memory.config.paths import canonical_source_path
from meetily_memory.db.fts import build_fts_query
from meetily_memory.db.rows import rows_to_dicts
from meetily_memory.db.schema import (
    OPERATION_STATE_SCHEMA,
    IndexConnectionFactory,
    IndexRebuildRequiredError,
    existing_index_connection,
    index_connection,
    index_needs_schema_initialization,
    index_projection_transaction,
    sqlite_read_snapshot,
)
from meetily_memory.domain import (
    Meeting,
    MeetingRef,
    MeetingSearchFilters,
    MemoryEntity,
    SearchHit,
    SourceExcerpt,
    canonical_entity_kind,
)
from meetily_memory.meeting_structure import ENTITY_KINDS, StructuredEntity
from meetily_memory.memory.entities import (
    ENTITY_DETAIL_SQL,
    ENTITY_INSERT_SQL,
    ENTITY_NODE_TYPES,
    ENTITY_SELECT_SQL,
    StructuredEntityContext,
    StructuredEntityRepository,
    assert_known_entity_kind,
    assert_known_task_status,
    assert_known_task_status_filter,
    structured_entity_sort_key,
)
from meetily_memory.memory.knowledge import KnowledgeContext, KnowledgeRepository
from meetily_memory.memory.task_status import TaskStatusContext, TaskStatusRepository
from meetily_memory.repositories.meetings import MeetingsContext, MeetingsRepository
from meetily_memory.repositories.records import (
    ChunkRecord,
    MeetingRecord,
    PostPublishIssue,
    ScanRunStats,
)
from meetily_memory.repositories.search import EvidenceResolutionError, SearchRepository
from meetily_memory.user_state import (
    SourcePathClaim,
    UserStateRepository,
    prepare_index_user_state,
    prepare_user_state_migration,
    register_state_owned_index_generation,
    task_identity,
    validate_existing_user_state_version,
)

__all__ = [
    "ChunkRecord",
    "EvidenceResolutionError",
    "IndexRepository",
    "MeetingRecord",
    "ScanRunStats",
    "build_fts_query",
]

MEMORY_ENTITY_BATCH_SIZE = 200
OPERATION_SNAPSHOT_PIN_SQL = """
SELECT
  (SELECT COUNT(*) FROM main.sources) AS index_source_count,
  (SELECT COUNT(*) FROM operation_state.sources) AS state_source_count
"""
ENTITY_KIND_ORDER = {kind: order for order, kind in enumerate(ENTITY_KINDS)}


class IndexRepository:
    entity_insert_sql = ENTITY_INSERT_SQL
    entity_select_sql = ENTITY_SELECT_SQL
    entity_node_types = ENTITY_NODE_TYPES

    def __init__(
        self,
        index_path: Path,
        *,
        state_path: Path | None = None,
        generation_ledger_paths: Iterable[Path] = (),
        _read_only: bool = False,
        _user_state: UserStateRepository | None = None,
    ) -> None:
        self.index_path = Path(index_path)
        index_existed = self.index_path.is_file()
        needs_schema_initialization = not index_existed or index_needs_schema_initialization(
            self.index_path
        )
        self.state_path = (
            Path(state_path) if state_path else self.index_path.with_name("state.sqlite")
        )
        self.read_only = _read_only
        self.connection: IndexConnectionFactory = (
            existing_index_connection if self.read_only else index_connection
        )
        self.operation_connection: IndexConnectionFactory = existing_index_connection
        ledger_paths = tuple(generation_ledger_paths)
        self.requires_rebuild = False
        if self.read_only:
            if ledger_paths:
                message = "Read-only index repositories cannot register generation paths."
                raise ValueError(message)
            with existing_index_connection(self.index_path):
                pass
            self.user_state = UserStateRepository.open_existing(self.state_path)
        else:
            now = utc_now()
            self.user_state = prepare_index_user_state(
                self.index_path,
                self.state_path,
                now=now,
                user_state=_user_state,
            )
            prepare_user_state_migration(
                self.index_path,
                self.user_state,
                now=now,
            )
            try:
                with index_connection(self.index_path):
                    pass
            except IndexRebuildRequiredError:
                self.requires_rebuild = True
            if not self.requires_rebuild:
                if needs_schema_initialization:
                    self.user_state = prepare_index_user_state(
                        self.index_path,
                        self.state_path,
                        now=now,
                        user_state=self.user_state,
                    )
                for ledger_path in ledger_paths:
                    self.register_state_owned_generation_path(ledger_path)
        self.meetings = MeetingsRepository(
            MeetingsContext(
                index_path=self.index_path,
                connection=self.connection,
                sync_meeting_knowledge=self._sync_meeting_knowledge,
                delete_meeting_knowledge=self._delete_meeting_knowledge,
            )
        )
        self.search_repo = SearchRepository(self.index_path, self.connection)
        self.entities = StructuredEntityRepository(
            StructuredEntityContext(
                index_path=self.index_path,
                connection=self.connection,
                delete_structured_knowledge=self._delete_structured_knowledge,
                delete_structured_entities=self._delete_structured_entities,
                sync_meeting_knowledge=self._sync_meeting_knowledge,
                list_entity_details=self._list_structured_entity_details_conn,
                list_all_entity_details=self._list_all_structured_entity_details_conn,
            )
        )
        self.knowledge = KnowledgeRepository(
            KnowledgeContext(
                index_path=self.index_path,
                connection=self.connection,
                search_meetings=self.search_repo.search_in_snapshot,
                chunk_rows=self.meetings.chunk_rows,
                meeting_people_rows=self.meetings.meeting_people_rows,
                structured_entity_rows=self.meetings.structured_entity_rows,
                all_structured_entity_details=self._list_all_structured_entity_details_conn,
                user_state=self.user_state,
                now=utc_now,
            )
        )
        self.task_status = TaskStatusRepository(
            TaskStatusContext(
                index_path=self.index_path,
                connection=self.connection,
                user_state=self.user_state,
                validate_status=assert_known_task_status,
                now=utc_now,
            )
        )

    @classmethod
    def open_existing(
        cls,
        index_path: Path,
        *,
        state_path: Path | None = None,
    ) -> "IndexRepository":
        return cls(index_path, state_path=state_path, _read_only=True)

    @contextmanager
    def operation_snapshot(self) -> Generator[sqlite3.Connection, None, None]:
        """Pin one per-file snapshot for index and attached state before retrieval."""
        state_uri = self.user_state.read_only_uri
        with self.operation_connection(self.index_path) as conn:
            conn.execute(
                f"ATTACH DATABASE ? AS {OPERATION_STATE_SCHEMA}",
                (state_uri,),
            )
            self.user_state.recheck_identity()
            state_version = int(
                conn.execute(f"PRAGMA {OPERATION_STATE_SCHEMA}.user_version").fetchone()[0]
            )
            validate_existing_user_state_version(state_version)
            with sqlite_read_snapshot(conn):
                conn.execute(OPERATION_SNAPSHOT_PIN_SQL).fetchone()
                yield conn

    def structured_entity_sort_key(self, row: dict[str, Any]) -> tuple[str, int]:
        return structured_entity_sort_key(row)

    def assert_known_entity_kind(self, kind: str) -> None:
        assert_known_entity_kind(kind)

    def assert_known_task_status(self, status: str) -> None:
        assert_known_task_status(status)

    def assert_known_task_status_filter(self, status: str) -> None:
        assert_known_task_status_filter(status)

    def utc_now(self) -> str:
        return utc_now()

    def rows_to_dicts(self, rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        return rows_to_dicts(rows)

    def upsert_source(  # noqa: PLR0913
        self,
        source_uuid: str,
        kind: str,
        path: str,
        now: str,
        label: str | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        return self.meetings.upsert_source(
            source_uuid,
            kind,
            path,
            now,
            label,
            connection=connection,
        )

    def get_source(self, kind: str, path: str) -> dict[str, Any] | None:
        return self.meetings.get_source(kind, path)

    def get_source_by_uuid(self, source_uuid: str) -> dict[str, Any] | None:
        return self.meetings.get_source_by_uuid(source_uuid)

    def source_meeting_external_ids(self, source_uuid: str) -> set[str]:
        with self.connection(self.index_path) as conn:
            rows = conn.execute(
                """
                SELECT m.external_id
                FROM meetings m
                JOIN sources s ON s.id = m.source_id
                WHERE s.source_uuid = ?
                """,
                (source_uuid,),
            ).fetchall()
        return {str(row["external_id"]) for row in rows}

    def update_source_path_projection(self, source_uuid: str, new_path: str) -> None:
        with index_connection(self.index_path) as conn:
            source = conn.execute(
                "SELECT id FROM sources WHERE source_uuid = ?",
                (source_uuid,),
            ).fetchone()
            if source is None:
                return
            source_id = int(source["id"])
            conn.execute("UPDATE sources SET path = ? WHERE id = ?", (new_path, source_id))
            conn.execute(
                "UPDATE meetings SET source_path = ? WHERE source_id = ?",
                (new_path, source_id),
            )
            conn.commit()

    def rebind_source_path_projection(self, claim: SourcePathClaim) -> set[str]:
        self._require_current_source_path_claim(claim)
        if self.requires_rebuild:
            rows = self._rebind_legacy_source_path_projection(
                claim.kind,
                claim.projected_path,
                claim.claimed_path,
            )
            self._require_current_source_path_claim(claim)
            return rows
        with index_connection(self.index_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_current_source_path_claim(claim)
            source = conn.execute(
                "SELECT id, kind, path FROM sources WHERE source_uuid = ?",
                (claim.source_uuid,),
            ).fetchone()
            if source is None:
                self._require_current_source_path_claim(claim)
                conn.commit()
                return set()
            if str(source["kind"]) != claim.kind:
                message = f"Source UUID {claim.source_uuid} is registered with a different kind."
                raise ValueError(message)
            source_id = int(source["id"])
            projected_path = str(source["path"])
            if projected_path not in {claim.projected_path, claim.claimed_path}:
                message = (
                    f"Source UUID {claim.source_uuid} is projected at unexpected path "
                    f"{projected_path}."
                )
                raise RuntimeError(message)
            path_owner = conn.execute(
                "SELECT id FROM sources WHERE kind = ? AND path = ?",
                (claim.kind, claim.claimed_path),
            ).fetchone()
            if path_owner is not None and int(path_owner["id"]) != source_id:
                message = "The rebind target is already projected as another source."
                raise ValueError(message)
            rows = conn.execute(
                "SELECT external_id FROM meetings WHERE source_id = ?",
                (source_id,),
            ).fetchall()
            cursor = conn.execute(
                """
                UPDATE sources
                SET path = ?
                WHERE id = ? AND source_uuid = ? AND kind = ? AND path = ?
                """,
                (
                    claim.claimed_path,
                    source_id,
                    claim.source_uuid,
                    claim.kind,
                    projected_path,
                ),
            )
            if cursor.rowcount != 1:
                message = f"Source UUID {claim.source_uuid} projection changed during rebind."
                raise RuntimeError(message)
            conn.execute(
                "UPDATE meetings SET source_path = ? WHERE source_id = ?",
                (claim.claimed_path, source_id),
            )
            self._require_current_source_path_claim(claim)
            conn.commit()
        return {str(row["external_id"]) for row in rows}

    def heal_pending_source_path_projection(self, source_uuid: str) -> set[str]:
        if self.requires_rebuild:
            return set()
        claim = self.user_state.get_pending_source_path_claim(source_uuid)
        if claim is None:
            return set()
        return self._heal_pending_source_path_claims((claim,)).get(source_uuid, set())

    def heal_pending_source_path_projections(self) -> dict[str, set[str]]:
        if self.requires_rebuild:
            return {}
        claims = self.user_state.list_pending_source_path_claims()
        return self._heal_pending_source_path_claims(claims)

    def project_pending_source_path_projections(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[SourcePathClaim, ...]:
        """Project every current pending claim without committing either database."""
        if self.requires_rebuild:
            return ()
        if not connection.in_transaction:
            message = "Pending source paths require the active projection transaction."
            raise RuntimeError(message)
        claims = self.user_state.list_pending_source_path_claims()
        verified_claims, _external_ids = self._project_source_path_claims(connection, claims)
        return verified_claims

    def _heal_pending_source_path_claims(
        self,
        claims: tuple[SourcePathClaim, ...],
    ) -> dict[str, set[str]]:
        if not claims:
            return {}
        with index_connection(self.index_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            verified_claims, external_ids = self._project_source_path_claims(conn, claims)
            conn.commit()
        if verified_claims and not self.user_state.finalize_source_path_claims(verified_claims):
            source_uuids = ", ".join(claim.source_uuid for claim in verified_claims)
            message = (
                "Source path claims changed before atomic finalization for UUID(s): "
                f"{source_uuids}."
            )
            raise RuntimeError(message)
        return external_ids

    def _project_source_path_claims(
        self,
        conn: sqlite3.Connection,
        claims: tuple[SourcePathClaim, ...],
    ) -> tuple[tuple[SourcePathClaim, ...], dict[str, set[str]]]:
        verified_claims: list[SourcePathClaim] = []
        external_ids: dict[str, set[str]] = {}
        for claim in claims:
            self._require_current_source_path_claim(claim)
            source = conn.execute(
                "SELECT id, kind, path FROM sources WHERE source_uuid = ?",
                (claim.source_uuid,),
            ).fetchone()
            if source is None:
                continue
            if str(source["kind"]) != claim.kind:
                message = f"Source UUID {claim.source_uuid} is registered with a different kind."
                raise ValueError(message)
            source_id = int(source["id"])
            projected_path = str(source["path"])
            if projected_path not in {claim.projected_path, claim.claimed_path}:
                message = (
                    f"Source UUID {claim.source_uuid} is projected at unexpected path "
                    f"{projected_path}."
                )
                raise RuntimeError(message)
            path_owner = conn.execute(
                "SELECT id FROM sources WHERE kind = ? AND path = ?",
                (claim.kind, claim.claimed_path),
            ).fetchone()
            if path_owner is not None and int(path_owner["id"]) != source_id:
                message = "The pending source target is projected as another source."
                raise ValueError(message)
            rows = conn.execute(
                "SELECT external_id FROM meetings WHERE source_id = ?",
                (source_id,),
            ).fetchall()
            if projected_path != claim.claimed_path:
                cursor = conn.execute(
                    """
                    UPDATE sources
                    SET path = ?
                    WHERE id = ? AND source_uuid = ? AND kind = ? AND path = ?
                    """,
                    (
                        claim.claimed_path,
                        source_id,
                        claim.source_uuid,
                        claim.kind,
                        projected_path,
                    ),
                )
                if cursor.rowcount != 1:
                    message = f"Source UUID {claim.source_uuid} projection changed during recovery."
                    raise RuntimeError(message)
            conn.execute(
                "UPDATE meetings SET source_path = ? WHERE source_id = ?",
                (claim.claimed_path, source_id),
            )
            self._require_current_source_path_claim(claim)
            verified_claims.append(claim)
            external_ids[claim.source_uuid] = {str(row["external_id"]) for row in rows}
        for claim in verified_claims:
            self._require_current_source_path_claim(claim)
        return tuple(verified_claims), external_ids

    def register_state_owned_generation_path(self, ledger_path: Path) -> str:
        return register_state_owned_index_generation(
            self.index_path,
            ledger_path,
            self.user_state,
            now=utc_now(),
        )

    def _require_current_source_path_claim(self, claim: SourcePathClaim) -> None:
        if self.user_state.is_source_path_claim_current(claim):
            return
        message = f"Source path claim for UUID {claim.source_uuid} is no longer current."
        raise RuntimeError(message)

    def restore_source_path_projection(self, rollback_claim: SourcePathClaim) -> bool:
        self._require_current_source_path_claim(rollback_claim)
        if self.requires_rebuild:
            return self._restore_legacy_source_path_projection(rollback_claim)
        with index_connection(self.index_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_current_source_path_claim(rollback_claim)
            source = conn.execute(
                "SELECT id, kind, path FROM sources WHERE source_uuid = ?",
                (rollback_claim.source_uuid,),
            ).fetchone()
            if source is None:
                self._require_current_source_path_claim(rollback_claim)
                conn.commit()
                return True
            if str(source["kind"]) != rollback_claim.kind:
                message = (
                    f"Source UUID {rollback_claim.source_uuid} is registered with a different kind."
                )
                raise ValueError(message)
            source_id = int(source["id"])
            current_path = str(source["path"])
            if current_path not in {
                rollback_claim.projected_path,
                rollback_claim.claimed_path,
            }:
                message = (
                    f"Source UUID {rollback_claim.source_uuid} is projected at unexpected path "
                    f"{current_path}."
                )
                raise RuntimeError(message)
            path_owner = conn.execute(
                "SELECT id FROM sources WHERE kind = ? AND path = ?",
                (rollback_claim.kind, rollback_claim.claimed_path),
            ).fetchone()
            if path_owner is not None and int(path_owner["id"]) != source_id:
                message = "The rollback target is already projected as another source."
                raise ValueError(message)
            if current_path != rollback_claim.claimed_path:
                cursor = conn.execute(
                    "UPDATE sources SET path = ? WHERE id = ? AND path = ?",
                    (rollback_claim.claimed_path, source_id, current_path),
                )
                if cursor.rowcount != 1:
                    message = (
                        f"Source UUID {rollback_claim.source_uuid} projection changed during "
                        "rollback."
                    )
                    raise RuntimeError(message)
            conn.execute(
                "UPDATE meetings SET source_path = ? WHERE source_id = ?",
                (rollback_claim.claimed_path, source_id),
            )
            self._require_current_source_path_claim(rollback_claim)
            conn.commit()
        return True

    def _restore_legacy_source_path_projection(
        self,
        rollback_claim: SourcePathClaim,
    ) -> bool:
        with sqlite3.connect(self.index_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            self._require_current_source_path_claim(rollback_claim)
            source_rows = conn.execute(
                "SELECT id, path FROM sources WHERE kind = ? ORDER BY id",
                (rollback_claim.kind,),
            ).fetchall()
            candidates = [
                row
                for row in source_rows
                if str(row["path"]) in {rollback_claim.projected_path, rollback_claim.claimed_path}
            ]
            if not candidates:
                if not source_rows:
                    self._require_current_source_path_claim(rollback_claim)
                    conn.commit()
                    return True
                conn.rollback()
                return False
            candidate_ids = {int(row["id"]) for row in candidates}
            if len(candidate_ids) != 1:
                conn.rollback()
                return False
            source_id = candidate_ids.pop()
            current_path = next(
                str(row["path"]) for row in candidates if int(row["id"]) == source_id
            )
            path_owner = conn.execute(
                "SELECT id FROM sources WHERE kind = ? AND path = ?",
                (rollback_claim.kind, rollback_claim.claimed_path),
            ).fetchone()
            if path_owner is not None and int(path_owner["id"]) != source_id:
                conn.rollback()
                return False
            if current_path != rollback_claim.claimed_path:
                cursor = conn.execute(
                    "UPDATE sources SET path = ? WHERE id = ? AND path = ?",
                    (rollback_claim.claimed_path, source_id, current_path),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return False
            conn.execute(
                "UPDATE meetings SET source_path = ? WHERE source_id = ?",
                (rollback_claim.claimed_path, source_id),
            )
            self._require_current_source_path_claim(rollback_claim)
            conn.commit()
            return True

    def _rebind_legacy_source_path_projection(
        self,
        kind: str,
        current_path: str,
        new_path: str,
    ) -> set[str]:
        with sqlite3.connect(self.index_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            source_rows = conn.execute(
                """
                SELECT id, path
                FROM sources
                WHERE kind = ?
                ORDER BY id
                """,
                (kind,),
            ).fetchall()
            exact_paths = {current_path, new_path}
            candidate_ids = {
                int(row["id"]) for row in source_rows if str(row["path"]) in exact_paths
            }
            canonical_target = canonical_source_path(Path(new_path))
            for row in source_rows:
                try:
                    resolved_path = canonical_source_path(Path(str(row["path"])))
                except (OSError, RuntimeError):
                    continue
                if resolved_path == canonical_target:
                    candidate_ids.add(int(row["id"]))
            if len(candidate_ids) > 1:
                message = "The selected UUID maps to multiple legacy index sources."
                raise ValueError(message)
            if not candidate_ids:
                if source_rows:
                    message = (
                        "The selected UUID cannot be mapped to a legacy index source by its "
                        "stored path; explicit rebind aborted."
                    )
                    raise ValueError(message)
                conn.commit()
                return set()
            source_id = candidate_ids.pop()
            path_owner = conn.execute(
                "SELECT id FROM sources WHERE kind = ? AND path = ?",
                (kind, new_path),
            ).fetchone()
            if path_owner is not None and int(path_owner["id"]) != source_id:
                message = "The rebind target is already projected as another source."
                raise ValueError(message)
            rows = conn.execute(
                "SELECT external_id FROM meetings WHERE source_id = ?",
                (source_id,),
            ).fetchall()
            conn.execute("UPDATE sources SET path = ? WHERE id = ?", (new_path, source_id))
            conn.execute(
                "UPDATE meetings SET source_path = ? WHERE source_id = ?",
                (new_path, source_id),
            )
            conn.commit()
        return {str(row["external_id"]) for row in rows}

    def get_meeting_by_source_id(
        self,
        source_id: int,
        external_id: str,
        *,
        filters: MeetingSearchFilters | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        return self.meetings.get_meeting_by_source_id(
            source_id,
            external_id,
            filters=filters,
            connection=connection,
        )

    def upsert_meeting_with_chunks(
        self,
        meeting: MeetingRecord,
        chunks: Iterable[ChunkRecord],
        *,
        force: bool = False,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[int, bool, int]:
        return self.meetings.upsert_meeting_with_chunks(
            meeting,
            chunks,
            force=force,
            connection=connection,
        )

    def reconcile_source_meetings(
        self,
        source_id: int,
        external_ids: set[str],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        return self.meetings.reconcile_source_meetings(
            source_id,
            external_ids,
            connection=connection,
        )

    def _delete_structured_entities(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        self.meetings.delete_structured_entities(conn, meeting_id)

    def _delete_meeting_knowledge(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        self.knowledge.delete_meeting_knowledge(conn, meeting_id)

    def _delete_structured_knowledge(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        self.knowledge.delete_structured_knowledge(conn, meeting_id)

    def _sync_meeting_knowledge(
        self,
        conn: sqlite3.Connection,
        meeting_id: int,
        now: str,
    ) -> None:
        self.knowledge.sync_meeting_knowledge(conn, meeting_id, now)

    def get_chunks_for_meeting(self, meeting_id: int) -> list[dict[str, Any]]:
        return self.meetings.get_chunks_for_meeting(meeting_id)

    def replace_structured_entities(
        self,
        meeting_id: int,
        entities: Iterable[StructuredEntity],
        now: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, int]:
        return self.entities.replace_structured_entities(
            meeting_id,
            entities,
            now,
            connection=connection,
        )

    def list_meeting_ids(self) -> list[int]:
        return self.meetings.list_meeting_ids()

    def list_structured_entities(
        self,
        meeting_id: int,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.entities.list_structured_entities(meeting_id, kind)

    def list_structured_entity_details(
        self,
        kind: str,
        limit: int = 20,
        *,
        status: str = "all",
    ) -> list[dict[str, Any]]:
        return self.entities.list_structured_entity_details(kind, limit, status=status)

    def list_all_structured_entity_details(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.entities.list_all_structured_entity_details(limit)

    def set_task_status(
        self,
        action_item_id: int,
        status: str,
        *,
        note: str | None,
        now: str | None = None,
    ) -> dict[str, Any]:
        return self.task_status.set_task_status(action_item_id, status, note=note, now=now)

    def ensure_topic(
        self,
        title: str,
        *,
        aliases: Iterable[str] = (),
    ) -> dict[str, Any]:
        return self.knowledge.ensure_topic(title, aliases=aliases)

    def add_topic_aliases(
        self,
        title: str,
        aliases: Iterable[str],
    ) -> dict[str, Any]:
        return self.knowledge.add_topic_aliases(title, aliases)

    def remove_topic_aliases(self, aliases: Iterable[str]) -> tuple[str, ...]:
        return self.knowledge.remove_topic_aliases(aliases)

    def project_topic_aliases(self, *, connection: sqlite3.Connection | None = None) -> None:
        self.knowledge.project_topic_aliases(connection=connection)

    def topic_memory(self, title: str, limit: int = 10) -> dict[str, Any]:
        return self.knowledge.topic_memory(title, limit)

    def list_topics(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.knowledge.list_topics(limit)

    def graph_for_topic(self, title: str, limit: int = 50) -> dict[str, Any]:
        return self.knowledge.graph_for_topic(title, limit)

    def _list_structured_entity_details_conn(
        self,
        conn: sqlite3.Connection,
        kind: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(ENTITY_DETAIL_SQL[kind], (limit,)).fetchall()
        details = rows_to_dicts(rows)
        if kind == "action_items":
            self._hydrate_task_statuses(details)
        return details

    def _list_all_structured_entity_details_conn(
        self,
        conn: sqlite3.Connection,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for kind in ENTITY_KINDS:
            rows.extend(self._list_structured_entity_details_conn(conn, kind, limit))
        return rows

    def search(  # noqa: PLR0913
        self,
        query: str,
        limit: int = 10,
        *,
        meeting_id: int | None = None,
        context: int = 0,
        filters: MeetingSearchFilters | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        if connection is not None:
            return self.search_repo.search_in_snapshot(
                connection,
                query,
                limit,
                meeting_id=meeting_id,
                context=context,
                filters=filters,
            )
        return self.search_repo.search(
            query,
            limit,
            meeting_id=meeting_id,
            context=context,
            filters=filters,
        )

    def search_hits(  # noqa: PLR0913
        self,
        query: str,
        limit: int = 10,
        *,
        meeting_id: int | None = None,
        context: int = 0,
        filters: MeetingSearchFilters | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[SearchHit, ...]:
        rows = self.search(
            query,
            limit,
            meeting_id=meeting_id,
            context=context,
            filters=filters,
            connection=connection,
        )
        return tuple(self.search_hit_from_row(row) for row in rows)

    @staticmethod
    def search_hit_from_row(row: Mapping[str, Any]) -> SearchHit:
        return search_hit_from_row(row)

    def expand_search_hits(
        self,
        hits: tuple[SearchHit, ...],
        context: int,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[SearchHit, ...]:
        if context <= 0 or not hits:
            return hits
        evidence_refs = tuple((hit.id, hit.meeting.ref) for hit in hits)
        rows = (
            self.search_repo.expand_evidence_refs_in_snapshot(connection, evidence_refs, context)
            if connection is not None
            else self.search_repo.expand_evidence_refs(evidence_refs, context)
        )
        return tuple(search_hit_from_row(row) for row in rows)

    def get_search_hit(self, evidence_id: str) -> SearchHit | None:
        row = self.search_repo.evidence_by_id(evidence_id)
        return search_hit_from_row(row) if row is not None else None

    def memory_entities_for_hits(self, hits: tuple[SearchHit, ...]) -> tuple[MemoryEntity, ...]:
        if not hits:
            return ()
        rows: list[dict[str, Any]] = []
        evidence_refs = tuple((hit.id, hit.meeting.ref) for hit in hits)
        with self.connection(self.index_path) as conn, sqlite_read_snapshot(conn):
            evidence_rows = self.search_repo.resolve_evidence_rows(conn, evidence_refs)
            chunk_ids = tuple(
                dict.fromkeys(int(evidence_row["chunk_id"]) for evidence_row in evidence_rows)
            )
            for chunk_id_batch in batched(chunk_ids, MEMORY_ENTITY_BATCH_SIZE):
                placeholders = ", ".join("?" for _ in chunk_id_batch)
                selects = [memory_entity_select_sql(kind, placeholders) for kind in ENTITY_KINDS]
                params = tuple(chunk_id_batch) * len(ENTITY_KINDS)
                entity_rows = conn.execute(" UNION ALL ".join(selects), params).fetchall()
                rows.extend(rows_to_dicts(entity_rows))
        rows.sort(
            key=lambda row: (
                str(row.get("meeting_date") or ""),
                -int(row["entity_ordinal"]),
                -ENTITY_KIND_ORDER[str(row["kind"])],
            ),
            reverse=True,
        )
        return tuple(
            MemoryEntity(
                kind=canonical_entity_kind(str(row["kind"])),
                content=str(row["entity_text"]),
                source=source_excerpt_from_entity_row(row),
                evidence_id=str(row["evidence_id"]),
                extraction_method=str(row["extraction_method"]),
            )
            for row in rows
        )

    def list_meetings(self, limit: int = 20, person: str | None = None) -> list[dict[str, Any]]:
        return self.meetings.list_meetings(limit, person)

    def get_meeting(
        self,
        external_id: str,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> dict[str, Any] | None:
        """Return the only meeting with a bare external ID, or raise on ambiguity."""
        return self.meetings.get_meeting_by_external_id(external_id, filters=filters)

    def get_meeting_by_local_id(
        self,
        meeting_id: int,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> dict[str, Any] | None:
        return self.meetings.get_meeting_by_local_id(meeting_id, filters=filters)

    def get_meeting_by_ref(
        self,
        ref: MeetingRef,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> dict[str, Any] | None:
        return self.meetings.get_meeting_by_ref(ref, filters=filters)

    def get_meetings_by_refs(
        self,
        refs: tuple[MeetingRef, ...],
        *,
        filters: MeetingSearchFilters | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[MeetingRef, dict[str, Any]]:
        return self.meetings.get_meetings_by_refs(
            refs,
            filters=filters,
            connection=connection,
        )

    def get_meetings_by_local_ids(
        self,
        meeting_ids: tuple[int, ...],
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> dict[int, dict[str, Any]]:
        return self.meetings.get_meetings_by_local_ids(meeting_ids, filters=filters)

    def meeting_ref_for_external_id(self, external_id: str) -> MeetingRef | None:
        meeting = self.get_meeting(external_id)
        return meeting_ref_from_row(meeting) if meeting is not None else None

    def meeting_ref_for_local_id(self, meeting_id: int) -> MeetingRef | None:
        meeting = self.get_meeting_by_local_id(meeting_id)
        return meeting_ref_from_row(meeting) if meeting is not None else None

    def meeting_by_ref(self, ref: MeetingRef) -> tuple[int, Meeting] | None:
        meeting = self.get_meeting_by_ref(ref)
        if meeting is None:
            return None
        return int(meeting["id"]), meeting_from_row(meeting)

    def source_identity_for_meeting(self, meeting_id: int) -> MeetingRef:
        ref = self.meeting_ref_for_local_id(meeting_id)
        if ref is None:
            message = f"Meeting not found: {meeting_id}"
            raise LookupError(message)
        return ref

    def meeting_transcript_text(self, ref: MeetingRef) -> str:
        meeting = self.get_meeting_by_ref(ref)
        if meeting is None:
            message = f"Meeting not found: {ref.source_uuid}/{ref.external_id}"
            raise ValueError(message)
        with self.connection(self.index_path) as conn:
            rows = self.meetings.chunk_rows(conn, int(meeting["id"]))
        return "\n".join(
            str(row["text"]) for row in rows if row["kind"] == "transcript" and row["text"]
        )

    def dominant_meeting_language(self) -> str | None:
        return self.meetings.dominant_meeting_language()

    def projection_transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        return index_projection_transaction(self.index_path)

    def begin_source_scan(self, source_uuid: str, started_at: str) -> tuple[int, int]:
        return self.meetings.begin_source_scan(source_uuid, started_at)

    def fail_abandoned_scan_runs(self, finished_at: str) -> None:
        self.meetings.fail_abandoned_scan_runs(finished_at)

    def update_scan_run_phase(self, run_id: int, phase: str) -> None:
        self.meetings.update_scan_run_phase(run_id, phase)

    def complete_scan_run(
        self,
        run_id: int,
        finished_at: str,
        result: ScanRunStats,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.meetings.complete_scan_run(
            run_id,
            finished_at,
            result,
            connection=connection,
        )

    def fail_scan_run(
        self,
        run_id: int,
        finished_at: str,
        phase: str,
        result: ScanRunStats,
        error_type: str,
    ) -> None:
        self.meetings.fail_scan_run(run_id, finished_at, phase, result, error_type)

    def record_post_publish_failure(
        self,
        run_id: int,
        issues: tuple[PostPublishIssue, ...],
    ) -> None:
        self.meetings.record_post_publish_failure(run_id, issues)

    def resolve_post_publish_failures(
        self,
        source_uuid: str,
        phases: tuple[str, ...],
    ) -> None:
        self.meetings.resolve_post_publish_failures(source_uuid, phases)

    def scan_run_diagnostics(self) -> dict[str, dict[str, Any] | None]:
        return self.meetings.scan_run_diagnostics()

    def stats(self) -> dict[str, int]:
        return self.meetings.stats()

    def _hydrate_task_statuses(self, rows: list[dict[str, Any]]) -> None:
        row_identities = [
            (
                row,
                task_identity(
                    str(row["source_uuid"]),
                    str(row["meeting_external_id"]),
                    str(row["chunk_external_id"]),
                    str(row["text"]),
                ),
            )
            for row in rows
            if row.get("chunk_external_id")
        ]
        states = self.user_state.get_task_states(identity for _, identity in row_identities)
        for row, identity in row_identities:
            state = states.get(identity)
            if state is None:
                continue
            row["status"] = state["status"]
            row["status_note"] = state["note"]
            row["status_source"] = state["source"]
            row["status_updated_at"] = state["updated_at"]


def memory_entity_select_sql(kind: str, placeholders: str) -> str:
    assert_known_entity_kind(kind)
    sql = f"""
        SELECT
          '{kind}' AS kind,
          e.source_chunk_id AS source_chunk_id,
          e.ordinal AS entity_ordinal,
          e.text AS entity_text,
          e.source AS extraction_method,
          COALESCE(m.updated_at, m.created_at, m.indexed_at) AS meeting_date,
          m.external_id AS meeting_external_id,
          s.source_uuid AS source_uuid,
          c.external_id AS chunk_external_id,
          c.evidence_id AS evidence_id,
          c.kind AS chunk_kind,
          c.ordinal AS chunk_ordinal,
          c.text AS chunk_text,
          c.speaker AS chunk_speaker,
          c.starts_at_seconds AS chunk_starts_at_seconds,
          c.ends_at_seconds AS chunk_ends_at_seconds,
          c.timestamp_label AS chunk_timestamp_label
        FROM {kind} e
        JOIN chunks c ON c.id = e.source_chunk_id
        JOIN meetings m ON m.id = c.meeting_id
        JOIN sources s ON s.id = m.source_id
        WHERE e.source_chunk_id IN ({placeholders})
    """  # noqa: S608
    return sql  # noqa: RET504


def search_hit_from_row(row: Mapping[str, Any]) -> SearchHit:
    excerpt = source_excerpt_from_search_row(row)
    return SearchHit(
        id=str(row["evidence_id"]),
        meeting=meeting_from_row(row),
        excerpt=excerpt,
        source_chunk_id=int(row["chunk_id"]),
        is_context=bool(row.get("is_context", False)),
    )


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def meeting_ref_from_row(row: Mapping[str, Any]) -> MeetingRef:
    return MeetingRef(
        source_uuid=str(row["source_uuid"]),
        external_id=str(row.get("meeting_external_id") or row["external_id"]),
    )


def meeting_from_row(row: Mapping[str, Any]) -> Meeting:
    return Meeting(
        id=int(row.get("meeting_id") or row["id"]),
        ref=meeting_ref_from_row(row),
        title=str(row["title"]),
        started_at=optional_str(row.get("started_at")),
        ended_at=optional_str(row.get("ended_at")),
        created_at=optional_str(row.get("created_at")),
        updated_at=optional_str(row.get("updated_at")),
        language=optional_str(row.get("language")),
        summary_text=optional_str(row.get("summary_text")),
        chunk_count=optional_int(row.get("chunk_count")),
    )


def source_excerpt_from_search_row(row: Mapping[str, Any]) -> SourceExcerpt:
    return SourceExcerpt(
        meeting_ref=meeting_ref_from_row(row),
        chunk_external_id=optional_str(row.get("chunk_external_id")),
        kind=str(row["kind"]),
        ordinal=int(row["ordinal"]),
        text=str(row["text"]),
        speaker=optional_str(row.get("speaker")),
        starts_at_seconds=optional_float(row.get("starts_at_seconds")),
        ends_at_seconds=optional_float(row.get("ends_at_seconds")),
        timestamp_label=optional_str(row.get("timestamp_label")),
    )


def source_excerpt_from_entity_row(row: Mapping[str, Any]) -> SourceExcerpt:
    return SourceExcerpt(
        meeting_ref=meeting_ref_from_row(row),
        chunk_external_id=optional_str(row.get("chunk_external_id")),
        kind=str(row["chunk_kind"]),
        ordinal=int(row["chunk_ordinal"]),
        text=str(row["chunk_text"]),
        speaker=optional_str(row.get("chunk_speaker")),
        starts_at_seconds=optional_float(row.get("chunk_starts_at_seconds")),
        ends_at_seconds=optional_float(row.get("chunk_ends_at_seconds")),
        timestamp_label=optional_str(row.get("chunk_timestamp_label")),
    )


def optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


def optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None
