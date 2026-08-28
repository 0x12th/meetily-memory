from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from locale import getlocale
from pathlib import Path
from typing import Annotated

import typer

from meetily_memory.cli.autosync_commands import autosync_runtime_status, enable_autosync
from meetily_memory.cli.common import (
    console,
    make_typer,
    print_json,
    print_text_block,
    sqlite_has_fts5,
)
from meetily_memory.config.paths import discover_meetily_db
from meetily_memory.config.settings import (
    AppSettings,
    load_app_settings,
    normalize_ui_language,
    update_app_settings,
)
from meetily_memory.db.migrations import CURRENT_SCHEMA_VERSION
from meetily_memory.db.repository import IndexRepository
from meetily_memory.db.schema import index_connection
from meetily_memory.diagnostics import (
    DatabaseDiagnostic,
    inspect_local_databases,
    inspect_source_database,
)
from meetily_memory.integrations import sync_obsidian_vault
from meetily_memory.refresh_lock import RefreshLock, RefreshLockBusyError
from meetily_memory.scanner.meetily_sqlite import (
    MeetilySQLiteScanner,
    ScanResult,
    inspect_meetily_schema,
    meeting_external_ids,
)
from meetily_memory.tagging import TagService

app = make_typer("Local Meetily history lifecycle commands.")
config_app = make_typer("Manage CLI settings.")
db_app = make_typer("Inspect the local index database.")
mcp_app = make_typer("Run the MCP server.")


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


SOURCE_KIND = "meetily_sqlite"


def migrated_source_settings(index_path: Path, settings_path: Path) -> AppSettings:
    settings = load_app_settings(settings_path)
    repo = IndexRepository(index_path)
    if settings.source_uuid:
        return settings
    if not settings.source_path:
        return settings
    source_uuid = repo.user_state.get_or_create_source(
        SOURCE_KIND,
        str(Path(settings.source_path).expanduser()),
        now=utc_now_iso(),
    )
    return update_app_settings(
        settings_path=settings_path,
        source_uuid=source_uuid,
        source_path=None,
    )


def configured_source_path(
    index_path: Path,
    settings_path: Path,
    explicit_source: Path | None = None,
) -> Path | None:
    if explicit_source is not None:
        return explicit_source
    settings = migrated_source_settings(index_path, settings_path)
    if settings.source_uuid:
        source = IndexRepository(index_path).user_state.get_source(settings.source_uuid)
        configured = Path(str(source["current_path"])).expanduser() if source else None
        if configured and configured.exists():
            return configured
    if settings.source_path:
        configured = Path(settings.source_path).expanduser()
        if configured.exists():
            return configured
    return discover_meetily_db()


def scan_update(
    index_path: Path,
    source_path: Path,
    *,
    finalize: bool = True,
) -> tuple[dict[str, object], ScanResult]:
    result = MeetilySQLiteScanner(index_path).scan(source_path, analyze=False, finalize=False)
    repo = IndexRepository(index_path)

    if finalize:
        repo.complete_scan_run(result.run_id, utc_now_iso(), result)

    payload: dict[str, object] = {
        "source_id": result.source_id,
        "meetings_seen": result.meetings_seen,
        "meetings_inserted": result.meetings_inserted,
        "meetings_updated": result.meetings_updated,
        "meetings_analyzed": result.meetings_analyzed,
        "chunks_seen": result.chunks_seen,
        "chunks_inserted": result.chunks_inserted,
        "chunks_updated": result.chunks_updated,
    }

    return payload, result


def print_update_payload(payload: dict[str, object]) -> None:
    console.print(f"meetings seen: {payload['meetings_seen']}")
    console.print(f"meetings inserted: {payload['meetings_inserted']}")
    console.print(f"meetings updated: {payload['meetings_updated']}")
    console.print(f"meetings analyzed: {payload['meetings_analyzed']}")
    console.print(f"chunks seen: {payload['chunks_seen']}")


def print_scan_diagnostics(diagnostics: dict[str, dict[str, object] | None]) -> None:
    completed = diagnostics["last_completed_run"]
    failed = diagnostics["last_failed_run"]
    running = diagnostics["last_running_run"]
    if completed:
        print_text_block(f"last completed run: #{completed['id']} at {completed['finished_at']}")
    if failed:
        print_text_block(
            f"last failed run: #{failed['id']} during {failed['phase']} ({failed['error_message']})"
        )
    if running:
        started_at = running.get("started_at") or "unknown"
        phase = running.get("phase") or "unknown"
        print_text_block(f"running run: #{running['id']} since {started_at} during {phase}")


