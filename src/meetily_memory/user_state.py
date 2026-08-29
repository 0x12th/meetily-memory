import hashlib
import sqlite3
import uuid
from collections.abc import Callable, Generator, Iterable
from contextlib import closing, contextmanager
from dataclasses import dataclass
from itertools import batched
from pathlib import Path
from typing import Any

from meetily_memory.config.paths import canonical_source_path
from meetily_memory.db.migration_identity import (
    MIGRATION_REPORT_SCHEMA_VERSION,
    LegacyTaskState,
    PersistedTaskStateIdentity,
    VerifiedMigrationReport,
    canonical_database_path,
    legacy_intent_digest,
    legacy_state_migration_key,
    main_database_path,
    read_legacy_task_states,
    task_state_identity_digest,
    verified_migration_report,
)
from meetily_memory.db.migrations import (
    CURRENT_SCHEMA_VERSION,
    INDEX_ALIAS_OWNER_LEGACY,
    INDEX_ALIAS_OWNER_STATE,
    ensure_index_generation_marker,
    execute_sql_statements,
    read_index_generation_marker,
)
from meetily_memory.db.rows import rows_to_dicts
from meetily_memory.db.schema import IndexReadError, missing_user_state_message

USER_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  uuid TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  current_path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(kind, current_path)
);

