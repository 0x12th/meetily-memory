from __future__ import annotations

import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from locale import getlocale
from pathlib import Path
from shlex import join as shell_join
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
from meetily_memory.config.paths import canonical_source_path, discover_meetily_db
from meetily_memory.config.settings import (
    AppSettings,
    load_app_settings,
    normalize_ui_language,
    update_app_settings,
)
from meetily_memory.db.migrations import CURRENT_SCHEMA_VERSION
from meetily_memory.db.repository import IndexRepository
from meetily_memory.diagnostics import (
    DatabaseDiagnostic,
    inspect_database_status,
    inspect_local_databases,
    inspect_source_database,
)
from meetily_memory.integrations import sync_obsidian_vault
from meetily_memory.refresh_lock import RefreshLock, RefreshLockBusyError
from meetily_memory.repositories.records import PostPublishIssue
from meetily_memory.scanner.meetily_sqlite import (
    MeetilySQLiteScanner,
    ScanResult,
    inspect_meetily_schema,
    meeting_external_ids,
    previous_index_backup_path,
)
from meetily_memory.user_state import (
    AmbiguousSourceIdentityError,
    SourcePathClaim,
    UserStateRepository,
    find_existing_source_by_uuid,
    find_existing_source_for_settings_path,
)

app = make_typer("Local Meetily history lifecycle commands.")
config_app = make_typer("Manage CLI settings.")
db_app = make_typer("Inspect the local index database.")
mcp_app = make_typer("Run the MCP server.")


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


SOURCE_KIND = "meetily_sqlite"


def _rebind_compensation_checkpoint(_name: str) -> None:
    return


class PostPublishRefreshError(RuntimeError):
    run_id: int
    issues: tuple[PostPublishIssue, ...]

    def __init__(self, run_id: int, issues: tuple[PostPublishIssue, ...]) -> None:
        message = (
            f"Index run #{run_id} completed, but post-publish work failed. "
            "The searchable index is current; run `mm status` for a safe retry action."
        )
        super().__init__(message)
        self.run_id = run_id
        self.issues = issues


def settings_update_issue(
    source_uuid: str,
    source_path: Path,
    retry_command: tuple[str, ...],
) -> PostPublishIssue:
    return PostPublishIssue(
        phase="settings_update",
        error_type="SettingsUpdateError",
        action="Retry the published source after fixing settings file access.",
        source_uuid=source_uuid,
        source_path=str(source_path),
        retry_command=retry_command,
    )


def obsidian_sync_issue(
    index_path: Path,
    source_uuid: str,
    source_path: Path,
) -> PostPublishIssue:
    return PostPublishIssue(
        phase="obsidian_sync",
        error_type="ObsidianSyncError",
        action="Retry Obsidian sync for the published source after fixing its configuration.",
        source_uuid=source_uuid,
        source_path=str(source_path),
        retry_command=(
            "mm",
            "--index",
            str(index_path),
            "refresh",
            "--source",
            str(source_path),
        ),
    )


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
    if settings.source_path:
        try:
            source = find_existing_source_for_settings_path(
                state_path,
                SOURCE_KIND,
                settings.source_path,
            )
        except AmbiguousSourceIdentityError as exc:
            raise typer.BadParameter(str(exc)) from exc
        if source is not None:
            return str(source["uuid"])
        message = (
            f"No state-owned source has the exact legacy path: {settings.source_path}. "
            "Pass --source-uuid UUID to choose the identity to repair."
        )
        raise typer.BadParameter(message)
    message = "No source identity is selected. Pass --source-uuid UUID with --rebind."
    raise typer.BadParameter(message)


def migrated_source_settings(index_path: Path, settings_path: Path) -> AppSettings:
    settings = load_app_settings(settings_path)
    if settings.source_uuid:
        return settings
    if not settings.source_path:
        return settings
    state_source = find_existing_source_for_settings_path(
        Path(index_path).with_name("state.sqlite"),
        SOURCE_KIND,
        settings.source_path,
    )
    if state_source is None:
        message = (
            "Legacy settings source_path has no exact state-owned current_path or pending "
            f"projected_path match: {settings.source_path}. Select the source explicitly with "
            "`mm config source PATH`, or repair an existing UUID with "
            "`mm config source PATH --rebind --source-uuid UUID`."
        )
        raise typer.BadParameter(message)
    source_uuid = str(state_source["uuid"])
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
        return require_canonical_source_path(explicit_source)
    settings = migrated_source_settings(index_path, settings_path)
    if settings.source_uuid:
        source = source_state_repository(index_path).get_source(settings.source_uuid)
        configured = Path(str(source["current_path"])).expanduser() if source else None
        if configured and configured.exists():
            return require_canonical_source_path(configured)
    discovered = discover_meetily_db()
    return require_canonical_source_path(discovered) if discovered is not None else None


