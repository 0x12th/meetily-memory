from typing import Annotated

import typer

from meetily_memory.cli.common import (
    make_typer,
    print_text_block,
    read_repository_from_context,
)
from meetily_memory.db.repository import IndexRepository
from meetily_memory.domain import MeetingRef
from meetily_memory.tagging import TagMutationResult, TagService

tag_app = make_typer("Manage meeting tags.")


@tag_app.command("add")
def add_tags(
    ctx: typer.Context,
    tags: Annotated[str, typer.Argument(help="Comma-separated tags.")],
    source_uuid: Annotated[str, typer.Option("--source-uuid", help="Stable source UUID.")],
    external_ids: Annotated[
        list[str] | None,
        typer.Option("--external-id", help="Stable meeting ID; repeat for multiple meetings."),
    ] = None,
) -> None:
    meeting_refs = parse_meeting_refs(source_uuid, external_ids)
    service = TagService(IndexRepository(ctx.obj["index_path"]))
    try:
        result = service.assign(meeting_refs, parse_tags(tags))
    except ValueError as exc:
        message = str(exc)
        if message.startswith("Meetings not found:"):
            message = f"{message}\nNo tags were changed."
        raise typer.BadParameter(message) from exc
    print_assignment_result(result)


@tag_app.command("remove")
def remove_tags(
    ctx: typer.Context,
    tags: Annotated[str, typer.Argument(help="Comma-separated tags.")],
    source_uuid: Annotated[str, typer.Option("--source-uuid", help="Stable source UUID.")],
    external_ids: Annotated[
        list[str] | None,
        typer.Option("--external-id", help="Stable meeting ID; repeat for multiple meetings."),
    ] = None,
) -> None:
    meeting_refs = parse_meeting_refs(source_uuid, external_ids)
    service = TagService(IndexRepository(ctx.obj["index_path"]))
    try:
        result = service.remove(meeting_refs, parse_tags(tags))
    except ValueError as exc:
        message = str(exc)
        if message.startswith("Meetings not found:"):
            message = f"{message}\nNo tags were changed."
        raise typer.BadParameter(message) from exc
    if result.removed_tags:
        print_text_block(f"Removed: {', '.join(result.removed_tags)}")
    if result.missing_tags:
        print_text_block(f"Not assigned: {', '.join(result.missing_tags)}")


@tag_app.command("list")
def list_tags(
    ctx: typer.Context,
    source_uuid: Annotated[str | None, typer.Option("--source-uuid")] = None,
    external_id: Annotated[str | None, typer.Option("--external-id")] = None,
) -> None:
    service = TagService(read_repository_from_context(ctx))
    if source_uuid is None and external_id is None:
        for tag in service.list_all():
            print_text_block(f"{tag.display_name}  {tag.active_meetings} meetings")
        return
    meeting_ref = require_meeting_ref(source_uuid, external_id)
    try:
        tags = service.list_for_meeting(meeting_ref)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    print_text_block(f"Meeting {meeting_ref.source_uuid}/{meeting_ref.external_id}")
    for tag in tags:
        print_text_block(f"- {tag.display_name}")


@tag_app.command("suggest")
def suggest_tags(
    ctx: typer.Context,
    source_uuid: Annotated[str, typer.Option("--source-uuid", help="Stable source UUID.")],
    external_id: Annotated[str, typer.Option("--external-id", help="Stable meeting ID.")],
) -> None:
    service = TagService(read_repository_from_context(ctx))
    meeting_ref = MeetingRef(source_uuid, external_id)
    try:
        suggestions = service.suggest(meeting_ref)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    print_text_block(f"Suggested tags for meeting {source_uuid}/{external_id}:")
    if not suggestions:
        print_text_block("No suggestions.")
        return
    for rank, suggestion in enumerate(suggestions, start=1):
        reason = suggestion.reason
        if suggestion.similar_meeting_id is not None:
            reason = f"similar to local meeting #{suggestion.similar_meeting_id}"
        print_text_block(f"{rank}. {suggestion.tag.display_name} — {reason}")


def parse_meeting_refs(
    source_uuid: str,
    external_ids: list[str] | None,
) -> tuple[MeetingRef, ...]:
    if not source_uuid.strip():
        message = "--source-uuid must not be empty."
        raise typer.BadParameter(message)
    values = tuple(value for value in (external_ids or ()) if value)
    if not values:
        message = "Provide at least one --external-id."
        raise typer.BadParameter(message)
    return tuple(MeetingRef(source_uuid, external_id) for external_id in values)


def require_meeting_ref(source_uuid: str | None, external_id: str | None) -> MeetingRef:
    if source_uuid is None or external_id is None:
        message = "Use --source-uuid and --external-id together."
        raise typer.BadParameter(message)
    return MeetingRef(source_uuid, external_id)


def parse_tags(value: str) -> tuple[str, ...]:
    tags = tuple(tag.strip() for tag in value.split(",") if tag.strip())
    if not tags:
        message = "No tags provided."
        raise typer.BadParameter(message)
    return tags


def print_assignment_result(result: TagMutationResult) -> None:
    if result.added_tags:
        print_text_block(f"Added: {', '.join(result.added_tags)}")
    if result.existing_tags:
        print_text_block(f"Already assigned: {', '.join(result.existing_tags)}")