CREATE TABLE IF NOT EXISTS task_states (
  id INTEGER PRIMARY KEY,
  source_uuid TEXT REFERENCES sources(uuid) ON DELETE RESTRICT,
  meeting_external_id TEXT,
  chunk_external_id TEXT,
  entity_kind TEXT NOT NULL,
  content_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL,
  note TEXT,
  source TEXT NOT NULL,
  orphaned INTEGER NOT NULL DEFAULT 0,
  orphaned_reason TEXT,
  legacy_action_item_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_task_states_identity
ON task_states(
  source_uuid,
  meeting_external_id,
  chunk_external_id,
  entity_kind,
  content_fingerprint
)
WHERE orphaned = 0;

CREATE TABLE IF NOT EXISTS migration_reports (
  id INTEGER PRIMARY KEY,
  index_path TEXT NOT NULL,
  migrated INTEGER NOT NULL,
  orphaned INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
"""
TAG_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY,
  normalized_name TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meeting_tags (
  source_uuid TEXT NOT NULL REFERENCES sources(uuid) ON DELETE RESTRICT,
  meeting_external_id TEXT NOT NULL,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL,
  PRIMARY KEY (source_uuid, meeting_external_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_meeting_tags_meeting
ON meeting_tags(source_uuid, meeting_external_id);

CREATE INDEX IF NOT EXISTS idx_meeting_tags_tag_id
ON meeting_tags(tag_id);
"""
MIGRATION_REPORT_SCHEMA = """
ALTER TABLE migration_reports ADD COLUMN migration_key TEXT;

CREATE UNIQUE INDEX idx_migration_reports_key
ON migration_reports(migration_key)
WHERE migration_key IS NOT NULL;

CREATE TABLE migration_report_items (
  report_id INTEGER NOT NULL REFERENCES migration_reports(id) ON DELETE RESTRICT,
  legacy_action_item_id INTEGER NOT NULL,
  task_state_id INTEGER NOT NULL REFERENCES task_states(id) ON DELETE RESTRICT,
  legacy_intent_digest TEXT NOT NULL CHECK (length(legacy_intent_digest) = 64),
  task_identity_digest TEXT NOT NULL CHECK (length(task_identity_digest) = 64),
  outcome TEXT NOT NULL CHECK (
    outcome IN (
      'active_inserted',
      'active_existing',
      'orphan_missing_identity',
      'conflict_existing_state',
      'conflict_duplicate_identity'
    )
  ),
  PRIMARY KEY (report_id, legacy_action_item_id),
  UNIQUE (report_id, task_state_id)
);

CREATE INDEX idx_migration_report_items_task_state
ON migration_report_items(task_state_id);
"""
SOURCE_REVISION_SCHEMA_VERSION = MIGRATION_REPORT_SCHEMA_VERSION + 1
SOURCE_REVISION_SCHEMA = """
ALTER TABLE sources
ADD COLUMN revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0);
"""
PENDING_SOURCE_BINDING_SCHEMA_VERSION = SOURCE_REVISION_SCHEMA_VERSION + 1
PENDING_SOURCE_BINDING_SCHEMA = """
ALTER TABLE sources ADD COLUMN projected_path TEXT;
ALTER TABLE sources
ADD COLUMN pending_revision INTEGER CHECK (pending_revision IS NULL OR pending_revision >= 0);

UPDATE sources
SET projected_path = current_path
WHERE projected_path IS NULL;

CREATE UNIQUE INDEX idx_sources_kind_projected_path
ON sources(kind, projected_path)
WHERE projected_path IS NOT NULL;
"""
TOPIC_ALIAS_STATE_SCHEMA_VERSION = PENDING_SOURCE_BINDING_SCHEMA_VERSION + 1
TOPIC_ALIAS_STATE_SCHEMA = """
CREATE TABLE topic_alias_topics (
  stable_key TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  normalized_title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  raw_metadata_json TEXT
);

CREATE TABLE topic_aliases (
  normalized_alias TEXT PRIMARY KEY,
  topic_stable_key TEXT NOT NULL
    REFERENCES topic_alias_topics(stable_key) ON DELETE CASCADE,
  alias TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_state_topic_aliases_topic
ON topic_aliases(topic_stable_key, normalized_alias);

CREATE TABLE topic_alias_imports (
  index_path TEXT PRIMARY KEY,
  source_schema_version INTEGER NOT NULL,
  source_alias_count INTEGER NOT NULL CHECK (source_alias_count >= 0),
  source_digest TEXT NOT NULL CHECK (length(source_digest) = 64),
  imported_at TEXT NOT NULL
);
"""
INDEX_GENERATION_STATE_SCHEMA_VERSION = TOPIC_ALIAS_STATE_SCHEMA_VERSION + 1
INDEX_GENERATION_STATE_SCHEMA = """
ALTER TABLE topic_alias_imports RENAME TO topic_alias_imports_v6;

CREATE TABLE index_generations (
  generation_id TEXT NOT NULL,
  index_path TEXT NOT NULL,
  alias_owner TEXT NOT NULL CHECK (alias_owner IN ('state', 'legacy')),
  registered_at TEXT NOT NULL,
  PRIMARY KEY (generation_id, index_path)
);

CREATE TABLE topic_alias_imports (
  generation_id TEXT NOT NULL,
  index_path TEXT NOT NULL,
  source_schema_version INTEGER NOT NULL,
  source_alias_count INTEGER NOT NULL CHECK (source_alias_count >= 0),
  source_digest TEXT NOT NULL CHECK (length(source_digest) = 64),
  imported_at TEXT NOT NULL,
  PRIMARY KEY (generation_id, index_path),
  FOREIGN KEY (generation_id, index_path)
    REFERENCES index_generations(generation_id, index_path) ON DELETE CASCADE
);

INSERT INTO index_generations (
  generation_id, index_path, alias_owner, registered_at
)
SELECT 'legacy-import:' || source_digest, index_path, 'legacy', imported_at
FROM topic_alias_imports_v6;

INSERT INTO topic_alias_imports (
  generation_id, index_path, source_schema_version,
  source_alias_count, source_digest, imported_at
)
SELECT
  'legacy-import:' || source_digest, index_path, source_schema_version,
  source_alias_count, source_digest, imported_at
FROM topic_alias_imports_v6;

DROP TABLE topic_alias_imports_v6;
"""
CURRENT_USER_STATE_SCHEMA_VERSION = INDEX_GENERATION_STATE_SCHEMA_VERSION
TAG_STATE_SCHEMA_VERSION = 2
LEGACY_INDEX_SCHEMA_VERSION = 3
DUPLICATE_LEGACY_IDENTITY_REASON = "duplicate legacy strict identity"
LEGACY_STATE_CONFLICT_REASON = "legacy status conflicts with persistent state"
MISSING_STATE_SOURCE_REASON = "legacy source identity is absent from persistent state"
TASK_STATE_READ_BATCH_SIZE = 100


class AmbiguousSourceIdentityError(RuntimeError):
    def __init__(self, message: str, *, source_uuids: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.source_uuids = source_uuids


@dataclass(frozen=True)
class SourcePathClaim:
    source_uuid: str
    kind: str
    previous_path: str
    projected_path: str
    claimed_path: str
    previous_updated_at: str
    previous_pending_revision: int | None
    claimed_revision: int
    resumed: bool


@dataclass(frozen=True)
class StoredTopic:
    stable_key: str
    title: str
    normalized_title: str
    created_at: str
    updated_at: str
    raw_metadata_json: str | None


@dataclass(frozen=True)
class UserStateFileIdentity:
    physical_path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class StoredTopicAlias:
    topic_stable_key: str
    topic_title: str
    topic_normalized_title: str
    topic_created_at: str
    topic_updated_at: str
    topic_raw_metadata_json: str | None
    alias: str
    normalized_alias: str
    alias_created_at: str

    @property
    def topic(self) -> StoredTopic:
        return StoredTopic(
            stable_key=self.topic_stable_key,
            title=self.topic_title,
            normalized_title=self.topic_normalized_title,
            created_at=self.topic_created_at,
            updated_at=self.topic_updated_at,
            raw_metadata_json=self.topic_raw_metadata_json,
        )


@dataclass(frozen=True)
class TaskIdentity:
    source_uuid: str
    meeting_external_id: str
    chunk_external_id: str
    entity_kind: str
    content_fingerprint: str


@dataclass(frozen=True)
class LegacyStateTransfer:
    migration_key: str
    report_id: int
    index_path: str
    state_path: str
    expected: int
    migrated: int
    orphaned: int


@dataclass(frozen=True)
class StoredTaskState:
    task_state_id: int
    status: str
    note: str | None


@dataclass(frozen=True)
class LegacyMigrationOutcome:
    legacy_action_item_id: int
    task_state_id: int
    legacy_intent_digest: str
    task_identity_digest: str
    outcome: str


class UserStateRepository:
    def __init__(
        self,
        state_path: Path,
        *,
        _read_only: bool = False,
        _expected_identity: UserStateFileIdentity | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.read_only = _read_only
        self._state_identity: UserStateFileIdentity | None = None
        if self.read_only or _expected_identity is not None:
            self._state_identity = _require_user_state_identity(
                self.state_path,
                expected=_expected_identity,
            )
            with self._connect():
                pass
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            ensure_user_state_schema(conn)
        self._state_identity = _require_user_state_identity(
            self.state_path,
            expected=None,
        )

    @classmethod
    def open_existing(cls, state_path: Path) -> "UserStateRepository":
        return cls(state_path, _read_only=True)

    def open_existing_writer(self) -> "UserStateRepository":
        if not self.read_only:
            return self
        if self._state_identity is None:
            message = "A validated user-state identity is required for writable access."
            raise RuntimeError(message)
        return type(self)(
            self.state_path,
            _expected_identity=self._state_identity,
        )

    def get_or_create_source(self, kind: str, path: str, *, now: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT uuid FROM sources WHERE kind = ? AND current_path = ?",
                (kind, path),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE sources SET updated_at = ? WHERE uuid = ?",
                    (now, row["uuid"]),
                )
                conn.commit()
                return str(row["uuid"])
            projected_owner = conn.execute(
                """
                SELECT uuid
                FROM sources
                WHERE kind = ? AND projected_path = ?
                """,
                (kind, path),
            ).fetchone()
            if projected_owner is not None:
                message = (
                    "Source path is still reserved by a pending index projection for UUID "
                    f"{projected_owner['uuid']}."
                )
                raise AmbiguousSourceIdentityError(
                    message,
                    source_uuids=(str(projected_owner["uuid"]),),
                )
            source_uuid = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO sources (
                  uuid, kind, current_path, created_at, updated_at, projected_path
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (source_uuid, kind, path, now, now, path),
            )
            conn.commit()
            return source_uuid

    def resolve_source(self, kind: str, path: Path, *, now: str) -> str:
        canonical_path = canonical_source_path(path)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = _canonical_source_match(conn, kind, canonical_path)
            if row is not None:
                source_uuid = str(row["uuid"])
                conn.execute(
                    "UPDATE sources SET updated_at = ? WHERE uuid = ?",
                    (now, source_uuid),
                )
                conn.commit()
                return source_uuid
            _require_unreserved_projection_path(conn, kind, canonical_path)
            source_uuid = str(uuid.uuid4())
            canonical_string = str(canonical_path)
            conn.execute(
                """
                INSERT INTO sources (
                  uuid, kind, current_path, created_at, updated_at, projected_path
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (source_uuid, kind, canonical_string, now, now, canonical_string),
            )
            conn.commit()
            return source_uuid

    def get_source_by_canonical_path(self, kind: str, path: Path) -> dict[str, Any] | None:
        canonical_path = canonical_source_path(path)
        with self._connect() as conn:
            row = _canonical_source_match(conn, kind, canonical_path)
            return dict(row) if row is not None else None

    def validate_source_path_claim(
        self,
        source_uuid: str,
        kind: str,
        path: Path,
    ) -> None:
        canonical_path = canonical_source_path(path)
        with self._connect() as conn:
            source = _require_source_binding(conn, source_uuid, kind)
            _require_available_claim_target(conn, source_uuid, kind, canonical_path)
            if str(source["kind"]) != kind:
                message = f"Source UUID {source_uuid} has an incompatible source kind."
                raise ValueError(message)

    def claim_source_path(
        self,
        source_uuid: str,
        kind: str,
        path: Path,
        *,
        now: str,
    ) -> SourcePathClaim:
        canonical_path = canonical_source_path(path)
        claimed_path = str(canonical_path)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = _require_source_binding(conn, source_uuid, kind)
            _require_available_claim_target(conn, source_uuid, kind, canonical_path)
            previous_path = str(source["current_path"])
            projected_path = str(source["projected_path"] or previous_path)
            previous_updated_at = str(source["updated_at"])
            previous_revision = int(source["revision"])
            pending_revision = optional_int(source["pending_revision"])
            if pending_revision is not None:
                if previous_path != claimed_path:
                    message = (
                        f"Source UUID {source_uuid} still has a pending projection to "
                        f"{previous_path}; repair it before claiming {claimed_path}."
                    )
                    raise RuntimeError(message)
                claimed_revision = previous_revision + 1
                cursor = conn.execute(
                    """
                    UPDATE sources
                    SET updated_at = ?, revision = ?, pending_revision = ?
                    WHERE uuid = ?
                      AND current_path = ?
                      AND revision = ?
                      AND pending_revision = ?
                    """,
                    (
                        now,
                        claimed_revision,
                        claimed_revision,
                        source_uuid,
                        previous_path,
                        previous_revision,
                        pending_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    message = f"Pending source claim changed while retrying UUID {source_uuid}."
                    raise RuntimeError(message)
                conn.commit()
                return SourcePathClaim(
                    source_uuid=source_uuid,
                    kind=kind,
                    previous_path=previous_path,
                    projected_path=projected_path,
                    claimed_path=claimed_path,
                    previous_updated_at=previous_updated_at,
                    previous_pending_revision=pending_revision,
                    claimed_revision=claimed_revision,
                    resumed=True,
                )

            claimed_revision = previous_revision + 1
            cursor = conn.execute(
                """
                UPDATE sources
                SET current_path = ?, updated_at = ?, revision = ?, pending_revision = ?
                WHERE uuid = ?
                  AND current_path = ?
                  AND revision = ?
                  AND pending_revision IS NULL
                """,
                (
                    claimed_path,
                    now,
                    claimed_revision,
                    claimed_revision,
                    source_uuid,
                    previous_path,
                    previous_revision,
                ),
            )
            if cursor.rowcount != 1:
                message = f"Source path changed while claiming UUID {source_uuid}."
                raise RuntimeError(message)
            conn.commit()
        return SourcePathClaim(
            source_uuid=source_uuid,
            kind=kind,
            previous_path=previous_path,
            projected_path=projected_path,
            claimed_path=claimed_path,
            previous_updated_at=previous_updated_at,
            previous_pending_revision=None,
            claimed_revision=claimed_revision,
            resumed=False,
        )

    def is_source_path_claim_current(self, claim: SourcePathClaim) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM sources
                WHERE uuid = ?
                  AND kind = ?
                  AND current_path = ?
                  AND projected_path = ?
                  AND revision = ?
                  AND pending_revision = ?
                """,
                (
                    claim.source_uuid,
                    claim.kind,
                    claim.claimed_path,
                    claim.projected_path,
                    claim.claimed_revision,
                    claim.claimed_revision,
                ),
            ).fetchone()
            return row is not None

    def finalize_source_path_claim(self, claim: SourcePathClaim) -> bool:
        return self.finalize_source_path_claims((claim,))

    def finalize_source_path_claims(self, claims: tuple[SourcePathClaim, ...]) -> bool:
        if not claims:
            return True
        source_uuids = [claim.source_uuid for claim in claims]
        if len(source_uuids) != len(set(source_uuids)):
            message = "A source path claim batch cannot contain duplicate source UUIDs."
            raise ValueError(message)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for claim in claims:
                    current = conn.execute(
                        """
                        SELECT 1
                        FROM sources
                        WHERE uuid = ?
                          AND kind = ?
                          AND current_path = ?
                          AND projected_path = ?
                          AND revision = ?
                          AND pending_revision = ?
                        """,
                        (
                            claim.source_uuid,
                            claim.kind,
                            claim.claimed_path,
                            claim.projected_path,
                            claim.claimed_revision,
                            claim.claimed_revision,
                        ),
                    ).fetchone()
                    if current is None:
                        conn.rollback()
                        return False
                for claim in claims:
                    cursor = conn.execute(
                        """
                        UPDATE sources
                        SET projected_path = current_path, pending_revision = NULL
                        WHERE uuid = ?
                          AND kind = ?
                          AND current_path = ?
                          AND projected_path = ?
                          AND revision = ?
                          AND pending_revision = ?
                        """,
                        (
                            claim.source_uuid,
                            claim.kind,
                            claim.claimed_path,
                            claim.projected_path,
                            claim.claimed_revision,
                            claim.claimed_revision,
                        ),
                    )
                    if cursor.rowcount != 1:
                        conn.rollback()
                        return False
                    _source_claim_finalize_checkpoint("row")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        _source_claim_finalize_checkpoint("committed")
        return True

    def begin_source_path_rollback(
        self,
        claim: SourcePathClaim,
        *,
        now: str,
    ) -> SourcePathClaim | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                """
                SELECT
                  kind, current_path, projected_path, updated_at,
                  revision, pending_revision
                FROM sources
                WHERE uuid = ?
                """,
                (claim.source_uuid,),
            ).fetchone()
            if (
                current is None
                or str(current["kind"]) != claim.kind
                or str(current["current_path"]) != claim.claimed_path
                or str(current["projected_path"]) != claim.projected_path
                or int(current["revision"]) != claim.claimed_revision
                or optional_int(current["pending_revision"]) != claim.claimed_revision
            ):
                conn.rollback()
                return None
            rollback_revision = claim.claimed_revision + 1
            try:
                cursor = conn.execute(
                    """
                    UPDATE sources
                    SET current_path = ?, projected_path = ?, updated_at = ?,
                        revision = ?, pending_revision = ?
                    WHERE uuid = ?
                      AND kind = ?
                      AND current_path = ?
                      AND projected_path = ?
                      AND revision = ?
                      AND pending_revision = ?
                    """,
                    (
                        claim.projected_path,
                        claim.claimed_path,
                        now,
                        rollback_revision,
                        rollback_revision,
                        claim.source_uuid,
                        claim.kind,
                        claim.claimed_path,
                        claim.projected_path,
                        claim.claimed_revision,
                        claim.claimed_revision,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return None
            if cursor.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
        return SourcePathClaim(
            source_uuid=claim.source_uuid,
            kind=claim.kind,
            previous_path=claim.claimed_path,
            projected_path=claim.claimed_path,
            claimed_path=claim.projected_path,
            previous_updated_at=str(current["updated_at"]),
            previous_pending_revision=claim.claimed_revision,
            claimed_revision=rollback_revision,
            resumed=True,
        )

    def get_source(self, source_uuid: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT uuid, kind, current_path FROM sources WHERE uuid = ?",
                (source_uuid,),
            ).fetchone()
            return dict(row) if row else None

    def get_source_binding(self, source_uuid: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                  uuid, kind, current_path, revision,
                  projected_path, pending_revision, updated_at
                FROM sources
                WHERE uuid = ?
                """,
                (source_uuid,),
            ).fetchone()
            return dict(row) if row else None

    def get_pending_source_path_claim(self, source_uuid: str) -> SourcePathClaim | None:
        binding = self.get_source_binding(source_uuid)
        if binding is None or binding["pending_revision"] is None:
            return None
        projected_path = str(binding["projected_path"] or binding["current_path"])
        return SourcePathClaim(
            source_uuid=source_uuid,
            kind=str(binding["kind"]),
            previous_path=projected_path,
            projected_path=projected_path,
            claimed_path=str(binding["current_path"]),
            previous_updated_at=str(binding["updated_at"]),
            previous_pending_revision=int(binding["pending_revision"]),
            claimed_revision=int(binding["pending_revision"]),
            resumed=True,
        )

    def list_pending_source_path_claims(self) -> tuple[SourcePathClaim, ...]:
        with self._connect() as conn:
            source_uuids = [
                str(row["uuid"])
                for row in conn.execute(
                    "SELECT uuid FROM sources WHERE pending_revision IS NOT NULL ORDER BY uuid"
                ).fetchall()
            ]
        return tuple(
            claim
            for source_uuid in source_uuids
            if (claim := self.get_pending_source_path_claim(source_uuid)) is not None
        )

    def get_sources_by_path(self, kind: str, path: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT uuid, kind, current_path
                FROM sources
                WHERE kind = ? AND current_path = ?
                ORDER BY uuid
                """,
                (kind, path),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_source_by_path(self, kind: str, path: str) -> dict[str, Any] | None:
        sources = self.get_sources_by_path(kind, path)
        return sources[0] if len(sources) == 1 else None

    def get_sources_by_projected_path(self, kind: str, path: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  uuid, kind, current_path, revision,
                  projected_path, pending_revision, updated_at
                FROM sources
                WHERE kind = ? AND projected_path = ?
                ORDER BY uuid
                """,
                (kind, path),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_sources_for_settings_path(self, kind: str, raw_path: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  uuid, kind, current_path, revision,
                  projected_path, pending_revision, updated_at
                FROM sources
                WHERE kind = ?
                  AND (
                    current_path = ?
                    OR (pending_revision IS NOT NULL AND projected_path = ?)
                  )
                ORDER BY uuid
                """,
                (kind, raw_path, raw_path),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_source_for_index_projection(
        self,
        kind: str,
        path: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  uuid, kind, current_path, revision,
                  projected_path, pending_revision, updated_at
                FROM sources
                WHERE kind = ? AND (current_path = ? OR projected_path = ?)
                ORDER BY uuid
                """,
                (kind, path, path),
            ).fetchall()
        unique_rows = {str(row["uuid"]): row for row in rows}
        if len(unique_rows) > 1:
            source_uuids = tuple(unique_rows)
            message = (
                f"Index projection path {path} is claimed by multiple source UUIDs: "
                f"{', '.join(source_uuids)}."
            )
            raise AmbiguousSourceIdentityError(message, source_uuids=source_uuids)
        row = next(iter(unique_rows.values()), None)
        return dict(row) if row is not None else None

    def update_source_path(self, source_uuid: str, path: str, *, now: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE sources
                SET current_path = ?, projected_path = ?, pending_revision = NULL,
                    updated_at = ?, revision = revision + 1
                WHERE uuid = ?
                """,
                (path, path, now, source_uuid),
            )
            if cursor.rowcount != 1:
                message = f"Persistent source not found: {source_uuid}"
                raise ValueError(message)
            conn.commit()

    def topic_for_query(self, query: str) -> StoredTopic | None:
        normalized = _normalize_topic_key(query)
        if not normalized:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                  stable_key, title, normalized_title,
                  created_at, updated_at, raw_metadata_json
                FROM topic_alias_topics
                WHERE stable_key = ?
                """,
                (_topic_stable_key(query),),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT
                      t.stable_key, t.title, t.normalized_title,
                      t.created_at, t.updated_at, t.raw_metadata_json
                    FROM topic_aliases a
                    JOIN topic_alias_topics t ON t.stable_key = a.topic_stable_key
                    WHERE a.normalized_alias = ?
                    """,
                    (normalized,),
                ).fetchone()
        return _stored_topic(row) if row is not None else None

    def list_topics(self) -> tuple[StoredTopic, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  stable_key, title, normalized_title,
                  created_at, updated_at, raw_metadata_json
                FROM topic_alias_topics
                ORDER BY updated_at DESC, title ASC
                """
            ).fetchall()
        return tuple(_stored_topic(row) for row in rows)

    def list_topic_aliases(
        self,
        topic_stable_key: str | None = None,
    ) -> tuple[StoredTopicAlias, ...]:
        with self._connect() as conn:
            if topic_stable_key is None:
                rows = conn.execute(
                    """
                    SELECT
                      t.stable_key AS topic_stable_key,
                      t.title AS topic_title,
                      t.normalized_title AS topic_normalized_title,
                      t.created_at AS topic_created_at,
                      t.updated_at AS topic_updated_at,
                      t.raw_metadata_json AS topic_raw_metadata_json,
                      a.alias,
                      a.normalized_alias,
                      a.created_at AS alias_created_at
                    FROM topic_aliases a
                    JOIN topic_alias_topics t ON t.stable_key = a.topic_stable_key
                    ORDER BY a.normalized_alias
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT
                      t.stable_key AS topic_stable_key,
                      t.title AS topic_title,
                      t.normalized_title AS topic_normalized_title,
                      t.created_at AS topic_created_at,
                      t.updated_at AS topic_updated_at,
                      t.raw_metadata_json AS topic_raw_metadata_json,
                      a.alias,
                      a.normalized_alias,
                      a.created_at AS alias_created_at
                    FROM topic_aliases a
                    JOIN topic_alias_topics t ON t.stable_key = a.topic_stable_key
                    WHERE a.topic_stable_key = ?
                    ORDER BY a.normalized_alias
                    """,
                    (topic_stable_key,),
                ).fetchall()
        return tuple(_stored_topic_alias(row) for row in rows)

    def add_topic_aliases(
        self,
        topic: StoredTopic,
        aliases: list[str],
        *,
        now: str,
    ) -> tuple[str, ...]:
        normalized_aliases = _normalized_alias_values(aliases)
        if not normalized_aliases:
            return ()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                aliases_to_add = _topic_alias_add_plan(conn, topic, normalized_aliases)
                if not aliases_to_add:
                    conn.rollback()
                    return ()
                _insert_stored_topic(conn, topic)
                conn.executemany(
                    """
                    INSERT INTO topic_aliases (
                      normalized_alias, topic_stable_key, alias, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (normalized_alias, topic.stable_key, alias, now)
                        for alias, normalized_alias in aliases_to_add
                    ],
                )
                self._commit_topic_alias_mutation(conn)
            except BaseException:
                conn.rollback()
                raise
        return tuple(alias for alias, _normalized_alias in aliases_to_add)

    def delete_topic_aliases(self, aliases: list[str]) -> tuple[str, ...]:
        normalized_aliases = _normalized_alias_values(aliases)
        removed: list[str] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for alias, normalized_alias in normalized_aliases:
                    row = conn.execute(
                        """
                        SELECT topic_stable_key, alias
                        FROM topic_aliases
                        WHERE normalized_alias = ?
                        """,
                        (normalized_alias,),
                    ).fetchone()
                    if row is None:
                        continue
                    conn.execute(
                        "DELETE FROM topic_aliases WHERE normalized_alias = ?",
                        (normalized_alias,),
                    )
                    removed.append(str(row["alias"]) or alias)
                    conn.execute(
                        """
                        DELETE FROM topic_alias_topics
                        WHERE stable_key = ?
                          AND NOT EXISTS (
                            SELECT 1
                            FROM topic_aliases
                            WHERE topic_stable_key = topic_alias_topics.stable_key
                          )
                        """,
                        (str(row["topic_stable_key"]),),
                    )
                self._commit_topic_alias_mutation(conn)
            except BaseException:
                conn.rollback()
                raise
        return tuple(removed)

    def register_index_generation(
        self,
        generation_id: str,
        index_path: Path,
        alias_owner: str,
        *,
        now: str,
    ) -> bool:
        canonical_index_path = canonical_database_path(index_path)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                inserted = _register_index_generation(
                    conn,
                    generation_id,
                    canonical_index_path,
                    alias_owner,
                    now=now,
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return inserted

    def import_topic_aliases(  # noqa: PLR0913
        self,
        generation_id: str,
        index_path: Path,
        source_schema_version: int,
        aliases: tuple[StoredTopicAlias, ...],
        *,
        now: str,
        verify_snapshot: Callable[[], bool],
    ) -> bool:
        canonical_index_path = canonical_database_path(index_path)
        source_digest = _topic_alias_digest(aliases)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _register_index_generation(
                    conn,
                    generation_id,
                    canonical_index_path,
                    INDEX_ALIAS_OWNER_LEGACY,
                    now=now,
                )
                imported = conn.execute(
                    """
                    SELECT 1
                    FROM topic_alias_imports
                    WHERE generation_id = ? AND index_path = ?
                    """,
                    (generation_id, canonical_index_path),
                ).fetchone()
                if imported is not None:
                    conn.commit()
                    return False
                _validate_topic_alias_import_namespace(conn, aliases)
                for alias in aliases:
                    _insert_stored_topic(conn, alias.topic)
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO topic_aliases (
                          normalized_alias, topic_stable_key, alias, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            alias.normalized_alias,
                            alias.topic_stable_key,
                            alias.alias,
                            alias.alias_created_at,
                        ),
                    )
                    _topic_alias_import_checkpoint("row")
                _topic_alias_import_checkpoint("before_recheck")
                if not verify_snapshot():
                    message = "Index topic aliases changed during persistent-state import."
                    raise RuntimeError(message)  # noqa: TRY301
                conn.execute(
                    """
                    INSERT INTO topic_alias_imports (
                      generation_id, index_path, source_schema_version,
                      source_alias_count, source_digest, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        generation_id,
                        canonical_index_path,
                        source_schema_version,
                        len(aliases),
                        source_digest,
                        now,
                    ),
                )
                _topic_alias_import_checkpoint("before_commit")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        _topic_alias_import_checkpoint("committed")
        return True

    def set_task_state(  # noqa: PLR0913
        self,
        identity: TaskIdentity,
        status: str,
        *,
        note: str | None,
        source: str,
        now: str,
        legacy_action_item_id: int | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_states (
                  source_uuid, meeting_external_id, chunk_external_id,
                  entity_kind, content_fingerprint, status, note, source,
                  orphaned, orphaned_reason, legacy_action_item_id,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?)
                ON CONFLICT(
                  source_uuid, meeting_external_id, chunk_external_id,
                  entity_kind, content_fingerprint
                ) WHERE orphaned = 0 DO UPDATE SET
                  status = excluded.status,
                  note = excluded.note,
                  source = excluded.source,
                  legacy_action_item_id = COALESCE(
                    excluded.legacy_action_item_id,
                    task_states.legacy_action_item_id
                  ),
                  updated_at = excluded.updated_at
                """,
                (
                    identity.source_uuid,
                    identity.meeting_external_id,
                    identity.chunk_external_id,
                    identity.entity_kind,
                    identity.content_fingerprint,
                    status,
                    note,
                    source,
                    legacy_action_item_id,
                    now,
                    now,
                ),
            )
            conn.commit()

    def get_task_state(self, identity: TaskIdentity) -> dict[str, Any] | None:
        return self.get_task_states((identity,)).get(identity)

    def get_task_states(
        self,
        identities: Iterable[TaskIdentity],
    ) -> dict[TaskIdentity, dict[str, Any]]:
        unique_identities = tuple(dict.fromkeys(identities))
        if not unique_identities:
            return {}
        states: dict[TaskIdentity, dict[str, Any]] = {}
        with self._connect() as conn:
            for identity_batch in batched(unique_identities, TASK_STATE_READ_BATCH_SIZE):
                placeholders = ", ".join("(?, ?, ?, ?, ?)" for _ in identity_batch)
                params = tuple(
                    value
                    for identity in identity_batch
                    for value in (
                        identity.source_uuid,
                        identity.meeting_external_id,
                        identity.chunk_external_id,
                        identity.entity_kind,
                        identity.content_fingerprint,
                    )
                )
                sql = f"""
                    SELECT
                      source_uuid, meeting_external_id, chunk_external_id,
                      entity_kind, content_fingerprint,
                      status, note, source, updated_at
                    FROM task_states
                    WHERE (
                      source_uuid, meeting_external_id, chunk_external_id,
                      entity_kind, content_fingerprint
                    ) IN (VALUES {placeholders})
                      AND orphaned = 0
                    """  # noqa: S608
                rows = conn.execute(sql, params).fetchall()
                for row in rows:
                    identity = TaskIdentity(
                        source_uuid=str(row["source_uuid"]),
                        meeting_external_id=str(row["meeting_external_id"]),
                        chunk_external_id=str(row["chunk_external_id"]),
                        entity_kind=str(row["entity_kind"]),
                        content_fingerprint=str(row["content_fingerprint"]),
                    )
                    states[identity] = {
                        "status": str(row["status"]),
                        "note": row["note"],
                        "source": str(row["source"]),
                        "updated_at": str(row["updated_at"]),
                    }
        return states

    def migrate_legacy_index_state(
        self,
        index_path: Path,
        rows: list[LegacyTaskState],
        *,
        now: str,
    ) -> LegacyStateTransfer:
        canonical_index_path = canonical_database_path(index_path)
        migration_key = legacy_state_migration_key(canonical_index_path, rows)
        wrote_state = False

        with self._connect() as conn:
            if conn.in_transaction:
                msg = "Legacy user-state transfer requires a clean connection."
                raise RuntimeError(msg)
            conn.execute("BEGIN IMMEDIATE")
            try:
                report = verified_migration_report(
                    conn,
                    migration_key,
                    canonical_index_path,
                    rows,
                )
                if report is None:
                    _require_absent_migration_report(conn, migration_key)
                    report_id = _adopt_exact_unkeyed_report(
                        conn,
                        migration_key,
                        canonical_index_path,
                        rows,
                    )
                    if report_id is None:
                        report_id = _insert_migration_report(
                            conn,
                            migration_key,
                            canonical_index_path,
                            now=now,
                        )
                        migrated, orphaned = _write_legacy_task_states(
                            conn,
                            report_id,
                            rows,
                        )
                        conn.execute(
                            """
                            UPDATE migration_reports
                            SET migrated = ?, orphaned = ?
                            WHERE id = ?
                            """,
                            (migrated, orphaned, report_id),
                        )
                    _state_transfer_checkpoint("report")
                    report = _require_verified_migration_report(
                        verified_migration_report(
                            conn,
                            migration_key,
                            canonical_index_path,
                            rows,
                        ),
                        "Legacy user-state outcomes could not be verified before commit.",
                    )
                    wrote_state = True
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        if wrote_state:
            _state_transfer_checkpoint("state_committed")
        with self._connect() as conn:
            report = verified_migration_report(
                conn,
                migration_key,
                canonical_index_path,
                rows,
            )
        report = _require_verified_migration_report(
            report,
            "Legacy user-state transfer could not be verified after commit.",
        )
        _state_transfer_checkpoint("state_verified")
        return _legacy_state_transfer(report, self.state_path)

    def latest_migration_report(self) -> dict[str, int] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT migrated, orphaned FROM migration_reports ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            return {
                "migrated": int(row["migrated"]),
                "orphaned": int(row["orphaned"]),
            }

    def list_orphans(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return rows_to_dicts(
                conn.execute("SELECT * FROM task_states WHERE orphaned = 1 ORDER BY id")
            )

    def _commit_topic_alias_mutation(self, conn: sqlite3.Connection) -> None:
        _topic_alias_mutation_checkpoint("before_identity_recheck")
        if self._state_identity is not None:
            _require_user_state_identity(
                self.state_path,
                expected=self._state_identity,
            )
        conn.commit()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        if self._state_identity is not None:
            identity = _require_user_state_identity(
                self.state_path,
                expected=self._state_identity,
            )
            mode = "ro" if self.read_only else "rw"
            uri = f"{identity.physical_path.as_uri()}?mode={mode}"
            try:
                connection = sqlite3.connect(uri, uri=True)
            except sqlite3.Error as exc:
                raise IndexReadError(_changed_user_state_message(self.state_path)) from exc
        else:
            connection = sqlite3.connect(self.state_path)
        with closing(connection) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            if self.read_only:
                conn.execute("PRAGMA query_only=ON")
            if self._state_identity is not None:
                validate_existing_user_state_schema(conn)
                _require_user_state_identity(
                    self.state_path,
                    expected=self._state_identity,
                )
            yield conn


def _require_user_state_identity(
    state_path: Path,
    *,
    expected: UserStateFileIdentity | None,
) -> UserStateFileIdentity:
    try:
        physical_path = Path(state_path).resolve(strict=True)
        path_stat = physical_path.stat()
    except OSError as exc:
        message = (
            _changed_user_state_message(state_path)
            if expected is not None
            else missing_user_state_message(state_path)
        )
        raise IndexReadError(message) from exc
    if not physical_path.is_file():
        message = (
            _changed_user_state_message(state_path)
            if expected is not None
            else missing_user_state_message(state_path)
        )
        raise IndexReadError(message)
    identity = UserStateFileIdentity(
        physical_path=physical_path,
        device=path_stat.st_dev,
        inode=path_stat.st_ino,
    )
    if expected is not None and identity != expected:
        raise IndexReadError(_changed_user_state_message(state_path))
    return identity


def _changed_user_state_message(state_path: Path) -> str:
    return (
        f"Meetily Memory user state no longer matches the database validated at {state_path}. "
        "Restore the authoritative `state.sqlite` from backup; refusing to create, migrate, or "
        "write a missing, replaced, or retargeted state database."
    )


def recover_and_validate_index(index_path: Path) -> int | None:
    index_path = Path(index_path)
    if not index_path.is_file():
        return None
    with closing(sqlite3.connect(index_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = _supported_index_version(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return version


def prepare_index_user_state(
    index_path: Path,
    state_path: Path,
    *,
    now: str,
) -> UserStateRepository:
    index_path = Path(index_path)
    if not index_path.is_file():
        return UserStateRepository(state_path)

    with closing(sqlite3.connect(index_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = _supported_index_version(conn)
            marker = read_index_generation_marker(conn)
            state = UserStateRepository(state_path)
            if marker is not None and marker[1] == INDEX_ALIAS_OWNER_STATE:
                state.register_index_generation(
                    marker[0],
                    index_path,
                    INDEX_ALIAS_OWNER_STATE,
                    now=now,
                )
            elif marker is not None or _has_index_topic_alias_schema(conn):
                aliases, alias_count, alias_digest = _index_topic_alias_snapshot(conn)
                generation_id = (
                    marker[0] if marker is not None else _legacy_index_generation_id(index_path)
                )

                def verify_snapshot() -> bool:
                    current_aliases, current_count, current_digest = _index_topic_alias_snapshot(
                        conn
                    )
                    return (
                        current_count == alias_count
                        and current_digest == alias_digest
                        and current_aliases == aliases
                    )

                state.import_topic_aliases(
                    generation_id,
                    index_path,
                    version,
                    aliases,
                    now=now,
                    verify_snapshot=verify_snapshot,
                )
                if marker is None and version == CURRENT_SCHEMA_VERSION:
                    ensure_index_generation_marker(
                        conn,
                        alias_owner=INDEX_ALIAS_OWNER_LEGACY,
                        generation_id=generation_id,
                    )
            conn.commit()
            return state  # noqa: TRY300
        except BaseException:
            conn.rollback()
            raise


def register_state_owned_index_generation(
    index_path: Path,
    ledger_path: Path,
    state: UserStateRepository,
    *,
    now: str,
) -> str:
    index_path = Path(index_path)
    with closing(sqlite3.connect(index_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            _supported_index_version(conn)
            marker = read_index_generation_marker(conn)
            if marker is None:
                message = "Current index is missing its stable generation marker."
                raise RuntimeError(message)  # noqa: TRY301
            generation_id, alias_owner = marker
            if alias_owner != INDEX_ALIAS_OWNER_STATE:
                message = "Only state-owned index generations can register projection paths."
                raise RuntimeError(message)  # noqa: TRY301
            state.register_index_generation(
                generation_id,
                ledger_path,
                INDEX_ALIAS_OWNER_STATE,
                now=now,
            )
            conn.commit()
            return generation_id  # noqa: TRY300
        except BaseException:
            conn.rollback()
            raise


def _legacy_index_generation_id(index_path: Path) -> str:
    physical_path = Path(index_path).resolve(strict=True)
    path_stat = physical_path.stat()
    birth_time = getattr(path_stat, "st_birthtime", None)
    identity = f"{path_stat.st_dev}\0{path_stat.st_ino}\0{birth_time or ''}"
    return f"legacy-fs:{hashlib.sha256(identity.encode()).hexdigest()}"


def _supported_index_version(conn: sqlite3.Connection) -> int:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > CURRENT_SCHEMA_VERSION:
        message = (
            f"Unsupported index schema version {version}; "
            f"this binary supports {CURRENT_SCHEMA_VERSION}."
        )
        raise RuntimeError(message)
    return version


def _has_index_topic_alias_schema(conn: sqlite3.Connection) -> bool:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    return {"knowledge_nodes", "topic_aliases"}.issubset(tables)


def _index_topic_alias_snapshot(
    conn: sqlite3.Connection,
) -> tuple[tuple[StoredTopicAlias, ...], int, str]:
    if not _has_index_topic_alias_schema(conn):
        aliases: tuple[StoredTopicAlias, ...] = ()
        return aliases, 0, _topic_alias_digest(aliases)
    alias_count = int(conn.execute("SELECT COUNT(*) FROM topic_aliases").fetchone()[0])
    rows = conn.execute(
        """
        SELECT
          n.type AS topic_type,
          n.stable_key AS topic_stable_key,
          n.title AS topic_title,
          n.normalized_title AS topic_normalized_title,
          n.created_at AS topic_created_at,
          n.updated_at AS topic_updated_at,
          n.raw_metadata_json AS topic_raw_metadata_json,
          a.alias,
          a.normalized_alias,
          a.created_at AS alias_created_at
        FROM topic_aliases a
        JOIN knowledge_nodes n ON n.id = a.topic_node_id
        ORDER BY a.normalized_alias
        """
    ).fetchall()
    if len(rows) != alias_count or any(str(row["topic_type"]) != "Topic" for row in rows):
        message = "Index topic aliases do not have a complete valid topic projection."
        raise RuntimeError(message)
    aliases = tuple(_stored_topic_alias(row) for row in rows)
    return aliases, alias_count, _topic_alias_digest(aliases)


def find_existing_source_by_uuid(
    state_path: Path,
    source_uuid: str,
) -> dict[str, Any] | None:
    state_path = Path(state_path)
    if not state_path.is_file():
        return None
    uri = f"{state_path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > CURRENT_USER_STATE_SCHEMA_VERSION:
            message = (
                f"Unsupported user-state schema version {version}; "
                f"this binary supports {CURRENT_USER_STATE_SCHEMA_VERSION}."
            )
            raise RuntimeError(message)
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sources)").fetchall()}
        if not {"uuid", "kind", "current_path"}.issubset(columns):
            return None
        row = conn.execute(
            """
            SELECT uuid, kind, current_path
            FROM sources
            WHERE uuid = ?
            """,
            (source_uuid,),
        ).fetchone()
    return dict(row) if row is not None else None


def find_existing_source_by_canonical_path(
    state_path: Path,
    kind: str,
    path: Path,
) -> dict[str, Any] | None:
    canonical_path = canonical_source_path(path)
    state_path = Path(state_path)
    if not state_path.is_file():
        return None
    uri = f"{state_path.resolve(strict=True).as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > CURRENT_USER_STATE_SCHEMA_VERSION:
            message = (
                f"Unsupported user-state schema version {version}; "
                f"this binary supports {CURRENT_USER_STATE_SCHEMA_VERSION}."
            )
            raise RuntimeError(message)
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sources)").fetchall()}
        if not {"uuid", "kind", "current_path"}.issubset(columns):
            return None
        rows = conn.execute(
            """
            SELECT uuid, kind, current_path
            FROM sources
            WHERE kind = ?
            ORDER BY uuid
            """,
            (kind,),
        ).fetchall()
    exact, collision_hints = _classify_source_path_claims(rows, canonical_path)
    row = _unique_canonical_source_match(exact, collision_hints, canonical_path)
    return dict(row) if row is not None else None


def find_existing_source_for_settings_path(
    state_path: Path,
    kind: str,
    raw_path: str,
) -> dict[str, Any] | None:
    state_path = Path(state_path)
    if not state_path.is_file():
        return None
    uri = f"{state_path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > CURRENT_USER_STATE_SCHEMA_VERSION:
            message = (
                f"Unsupported user-state schema version {version}; "
                f"this binary supports {CURRENT_USER_STATE_SCHEMA_VERSION}."
            )
            raise RuntimeError(message)
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sources)").fetchall()}
        if not {"uuid", "kind", "current_path"}.issubset(columns):
            return None
        if {"projected_path", "pending_revision"}.issubset(columns):
            rows = conn.execute(
                """
                SELECT uuid, kind, current_path, projected_path, pending_revision
                FROM sources
                WHERE kind = ?
                  AND (
                    current_path = ?
                    OR (pending_revision IS NOT NULL AND projected_path = ?)
                  )
                ORDER BY uuid
                """,
                (kind, raw_path, raw_path),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT uuid, kind, current_path
                FROM sources
                WHERE kind = ? AND current_path = ?
                ORDER BY uuid
                """,
                (kind, raw_path),
            ).fetchall()
    if len(rows) > 1:
        source_uuids = tuple(str(row["uuid"]) for row in rows)
        message = (
            f"Legacy settings path {raw_path} maps to multiple source UUIDs: "
            f"{', '.join(source_uuids)}."
        )
        raise AmbiguousSourceIdentityError(message, source_uuids=source_uuids)
    return dict(rows[0]) if rows else None


def _source_path_claims(
    conn: sqlite3.Connection,
    kind: str,
    canonical_path: Path,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    rows = conn.execute(
        """
        SELECT
          uuid, kind, current_path, revision,
          projected_path, pending_revision, updated_at
        FROM sources
        WHERE kind = ?
        ORDER BY uuid
        """,
        (kind,),
    ).fetchall()
    return _classify_source_path_claims(rows, canonical_path)


def _source_target_claims(
    conn: sqlite3.Connection,
    kind: str,
    canonical_path: Path,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    rows = conn.execute(
        """
        SELECT
          uuid, kind, current_path, revision,
          projected_path, pending_revision, updated_at
        FROM sources
        WHERE kind = ?
        ORDER BY uuid
        """,
        (kind,),
    ).fetchall()
    canonical_string = str(canonical_path)
    exact: dict[str, sqlite3.Row] = {}
    collision_hints: dict[str, sqlite3.Row] = {}
    for row in rows:
        stored_paths = {str(row["current_path"])}
        if row["projected_path"] is not None:
            stored_paths.add(str(row["projected_path"]))
        for stored_string in stored_paths:
            if stored_string == canonical_string:
                exact[str(row["uuid"])] = row
                continue
            try:
                resolved_stored_path = canonical_source_path(Path(stored_string))
            except (OSError, RuntimeError):
                continue
            if resolved_stored_path == canonical_path:
                collision_hints[str(row["uuid"])] = row
    return list(exact.values()), list(collision_hints.values())


def _require_source_binding(
    conn: sqlite3.Connection,
    source_uuid: str,
    kind: str,
) -> sqlite3.Row:
    source = conn.execute(
        """
        SELECT
          uuid, kind, current_path, updated_at, revision,
          projected_path, pending_revision
        FROM sources
        WHERE uuid = ?
        """,
        (source_uuid,),
    ).fetchone()
    if source is None:
        message = f"Source UUID not found in user state: {source_uuid}."
        raise ValueError(message)
    if str(source["kind"]) != kind:
        message = f"Source UUID {source_uuid} has an incompatible source kind."
        raise ValueError(message)
    return source


def _require_available_claim_target(
    conn: sqlite3.Connection,
    source_uuid: str,
    kind: str,
    canonical_path: Path,
) -> None:
    exact, collision_hints = _source_target_claims(conn, kind, canonical_path)
    conflicts = {
        str(row["uuid"]): row
        for row in (*exact, *collision_hints)
        if str(row["uuid"]) != source_uuid
    }
    if not conflicts:
        return
    conflicting_uuids = tuple(conflicts)
    source_uuids = ", ".join(conflicting_uuids)
    message = f"The rebind target is already linked to another source UUID: {source_uuids}."
    raise AmbiguousSourceIdentityError(message, source_uuids=conflicting_uuids)


def _require_unreserved_projection_path(
    conn: sqlite3.Connection,
    kind: str,
    canonical_path: Path,
) -> None:
    exact, collision_hints = _source_target_claims(conn, kind, canonical_path)
    conflicts = {str(row["uuid"]): row for row in (*exact, *collision_hints)}
    if not conflicts:
        return
    conflicting_uuids = tuple(conflicts)
    message = (
        f"Source path {canonical_path} is reserved by source UUID(s) "
        f"{', '.join(conflicting_uuids)}."
    )
    raise AmbiguousSourceIdentityError(message, source_uuids=conflicting_uuids)


def _classify_source_path_claims(
    rows: list[sqlite3.Row],
    canonical_path: Path,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    canonical_string = str(canonical_path)
    exact: list[sqlite3.Row] = []
    collision_hints: list[sqlite3.Row] = []
    for row in rows:
        stored_string = str(row["current_path"])
        if stored_string == canonical_string:
            exact.append(row)
            continue
        try:
            resolved_stored_path = canonical_source_path(Path(stored_string))
        except (OSError, RuntimeError):
            continue
        if resolved_stored_path == canonical_path:
            collision_hints.append(row)
    return exact, collision_hints


def _canonical_source_match(
    conn: sqlite3.Connection,
    kind: str,
    canonical_path: Path,
) -> sqlite3.Row | None:
    exact, collision_hints = _source_path_claims(conn, kind, canonical_path)
    return _unique_canonical_source_match(exact, collision_hints, canonical_path)


def _unique_canonical_source_match(
    exact: list[sqlite3.Row],
    collision_hints: list[sqlite3.Row],
    canonical_path: Path,
) -> sqlite3.Row | None:
    if collision_hints:
        conflicting_uuids = tuple(str(row["uuid"]) for row in collision_hints)
        source_uuids = ", ".join(conflicting_uuids)
        message = (
            f"Source path {canonical_path} has an ambiguous collision with non-canonical "
            f"user-state path(s) for UUID(s) {source_uuids}. Automatic reuse or duplicate "
            "registration is unsafe; run `mm config source NEW_PATH --rebind --source-uuid UUID`."
        )
        raise AmbiguousSourceIdentityError(message, source_uuids=conflicting_uuids)
    if len(exact) > 1:
        conflicting_uuids = tuple(str(row["uuid"]) for row in exact)
        source_uuids = ", ".join(conflicting_uuids)
        message = (
            f"Canonical source path is ambiguous in user state: {canonical_path}. "
            f"Conflicting source UUIDs: {source_uuids}."
        )
        raise AmbiguousSourceIdentityError(message, source_uuids=conflicting_uuids)
    return exact[0] if exact else None


def optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, str)):
        return int(value)
    message = f"Expected integer-compatible value, got {type(value).__name__}."
    raise TypeError(message)


def _normalize_topic_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _topic_stable_key(title: str) -> str:
    return f"topic:{_normalize_topic_key(title)}"


def _normalized_alias_values(aliases: list[str]) -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = []
    seen: set[str] = set()
    for alias in aliases:
        normalized = _normalize_topic_key(alias)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        values.append((alias, normalized))
    return tuple(values)


def _topic_alias_add_plan(
    conn: sqlite3.Connection,
    topic: StoredTopic,
    aliases: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    canonical_namespace = _validate_stored_topic_identity(topic)
    canonical_owners, alias_owners = _topic_namespace_owners(conn)
    owner = topic.stable_key
    if canonical_owners.get(canonical_namespace, owner) != owner:
        return ()
    if alias_owners.get(canonical_namespace, owner) != owner:
        return ()

    existing_topic = conn.execute(
        "SELECT normalized_title FROM topic_alias_topics WHERE stable_key = ?",
        (owner,),
    ).fetchone()
    if (
        existing_topic is not None
        and _normalize_topic_key(str(existing_topic["normalized_title"])) != canonical_namespace
    ):
        message = f"Stored topic metadata conflicts with canonical identity {owner}."
        raise RuntimeError(message)

    planned: list[tuple[str, str]] = []
    for alias, normalized_alias in aliases:
        if normalized_alias == canonical_namespace:
            continue
        canonical_owner = canonical_owners.get(normalized_alias)
        if canonical_owner is not None and canonical_owner != owner:
            return ()
        alias_owner = alias_owners.get(normalized_alias)
        if alias_owner is not None:
            if alias_owner != owner:
                return ()
            continue
        planned.append((alias, normalized_alias))
    return tuple(planned)


def _validate_topic_alias_import_namespace(
    conn: sqlite3.Connection,
    aliases: tuple[StoredTopicAlias, ...],
) -> None:
    canonical_owners, alias_owners = _topic_namespace_owners(conn)
    for alias in aliases:
        canonical_namespace = _validate_stored_topic_identity(alias.topic)
        owner = alias.topic_stable_key
        canonical_owner = canonical_owners.get(canonical_namespace)
        if canonical_owner is not None and canonical_owner != owner:
            _raise_topic_namespace_conflict(canonical_namespace)
        alias_owner = alias_owners.get(canonical_namespace)
        if alias_owner is not None and alias_owner != owner:
            _raise_topic_namespace_conflict(canonical_namespace)
        canonical_owners[canonical_namespace] = owner

    for alias in aliases:
        normalized_alias = _normalize_topic_key(alias.normalized_alias)
        owner = alias.topic_stable_key
        canonical_owner = canonical_owners.get(normalized_alias)
        if canonical_owner is not None and canonical_owner != owner:
            _raise_topic_namespace_conflict(normalized_alias)
        alias_owner = alias_owners.get(normalized_alias)
        if alias_owner is not None and alias_owner != owner:
            _raise_topic_namespace_conflict(normalized_alias)
        alias_owners[normalized_alias] = owner


def _topic_namespace_owners(
    conn: sqlite3.Connection,
) -> tuple[dict[str, str], dict[str, str]]:
    canonical_owners: dict[str, str] = {}
    for row in conn.execute(
        "SELECT stable_key, normalized_title FROM topic_alias_topics"
    ).fetchall():
        namespace = _normalize_topic_key(str(row["normalized_title"]))
        owner = str(row["stable_key"])
        existing_owner = canonical_owners.setdefault(namespace, owner)
        if existing_owner != owner:
            _raise_topic_namespace_conflict(namespace)

    alias_owners: dict[str, str] = {}
    for row in conn.execute(
        "SELECT normalized_alias, topic_stable_key FROM topic_aliases"
    ).fetchall():
        namespace = _normalize_topic_key(str(row["normalized_alias"]))
        owner = str(row["topic_stable_key"])
        existing_owner = alias_owners.setdefault(namespace, owner)
        if existing_owner != owner:
            _raise_topic_namespace_conflict(namespace)
        canonical_owner = canonical_owners.get(namespace)
        if canonical_owner is not None and canonical_owner != owner:
            _raise_topic_namespace_conflict(namespace)
    return canonical_owners, alias_owners


def _validate_stored_topic_identity(topic: StoredTopic) -> str:
    canonical_namespace = _normalize_topic_key(topic.normalized_title)
    if (
        not canonical_namespace
        or _normalize_topic_key(topic.title) != canonical_namespace
        or topic.stable_key != _topic_stable_key(canonical_namespace)
    ):
        message = f"Invalid canonical topic identity: {topic.stable_key}."
        raise ValueError(message)
    return canonical_namespace


def _raise_topic_namespace_conflict(namespace: str) -> None:
    message = f"Topic canonical/alias namespace has conflicting owners for {namespace!r}."
    raise RuntimeError(message)


def _stored_topic(row: sqlite3.Row) -> StoredTopic:
    return StoredTopic(
        stable_key=str(row["stable_key"]),
        title=str(row["title"]),
        normalized_title=str(row["normalized_title"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        raw_metadata_json=(
            str(row["raw_metadata_json"]) if row["raw_metadata_json"] is not None else None
        ),
    )


def _stored_topic_alias(row: sqlite3.Row) -> StoredTopicAlias:
    return StoredTopicAlias(
        topic_stable_key=str(row["topic_stable_key"]),
        topic_title=str(row["topic_title"]),
        topic_normalized_title=str(row["topic_normalized_title"]),
        topic_created_at=str(row["topic_created_at"]),
        topic_updated_at=str(row["topic_updated_at"]),
        topic_raw_metadata_json=(
            str(row["topic_raw_metadata_json"])
            if row["topic_raw_metadata_json"] is not None
            else None
        ),
        alias=str(row["alias"]),
        normalized_alias=str(row["normalized_alias"]),
        alias_created_at=str(row["alias_created_at"]),
    )


def _register_index_generation(
    conn: sqlite3.Connection,
    generation_id: str,
    canonical_index_path: str,
    alias_owner: str,
    *,
    now: str,
) -> bool:
    if alias_owner not in {INDEX_ALIAS_OWNER_STATE, INDEX_ALIAS_OWNER_LEGACY}:
        message = f"Unsupported index alias owner: {alias_owner}."
        raise ValueError(message)
    existing = conn.execute(
        """
        SELECT alias_owner
        FROM index_generations
        WHERE generation_id = ? AND index_path = ?
        """,
        (generation_id, canonical_index_path),
    ).fetchone()
    if existing is not None:
        if str(existing["alias_owner"]) != alias_owner:
            message = "Index generation alias ownership conflicts with its persistent-state ledger."
            raise RuntimeError(message)
        return False
    conn.execute(
        """
        INSERT INTO index_generations (
          generation_id, index_path, alias_owner, registered_at
        ) VALUES (?, ?, ?, ?)
        """,
        (generation_id, canonical_index_path, alias_owner, now),
    )
    return True


def _insert_stored_topic(conn: sqlite3.Connection, topic: StoredTopic) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO topic_alias_topics (
          stable_key, title, normalized_title,
          created_at, updated_at, raw_metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            topic.stable_key,
            topic.title,
            topic.normalized_title,
            topic.created_at,
            topic.updated_at,
            topic.raw_metadata_json,
        ),
    )


def _topic_alias_digest(aliases: tuple[StoredTopicAlias, ...]) -> str:
    digest = hashlib.sha256()
    for alias in aliases:
        values = (
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
        for value in values:
            encoded = (value if value is not None else "\0").encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def task_identity(
    source_uuid: str,
    meeting_external_id: str,
    chunk_external_id: str,
    text: str,
) -> TaskIdentity:
    return TaskIdentity(
        source_uuid=source_uuid,
        meeting_external_id=meeting_external_id,
        chunk_external_id=chunk_external_id,
        entity_kind="task",
        content_fingerprint=content_fingerprint(text),
    )


def content_fingerprint(text: str) -> str:
    normalized = " ".join(text.casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def validate_existing_user_state_schema(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version != CURRENT_USER_STATE_SCHEMA_VERSION:
        if version > CURRENT_USER_STATE_SCHEMA_VERSION:
            message = (
                f"User-state schema {version} is newer than supported schema "
                f"{CURRENT_USER_STATE_SCHEMA_VERSION}. Update Meetily Memory before reading it."
            )
        else:
            message = (
                f"User-state schema {version} is outdated; schema "
                f"{CURRENT_USER_STATE_SCHEMA_VERSION} is required. "
                "Run `mm refresh` or `mm scan --source PATH` to migrate writer-owned state."
            )
        raise IndexReadError(message)


def ensure_user_state_schema(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > CURRENT_USER_STATE_SCHEMA_VERSION:
        message = (
            f"Unsupported user-state schema version {version}; "
            f"this binary supports {CURRENT_USER_STATE_SCHEMA_VERSION}."
        )
        raise RuntimeError(message)
    migrations = (
        (1, USER_STATE_SCHEMA),
        (TAG_STATE_SCHEMA_VERSION, TAG_STATE_SCHEMA),
        (MIGRATION_REPORT_SCHEMA_VERSION, MIGRATION_REPORT_SCHEMA),
        (SOURCE_REVISION_SCHEMA_VERSION, SOURCE_REVISION_SCHEMA),
        (PENDING_SOURCE_BINDING_SCHEMA_VERSION, PENDING_SOURCE_BINDING_SCHEMA),
        (TOPIC_ALIAS_STATE_SCHEMA_VERSION, TOPIC_ALIAS_STATE_SCHEMA),
        (INDEX_GENERATION_STATE_SCHEMA_VERSION, INDEX_GENERATION_STATE_SCHEMA),
    )
    for target_version, script in migrations:
        if version < target_version:
            _apply_user_state_schema_migration(conn, target_version, script)


def _require_user_state_predecessor(current_version: int, target_version: int) -> None:
    if current_version != target_version - 1:
        msg = (
            f"Cannot migrate user-state schema from version {current_version} "
            f"directly to version {target_version}."
        )
        raise RuntimeError(msg)


def _apply_user_state_schema_migration(
    conn: sqlite3.Connection,
    target_version: int,
    script: str,
) -> None:
    if conn.in_transaction:
        msg = f"User-state migration to version {target_version} requires a clean connection."
        raise RuntimeError(msg)

    conn.execute("BEGIN IMMEDIATE")
    try:
        current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current_version >= target_version:
            conn.commit()
            return
        _require_user_state_predecessor(current_version, target_version)
        execute_sql_statements(conn, script)
        conn.execute(f"PRAGMA user_version = {target_version}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _state_transfer_checkpoint(_name: str) -> None:
    # A narrow no-op seam lets tests interrupt the cross-database handoff.
    return


def _topic_alias_import_checkpoint(_name: str) -> None:
    return


def _topic_alias_mutation_checkpoint(_name: str) -> None:
    return


def _source_claim_finalize_checkpoint(_name: str) -> None:
    return


def _require_lastrowid(cursor: sqlite3.Cursor) -> int:
    lastrowid = cursor.lastrowid
    if lastrowid is None:
        msg = "SQLite did not return a migration row id."
        raise RuntimeError(msg)
    return lastrowid


def _migration_report_exists(conn: sqlite3.Connection, migration_key: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM migration_reports WHERE migration_key = ?",
            (migration_key,),
        ).fetchone()
        is not None
    )


def _require_absent_migration_report(
    conn: sqlite3.Connection,
    migration_key: str,
) -> None:
    if _migration_report_exists(conn, migration_key):
        msg = "The durable legacy migration report is incomplete or invalid."
        raise RuntimeError(msg)


def _require_verified_migration_report(
    report: VerifiedMigrationReport | None,
    message: str,
) -> VerifiedMigrationReport:
    if report is None:
        raise RuntimeError(message)
    return report


def _insert_migration_report(
    conn: sqlite3.Connection,
    migration_key: str,
    index_path: str,
    *,
    now: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO migration_reports (
          index_path, migrated, orphaned, created_at, migration_key
        ) VALUES (?, 0, 0, ?, ?)
        """,
        (index_path, now, migration_key),
    )
    return _require_lastrowid(cursor)


def _legacy_state_transfer(
    report: VerifiedMigrationReport,
    state_path: Path,
) -> LegacyStateTransfer:
    return LegacyStateTransfer(
        migration_key=report.migration_key,
        report_id=report.report_id,
        index_path=report.index_path,
        state_path=canonical_database_path(state_path),
        expected=report.expected,
        migrated=report.migrated,
        orphaned=report.orphaned,
    )


def _adopt_exact_unkeyed_report(
    conn: sqlite3.Connection,
    migration_key: str,
    index_path: str,
    rows: list[LegacyTaskState],
) -> int | None:
    outcomes = _exact_historical_outcomes(conn, rows)
    if outcomes is None:
        return None
    migrated = sum(outcome.outcome == "active_existing" for outcome in outcomes)
    orphaned = len(outcomes) - migrated
    report_id = _single_matching_unkeyed_report(
        conn,
        index_path,
        migrated=migrated,
        orphaned=orphaned,
    )
    if report_id is None:
        return None

    cursor = conn.execute(
        """
        UPDATE migration_reports
        SET migration_key = ?, index_path = ?
        WHERE id = ? AND migration_key IS NULL
        """,
        (migration_key, index_path, report_id),
    )
    if cursor.rowcount != 1:
        msg = "The historical migration report changed while it was being adopted."
        raise RuntimeError(msg)
    conn.executemany(
        """
        INSERT INTO migration_report_items (
          report_id, legacy_action_item_id, task_state_id,
          legacy_intent_digest, task_identity_digest, outcome
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                report_id,
                outcome.legacy_action_item_id,
                outcome.task_state_id,
                outcome.legacy_intent_digest,
                outcome.task_identity_digest,
                outcome.outcome,
            )
            for outcome in outcomes
        ],
    )
    return report_id


def _single_matching_unkeyed_report(
    conn: sqlite3.Connection,
    index_path: str,
    *,
    migrated: int,
    orphaned: int,
) -> int | None:
    reports = conn.execute(
        """
        SELECT r.id, r.index_path
        FROM migration_reports r
        WHERE r.migration_key IS NULL
          AND r.migrated = ?
          AND r.orphaned = ?
          AND NOT EXISTS (
            SELECT 1 FROM migration_report_items i WHERE i.report_id = r.id
          )
        ORDER BY r.id
        """,
        (migrated, orphaned),
    ).fetchall()
    matching = [
        int(report["id"])
        for report in reports
        if canonical_database_path(str(report["index_path"])) == index_path
    ]
    return matching[0] if len(matching) == 1 else None


def _exact_historical_outcomes(
    conn: sqlite3.Connection,
    rows: list[LegacyTaskState],
) -> list[LegacyMigrationOutcome] | None:
    source_uuids: dict[tuple[str, str], str | None] = {}
    claimed_identities: set[TaskIdentity] = set()
    outcomes: list[LegacyMigrationOutcome] = []
    for row in rows:
        source_uuid: str | None = None
        if row.source_kind is not None and row.source_path is not None:
            source_key = (row.source_kind, row.source_path)
            if source_key not in source_uuids:
                source_uuids[source_key] = _exact_historical_source_uuid(
                    conn,
                    *source_key,
                )
            source_uuid = source_uuids[source_key]

        orphaned_reason = row.orphaned_reason()
        if orphaned_reason is not None:
            task_state_id = _exact_historical_orphan_id(
                conn,
                row,
                source_uuid=source_uuid,
                reason=orphaned_reason,
            )
            if task_state_id is None:
                return None
            outcomes.append(
                _migration_outcome(
                    row,
                    _orphan_persisted_identity(
                        task_state_id,
                        row,
                        source_uuid=source_uuid,
                        reason=orphaned_reason,
                    ),
                    "orphan_missing_identity",
                )
            )
            continue
        if source_uuid is None:
            return None
        identity = _required_legacy_identity(row, source_uuid)
        if identity in claimed_identities:
            return None
        claimed_identities.add(identity)
        task_state_id = _exact_historical_active_id(conn, identity, row)
        if task_state_id is None:
            return None
        outcomes.append(
            _migration_outcome(
                row,
                _active_persisted_identity(task_state_id, identity),
                "active_existing",
            )
        )
    return outcomes


def _exact_historical_source_uuid(
    conn: sqlite3.Connection,
    kind: str,
    path: str,
) -> str | None:
    sources = conn.execute(
        """
        SELECT uuid
        FROM sources
        WHERE kind = ? AND (current_path = ? OR projected_path = ?)
        """,
        (kind, path, path),
    ).fetchall()
    unique_uuids = {str(source["uuid"]) for source in sources}
    return next(iter(unique_uuids)) if len(unique_uuids) == 1 else None


def _exact_historical_active_id(
    conn: sqlite3.Connection,
    identity: TaskIdentity,
    row: LegacyTaskState,
) -> int | None:
    states = conn.execute(
        """
        SELECT id
        FROM task_states
        WHERE source_uuid = ?
          AND meeting_external_id = ?
          AND chunk_external_id = ?
          AND entity_kind = ?
          AND content_fingerprint = ?
          AND status = ?
          AND note IS ?
          AND source = ?
          AND orphaned = 0
          AND legacy_action_item_id = ?
          AND updated_at = ?
        """,
        (
            identity.source_uuid,
            identity.meeting_external_id,
            identity.chunk_external_id,
            identity.entity_kind,
            identity.content_fingerprint,
            row.status,
            row.note,
            row.source,
            row.action_item_id,
            row.updated_at,
        ),
    ).fetchall()
    return int(states[0]["id"]) if len(states) == 1 else None


def _exact_historical_orphan_id(
    conn: sqlite3.Connection,
    row: LegacyTaskState,
    *,
    source_uuid: str | None,
    reason: str,
) -> int | None:
    states = conn.execute(
        """
        SELECT id
        FROM task_states
        WHERE source_uuid IS ?
          AND meeting_external_id IS ?
          AND chunk_external_id IS ?
          AND entity_kind = 'task'
          AND content_fingerprint = ?
          AND status = ?
          AND note IS ?
          AND source = ?
          AND orphaned = 1
          AND orphaned_reason = ?
          AND legacy_action_item_id = ?
          AND created_at = ?
          AND updated_at = ?
        """,
        (
            source_uuid,
            row.meeting_external_id,
            row.chunk_external_id,
            content_fingerprint(row.text or ""),
            row.status,
            row.note,
            row.source,
            reason,
            row.action_item_id,
            row.created_at,
            row.updated_at,
        ),
    ).fetchall()
    return int(states[0]["id"]) if len(states) == 1 else None


def _active_persisted_identity(
    task_state_id: int,
    identity: TaskIdentity,
) -> PersistedTaskStateIdentity:
    return PersistedTaskStateIdentity(
        task_state_id=task_state_id,
        source_uuid=identity.source_uuid,
        meeting_external_id=identity.meeting_external_id,
        chunk_external_id=identity.chunk_external_id,
        entity_kind=identity.entity_kind,
        content_fingerprint=identity.content_fingerprint,
        orphaned=False,
    )


def _orphan_persisted_identity(
    task_state_id: int,
    row: LegacyTaskState,
    *,
    source_uuid: str | None,
    reason: str,
) -> PersistedTaskStateIdentity:
    return PersistedTaskStateIdentity(
        task_state_id=task_state_id,
        source_uuid=source_uuid,
        meeting_external_id=row.meeting_external_id,
        chunk_external_id=row.chunk_external_id,
        entity_kind="task",
        content_fingerprint=content_fingerprint(row.text or ""),
        orphaned=True,
        orphaned_reason=reason,
        legacy_action_item_id=row.action_item_id,
        created_at=row.created_at,
    )


def _migration_outcome(
    row: LegacyTaskState,
    identity: PersistedTaskStateIdentity,
    outcome: str,
) -> LegacyMigrationOutcome:
    return LegacyMigrationOutcome(
        legacy_action_item_id=row.action_item_id,
        task_state_id=identity.task_state_id,
        legacy_intent_digest=legacy_intent_digest(row),
        task_identity_digest=task_state_identity_digest(identity),
        outcome=outcome,
    )


def _write_legacy_task_states(
    conn: sqlite3.Connection,
    report_id: int,
    rows: list[LegacyTaskState],
) -> tuple[int, int]:
    source_uuids: dict[tuple[str, str], str | None] = {}
    claimed_identities: set[TaskIdentity] = set()
    migrated = 0
    orphaned = 0
    for row in rows:
        source_uuid: str | None = None
        if row.source_kind is not None and row.source_path is not None:
            source_key = (row.source_kind, row.source_path)
            if source_key not in source_uuids:
                source_uuids[source_key] = _find_existing_source(conn, *source_key)
                _state_transfer_checkpoint("source")
            source_uuid = source_uuids[source_key]

        orphaned_reason = row.orphaned_reason()
        if orphaned_reason is None and source_uuid is None:
            orphaned_reason = MISSING_STATE_SOURCE_REASON
        if orphaned_reason is not None:
            task_state_id = _upsert_legacy_orphan(
                conn,
                row,
                source_uuid=source_uuid,
                reason=orphaned_reason,
            )
            persisted_identity = _orphan_persisted_identity(
                task_state_id,
                row,
                source_uuid=source_uuid,
                reason=orphaned_reason,
            )
            outcome = "orphan_missing_identity"
            orphaned += 1
            _state_transfer_checkpoint("orphan")
        else:
            identity = _required_legacy_identity(row, source_uuid)
            if identity in claimed_identities:
                task_state_id = _upsert_legacy_orphan(
                    conn,
                    row,
                    source_uuid=source_uuid,
                    reason=DUPLICATE_LEGACY_IDENTITY_REASON,
                )
                persisted_identity = _orphan_persisted_identity(
                    task_state_id,
                    row,
                    source_uuid=source_uuid,
                    reason=DUPLICATE_LEGACY_IDENTITY_REASON,
                )
                outcome = "conflict_duplicate_identity"
                orphaned += 1
                _state_transfer_checkpoint("orphan")
            else:
                claimed_identities.add(identity)
                existing = _active_task_state(conn, identity)
                if existing is None:
                    task_state_id = _insert_legacy_task_state(conn, identity, row)
                    persisted_identity = _active_persisted_identity(task_state_id, identity)
                    outcome = "active_inserted"
                    migrated += 1
                    _state_transfer_checkpoint("migrated_task")
                elif existing.status == row.status and existing.note == row.note:
                    task_state_id = existing.task_state_id
                    persisted_identity = _active_persisted_identity(task_state_id, identity)
                    outcome = "active_existing"
                    migrated += 1
                    _state_transfer_checkpoint("migrated_task")
                else:
                    task_state_id = _upsert_legacy_orphan(
                        conn,
                        row,
                        source_uuid=source_uuid,
                        reason=LEGACY_STATE_CONFLICT_REASON,
                    )
                    persisted_identity = _orphan_persisted_identity(
                        task_state_id,
                        row,
                        source_uuid=source_uuid,
                        reason=LEGACY_STATE_CONFLICT_REASON,
                    )
                    outcome = "conflict_existing_state"
                    orphaned += 1
                    _state_transfer_checkpoint("orphan")
        _record_migration_outcome(
            conn,
            report_id,
            _migration_outcome(row, persisted_identity, outcome),
        )
    return migrated, orphaned


def _find_existing_source(
    conn: sqlite3.Connection,
    kind: str,
    path: str,
) -> str | None:
    rows = conn.execute(
        """
        SELECT uuid
        FROM sources
        WHERE kind = ? AND (current_path = ? OR projected_path = ?)
        """,
        (kind, path, path),
    ).fetchall()
    unique_uuids = {str(row["uuid"]) for row in rows}
    if len(unique_uuids) > 1:
        message = f"Legacy source path {path} maps to multiple persistent source UUIDs."
        raise AmbiguousSourceIdentityError(message, source_uuids=tuple(unique_uuids))
    return next(iter(unique_uuids), None)


def _required_legacy_identity(
    row: LegacyTaskState,
    source_uuid: str | None,
) -> TaskIdentity:
    if (
        source_uuid is None
        or row.meeting_external_id is None
        or row.chunk_external_id is None
        or row.text is None
    ):
        msg = f"Legacy task {row.action_item_id} has an invalid active identity."
        raise RuntimeError(msg)
    return task_identity(
        source_uuid,
        row.meeting_external_id,
        row.chunk_external_id,
        row.text,
    )


def _active_task_state(
    conn: sqlite3.Connection,
    identity: TaskIdentity,
) -> StoredTaskState | None:
    row = conn.execute(
        """
        SELECT id, status, note
        FROM task_states
        WHERE source_uuid = ?
          AND meeting_external_id = ?
          AND chunk_external_id = ?
          AND entity_kind = ?
          AND content_fingerprint = ?
          AND orphaned = 0
        """,
        (
            identity.source_uuid,
            identity.meeting_external_id,
            identity.chunk_external_id,
            identity.entity_kind,
            identity.content_fingerprint,
        ),
    ).fetchone()
    if row is None:
        return None
    return StoredTaskState(
        task_state_id=int(row["id"]),
        status=str(row["status"]),
        note=str(row["note"]) if row["note"] is not None else None,
    )


def _insert_legacy_task_state(
    conn: sqlite3.Connection,
    identity: TaskIdentity,
    row: LegacyTaskState,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO task_states (
          source_uuid, meeting_external_id, chunk_external_id,
          entity_kind, content_fingerprint, status, note, source,
          orphaned, orphaned_reason, legacy_action_item_id,
          created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?)
        """,
        (
            identity.source_uuid,
            identity.meeting_external_id,
            identity.chunk_external_id,
            identity.entity_kind,
            identity.content_fingerprint,
            row.status,
            row.note,
            row.source,
            row.action_item_id,
            row.created_at,
            row.updated_at,
        ),
    )
    return _require_lastrowid(cursor)


def _upsert_legacy_orphan(
    conn: sqlite3.Connection,
    row: LegacyTaskState,
    *,
    source_uuid: str | None,
    reason: str,
) -> int:
    fingerprint = content_fingerprint(row.text or "")
    existing = conn.execute(
        """
        SELECT id
        FROM task_states
        WHERE orphaned = 1
          AND source_uuid IS ?
          AND meeting_external_id IS ?
          AND chunk_external_id IS ?
          AND entity_kind = 'task'
          AND content_fingerprint = ?
          AND status = ?
          AND note IS ?
          AND source = ?
          AND orphaned_reason = ?
          AND legacy_action_item_id = ?
          AND created_at = ?
          AND updated_at = ?
        ORDER BY id
        LIMIT 1
        """,
        (
            source_uuid,
            row.meeting_external_id,
            row.chunk_external_id,
            fingerprint,
            row.status,
            row.note,
            row.source,
            reason,
            row.action_item_id,
            row.created_at,
            row.updated_at,
        ),
    ).fetchone()
    if existing is not None:
        return int(existing["id"])
    cursor = conn.execute(
        """
        INSERT INTO task_states (
          source_uuid, meeting_external_id, chunk_external_id,
          entity_kind, content_fingerprint, status, note, source,
          orphaned, orphaned_reason, legacy_action_item_id,
          created_at, updated_at
        ) VALUES (?, ?, ?, 'task', ?, ?, ?, ?, 1, ?, ?, ?, ?)
        """,
        (
            source_uuid,
            row.meeting_external_id,
            row.chunk_external_id,
            fingerprint,
            row.status,
            row.note,
            row.source,
            reason,
            row.action_item_id,
            row.created_at,
            row.updated_at,
        ),
    )
    return _require_lastrowid(cursor)


def _record_migration_outcome(
    conn: sqlite3.Connection,
    report_id: int,
    outcome: LegacyMigrationOutcome,
) -> None:
    conn.execute(
        """
        INSERT INTO migration_report_items (
          report_id, legacy_action_item_id, task_state_id,
          legacy_intent_digest, task_identity_digest, outcome
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            report_id,
            outcome.legacy_action_item_id,
            outcome.task_state_id,
            outcome.legacy_intent_digest,
            outcome.task_identity_digest,
            outcome.outcome,
        ),
    )


def _require_legacy_status_table(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "task_status_overrides"):
        msg = "The v3 index no longer contains legacy task statuses."
        raise RuntimeError(msg)


def _legacy_rows_and_path(
    conn: sqlite3.Connection,
) -> tuple[str, list[LegacyTaskState]]:
    rows = read_legacy_task_states(conn)
    legacy_status_count = int(
        conn.execute("SELECT COUNT(*) FROM task_status_overrides").fetchone()[0]
    )
    if len(rows) != legacy_status_count:
        msg = "Not every legacy task status could be read for persistent-state transfer."
        raise RuntimeError(msg)
    return main_database_path(conn), rows


def _require_matching_transfer_snapshot(
    transfer: LegacyStateTransfer,
    actual_index_path: str,
    rows: list[LegacyTaskState],
) -> None:
    actual_migration_key = legacy_state_migration_key(actual_index_path, rows)
    if (
        transfer.expected != len(rows)
        or transfer.index_path != actual_index_path
        or transfer.migration_key != actual_migration_key
    ):
        msg = "Legacy task statuses changed during persistent-state transfer; retry the upgrade."
        raise RuntimeError(msg)


def _write_index_ready_marker(
    index_path: Path,
    transfer: LegacyStateTransfer,
) -> None:
    with closing(sqlite3.connect(index_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != LEGACY_INDEX_SCHEMA_VERSION:
                conn.commit()
                return
            _require_legacy_status_table(conn)
            actual_index_path, rows = _legacy_rows_and_path(conn)
            _require_matching_transfer_snapshot(transfer, actual_index_path, rows)

            conn.execute("DROP TABLE IF EXISTS user_state_migration_ready")
            conn.execute(
                """
                CREATE TABLE user_state_migration_ready (
                  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                  migration_key TEXT NOT NULL,
                  report_id INTEGER NOT NULL,
                  index_path TEXT NOT NULL,
                  state_path TEXT NOT NULL,
                  state_schema_version INTEGER NOT NULL,
                  expected INTEGER NOT NULL CHECK (expected >= 0),
                  migrated INTEGER NOT NULL CHECK (migrated >= 0),
                  orphaned INTEGER NOT NULL CHECK (orphaned >= 0),
                  CHECK (expected = migrated + orphaned)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO user_state_migration_ready (
                  singleton, migration_key, report_id, index_path, state_path,
                  state_schema_version, expected, migrated, orphaned
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transfer.migration_key,
                    transfer.report_id,
                    transfer.index_path,
                    transfer.state_path,
                    MIGRATION_REPORT_SCHEMA_VERSION,
                    transfer.expected,
                    transfer.migrated,
                    transfer.orphaned,
                ),
            )
            _state_transfer_checkpoint("index_marker")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    _state_transfer_checkpoint("index_ready")


def _legacy_index_snapshot(
    index_path: Path,
) -> tuple[str, list[LegacyTaskState]] | None:
    with closing(sqlite3.connect(index_path)) as conn:
        conn.execute("BEGIN")
        snapshot: tuple[str, list[LegacyTaskState]] | None = None
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version == LEGACY_INDEX_SCHEMA_VERSION and table_exists(
                conn, "task_status_overrides"
            ):
                snapshot = _legacy_rows_and_path(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        else:
            return snapshot


def prepare_user_state_migration(
    index_path: Path,
    state: UserStateRepository,
    *,
    now: str,
) -> None:
    index_path = Path(index_path)
    if not index_path.exists():
        return
    snapshot = _legacy_index_snapshot(index_path)
    if snapshot is None:
        return
    actual_index_path, rows = snapshot
    transfer = state.migrate_legacy_index_state(Path(actual_index_path), rows, now=now)
    _write_index_ready_marker(index_path, transfer)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )
