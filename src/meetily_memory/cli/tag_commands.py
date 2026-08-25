from typing import Annotated

import typer

from meetily_memory.cli.common import make_typer, print_text_block
from meetily_memory.db.repository import IndexRepository
from meetily_memory.tagging import TagMutationResult, TagService

tag_app = make_typer("Manage meeting tags.")


@tag_app.command("add")
def add_tags(
    ctx: typer.Context,
    values: Annotated[list[str], typer.Argument()],
) -> None:
    meeting_ids, tags = parse_tag_arguments(values)
    service = TagService(IndexRepository(ctx.obj["index_path"]))
    try:
        result = service.assign(meeting_ids, tags)
    except ValueError as exc:
        message = str(exc)
        if message.startswith("Meetings not found:"):
            message = f"{message}\nNo tags were changed."
        raise typer.BadParameter(message) from exc
    print_assignment_result(result)


@tag_app.command("remove")
def remove_tags(
    ctx: typer.Context,
    values: Annotated[list[str], typer.Argument()],
) -> None:
    meeting_ids, tags = parse_tag_arguments(values)
    service = TagService(IndexRepository(ctx.obj["index_path"]))
    try:
        result = service.remove(meeting_ids, tags)
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
    meeting_id: Annotated[str | None, typer.Argument()] = None,
) -> None:
    service = TagService(IndexRepository(ctx.obj["index_path"]))
    if meeting_id is not None:
        try:
            tags = service.list_for_meeting(meeting_id)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        print_text_block(f"Meeting #{meeting_id}")
        for tag in tags:
            print_text_block(f"- {tag.display_name}")
        return
    for tag in service.list_all():
        print_text_block(f"{tag.display_name}  {tag.active_meetings} meetings")


@tag_app.command("suggest")
def suggest_tags(
    ctx: typer.Context,
    meeting_id: Annotated[str, typer.Argument()],
) -> None:
    service = TagService(IndexRepository(ctx.obj["index_path"]))
    try:
        suggestions = service.suggest(meeting_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    print_text_block(f"Suggested tags for meeting #{meeting_id}:")
    if not suggestions:
        print_text_block("No suggestions.")
        return
    for rank, suggestion in enumerate(suggestions, start=1):
        reason = suggestion.reason
        if suggestion.similar_meeting_id is not None:
            reason = f"similar to meeting #{suggestion.similar_meeting_id}"
        print_text_block(f"{rank}. {suggestion.tag.display_name} — {reason}")


def parse_tag_arguments(values: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    meeting_ids: list[str] = []
    tag_arguments: list[str] = []
    tags_started = False
    for value in values:
        if not tags_started and value.isdigit():
            meeting_ids.append(value)
            continue
        tags_started = True
        tag_arguments.append(value)
    if not meeting_ids:
        message = "No meeting IDs provided."
        raise typer.BadParameter(message)
    if not tag_arguments:
        message = "No tags provided."
        raise typer.BadParameter(message)
    if len(tag_arguments) != 1:
        message = "Use comma-separated tags; quote a tag that contains spaces."
        raise typer.BadParameter(message)
    tags = tuple(tag.strip() for tag in tag_arguments[0].split(",") if tag.strip())
    if not tags:
        message = "No tags provided."
        raise typer.BadParameter(message)
    return tuple(meeting_ids), tags


def print_assignment_result(result: TagMutationResult) -> None:
    if result.added_tags:
        print_text_block(f"Added: {', '.join(result.added_tags)}")
    if result.existing_tags:
        print_text_block(f"Already assigned: {', '.join(result.existing_tags)}")
