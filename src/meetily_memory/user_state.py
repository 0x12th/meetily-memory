import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from meetily_memory.db.migrations import execute_sql_statements
from meetily_memory.db.rows import rows_to_dicts

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
CURRENT_USER_STATE_SCHEMA_VERSION = MIGRATION_REPORT_SCHEMA_VERSION
TAG_STATE_SCHEMA_VERSION = 2
LEGACY_INDEX_SCHEMA_VERSION = 3
DUPLICATE_LEGACY_IDENTITY_REASON = "duplicate legacy strict identity"
LEGACY_STATE_CONFLICT_REASON = "legacy status conflicts with persistent state"


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
    def __init__(self, state_path: Path) -> None:
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            ensure_user_state_schema(conn)

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
            source_uuid = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO sources (uuid, kind, current_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source_uuid, kind, path, now, now),
            )
            conn.commit()
            return source_uuid

    def source_uuid(self, kind: str, path: str, *, now: str) -> str:
        return self.get_or_create_source(kind, path, now=now)

    def get_source(self, source_uuid: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT uuid, kind, current_path FROM sources WHERE uuid = ?",
                (source_uuid,),
            ).fetchone()
            return dict(row) if row else None

    def get_source_by_path(self, kind: str, path: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT uuid, kind, current_path
                FROM sources
                WHERE kind = ? AND current_path = ?
                """,
                (kind, path),
            ).fetchone()
            return dict(row) if row else None

    def update_source_path(self, source_uuid: str, path: str, *, now: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE sources SET current_path = ?, updated_at = ? WHERE uuid = ?",
                (path, now, source_uuid),
            )
            if cursor.rowcount != 1:
                message = f"Persistent source not found: {source_uuid}"
                raise ValueError(message)
            conn.commit()

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
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT status, note, source, updated_at
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
            return dict(row) if row else None

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
                            now=now,
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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.state_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


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
        "SELECT uuid FROM sources WHERE kind = ? AND current_path = ?",
        (kind, path),
    ).fetchall()
    return str(sources[0]["uuid"]) if len(sources) == 1 else None


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
    *,
    now: str,
) -> tuple[int, int]:
    source_uuids: dict[tuple[str, str], str] = {}
    claimed_identities: set[TaskIdentity] = set()
    migrated = 0
    orphaned = 0
    for row in rows:
        source_uuid: str | None = None
        if row.source_kind is not None and row.source_path is not None:
            source_key = (row.source_kind, row.source_path)
            source_uuid = source_uuids.get(source_key)
            if source_uuid is None:
                source_uuid = _get_or_create_source(conn, *source_key, now=now)
                source_uuids[source_key] = source_uuid
                _state_transfer_checkpoint("source")

        orphaned_reason = row.orphaned_reason()
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


def _get_or_create_source(
    conn: sqlite3.Connection,
    kind: str,
    path: str,
    *,
    now: str,
) -> str:
    row = conn.execute(
        "SELECT uuid FROM sources WHERE kind = ? AND current_path = ?",
        (kind, path),
    ).fetchone()
    if row is not None:
        source_uuid = str(row["uuid"])
        conn.execute(
            "UPDATE sources SET updated_at = ? WHERE uuid = ?",
            (now, source_uuid),
        )
        return source_uuid
    source_uuid = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO sources (uuid, kind, current_path, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (source_uuid, kind, path, now, now),
    )
    return source_uuid


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
    with sqlite3.connect(index_path) as conn:
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
    with sqlite3.connect(index_path) as conn:
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
