import os
import sqlite3
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, closing, contextmanager, suppress
from pathlib import Path
from time import monotonic, sleep

from meetily_memory.db.migrations import (
    CURRENT_SCHEMA_VERSION,
    LATEST_IN_PLACE_SCHEMA_VERSION,
    MIGRATIONS,
    initialize_current_schema,
)

IndexConnectionFactory = Callable[[Path], AbstractContextManager[sqlite3.Connection]]
TRANSIENT_WAL_CLEANUP_TIMEOUT_SECONDS = 5.0
TRANSIENT_WAL_CLEANUP_RETRY_SECONDS = 0.01
INDEX_PROJECTION_CLEANUP_MESSAGE = (
    "Index projection committed, but transient SQLite cleanup failed. "
    "The published snapshot is usable; rerun refresh to retry cleanup."
)


class IndexRebuildRequiredError(RuntimeError):
    pass


class IndexReadError(RuntimeError):
    pass


class IndexProjectionCleanupError(RuntimeError):
    """The projection committed, but transient WAL cleanup did not finish."""


def missing_user_state_message(state_path: Path) -> str:
    return (
        f"Meetily Memory user state not found: {state_path}. Restore the authoritative "
        "`state.sqlite` from backup; `mm refresh` alone cannot recover the source UUID already "
        "projected by the current index. For an intentional identity reset, first move or remove "
        "the disposable `index.sqlite`, then run `mm init` or `mm scan --source PATH`. Manual "
        "tags, task statuses, and task notes cannot be recovered without the original "
        "`state.sqlite`. Manual topic aliases cannot be recovered either."
    )


@contextmanager
def sqlite_read_snapshot(conn: sqlite3.Connection) -> Generator[None, None, None]:
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if owns_transaction and conn.in_transaction:
            conn.rollback()


@contextmanager
def index_connection(index_path: Path) -> Generator[sqlite3.Connection, None, None]:
    index_path = Path(index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(index_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_schema(conn)
        yield conn


@contextmanager
def index_projection_transaction(  # noqa: C901, PLR0912, PLR0915
    index_path: Path,
) -> Generator[sqlite3.Connection, None, None]:
    """Publish one projection commit while keeping crash-time readers on the old snapshot."""
    index_path = Path(index_path)
    marker_path = transient_wal_marker_path(index_path)
    published = False
    restore_delete_mode = False
    post_commit_cleanup_failed = False
    try:
        with index_connection(index_path) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            initial_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
            restore_delete_mode = initial_mode != "wal" or marker_path.exists()
            if restore_delete_mode:
                _write_transient_wal_marker(marker_path)
            try:
                selected_mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0])
            except BaseException:
                if restore_delete_mode:
                    _discard_transient_wal_marker(marker_path)
                raise
            if selected_mode.casefold() != "wal":
                if restore_delete_mode:
                    _discard_transient_wal_marker(marker_path)
                message = "Index projection could not enter transient WAL journal mode."
                raise RuntimeError(message)  # noqa: TRY301
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
                published = True
            except BaseException as projection_error:
                if conn.in_transaction:
                    conn.rollback()
                if restore_delete_mode:
                    try:
                        _restore_delete_journal_mode(conn)
                        _require_clean_index_sidecars(index_path)
                        _discard_transient_wal_marker(marker_path)
                    except BaseException as cleanup_error:  # noqa: BLE001
                        projection_error.add_note(
                            "Transient WAL cleanup also failed; the next refresh will retry "
                            f"recovery after {type(cleanup_error).__name__}."
                        )
                raise
            if restore_delete_mode:
                _restore_delete_journal_mode(conn)
        if published and restore_delete_mode:
            _require_clean_index_sidecars(index_path)
            _discard_transient_wal_marker(marker_path)
    except BaseException:
        if not published or not restore_delete_mode:
            raise
        post_commit_cleanup_failed = True
    if post_commit_cleanup_failed:
        raise IndexProjectionCleanupError(INDEX_PROJECTION_CLEANUP_MESSAGE) from None


def transient_wal_marker_path(index_path: Path) -> Path:
    return index_path.with_name(f".{index_path.name}.refresh-wal")


def _write_transient_wal_marker(marker_path: Path) -> None:
    with marker_path.open("wb") as marker:
        marker.write(b"delete\n")
        marker.flush()
        os.fsync(marker.fileno())
    _fsync_directory(marker_path.parent)


def _discard_transient_wal_marker(marker_path: Path) -> None:
    marker_path.unlink(missing_ok=True)
    _fsync_directory(marker_path.parent)


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        with suppress(OSError):
            os.close(directory_fd)


def _restore_delete_journal_mode(conn: sqlite3.Connection) -> None:
    deadline = monotonic() + TRANSIENT_WAL_CLEANUP_TIMEOUT_SECONDS
    while True:
        checkpoint: sqlite3.Row | tuple[int, ...] | None = None
        try:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            checkpoint_complete = (
                checkpoint is not None
                and int(checkpoint[0]) == 0
                and int(checkpoint[1]) == int(checkpoint[2])
            )
            if checkpoint_complete:
                selected_mode = str(conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0])
                if selected_mode.casefold() == "delete":
                    return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).casefold() and "busy" not in str(exc).casefold():
                raise
        if monotonic() >= deadline:
            message = "Transient index WAL could not be checkpointed and restored to DELETE mode."
            raise RuntimeError(message)
        sleep(TRANSIENT_WAL_CLEANUP_RETRY_SECONDS)


def _require_clean_index_sidecars(index_path: Path) -> None:
    stale = [
        sidecar
        for suffix in ("-wal", "-shm", "-journal")
        if (sidecar := index_path.with_name(index_path.name + suffix)).exists()
    ]
    if stale:
        names = ", ".join(path.name for path in stale)
        message = (
            "Index projection committed, but SQLite sidecars remain: "
            f"{names}. Rerun refresh to retry cleanup."
        )
        raise IndexProjectionCleanupError(message)


@contextmanager
def existing_index_connection(index_path: Path) -> Generator[sqlite3.Connection, None, None]:
    index_path = Path(index_path)
    if not index_path.is_file():
        message = (
            f"Meetily Memory index not found: {index_path}. "
            "Run `mm refresh` or `mm scan --source PATH` to build it."
        )
        raise IndexReadError(message)

    try:
        physical_path = index_path.resolve(strict=True)
    except OSError as exc:
        message = f"Meetily Memory index cannot be opened: {index_path}."
        raise IndexReadError(message) from exc
    uri = f"{physical_path.as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA foreign_keys=ON")
            validate_existing_index_schema(conn)
            yield conn
    except sqlite3.Error as exc:
        message = f"Meetily Memory index cannot be read: {index_path}: {exc}"
        raise IndexReadError(message) from exc


def ensure_schema(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > CURRENT_SCHEMA_VERSION:
        message = (
            f"Unsupported index schema version {version}; "
            f"this binary supports {CURRENT_SCHEMA_VERSION}."
        )
        raise RuntimeError(message)

    if version == 0 and not _has_application_tables(conn):
        initialize_current_schema(conn)
        return

    for next_version in range(version + 1, LATEST_IN_PLACE_SCHEMA_VERSION + 1):
        MIGRATIONS[next_version](conn)

    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < CURRENT_SCHEMA_VERSION:
        message = (
            f"Index schema {version} requires a source-aware rebuild to schema "
            f"{CURRENT_SCHEMA_VERSION}. Run refresh or scan with the source database."
        )
        raise IndexRebuildRequiredError(message)


def validate_existing_index_schema(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version != CURRENT_SCHEMA_VERSION:
        if version > CURRENT_SCHEMA_VERSION:
            message = (
                f"Index schema {version} is newer than supported schema "
                f"{CURRENT_SCHEMA_VERSION}. Update Meetily Memory before reading it."
            )
        else:
            message = (
                f"Index schema {version} is outdated; schema {CURRENT_SCHEMA_VERSION} is required. "
                "Run `mm refresh` or `mm scan --source PATH` to rebuild the disposable index."
            )
        raise IndexReadError(message)

    columns = {
        str(row["name"]): row for row in conn.execute("PRAGMA table_info(chunks)").fetchall()
    }
    evidence_column = columns.get("evidence_id")
    if (
        evidence_column is None
        or str(evidence_column["type"]).upper() != "TEXT"
        or int(evidence_column["notnull"]) != 1
    ):
        _raise_invalid_current_index("chunks.evidence_id TEXT NOT NULL is missing")

    unique_evidence_index = False
    for index in conn.execute("PRAGMA index_list(chunks)").fetchall():
        if int(index["unique"]) != 1:
            continue
        indexed_columns = tuple(
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                (str(index["name"]),),
            ).fetchall()
        )
        if indexed_columns == ("evidence_id",):
            unique_evidence_index = True
            break
    if not unique_evidence_index:
        _raise_invalid_current_index("the unique chunks.evidence_id index is missing")


def _raise_invalid_current_index(reason: str) -> None:
    message = (
        f"Index schema {CURRENT_SCHEMA_VERSION} is incomplete: {reason}. "
        "Run `mm refresh` or `mm scan --source PATH` to rebuild the disposable index."
    )
    raise IndexReadError(message)


def _has_application_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        LIMIT 1
        """
    ).fetchone()
    return row is not None