def scan_update(
    index_path: Path,
    source_path: Path,
    *,
    finalize: bool = True,
) -> tuple[dict[str, object], ScanResult]:
    result = MeetilySQLiteScanner(index_path).scan(
        source_path,
        analyze=False,
        finalize=finalize,
    )

    payload: dict[str, object] = {
        "source_uuid": result.source_uuid,
        "source_local_id": result.source_id,
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


def post_publish_retry_commands(details: object) -> tuple[tuple[str, ...], ...]:
    if not isinstance(details, dict):
        return ()
    issues = details.get("issues")
    if not isinstance(issues, list):
        return ()
    retry_commands: set[tuple[str, ...]] = set()
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        command = issue.get("retry_command")
        if not isinstance(command, list):
            continue
        typed_command: list[str] = []
        for argument in command:
            if not isinstance(argument, str):
                break
            typed_command.append(argument)
        else:
            retry_commands.add(tuple(typed_command))
    return tuple(sorted(retry_commands))


def print_scan_diagnostics(diagnostics: dict[str, dict[str, object] | None]) -> None:
    completed = diagnostics["last_completed_run"]
    failed = diagnostics["last_failed_run"]
    running = diagnostics["last_running_run"]
    post_publish = diagnostics.get("last_post_publish_error")
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
    if post_publish:
        print_text_block(
            "post-publish error: "
            f"run #{post_publish['id']} during {post_publish['phase']} "
            f"({post_publish['error_message']})"
        )
        for retry_command in post_publish_retry_commands(post_publish.get("post_publish")):
            print_text_block(f"post-publish retry: {shell_join(retry_command)}")


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
    should_enable_autosync = autosync
    if should_enable_autosync is None:
        should_enable_autosync = typer.confirm("Enable automatic index refreshes?", default=False)
    try:
        with RefreshLock(ctx.obj["index_path"]):
            source_path = configured_source_path(
                ctx.obj["index_path"], ctx.obj["settings_path"], source
            )
            if source_path is None:
                message = "Meetily DB was not found. Pass --source /path/to/meeting_minutes.sqlite."
                raise typer.BadParameter(message)
            payload, result = scan_update(ctx.obj["index_path"], source_path)
            source_uuid = str(payload["source_uuid"])
            repo = IndexRepository(ctx.obj["index_path"])
            try:
                settings = update_app_settings(
                    settings_path=ctx.obj["settings_path"],
                    source_uuid=source_uuid,
                    source_path=None,
                    autosync_enabled=False,
                    last_update_at=utc_now_iso(),
                )
            except Exception:  # noqa: BLE001
                settings = None
            if settings is None:
                issue = settings_update_issue(
                    source_uuid,
                    source_path,
                    (
                        "mm",
                        "--index",
                        str(ctx.obj["index_path"]),
                        "init",
                        "--source",
                        str(source_path),
                        "--no-autosync",
                    ),
                )
                repo.record_post_publish_failure(result.run_id, (issue,))
                raise PostPublishRefreshError(result.run_id, (issue,)) from None
            repo.resolve_post_publish_failures(source_uuid, ("settings_update",))
    except RefreshLockBusyError as exc:
        raise typer.BadParameter(str(exc)) from exc
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
                settings = load_app_settings(ctx.obj["settings_path"])
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
                    repo = IndexRepository(index_path, state_path=user_state.state_path)
                    repo.heal_pending_source_path_projection(rebind_uuid)
                    claim = user_state.claim_source_path(
                        rebind_uuid,
                        SOURCE_KIND,
                        new_path,
                        now=utc_now_iso(),
                    )
                    payload = rebind_source_identity(
                        index_path,
                        user_state,
                        claim,
                        new_path,
                        ctx.obj["settings_path"],
                        repo=repo,
                    )
                except (AmbiguousSourceIdentityError, ValueError, RuntimeError) as exc:
                    raise typer.BadParameter(str(exc)) from exc
            else:
                payload = select_source(
                    source_state_repository(index_path),
                    new_path,
                    ctx.obj["settings_path"],
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


def select_source(
    user_state: UserStateRepository,
    new_path: Path,
    settings_path: Path,
) -> dict[str, object]:
    new_path = require_canonical_source_path(new_path)
    source_uuid = resolve_source_uuid(user_state, new_path)
    update_app_settings(
        settings_path=settings_path,
        source_uuid=source_uuid,
        source_path=None,
    )
    return {"source_uuid": source_uuid, "source_path": str(new_path), "rebound": False}


def finalize_source_path_claim(
    user_state: UserStateRepository,
    claim: SourcePathClaim,
) -> None:
    if user_state.finalize_source_path_claim(claim):
        return
    message = f"Source path claim for UUID {claim.source_uuid} changed before finalization."
    raise RuntimeError(message)


def rebind_source_identity(
    index_path: Path,
    user_state: UserStateRepository,
    claim: SourcePathClaim,
    new_path: Path,
    settings_path: Path,
    *,
    repo: IndexRepository | None = None,
) -> dict[str, object]:
    new_path = require_canonical_source_path(new_path)
    try:
        repo = repo or IndexRepository(index_path, state_path=user_state.state_path)
        target_ids = meeting_external_ids(new_path)
        indexed_ids = repo.rebind_source_path_projection(claim)
        matching = indexed_ids & target_ids
        update_app_settings(
            settings_path=settings_path,
            source_uuid=claim.source_uuid,
            source_path=None,
        )
        finalize_source_path_claim(user_state, claim)
    except BaseException as exc:
        try:
            rollback_claim = user_state.begin_source_path_rollback(
                claim,
                now=utc_now_iso(),
            )
        except (RuntimeError, sqlite3.Error):
            rollback_claim = None
        projection_restored = False
        state_finalized = False
        if rollback_claim is not None:
            _rebind_compensation_checkpoint("rollback_pending")
            if repo is not None:
                try:
                    projection_restored = repo.restore_source_path_projection(rollback_claim)
                except (OSError, RuntimeError, ValueError, sqlite3.Error):
                    projection_restored = False
            if projection_restored:
                _rebind_compensation_checkpoint("index_rolled_back")
                try:
                    state_finalized = user_state.finalize_source_path_claim(rollback_claim)
                except (RuntimeError, sqlite3.Error):
                    state_finalized = False
        if rollback_claim is None or not projection_restored or not state_finalized:
            message = (
                "Source rebind failed and automatic compensation was incomplete. "
                f"State UUID {claim.source_uuid} remains authoritative; a newer claim or a "
                "persisted rollback-pending claim was not fully reconciled. Inspect state/index "
                "paths and rerun refresh or explicit --rebind before scanning."
            )
            raise RuntimeError(message) from exc
        raise
    return {
        "source_uuid": claim.source_uuid,
        "old_source_path": claim.previous_path,
        "new_source_path": claim.claimed_path,
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
    try:
        with RefreshLock(ctx.obj["index_path"]):
            source_path = configured_source_path(
                ctx.obj["index_path"], ctx.obj["settings_path"], source
            )
            if source_path is None:
                message = "Meetily DB was not found. Pass --source /path/to/meeting_minutes.sqlite."
                raise typer.BadParameter(message)
            result = MeetilySQLiteScanner(ctx.obj["index_path"]).scan(
                source_path,
                force=force,
                analyze=analyze_output,
            )
    except RefreshLockBusyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = {
        "source_uuid": result.source_uuid,
        "source_local_id": result.source_id,
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
    source_path = require_canonical_source_path(source_path)
    previous_backup = previous_index_backup_path(index_path)
    discard_previous_backup = previous_backup.exists()
    settings = load_app_settings(settings_path)
    payload, result = scan_update(
        index_path,
        source_path,
        finalize=False,
    )
    repo = IndexRepository(index_path)
    source_uuid = result.source_uuid
    obsidian_synced_at: str | None = None
    obsidian_synced = False
    failures: list[PostPublishIssue] = []
    if settings.obsidian.vault_path and settings.obsidian.sync_after_update:
        try:
            sync_obsidian_vault(
                index_path,
                Path(settings.obsidian.vault_path),
                settings.obsidian.folder,
            )
            obsidian_synced_at = utc_now_iso()
            obsidian_synced = True
        except Exception:  # noqa: BLE001
            failures.append(obsidian_sync_issue(index_path, source_uuid, source_path))
        else:
            repo.resolve_post_publish_failures(source_uuid, ("obsidian_sync",))
    try:
        update_app_settings(
            settings_path=settings_path,
            expected_obsidian=settings.obsidian if obsidian_synced_at else None,
            obsidian_last_sync_at=obsidian_synced_at,
            source_uuid=source_uuid,
            source_path=None,
            last_update_at=utc_now_iso(),
        )
    except Exception:  # noqa: BLE001
        failures.append(
            settings_update_issue(
                source_uuid,
                source_path,
                ("mm", "--index", str(index_path), "refresh", "--source", str(source_path)),
            )
        )
    else:
        repo.resolve_post_publish_failures(source_uuid, ("settings_update",))
    if failures:
        issues = tuple(failures)
        repo.record_post_publish_failure(result.run_id, issues)
        raise PostPublishRefreshError(result.run_id, issues) from None
    if discard_previous_backup:
        previous_backup.unlink(missing_ok=True)
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
    index_path = Path(ctx.obj["index_path"])
    state_path = index_path.with_name("state.sqlite")
    status_diagnostics = inspect_database_status(index_path)
    diagnostics = status_diagnostics.local
    index_database = diagnostics.index_database
    state_database = diagnostics.state_database
    migration_report = status_diagnostics.migration_report
    orphaned_tag_assignments = status_diagnostics.orphaned_tag_assignments
    migration_status = {
        "current": "current",
        "legacy": "rebuild_required",
        "missing": "missing",
        "incompatible": "incompatible",
    }[index_database.status]
    payload = {
        "index_path": str(index_path),
        "state_path": str(state_path),
        "schema_version": index_database.schema_version,
        "current_schema_version": CURRENT_SCHEMA_VERSION,
        "schema_status": index_database.status,
        "state_schema_version": state_database.schema_version,
        "state_schema_status": state_database.status,
        "migration_status": migration_status,
        "index_database": index_database.as_payload(),
        "state_database": state_database.as_payload(),
        "user_state_migration": migration_report,
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
    print_text_block(f"current schema version: {CURRENT_SCHEMA_VERSION}")
    print_text_block(f"schema status: {index_database.status}")
    print_text_block(f"migration status: {migration_status}")
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
