import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

MIGRATION_REPORT_SCHEMA_VERSION = 3
ACTIVE_MIGRATION_OUTCOMES = ("active_inserted", "active_existing")
ORPHAN_MIGRATION_OUTCOMES = (
    "orphan_missing_identity",
    "conflict_existing_state",
    "conflict_duplicate_identity",
)
ALL_MIGRATION_OUTCOMES = ACTIVE_MIGRATION_OUTCOMES + ORPHAN_MIGRATION_OUTCOMES


@dataclass(frozen=True)
class LegacyTaskState:
    action_item_id: int
    status: str
    note: str | None
    source: str
    created_at: str
    updated_at: str
    text: str | None
    meeting_external_id: str | None
    chunk_external_id: str | None
    source_kind: str | None
    source_path: str | None

    def orphaned_reason(self) -> str | None:
        if self.text is None:
            return "missing action item"
        if self.source_kind is None or self.source_path is None:
            return "missing source identity"
        if self.meeting_external_id is None:
            return "missing meeting_external_id"
        if not self.chunk_external_id:
            return "missing chunk_external_id"
        return None


@dataclass(frozen=True)
class PersistedTaskStateIdentity:
    task_state_id: int
    source_uuid: str | None
    meeting_external_id: str | None
    chunk_external_id: str | None
    entity_kind: str
    content_fingerprint: str
    orphaned: bool
    orphaned_reason: str | None = None
    legacy_action_item_id: int | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class VerifiedMigrationReport:
    report_id: int
    migration_key: str
    index_path: str
    migrated: int
    orphaned: int

    @property
    def expected(self) -> int:
        return self.migrated + self.orphaned


def canonical_database_path(path: Path | str) -> str:
    return str(Path(path).expanduser().resolve())


