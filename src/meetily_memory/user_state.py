import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
LEGACY_INDEX_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class TaskIdentity:
    source_uuid: str
    meeting_external_id: str
    chunk_external_id: str
    entity_kind: str
    content_fingerprint: str


class UserStateRepository:
    def __init__(self, state_path: Path) -> None:
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(USER_STATE_SCHEMA)
            conn.execute("PRAGMA user_version = 1")
            conn.commit()

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

    def add_orphan(
        self,
        row: dict[str, Any],
        *,
        source_uuid: str | None,
        reason: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
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
                    row.get("meeting_external_id"),
                    row.get("chunk_external_id"),
                    content_fingerprint(str(row.get("text") or "")),
                    row["status"],
                    row.get("note"),
                    row["source"],
                    reason,
                    row["action_item_id"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )
            conn.commit()

    def record_migration_report(
        self,
        index_path: Path,
        *,
        migrated: int,
        orphaned: int,
        now: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO migration_reports (
                  index_path, migrated, orphaned, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (str(index_path), migrated, orphaned, now),
            )
            conn.commit()

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


def prepare_user_state_migration(
    index_path: Path,
    state: UserStateRepository,
    *,
    now: str,
) -> None:
    index_path = Path(index_path)
    if not index_path.exists():
        return
    with sqlite3.connect(index_path) as conn:
        conn.row_factory = sqlite3.Row
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != LEGACY_INDEX_SCHEMA_VERSION or not table_exists(
            conn, "task_status_overrides"
        ):
            return
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT
                  o.action_item_id, o.status, o.note, o.source,
                  o.created_at, o.updated_at,
                  e.text,
                  m.external_id AS meeting_external_id,
                  c.external_id AS chunk_external_id,
                  s.kind AS source_kind,
                  s.path AS source_path
                FROM task_status_overrides o
                JOIN action_items e ON e.id = o.action_item_id
                JOIN meetings m ON m.id = e.meeting_id
                JOIN sources s ON s.id = m.source_id
                LEFT JOIN chunks c ON c.id = e.source_chunk_id
                ORDER BY o.action_item_id
                """
            )
        )
        migrated = 0
        orphaned = 0
        for row in rows:
            source_uuid = state.get_or_create_source(
                str(row["source_kind"]),
                str(row["source_path"]),
                now=now,
            )
            chunk_external_id = row.get("chunk_external_id")
            if not chunk_external_id:
                state.add_orphan(
                    row,
                    source_uuid=source_uuid,
                    reason="missing chunk_external_id",
                )
                orphaned += 1
                continue
            identity = task_identity(
                source_uuid,
                str(row["meeting_external_id"]),
                str(chunk_external_id),
                str(row["text"]),
            )
            state.set_task_state(
                identity,
                str(row["status"]),
                note=str(row["note"]) if row.get("note") is not None else None,
                source=str(row["source"]),
                now=str(row["updated_at"]),
                legacy_action_item_id=int(row["action_item_id"]),
            )
            migrated += 1
        state.record_migration_report(
            index_path,
            migrated=migrated,
            orphaned=orphaned,
            now=now,
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_state_migration_ready (
              migrated INTEGER NOT NULL,
              orphaned INTEGER NOT NULL
            )
            """
        )
        conn.execute("DELETE FROM user_state_migration_ready")
        conn.execute(
            "INSERT INTO user_state_migration_ready (migrated, orphaned) VALUES (?, ?)",
            (migrated, orphaned),
        )
        conn.commit()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )
