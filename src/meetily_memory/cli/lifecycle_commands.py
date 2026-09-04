from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, datetime
from locale import getlocale
from pathlib import Path
from typing import Annotated

import typer

from meetily_memory import autosync as autosync_service
from meetily_memory.cli.common import (
    console,
    make_typer,
    print_json,
    print_text_block,
    sqlite_has_fts5,
)
from meetily_memory.config.paths import canonical_source_path, discover_meetily_db
from meetily_memory.config.settings import (
    AppSettings,
    load_app_settings,
    normalize_ui_language,
)
from meetily_memory.db.schema_family import INDEX_SCHEMA_USER_VERSION
from meetily_memory.db.state_schema import StateSchemaError
from meetily_memory.diagnostics import (
    DatabaseDiagnostic,
    inspect_database_status,
    inspect_local_databases,
    inspect_source_database,
)
from meetily_memory.obsidian_sync import sync_configured_obsidian_locked
from meetily_memory.refresh import (
    PublishedIndex,
    refresh_index_locked,
    relocate_selected_source_locked,
    switch_selected_source_locked,
)
from meetily_memory.refresh_lock import RefreshLock, RefreshLockBusyError
from meetily_memory.scanner.meetily_sqlite import inspect_meetily_schema
from meetily_memory.user_state import (
    AmbiguousSourceIdentityError,
    UserStateRepository,
    find_existing_source_by_uuid,
)

app = make_typer("Local Meetily history lifecycle commands.")
config_app = make_typer("Manage CLI settings.")
db_app = make_typer("Inspect the local index database.")


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


SOURCE_KIND = "meetily_sqlite"


def require_canonical_source_path(path: Path) -> Path:
    try:
        return canonical_source_path(path)
    except OSError as exc:
        message = f"Source path does not exist or cannot be resolved: {path}"
        raise typer.BadParameter(message) from exc


def source_state_repository(index_path: Path) -> UserStateRepository:
    return UserStateRepository(Path(index_path).with_name("state.sqlite"))


def resolve_source_uuid(user_state: UserStateRepository, source_path: Path) -> str:
    try:
        return user_state.resolve_source(SOURCE_KIND, source_path, now=utc_now_iso())
    except AmbiguousSourceIdentityError as exc:
        raise typer.BadParameter(str(exc)) from exc


def resolve_existing_rebind_source_uuid(
    state_path: Path,
    settings: AppSettings,
    explicit_source_uuid: str | None,
) -> str:
    source_uuid = explicit_source_uuid if explicit_source_uuid is not None else settings.source_uuid
    if source_uuid is not None:
        source = find_existing_source_by_uuid(state_path, source_uuid)
        if source is None:
            message = f"Source UUID not found in user state: {source_uuid}."
            raise typer.BadParameter(message)
        if str(source["kind"]) != SOURCE_KIND:
            message = f"Source UUID {source_uuid} has an incompatible source kind."
            raise typer.BadParameter(message)
        return source_uuid

    message = "No source identity is selected. Pass --source-uuid UUID with --rebind."
    raise typer.BadParameter(message)


def configured_source_path(
    state_path: Path,
    explicit_source: Path | None = None,
) -> Path | None:
    if explicit_source is not None:
        return require_canonical_source_path(explicit_source)
    settings = load_app_settings(state_path)
    if settings.source_uuid:
        source = find_existing_source_by_uuid(
            state_path,
            settings.source_uuid,
        )
        if source is None:
            message = f"Source UUID not found in user state: {settings.source_uuid}."
            raise typer.BadParameter(message)
        if str(source["kind"]) != SOURCE_KIND:
            message = f"Source UUID {settings.source_uuid} has an incompatible source kind."
            raise typer.BadParameter(message)
        current_path = source["current_path"]
        if not isinstance(current_path, str) or not current_path.strip():
            message = f"Source UUID {settings.source_uuid} has no usable current path."
            raise typer.BadParameter(message)
        configured = Path(current_path).expanduser()
        try:
            canonical = canonical_source_path(configured)
        except OSError as exc:
            message = (
                f"Source path for selected UUID {settings.source_uuid} does not exist or cannot "
                f"be resolved: {configured}"
            )
            raise typer.BadParameter(message) from exc
        if not canonical.is_file():
            message = (
                f"Source path for selected UUID {settings.source_uuid} is unavailable: {canonical}"
            )
            raise typer.BadParameter(message)
        return canonical
    discovered = discover_meetily_db()
    return require_canonical_source_path(discovered) if discovered is not None else None


def scan_update(
    index_path: Path,
    source_path: Path,
    *,
    force: bool = False,
) -> tuple[dict[str, object], PublishedIndex]:
    user_state = source_state_repository(index_path)
    source_uuid = resolve_source_uuid(user_state, source_path)
    selected = user_state.get_selected_source_binding()
    if selected is not None and str(selected["uuid"]) == source_uuid:
        result = refresh_index_locked(index_path, user_state, force=force)
    else:
        result = switch_selected_source_locked(index_path, user_state, source_uuid)
    payload: dict[str, object] = {
        "source_uuid": result.source.source_uuid,
        "source_revision": result.source.source_revision,
        "meetings_seen": result.meetings,
        "chunks_seen": result.chunks,
        "fts_rows": result.fts_rows,
        "index_bytes": result.bytes,
        "changed": result.changed,
    }
    return payload, result


def print_update_payload(payload: dict[str, object]) -> None:
    print_text_block(f"changed: {'yes' if payload['changed'] else 'no'}")
    console.print(f"meetings: {payload['meetings_seen']}")
    console.print(f"chunks: {payload['chunks_seen']}")
    console.print(f"fts rows: {payload['fts_rows']}")
    console.print(f"source revision: {payload['source_revision']}")


def print_database_diagnostic(label: str, diagnostic: DatabaseDiagnostic) -> None:
    print_text_block(f"{label} database: {diagnostic.status_label()}")
    if diagnostic.error:
        print_text_block(f"{label} database error: {diagnostic.error}")


def diagnostic_source_path(
    persisted_source_path: Path | None,
) -> Path | None:
    return persisted_source_path


def resolve_diagnostic_ui_language(settings: AppSettings, indexed_language: str | None) -> str:
    if settings.ui_language:
        return settings.ui_language
    normalized_indexed_language = normalize_ui_language(indexed_language)
    if normalized_indexed_language:
        return normalized_indexed_language
    system_language = normalize_ui_language(getlocale()[0])
    return system_language or "en"


@app.command()
def init(
    ctx: typer.Context,
    source: Annotated[
        Path | None,
        typer.Option("--source", help="Path to Meetily meeting_minutes.sqlite."),
    ] = None,
    autosync: Annotated[
        bool | None,
        typer.Option(
            "--autosync/--no-autosync",
            help="Enable or disable periodic refresh for this index.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    try:
        with RefreshLock(ctx.obj["index_path"]):
            source_path = configured_source_path(ctx.obj["state_path"], source)
            if source_path is None:
                message = "Meetily DB was not found. Pass --source /path/to/meeting_minutes.sqlite."
                raise typer.BadParameter(message)
            payload, _result = scan_update(ctx.obj["index_path"], source_path)
            UserStateRepository(ctx.obj["state_path"]).record_refresh(utc_now_iso())
    except RefreshLockBusyError as exc:
        raise typer.BadParameter(str(exc)) from exc

    autosync_state = "not-requested"
    if autosync is not None or not ctx.obj["index_explicit"]:
        should_enable_autosync = autosync is not False
        try:
            if should_enable_autosync:
                autosync_result = autosync_service.enable(ctx.obj["index_path"])
            else:
                current_autosync = autosync_service.status(ctx.obj["index_path"])
                current_index = Path(ctx.obj["index_path"]).absolute()
                owns_job = current_autosync.configured_index == current_index
                autosync_result = (
                    autosync_service.disable(ctx.obj["index_path"])
                    if owns_job and current_autosync.state != "disabled"
                    else current_autosync
                )
            autosync_state = autosync_result.state
        except autosync_service.AutosyncError as exc:
            message = (
                "Initialization completed, but autosync failed: "
                f"{exc}. Retry with `mm autosync enable`."
            )
            raise RuntimeError(message) from exc

    response = {
        "initialized": True,
        "index_path": str(ctx.obj["index_path"]),
        "source_path": str(source_path),
        "autosync": autosync_state,
        **payload,
    }
    if json_output:
        print_json(response)
        return
    print_text_block("initialized: yes")
    print_text_block(f"index path: {ctx.obj['index_path']}")
    print_text_block(f"source path: {source_path}")
    print_update_payload(payload)


@app.command()
def status(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    index_path = ctx.obj["index_path"]
    settings = load_app_settings(ctx.obj["state_path"])
    diagnostics = inspect_local_databases(index_path, settings.source_uuid)

    configured_source = diagnostic_source_path(diagnostics.configured_source_path)
    source_path = str(configured_source) if configured_source else None
    stats = diagnostics.stats
    obsidian_configured = bool(settings.obsidian.vault_path)
    autosync_state = (
        autosync_service.status(index_path).state if sys.platform == "darwin" else "unavailable"
    )
    resolved_ui_language = resolve_diagnostic_ui_language(
        settings, diagnostics.dominant_meeting_language
    )
    payload = {
        "index_path": str(index_path),
        "index_database": diagnostics.index_database.as_payload(),
        "state_database": diagnostics.state_database.as_payload(),
        "source_path": source_path,
        "ui_language": settings.ui_language,
        "resolved_ui_language": resolved_ui_language,
        "last_update_at": settings.last_update_at,
        "obsidian_configured": obsidian_configured,
        "autosync": autosync_state,
        **stats,
    }
    if json_output:
        print_json(payload)
        return
    print_text_block(f"index path: {index_path}")
    print_database_diagnostic("index", diagnostics.index_database)
    print_database_diagnostic("state", diagnostics.state_database)
    print_text_block(f"source path: {source_path or 'not configured'}")
    configured_label = "configured" if settings.ui_language else "auto"
    print_text_block(f"language: {resolved_ui_language} ({configured_label})")
    print_text_block(f"last refresh: {settings.last_update_at or 'never'}")
    print_text_block(f"obsidian: {'configured' if obsidian_configured else 'not configured'}")
    print_text_block(f"autosync: {autosync_state}")
    print_text_block(f"meetings: {stats['meetings']}")
    print_text_block(f"chunks: {stats['chunks']}")


@config_app.command("language")
def config_language(
    ctx: typer.Context,
    language: Annotated[
        str,
        typer.Argument(help="UI language: en, ru, or auto."),
    ],
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    normalized = language.casefold().replace("_", "-").split("-", maxsplit=1)[0]
    if normalized == "auto":
        configured_language = None
    elif normalized in {"en", "ru"}:
        configured_language = normalized
    else:
        message = "UI language must be one of: en, ru, auto."
        raise typer.BadParameter(message)

    UserStateRepository(ctx.obj["state_path"]).set_ui_language(configured_language)
    payload = {"ui_language": configured_language}
    if json_output:
        print_json(payload)
        return
    print_text_block(f"ui language: {configured_language or 'auto'}")


@config_app.command("source")
def config_source(
    ctx: typer.Context,
    new_path: Annotated[Path, typer.Argument(help="Path to Meetily meeting_minutes.sqlite.")],
    rebind: Annotated[
        bool,
        typer.Option("--rebind", help="Preserve a state-owned source UUID after verification."),
    ] = False,
    source_uuid: Annotated[
        str | None,
        typer.Option(
            "--source-uuid",
            help="State-owned UUID to repair; requires --rebind.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    if source_uuid is not None and not rebind:
        message = "--source-uuid requires --rebind."
        raise typer.BadParameter(message)
    new_path = require_canonical_source_path(new_path)
    valid, schema_error = inspect_meetily_schema(new_path)
    if not valid:
        raise typer.BadParameter(schema_error or "Meetily DB schema is unsupported.")
    index_path = ctx.obj["index_path"]
    try:
        with RefreshLock(index_path):
            if rebind:
                state_path = Path(index_path).with_name("state.sqlite")
                settings = load_app_settings(ctx.obj["state_path"])
                rebind_uuid = resolve_existing_rebind_source_uuid(
                    state_path,
                    settings,
                    source_uuid,
                )
                user_state = source_state_repository(index_path)
                try:
                    user_state.validate_source_path_claim(
                        rebind_uuid,
                        SOURCE_KIND,
                        new_path,
                    )
                except (AmbiguousSourceIdentityError, ValueError, RuntimeError) as exc:
                    raise typer.BadParameter(str(exc)) from exc
                try:
                    relocated = relocate_selected_source_locked(
                        index_path,
                        user_state,
                        rebind_uuid,
                        new_path,
                        now=utc_now_iso(),
                    )
                    user_state.record_refresh(utc_now_iso())
                    payload = {
                        "source_uuid": rebind_uuid,
                        "old_source_path": str(relocated.previous_path),
                        "new_source_path": str(relocated.published.source.source_path),
                        "matching_meetings": relocated.published.meetings,
                        "source_revision": relocated.published.source.source_revision,
                        "rebound": True,
                    }
                except (AmbiguousSourceIdentityError, ValueError, RuntimeError) as exc:
                    raise typer.BadParameter(str(exc)) from exc
            else:
                payload, selected = scan_update(index_path, new_path)
                UserStateRepository(ctx.obj["state_path"]).record_refresh(utc_now_iso())
                payload.update(
                    {
                        "source_path": str(selected.source.source_path),
                        "rebound": False,
                    }
                )
    except RefreshLockBusyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_output:
        print_json(payload)
        return
    if payload["rebound"]:
        print_text_block(f"old source path: {payload['old_source_path']}")
        print_text_block(f"new source path: {payload['new_source_path']}")
        print_text_block(f"matching meetings: {payload['matching_meetings']}")
        return
    print_text_block(f"source path: {payload['source_path']}")
    print_text_block(f"source uuid: {payload['source_uuid']}")


@app.command(hidden=True)
def scan(
    ctx: typer.Context,
    source: Annotated[
        Path | None,
        typer.Option("--source", help="Path to Meetily meeting_minutes.sqlite."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    try:
        with RefreshLock(ctx.obj["index_path"]):
            source_path = configured_source_path(ctx.obj["state_path"], source)
            if source_path is None:
                message = "Meetily DB was not found. Pass --source /path/to/meeting_minutes.sqlite."
                raise typer.BadParameter(message)
            payload, _result = scan_update(ctx.obj["index_path"], source_path)
            UserStateRepository(ctx.obj["state_path"]).record_refresh(utc_now_iso())
    except RefreshLockBusyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_output:
        print_json(payload)
        return
    print_update_payload(payload)


def run_refresh(
    index_path: Path,
    state_path: Path,
    source_path: Path,
    *,
    force: bool = False,
    sync_obsidian: bool = False,
) -> dict[str, object]:
    source_path = require_canonical_source_path(source_path)
    payload, result = scan_update(index_path, source_path, force=force)
    try:
        UserStateRepository(state_path).record_refresh(utc_now_iso())
    except Exception as exc:
        message = (
            "The fresh index was published and must not be rolled back, but state settings could "
            "not record the refresh timestamp. Fix state.sqlite access and rerun `mm refresh`."
        )
        raise RuntimeError(message) from exc
    if sync_obsidian:
        try:
            obsidian_result = sync_configured_obsidian_locked(index_path, state_path)
        except Exception as exc:
            state = "completed" if result.changed else "unchanged"
            message = f"Index refresh {state}; Obsidian sync failed: {exc}"
            raise RuntimeError(message) from exc
        payload["obsidian_sync"] = (
            obsidian_result.as_payload() if obsidian_result is not None else None
        )
    return payload


@app.command("refresh")
def refresh(
    ctx: typer.Context,
    source: Annotated[
        Path | None,
        typer.Option("--source", help="Path to Meetily meeting_minutes.sqlite."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Rebuild even when the source fingerprint is unchanged."),
    ] = False,
    sync_obsidian: Annotated[
        bool,
        typer.Option("--sync-obsidian", help="Sync configured Obsidian notes after refresh."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    try:
        with RefreshLock(ctx.obj["index_path"]):
            source_path = configured_source_path(ctx.obj["state_path"], source)
            if source_path is None:
                message = "Meetily DB was not found. Pass --source /path/to/meeting_minutes.sqlite."
                raise typer.BadParameter(message)
            payload = run_refresh(
                ctx.obj["index_path"],
                ctx.obj["state_path"],
                source_path,
                force=force,
                sync_obsidian=sync_obsidian,
            )
    except RefreshLockBusyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_output:
        print_json(payload)
        return
    print_update_payload(payload)
    if sync_obsidian:
        print_text_block(
            "obsidian sync: completed"
            if payload.get("obsidian_sync") is not None
            else "obsidian sync: skipped"
        )


@app.command("update")
def update() -> None:
    """Update the installed meetily-memory utility through Homebrew."""
    brew = shutil.which("brew")
    if brew is None:
        message = (
            "Homebrew was not found. If Meetily Memory was installed another way, "
            "update it with that package manager."
        )
        raise typer.BadParameter(message)
    result = subprocess.run([brew, "upgrade", "meetily-memory"], check=False)  # noqa: S603
    if result.returncode != 0:
        message = "Homebrew upgrade failed: brew upgrade meetily-memory"
        raise typer.BadParameter(message)
    print_text_block("updated: meetily-memory")


@db_app.command("status")
def db_status(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    index_path = Path(ctx.obj["index_path"])
    state_path = index_path.with_name("state.sqlite")
    status_diagnostics = inspect_database_status(index_path)
    diagnostics = status_diagnostics.local
    index_database = diagnostics.index_database
    state_database = diagnostics.state_database
    orphaned_tag_assignments = status_diagnostics.orphaned_tag_assignments
    payload = {
        "index_path": str(index_path),
        "state_path": str(state_path),
        "schema_version": index_database.schema_version,
        "current_schema_version": INDEX_SCHEMA_USER_VERSION,
        "schema_status": index_database.status,
        "state_schema_version": state_database.schema_version,
        "state_schema_status": state_database.status,
        "index_database": index_database.as_payload(),
        "state_database": state_database.as_payload(),
        "orphaned_tag_assignments": orphaned_tag_assignments,
        "details_error": status_diagnostics.details_error,
    }
    if json_output:
        print_json(payload)
        return
    print_text_block(f"index path: {index_path}")
    print_text_block(f"state path: {state_path}")
    schema_version = index_database.schema_version
    print_text_block(
        f"schema version: {schema_version if schema_version is not None else 'missing'}"
    )
    print_text_block(f"current schema version: {INDEX_SCHEMA_USER_VERSION}")
    print_text_block(f"schema status: {index_database.status}")
    orphaned_label = (
        str(orphaned_tag_assignments) if orphaned_tag_assignments is not None else "unavailable"
    )
    print_text_block(f"orphaned tag assignments: {orphaned_label}")
    if index_database.error:
        print_text_block(f"index database error: {index_database.error}")
    if state_database.error:
        print_text_block(f"state database error: {state_database.error}")
    if status_diagnostics.details_error:
        print_text_block(f"database details error: {status_diagnostics.details_error}")


@app.command()
def doctor(
    ctx: typer.Context,
    source: Annotated[Path | None, typer.Option("--source")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    index_path = ctx.obj["index_path"]
    try:
        settings = load_app_settings(ctx.obj["state_path"])
    except StateSchemaError:
        settings = AppSettings()
    diagnostics = inspect_local_databases(index_path, settings.source_uuid)
    configured_source = diagnostic_source_path(diagnostics.configured_source_path)
    source_path = source.expanduser() if source else configured_source or discover_meetily_db()
    source_diagnostic = inspect_source_database(source_path)
    fts5 = sqlite_has_fts5()
    stats = diagnostics.stats
    payload = {
        "index_path": str(index_path),
        "index_database": diagnostics.index_database.as_payload(),
        "state_database": diagnostics.state_database.as_payload(),
        "source_path": str(source_path) if source_path else None,
        "source_readable": source_diagnostic.readable,
        "source_schema_valid": source_diagnostic.schema_valid,
        "source_schema_error": source_diagnostic.schema_error,
        "source_read_error": source_diagnostic.read_error,
        "fts5": fts5,
        **stats,
    }
    if json_output:
        print_json(payload)
        return
    console.print(f"index path: {index_path}")
    print_database_diagnostic("index", diagnostics.index_database)
    print_database_diagnostic("state", diagnostics.state_database)
    console.print(f"source path: {source_path or 'not found'}")
    console.print(f"source readable: {'yes' if source_diagnostic.readable else 'no'}")
    console.print(f"source schema: {'valid' if source_diagnostic.schema_valid else 'invalid'}")
    if source_diagnostic.read_error:
        console.print(f"source read error: {source_diagnostic.read_error}")
    if source_diagnostic.schema_error:
        console.print(f"source schema error: {source_diagnostic.schema_error}")
    console.print(f"fts5: {'yes' if fts5 else 'no'}")
    console.print(f"meetings: {stats['meetings']}")
    console.print(f"chunks: {stats['chunks']}")
