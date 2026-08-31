# ruff: noqa: FBT003

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meetily_memory.config.paths import canonical_source_path
from meetily_memory.db.row_decode import (
    decode_nullable_integer,
    decode_required_integer,
    decode_required_text,
)
from meetily_memory.db.schema import IndexReadError, missing_user_state_message
from meetily_memory.db.schema_family import STATE_SCHEMA_USER_VERSION
from meetily_memory.db.state_schema import (
    StateSchemaError,
    create_state_database,
    validate_state_database,
    validate_state_schema,
)

if TYPE_CHECKING:
    from collections.abc import Generator

CURRENT_USER_STATE_SCHEMA_VERSION = STATE_SCHEMA_USER_VERSION


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
class UserStateFileIdentity:
    physical_path: Path
    device: int
    inode: int


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

        if _expected_identity is not None or self.read_only:
            self._state_identity = _require_user_state_identity(
                self.state_path,
                expected=_expected_identity,
            )
            with self._connect():
                pass
            return

        if self.state_path.exists():
            identity = _require_user_state_identity(self.state_path, expected=None)
            validate_state_database(identity.physical_path)
        else:
            create_state_database(self.state_path)
        self._state_identity = _require_user_state_identity(self.state_path, expected=None)

    @classmethod
    def open_existing(
        cls,
        state_path: Path,
        *,
        expected_identity: UserStateFileIdentity | None = None,
    ) -> UserStateRepository:
        return cls(state_path, _read_only=True, _expected_identity=expected_identity)

    @property
    def read_only_uri(self) -> str:
        self.recheck_identity()
        if self._state_identity is None:
            message = "A validated user-state identity is required for pinned access."
            raise RuntimeError(message)
        return f"{self._state_identity.physical_path.as_uri()}?mode=ro"

    def recheck_identity(self) -> None:
        if self._state_identity is None:
            message = "A validated user-state identity is required for pinned access."
            raise RuntimeError(message)
        _require_user_state_identity(self.state_path, expected=self._state_identity)

    def open_existing_writer(self) -> UserStateRepository:
        if not self.read_only:
            return self
        if self._state_identity is None:
            message = "A validated user-state identity is required for writable access."
            raise RuntimeError(message)
        return type(self)(self.state_path, _expected_identity=self._state_identity)

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
                return decode_required_text(
                    row["uuid"],
                    table="sources",
                    column="uuid",
                    context="existing source lookup",
                    error_type=StateSchemaError,
                )
            projected_owner = conn.execute(
                "SELECT uuid FROM sources WHERE kind = ? AND projected_path = ?",
                (kind, path),
            ).fetchone()
            if projected_owner is not None:
                projected_owner_uuid = decode_required_text(
                    projected_owner["uuid"],
                    table="sources",
                    column="uuid",
                    context="pending index projection lookup",
                    error_type=StateSchemaError,
                )
                message = (
                    "Source path is still reserved by a pending index projection for UUID "
                    f"{projected_owner_uuid}."
                )
                raise AmbiguousSourceIdentityError(
                    message,
                    source_uuids=(projected_owner_uuid,),
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
                source_uuid = decode_required_text(
                    row["uuid"],
                    table="sources",
                    column="uuid",
                    context="canonical source lookup",
                    error_type=StateSchemaError,
                )
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

    def validate_source_path_claim(self, source_uuid: str, kind: str, path: Path) -> None:
        canonical_path = canonical_source_path(path)
        with self._connect() as conn:
            _require_source_binding(conn, source_uuid, kind)
            _require_available_claim_target(conn, source_uuid, kind, canonical_path)

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
            previous_path = source["current_path"]
            projected_path = source["projected_path"]
            previous_updated_at = source["updated_at"]
            previous_revision = source["revision"]
            pending_revision = source["pending_revision"]
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
                    WHERE uuid = ? AND current_path = ? AND revision = ?
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
                    source_uuid,
                    kind,
                    previous_path,
                    projected_path,
                    claimed_path,
                    previous_updated_at,
                    pending_revision,
                    claimed_revision,
                    True,
                )

            claimed_revision = previous_revision + 1
            cursor = conn.execute(
                """
                UPDATE sources
                SET current_path = ?, updated_at = ?, revision = ?, pending_revision = ?
                WHERE uuid = ? AND current_path = ? AND revision = ?
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
            source_uuid,
            kind,
            previous_path,
            projected_path,
            claimed_path,
            previous_updated_at,
            None,
            claimed_revision,
            False,
        )

    def is_source_path_claim_current(self, claim: SourcePathClaim) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM sources
                WHERE uuid = ? AND kind = ? AND current_path = ? AND projected_path = ?
                  AND revision = ? AND pending_revision = ?
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
                        SELECT 1 FROM sources
                        WHERE uuid = ? AND kind = ? AND current_path = ? AND projected_path = ?
                          AND revision = ? AND pending_revision = ?
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
                        WHERE uuid = ? AND kind = ? AND current_path = ? AND projected_path = ?
                          AND revision = ? AND pending_revision = ?
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
            current_row = conn.execute(
                """
                SELECT uuid, kind, current_path, projected_path, updated_at,
                       revision, pending_revision
                FROM sources WHERE uuid = ?
                """,
                (claim.source_uuid,),
            ).fetchone()
            current = (
                _decode_source_binding(current_row, "source rollback binding")
                if current_row is not None
                else None
            )
            if (
                current is None
                or current["kind"] != claim.kind
                or current["current_path"] != claim.claimed_path
                or current["projected_path"] != claim.projected_path
                or current["revision"] != claim.claimed_revision
                or current["pending_revision"] != claim.claimed_revision
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
                    WHERE uuid = ? AND kind = ? AND current_path = ? AND projected_path = ?
                      AND revision = ? AND pending_revision = ?
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
            claim.source_uuid,
            claim.kind,
            claim.claimed_path,
            claim.claimed_path,
            claim.projected_path,
            current["updated_at"],
            claim.claimed_revision,
            rollback_revision,
            True,
        )

    def get_source(self, source_uuid: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT uuid, kind, current_path FROM sources WHERE uuid = ?",
                (source_uuid,),
            ).fetchone()
            return _decode_source_identity(row, "source lookup") if row is not None else None

    def get_source_binding(self, source_uuid: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT uuid, kind, current_path, revision,
                       projected_path, pending_revision, updated_at
                FROM sources WHERE uuid = ?
                """,
                (source_uuid,),
            ).fetchone()
            return _decode_source_binding(row, "source binding lookup") if row is not None else None

    def get_selected_source_binding(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT s.uuid, s.kind, s.current_path, s.revision,
                       s.projected_path, s.pending_revision, s.updated_at
                FROM app_settings a
                JOIN sources s ON s.uuid = a.source_uuid
                WHERE a.singleton = 1
                """
            ).fetchone()
            return (
                _decode_source_binding(row, "selected source binding") if row is not None else None
            )

    def source_binding_is_current(
        self,
        source_uuid: str,
        kind: str,
        current_path: str,
        revision: int,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM sources
                WHERE uuid = ? AND kind = ? AND current_path = ? AND projected_path = ?
                  AND revision = ? AND pending_revision IS NULL
                """,
                (source_uuid, kind, current_path, current_path, revision),
            ).fetchone()
            return row is not None

    def select_source(self, source_uuid: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = conn.execute(
                "SELECT 1 FROM sources WHERE uuid = ? AND pending_revision IS NULL",
                (source_uuid,),
            ).fetchone()
            if source is None:
                message = f"Source UUID is missing or has a pending relocation: {source_uuid}."
                raise ValueError(message)
            cursor = conn.execute(
                """
                UPDATE app_settings
                SET source_uuid = ?, source_path = NULL
                WHERE singleton = 1
                """,
                (source_uuid,),
            )
            if cursor.rowcount != 1:
                message = "State app_settings singleton is missing."
                raise StateSchemaError(message)
            conn.commit()

    def relocate_selected_source(  # noqa: PLR0913
        self,
        source_uuid: str,
        kind: str,
        previous_path: str,
        previous_revision: int,
        new_path: Path,
        *,
        now: str,
    ) -> int:
        canonical_path = canonical_source_path(new_path)
        canonical_string = str(canonical_path)
        next_revision = previous_revision + 1
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _require_available_claim_target(conn, source_uuid, kind, canonical_path)
            cursor = conn.execute(
                """
                UPDATE sources
                SET current_path = ?, projected_path = ?, updated_at = ?, revision = ?,
                    pending_revision = NULL
                WHERE uuid = ? AND kind = ? AND current_path = ? AND projected_path = ?
                  AND revision = ? AND pending_revision IS NULL
                """,
                (
                    canonical_string,
                    canonical_string,
                    now,
                    next_revision,
                    source_uuid,
                    kind,
                    previous_path,
                    previous_path,
                    previous_revision,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                message = f"Source binding changed before relocating UUID {source_uuid}."
                raise RuntimeError(message)
            selected = conn.execute(
                "UPDATE app_settings SET source_uuid = ?, source_path = NULL WHERE singleton = 1",
                (source_uuid,),
            )
            if selected.rowcount != 1:
                conn.rollback()
                message = "State app_settings singleton is missing."
                raise StateSchemaError(message)
            conn.commit()
        return next_revision

    def get_pending_source_path_claim(self, source_uuid: str) -> SourcePathClaim | None:
        binding = self.get_source_binding(source_uuid)
        if binding is None or binding["pending_revision"] is None:
            return None
        projected_path = binding["projected_path"]
        pending_revision = binding["pending_revision"]
        if type(pending_revision) is not int:
            message = "Decoded pending source claim is missing its integer revision."
            raise StateSchemaError(message)
        return SourcePathClaim(
            source_uuid,
            binding["kind"],
            projected_path,
            projected_path,
            binding["current_path"],
            binding["updated_at"],
            pending_revision,
            pending_revision,
            True,
        )

    def list_pending_source_path_claims(self) -> tuple[SourcePathClaim, ...]:
        with self._connect() as conn:
            source_uuids = [
                decode_required_text(
                    row["uuid"],
                    table="sources",
                    column="uuid",
                    context="pending source claim list",
                    error_type=StateSchemaError,
                )
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
                SELECT uuid, kind, current_path FROM sources
                WHERE kind = ? AND current_path = ? ORDER BY uuid
                """,
                (kind, path),
            ).fetchall()
            return [_decode_source_identity(row, "source path lookup") for row in rows]

    def get_source_by_path(self, kind: str, path: str) -> dict[str, Any] | None:
        sources = self.get_sources_by_path(kind, path)
        return sources[0] if len(sources) == 1 else None

    def get_sources_by_projected_path(self, kind: str, path: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT uuid, kind, current_path, revision,
                       projected_path, pending_revision, updated_at
                FROM sources WHERE kind = ? AND projected_path = ? ORDER BY uuid
                """,
                (kind, path),
            ).fetchall()
            return [_decode_source_binding(row, "projected source path lookup") for row in rows]

    def get_sources_for_settings_path(self, kind: str, raw_path: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT uuid, kind, current_path, revision,
                       projected_path, pending_revision, updated_at
                FROM sources
                WHERE kind = ? AND (
                  current_path = ? OR (pending_revision IS NOT NULL AND projected_path = ?)
                )
                ORDER BY uuid
                """,
                (kind, raw_path, raw_path),
            ).fetchall()
            return [_decode_source_binding(row, "settings source path lookup") for row in rows]

    def get_source_for_index_projection(self, kind: str, path: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT uuid, kind, current_path, revision,
                       projected_path, pending_revision, updated_at
                FROM sources
                WHERE kind = ? AND (current_path = ? OR projected_path = ?)
                ORDER BY uuid
                """,
                (kind, path, path),
            ).fetchall()
        decoded_rows = [
            _decode_source_binding(row, "index projection source lookup") for row in rows
        ]
        unique_rows = {row["uuid"]: row for row in decoded_rows}
        if len(unique_rows) > 1:
            source_uuids = tuple(unique_rows)
            message = (
                f"Index projection path {path} is claimed by multiple source UUIDs: "
                f"{', '.join(source_uuids)}."
            )
            raise AmbiguousSourceIdentityError(message, source_uuids=source_uuids)
        return next(iter(unique_rows.values()), None)

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

    def read_app_settings(self) -> dict[str, object]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM app_settings WHERE singleton = 1").fetchone()
            if row is None:
                message = "State app_settings singleton is missing."
                raise StateSchemaError(message)
            return dict(row)

    def replace_app_settings(self, values: dict[str, object]) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE app_settings
                SET source_uuid = :source_uuid,
                    source_path = :source_path,
                    ui_language = :ui_language,
                    last_update_at = :last_update_at,
                    obsidian_vault_path = :obsidian_vault_path,
                    obsidian_folder = :obsidian_folder,
                    obsidian_last_sync_at = :obsidian_last_sync_at
                WHERE singleton = 1
                """,
                values,
            )
            if cursor.rowcount != 1:
                message = "State app_settings singleton is missing."
                raise StateSchemaError(message)
            conn.commit()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        if self._state_identity is None:
            message = "State repository has no validated file identity."
            raise RuntimeError(message)
        identity = _require_user_state_identity(self.state_path, expected=self._state_identity)
        mode = "ro" if self.read_only else "rw"
        uri = f"{identity.physical_path.as_uri()}?mode={mode}"
        try:
            connection = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:
            raise IndexReadError(_changed_user_state_message(self.state_path)) from exc
        with closing(connection) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            if self.read_only:
                conn.execute("PRAGMA query_only=ON")
            validate_existing_user_state_schema(conn)
            _require_user_state_identity(self.state_path, expected=self._state_identity)
            yield conn


def validate_existing_user_state_schema(
    conn: sqlite3.Connection,
    *,
    schema: str = "main",
) -> None:
    validate_state_schema(conn, schema=schema)


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
    identity = UserStateFileIdentity(physical_path, path_stat.st_dev, path_stat.st_ino)
    if expected is not None and identity != expected:
        raise IndexReadError(_changed_user_state_message(state_path))
    return identity


def pin_user_state_identity(
    state_path: Path,
    *,
    expected: UserStateFileIdentity | None = None,
) -> UserStateFileIdentity:
    return _require_user_state_identity(Path(state_path), expected=expected)


def _changed_user_state_message(state_path: Path) -> str:
    return (
        f"Meetily Memory state no longer matches the database validated at {state_path}. "
        "Remove `state.sqlite` together with the disposable `index.sqlite`, then reinitialize with "
        "`mm init --source PATH` or `mm refresh --source PATH`. Deleting state permanently loses "
        "manual tags and application settings."
    )


def find_existing_source_by_uuid(
    state_path: Path,
    source_uuid: str,
    *,
    expected_identity: UserStateFileIdentity | None = None,
) -> dict[str, Any] | None:
    state_path = Path(state_path)
    if expected_identity is None and not state_path.is_file():
        return None
    repository = UserStateRepository.open_existing(
        state_path,
        expected_identity=expected_identity,
    )
    return repository.get_source_binding(source_uuid)


def find_existing_source_by_canonical_path(
    state_path: Path,
    kind: str,
    path: Path,
) -> dict[str, Any] | None:
    state_path = Path(state_path)
    if not state_path.is_file():
        return None
    return UserStateRepository.open_existing(state_path).get_source_by_canonical_path(kind, path)


def find_existing_source_for_settings_path(
    state_path: Path,
    kind: str,
    raw_path: str,
) -> dict[str, Any] | None:
    state_path = Path(state_path)
    if not state_path.is_file():
        return None
    rows = UserStateRepository.open_existing(state_path).get_sources_for_settings_path(
        kind,
        raw_path,
    )
    if len(rows) > 1:
        source_uuids = tuple(
            decode_required_text(
                row["uuid"],
                table="sources",
                column="uuid",
                context="settings source ambiguity check",
                error_type=StateSchemaError,
            )
            for row in rows
        )
        message = (
            f"Settings path {raw_path} maps to multiple source UUIDs: {', '.join(source_uuids)}."
        )
        raise AmbiguousSourceIdentityError(message, source_uuids=source_uuids)
    return rows[0] if rows else None


def _source_path_claims(
    conn: sqlite3.Connection,
    kind: str,
    canonical_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT uuid, kind, current_path, revision,
               projected_path, pending_revision, updated_at
        FROM sources WHERE kind = ? ORDER BY uuid
        """,
        (kind,),
    ).fetchall()
    decoded = [_decode_source_binding(row, "source path classification") for row in rows]
    return _classify_source_path_claims(decoded, canonical_path)


def _source_target_claims(
    conn: sqlite3.Connection,
    kind: str,
    canonical_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT uuid, kind, current_path, revision,
               projected_path, pending_revision, updated_at
        FROM sources WHERE kind = ? ORDER BY uuid
        """,
        (kind,),
    ).fetchall()
    canonical_string = str(canonical_path)
    exact: dict[str, dict[str, Any]] = {}
    collision_hints: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        row = _decode_source_binding(raw_row, "source claim target classification")
        stored_paths = {row["current_path"], row["projected_path"]}
        for stored_string in stored_paths:
            if stored_string == canonical_string:
                exact[row["uuid"]] = row
                continue
            try:
                resolved_stored_path = canonical_source_path(Path(stored_string))
            except (OSError, RuntimeError):
                continue
            if resolved_stored_path == canonical_path:
                collision_hints[row["uuid"]] = row
    return list(exact.values()), list(collision_hints.values())


def _require_source_binding(
    conn: sqlite3.Connection,
    source_uuid: str,
    kind: str,
) -> dict[str, Any]:
    source = conn.execute(
        """
        SELECT uuid, kind, current_path, updated_at, revision,
               projected_path, pending_revision
        FROM sources WHERE uuid = ?
        """,
        (source_uuid,),
    ).fetchone()
    if source is None:
        message = f"Source UUID not found in user state: {source_uuid}."
        raise ValueError(message)
    decoded = _decode_source_binding(source, "required source binding")
    if decoded["kind"] != kind:
        message = f"Source UUID {source_uuid} has an incompatible source kind."
        raise ValueError(message)
    return decoded


def _require_available_claim_target(
    conn: sqlite3.Connection,
    source_uuid: str,
    kind: str,
    canonical_path: Path,
) -> None:
    exact, collision_hints = _source_target_claims(conn, kind, canonical_path)
    conflicts = {
        row["uuid"]: row for row in (*exact, *collision_hints) if row["uuid"] != source_uuid
    }
    if conflicts:
        conflicting_uuids = tuple(conflicts)
        message = (
            "The rebind target is already linked to another source UUID: "
            f"{', '.join(conflicting_uuids)}."
        )
        raise AmbiguousSourceIdentityError(message, source_uuids=conflicting_uuids)


def _require_unreserved_projection_path(
    conn: sqlite3.Connection,
    kind: str,
    canonical_path: Path,
) -> None:
    exact, collision_hints = _source_target_claims(conn, kind, canonical_path)
    conflicts = {row["uuid"]: row for row in (*exact, *collision_hints)}
    if conflicts:
        conflicting_uuids = tuple(conflicts)
        message = (
            f"Source path {canonical_path} is reserved by source UUID(s) "
            f"{', '.join(conflicting_uuids)}."
        )
        raise AmbiguousSourceIdentityError(message, source_uuids=conflicting_uuids)


def _classify_source_path_claims(
    rows: list[dict[str, Any]],
    canonical_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    canonical_string = str(canonical_path)
    exact: list[dict[str, Any]] = []
    collision_hints: list[dict[str, Any]] = []
    for row in rows:
        stored_string = row["current_path"]
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
) -> dict[str, Any] | None:
    exact, collision_hints = _source_path_claims(conn, kind, canonical_path)
    return _unique_canonical_source_match(exact, collision_hints, canonical_path)


def _unique_canonical_source_match(
    exact: list[dict[str, Any]],
    collision_hints: list[dict[str, Any]],
    canonical_path: Path,
) -> dict[str, Any] | None:
    if collision_hints:
        conflicting_uuids = tuple(row["uuid"] for row in collision_hints)
        message = (
            f"Source path {canonical_path} has an ambiguous collision with non-canonical "
            f"user-state path(s) for UUID(s) {', '.join(conflicting_uuids)}. Automatic reuse "
            "is unsafe; run `mm config source NEW_PATH --rebind --source-uuid UUID`."
        )
        raise AmbiguousSourceIdentityError(message, source_uuids=conflicting_uuids)
    if len(exact) > 1:
        conflicting_uuids = tuple(row["uuid"] for row in exact)
        message = (
            f"Canonical source path is ambiguous in user state: {canonical_path}. "
            f"Conflicting source UUIDs: {', '.join(conflicting_uuids)}."
        )
        raise AmbiguousSourceIdentityError(message, source_uuids=conflicting_uuids)
    return exact[0] if exact else None


def _decode_source_identity(row: sqlite3.Row, context: str) -> dict[str, Any]:
    return {
        "uuid": decode_required_text(
            row["uuid"],
            table="sources",
            column="uuid",
            context=context,
            error_type=StateSchemaError,
        ),
        "kind": decode_required_text(
            row["kind"],
            table="sources",
            column="kind",
            context=context,
            error_type=StateSchemaError,
        ),
        "current_path": decode_required_text(
            row["current_path"],
            table="sources",
            column="current_path",
            context=context,
            error_type=StateSchemaError,
        ),
    }


def _decode_source_binding(row: sqlite3.Row, context: str) -> dict[str, Any]:
    binding = _decode_source_identity(row, context)
    binding.update(
        {
            "revision": decode_required_integer(
                row["revision"],
                table="sources",
                column="revision",
                context=context,
                error_type=StateSchemaError,
            ),
            "projected_path": decode_required_text(
                row["projected_path"],
                table="sources",
                column="projected_path",
                context=context,
                error_type=StateSchemaError,
            ),
            "pending_revision": decode_nullable_integer(
                row["pending_revision"],
                table="sources",
                column="pending_revision",
                context=context,
                error_type=StateSchemaError,
            ),
            "updated_at": decode_required_text(
                row["updated_at"],
                table="sources",
                column="updated_at",
                context=context,
                error_type=StateSchemaError,
            ),
        }
    )
    return binding


def _source_claim_finalize_checkpoint(_name: str) -> None:
    return