def main_database_path(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list"):
        if str(row[1]) == "main" and str(row[2]):
            return canonical_database_path(str(row[2]))
    msg = "The index connection is not backed by a filesystem database."
    raise RuntimeError(msg)


def read_legacy_task_states(conn: sqlite3.Connection) -> list[LegacyTaskState]:
    rows = conn.execute(
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
        LEFT JOIN action_items e ON e.id = o.action_item_id
        LEFT JOIN meetings m ON m.id = e.meeting_id
        LEFT JOIN sources s ON s.id = m.source_id
        LEFT JOIN chunks c ON c.id = e.source_chunk_id
        ORDER BY o.action_item_id
        """
    )
    return [
        LegacyTaskState(
            action_item_id=int(row[0]),
            status=str(row[1]),
            note=_optional_string(row[2]),
            source=str(row[3]),
            created_at=str(row[4]),
            updated_at=str(row[5]),
            text=_optional_string(row[6]),
            meeting_external_id=_optional_string(row[7]),
            chunk_external_id=_optional_string(row[8]),
            source_kind=_optional_string(row[9]),
            source_path=_optional_string(row[10]),
        )
        for row in rows
    ]


def legacy_state_migration_key(
    index_path: Path | str,
    rows: list[LegacyTaskState],
) -> str:
    payload = {
        "index_path": canonical_database_path(index_path),
        "rows": [asdict(row) for row in rows],
    }
    return _digest_payload(payload)


def legacy_intent_digest(row: LegacyTaskState) -> str:
    return _digest_payload(
        {
            "ledger": "legacy-intent-v1",
            "row": asdict(row),
        }
    )


def task_state_identity_digest(identity: PersistedTaskStateIdentity) -> str:
    payload: dict[str, object] = {
        "ledger": "persisted-task-identity-v1",
        "task_state_id": identity.task_state_id,
        "source_uuid": identity.source_uuid,
        "meeting_external_id": identity.meeting_external_id,
        "chunk_external_id": identity.chunk_external_id,
        "entity_kind": identity.entity_kind,
        "content_fingerprint": identity.content_fingerprint,
        "orphaned": identity.orphaned,
    }
    if identity.orphaned:
        payload.update(
            {
                "orphaned_reason": identity.orphaned_reason,
                "legacy_action_item_id": identity.legacy_action_item_id,
                "created_at": identity.created_at,
            }
        )
    return _digest_payload(payload)


def verified_migration_report(
    conn: sqlite3.Connection,
    migration_key: str,
    index_path: Path | str,
    rows: list[LegacyTaskState],
) -> VerifiedMigrationReport | None:
    report = conn.execute(
        """
        SELECT id, migration_key, index_path, migrated, orphaned
        FROM migration_reports
        WHERE migration_key = ?
        """,
        (migration_key,),
    ).fetchone()
    if report is None:
        return None

    verified = VerifiedMigrationReport(
        report_id=int(report[0]),
        migration_key=str(report[1]),
        index_path=str(report[2]),
        migrated=int(report[3]),
        orphaned=int(report[4]),
    )
    if (
        verified.migration_key != migration_key
        or canonical_database_path(verified.index_path) != canonical_database_path(index_path)
        or verified.expected != len(rows)
    ):
        return None

    outcomes = _verified_migration_outcomes(conn, verified.report_id, rows)
    if outcomes is None:
        return None

    migrated = sum(outcome in ACTIVE_MIGRATION_OUTCOMES for outcome in outcomes)
    orphaned = sum(outcome in ORPHAN_MIGRATION_OUTCOMES for outcome in outcomes)
    if migrated != verified.migrated or orphaned != verified.orphaned:
        return None
    return verified


def _verified_migration_outcomes(
    conn: sqlite3.Connection,
    report_id: int,
    rows: list[LegacyTaskState],
) -> list[str] | None:
    items = [
        tuple(item)
        for item in conn.execute(
            """
            SELECT
              i.legacy_action_item_id, i.task_state_id,
              i.legacy_intent_digest, i.task_identity_digest, i.outcome,
              t.source_uuid, t.meeting_external_id, t.chunk_external_id,
              t.entity_kind, t.content_fingerprint, t.orphaned,
              t.orphaned_reason, t.legacy_action_item_id, t.created_at
            FROM migration_report_items i
            JOIN task_states t ON t.id = i.task_state_id
            WHERE i.report_id = ?
            ORDER BY i.legacy_action_item_id
            """,
            (report_id,),
        ).fetchall()
    ]
    expected_rows = sorted(rows, key=lambda row: row.action_item_id)
    if len(items) != len(expected_rows):
        return None

    task_state_ids: set[int] = set()
    outcomes: list[str] = []
    for row, item in zip(expected_rows, items, strict=True):
        verified_item = _verified_migration_item(row, item)
        if verified_item is None:
            return None
        task_state_id, outcome = verified_item
        if task_state_id in task_state_ids:
            return None
        task_state_ids.add(task_state_id)
        outcomes.append(outcome)
    return outcomes


def _verified_migration_item(
    row: LegacyTaskState,
    item: tuple[object, ...],
) -> tuple[int, str] | None:
    try:
        legacy_action_item_id = _required_int(item[0])
        task_state_id = _required_int(item[1])
        orphaned_value = _required_int(item[10])
        legacy_action_item_id_in_state = _optional_int(item[12])
    except (TypeError, ValueError):
        return None
    if (
        legacy_action_item_id != row.action_item_id
        or orphaned_value not in (0, 1)
        or str(item[2]) != legacy_intent_digest(row)
    ):
        return None

    identity = PersistedTaskStateIdentity(
        task_state_id=task_state_id,
        source_uuid=_optional_string(item[5]),
        meeting_external_id=_optional_string(item[6]),
        chunk_external_id=_optional_string(item[7]),
        entity_kind=str(item[8]),
        content_fingerprint=str(item[9]),
        orphaned=bool(orphaned_value),
        orphaned_reason=_optional_string(item[11]),
        legacy_action_item_id=legacy_action_item_id_in_state,
        created_at=_optional_string(item[13]),
    )
    if str(item[3]) != task_state_identity_digest(identity):
        return None

    outcome = str(item[4])
    if outcome not in ALL_MIGRATION_OUTCOMES:
        return None
    if identity.orphaned is (outcome in ACTIVE_MIGRATION_OUTCOMES):
        return None
    return task_state_id, outcome


def _digest_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _required_int(value: object) -> int:
    if not isinstance(value, int | str | bytes | bytearray):
        raise TypeError
    return int(value)


def _optional_int(value: object) -> int | None:
    return _required_int(value) if value is not None else None


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None