def print_database_diagnostic(label: str, diagnostic: DatabaseDiagnostic) -> None:
    print_text_block(f"{label} database: {diagnostic.status_label()}")
    if diagnostic.error:
        print_text_block(f"{label} database error: {diagnostic.error}")


def diagnostic_source_path(
    settings: AppSettings,
    persisted_source_path: Path | None,
) -> Path | None:
    if persisted_source_path is not None:
        return persisted_source_path
    if settings.source_path:
        return Path(settings.source_path).expanduser()
    return None


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
        typer.Option("--autosync/--no-autosync", help="Enable automatic index refreshes."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    source_path = configured_source_path(ctx.obj["index_path"], ctx.obj["settings_path"], source)
    if source_path is None:
        message = "Meetily DB was not found. Pass --source /path/to/meeting_minutes.sqlite."
        raise typer.BadParameter(message)
    should_enable_autosync = autosync
    if should_enable_autosync is None:
        should_enable_autosync = typer.confirm("Enable automatic index refreshes?", default=False)
    try:
        with RefreshLock(ctx.obj["index_path"]):
            payload, _ = scan_update(ctx.obj["index_path"], source_path)
    except RefreshLockBusyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    repo = IndexRepository(ctx.obj["index_path"])
    source_uuid = repo.user_state.get_or_create_source(
        SOURCE_KIND, str(source_path), now=utc_now_iso()
    )
    settings = update_app_settings(
        settings_path=ctx.obj["settings_path"],
        source_uuid=source_uuid,
        source_path=None,
        autosync_enabled=False,
        last_update_at=utc_now_iso(),
    )
    if should_enable_autosync:
        try:
            enable_autosync(
                ctx.obj["index_path"],
                ctx.obj["settings_path"],
                interval_minutes=30,
            )
        except RuntimeError as exc:
            raise typer.BadParameter(str(exc)) from exc
        settings = load_app_settings(ctx.obj["settings_path"])
    response = {
        "initialized": True,
        "index_path": str(ctx.obj["index_path"]),
        "source_path": str(source_path),
        "autosync_enabled": settings.autosync_enabled,
        **payload,
    }
    if json_output:
        print_json(response)
        return
    print_text_block("initialized: yes")
    print_text_block(f"index path: {ctx.obj['index_path']}")
    print_text_block(f"source path: {source_path}")
    print_text_block(f"autosync: {'enabled' if settings.autosync_enabled else 'disabled'}")
    print_update_payload(payload)


@app.command()
def status(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    index_path = ctx.obj["index_path"]
    settings = load_app_settings(ctx.obj["settings_path"])
    diagnostics = inspect_local_databases(index_path, settings.source_uuid)
    autosync_status = autosync_runtime_status(ctx.obj["settings_path"], index_path)
    configured_source = diagnostic_source_path(settings, diagnostics.configured_source_path)
    source_path = str(configured_source) if configured_source else None
    stats = diagnostics.stats
    scan_diagnostics = diagnostics.scan_runs
    obsidian_configured = bool(settings.obsidian.vault_path)
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
        "autosync_enabled": autosync_status.enabled,
        "autosync_configured": autosync_status.configured,
        "autosync_installed": autosync_status.installed,
        "autosync_active": autosync_status.active,
        "obsidian_configured": obsidian_configured,
        **scan_diagnostics,
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
    print_text_block(f"autosync: {autosync_status.label}")
    print_text_block(f"obsidian: {'configured' if obsidian_configured else 'not configured'}")
    print_text_block(f"meetings: {stats['meetings']}")
    print_text_block(f"chunks: {stats['chunks']}")
    print_scan_diagnostics(scan_diagnostics)


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
        settings = update_app_settings(settings_path=ctx.obj["settings_path"], ui_language=None)
    elif normalized in {"en", "ru"}:
        settings = update_app_settings(
            settings_path=ctx.obj["settings_path"], ui_language=normalized
        )
    else:
        message = "UI language must be one of: en, ru, auto."
        raise typer.BadParameter(message)

    payload = {"ui_language": settings.ui_language}
    if json_output:
        print_json(payload)
        return
    print_text_block(f"ui language: {settings.ui_language or 'auto'}")


@config_app.command("source")
def config_source(
    ctx: typer.Context,
    new_path: Annotated[Path, typer.Argument(help="Path to Meetily meeting_minutes.sqlite.")],
    rebind: Annotated[
        bool,
        typer.Option("--rebind", help="Preserve the selected source UUID after verification."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    new_path = new_path.expanduser()
    valid, schema_error = inspect_meetily_schema(new_path)
    if not valid:
        raise typer.BadParameter(schema_error or "Meetily DB schema is unsupported.")
    index_path = ctx.obj["index_path"]
    repo = IndexRepository(index_path)
    settings = migrated_source_settings(index_path, ctx.obj["settings_path"])
    payload = (
        rebind_selected_source(repo, settings, new_path, ctx.obj["settings_path"])
        if rebind
        else select_source(repo, new_path, ctx.obj["settings_path"])
    )
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


def select_source(repo: IndexRepository, new_path: Path, settings_path: Path) -> dict[str, object]:
    source_uuid = repo.user_state.get_or_create_source(
        SOURCE_KIND, str(new_path), now=utc_now_iso()
    )
    update_app_settings(
        settings_path=settings_path,
        source_uuid=source_uuid,
        source_path=None,
    )
    return {"source_uuid": source_uuid, "source_path": str(new_path), "rebound": False}


def rebind_selected_source(
    repo: IndexRepository, settings: AppSettings, new_path: Path, settings_path: Path
) -> dict[str, object]:
    if not settings.source_uuid:
        message = "No selected source is available to rebind."
        raise typer.BadParameter(message)
    current = repo.user_state.get_source(settings.source_uuid)
    if current is None:
        message = "The selected source no longer exists in user state."
        raise typer.BadParameter(message)
    old_path = str(current["current_path"])
    target = repo.user_state.get_source_by_path(SOURCE_KIND, str(new_path))
    if target and target["uuid"] != settings.source_uuid:
        message = "The new path is already linked to another source UUID."
        raise typer.BadParameter(message)
    indexed_ids = repo.source_meeting_external_ids(SOURCE_KIND, old_path)
    if not indexed_ids:
        return select_source(repo, new_path, settings_path)
    matching = indexed_ids & meeting_external_ids(new_path)
    if not matching:
        message = "The new source has no matching meeting IDs."
        raise typer.BadParameter(message)

    repo.user_state.update_source_path(settings.source_uuid, str(new_path), now=utc_now_iso())
    repo.update_source_path_projection(SOURCE_KIND, old_path, str(new_path))
    update_app_settings(
        settings_path=settings_path,
        source_uuid=settings.source_uuid,
        source_path=None,
    )
    return {
        "source_uuid": settings.source_uuid,
        "old_source_path": old_path,
        "new_source_path": str(new_path),
        "matching_meetings": len(matching),
        "rebound": True,
    }


@app.command(hidden=True)
def scan(
    ctx: typer.Context,
    source: Annotated[
        Path | None,
        typer.Option("--source", help="Path to Meetily meeting_minutes.sqlite."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Reindex unchanged meetings and rebuild FTS rows."),
    ] = False,
    analyze_output: Annotated[
        bool,
        typer.Option("--analyze/--no-analyze", help="Analyze new or changed meetings."),
    ] = True,
) -> None:
    source_path = configured_source_path(ctx.obj["index_path"], ctx.obj["settings_path"], source)
    if source_path is None:
        message = "Meetily DB was not found. Pass --source /path/to/meeting_minutes.sqlite."
        raise typer.BadParameter(message)
    try:
        with RefreshLock(ctx.obj["index_path"]):
            result = MeetilySQLiteScanner(ctx.obj["index_path"]).scan(
                source_path,
                force=force,
                analyze=analyze_output,
            )
    except RefreshLockBusyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = {
        "source_id": result.source_id,
        "meetings_seen": result.meetings_seen,
        "meetings_inserted": result.meetings_inserted,
        "meetings_updated": result.meetings_updated,
        "meetings_analyzed": result.meetings_analyzed,
        "chunks_seen": result.chunks_seen,
        "chunks_inserted": result.chunks_inserted,
        "chunks_updated": result.chunks_updated,
    }
    if json_output:
        print_json(payload)
        return
    console.print(f"meetings seen: {result.meetings_seen}")
    console.print(f"meetings inserted: {result.meetings_inserted}")
    console.print(f"meetings updated: {result.meetings_updated}")
    console.print(f"meetings analyzed: {result.meetings_analyzed}")
    console.print(f"chunks seen: {result.chunks_seen}")


def run_refresh(
    index_path: Path,
    settings_path: Path,
    source_path: Path,
) -> tuple[dict[str, object], bool]:
    repo = IndexRepository(index_path)
    repo.fail_abandoned_scan_runs(utc_now_iso())
    settings = load_app_settings(settings_path)
    payload, result = scan_update(
        index_path,
        source_path,
        finalize=False,
    )
    phase = "source_scan"
    obsidian_synced_at: str | None = None
    try:
        phase = "finalizing"
        repo.update_scan_run_phase(result.run_id, phase)
        source_uuid = repo.user_state.get_or_create_source(
            SOURCE_KIND, str(source_path), now=utc_now_iso()
        )
        obsidian_synced = False
        if settings.obsidian.vault_path and settings.obsidian.sync_after_update:
            phase = "obsidian_sync"
            repo.update_scan_run_phase(result.run_id, phase)
            sync_obsidian_vault(
                index_path,
                Path(settings.obsidian.vault_path),
                settings.obsidian.folder,
            )
            obsidian_synced_at = utc_now_iso()
            obsidian_synced = True
        phase = "finalizing"
        repo.update_scan_run_phase(result.run_id, phase)
        repo.complete_scan_run(result.run_id, utc_now_iso(), result)
        update_app_settings(
            settings_path=settings_path,
            expected_obsidian=settings.obsidian if obsidian_synced_at else None,
            obsidian_last_sync_at=obsidian_synced_at,
            source_uuid=source_uuid,
            source_path=None,
            last_update_at=utc_now_iso(),
        )
    except Exception as exc:
        repo.fail_scan_run(
            result.run_id,
            utc_now_iso(),
            phase,
            result,
            type(exc).__name__,
        )
        raise
    return payload, obsidian_synced


@app.command("refresh")
def refresh(
    ctx: typer.Context,
    source: Annotated[
        Path | None,
        typer.Option("--source", help="Path to Meetily meeting_minutes.sqlite."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
    autosync_run: Annotated[bool, typer.Option("--autosync-run", hidden=True)] = False,
) -> None:
    try:
        with RefreshLock(ctx.obj["index_path"]):
            source_path = configured_source_path(
                ctx.obj["index_path"], ctx.obj["settings_path"], source
            )
            if source_path is None:
                message = "Meetily DB was not found. Pass --source /path/to/meeting_minutes.sqlite."
                raise typer.BadParameter(message)
            payload, obsidian_synced = run_refresh(
                ctx.obj["index_path"],
                ctx.obj["settings_path"],
                source_path,
            )
    except RefreshLockBusyError as exc:
        if autosync_run:
            typer.echo(f"Autosync skipped: {exc}", err=True)
            return
        raise typer.BadParameter(str(exc)) from exc
    if json_output:
        payload["obsidian_synced"] = obsidian_synced
        print_json(payload)
        return
    print_update_payload(payload)
    if obsidian_synced:
        console.print("obsidian sync: yes")


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
    index_path = ctx.obj["index_path"]
    repo = IndexRepository(index_path)
    with index_connection(index_path) as conn:
        schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    migration_report = repo.user_state.latest_migration_report()
    orphaned_tag_assignments = TagService(repo).orphaned_assignment_count()
    payload = {
        "index_path": str(index_path),
        "state_path": str(repo.state_path),
        "schema_version": schema_version,
        "current_schema_version": CURRENT_SCHEMA_VERSION,
        "user_state_migration": migration_report,
        "orphaned_tag_assignments": orphaned_tag_assignments,
    }
    if json_output:
        print_json(payload)
        return
    print_text_block(f"index path: {index_path}")
    print_text_block(f"state path: {repo.state_path}")
    print_text_block(f"schema version: {schema_version}")
    print_text_block(f"current schema version: {CURRENT_SCHEMA_VERSION}")
    print_text_block(f"orphaned tag assignments: {orphaned_tag_assignments}")
    if migration_report:
        print_text_block(
            "user state migration: "
            f"{migration_report['migrated']} migrated, "
            f"{migration_report['orphaned']} orphaned"
        )


@mcp_app.command("serve")
def mcp_serve(ctx: typer.Context) -> None:
    try:
        from meetily_memory.mcp_server import run_mcp_server  # noqa: PLC0415
    except ImportError as exc:
        message = "MCP support is optional. Install with `meetily-memory[mcp]`."
        raise typer.BadParameter(message) from exc
    run_mcp_server(ctx.obj["index_path"])


@app.command()
def doctor(
    ctx: typer.Context,
    source: Annotated[Path | None, typer.Option("--source")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    index_path = ctx.obj["index_path"]
    settings = load_app_settings(ctx.obj["settings_path"])
    diagnostics = inspect_local_databases(index_path, settings.source_uuid)
    configured_source = diagnostic_source_path(settings, diagnostics.configured_source_path)
    source_path = source.expanduser() if source else configured_source or discover_meetily_db()
    source_diagnostic = inspect_source_database(source_path)
    fts5 = sqlite_has_fts5()
    stats = diagnostics.stats
    scan_diagnostics = diagnostics.scan_runs
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
        **scan_diagnostics,
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
    console.print(f"decisions: {stats['decisions']}")
    console.print(f"action items: {stats['action_items']}")
    console.print(f"risks: {stats['risks']}")
    console.print(f"open questions: {stats['open_questions']}")
    print_scan_diagnostics(scan_diagnostics)
