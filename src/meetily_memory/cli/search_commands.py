from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Annotated, cast

import typer

from meetily_memory.cli.common import (
    compact_date,
    console,
    core_from_context,
    make_typer,
    open_path,
    print_json,
    print_text_block,
    ui_language_from_context,
)
from meetily_memory.cli.renderers import print_topic_memory
from meetily_memory.context_builder import DEFAULT_CONTEXT_LIMIT, ContextRenderer
from meetily_memory.db.repository import IndexRepository
from meetily_memory.domain import MeetingSearchFilters
from meetily_memory.serializers import (
    meeting_search_result_payload,
    topic_alias_payload,
    topic_memory_payload,
)

app = make_typer("Search and context commands.")


def parse_search_filters(
    *,
    since: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    local_timezone: tzinfo | None = None,
) -> MeetingSearchFilters:
    if since is not None and from_date is not None:
        message = "--since and --from are mutually exclusive"
        raise ValueError(message)

    timezone = local_timezone or datetime.now().astimezone().tzinfo or UTC
    from_utc: datetime | None = None
    to_utc: datetime | None = None
    if since is not None:
        if not since.endswith("d") or not since[:-1].isdigit() or int(since[:-1]) <= 0:
            message = "--since must be a positive number of days, for example 7d"
            raise ValueError(message)
        current = now().astimezone(UTC)
        from_utc = current - timedelta(days=int(since[:-1]))
        to_utc = current
    if from_date is not None:
        from_utc = local_date_boundary(from_date, "--from", timezone).astimezone(UTC)
    if to_date is not None:
        end_date = local_date_boundary(to_date, "--to", timezone) + timedelta(days=1)
        to_utc = end_date.astimezone(UTC)
    if from_utc is not None and to_utc is not None and from_utc >= to_utc:
        message = "--to must not be earlier than --from or --since"
        raise ValueError(message)
    return MeetingSearchFilters(from_utc=from_utc, to_utc=to_utc)


def local_date_boundary(value: str, option: str, timezone: tzinfo) -> datetime:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        message = f"Invalid {option} date: {value}. Expected YYYY-MM-DD"
        raise ValueError(message) from exc
    if parsed.isoformat() != value:
        message = f"Invalid {option} date: {value}. Expected YYYY-MM-DD"
        raise ValueError(message)
    return datetime.combine(parsed, datetime.min.time(), timezone)


@app.command("s")
def search(
    ctx: typer.Context,
    query: str,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    context: Annotated[
        int,
        typer.Option("--context", "-C", min=0, help="Include N chunks before and after each hit."),
    ] = 0,
    since: Annotated[
        str | None,
        typer.Option("--since", help="From now minus a positive number of days, e.g. 7d."),
    ] = None,
    from_date: Annotated[
        str | None,
        typer.Option("--from", help="inclusive local date (YYYY-MM-DD)."),
    ] = None,
    to_date: Annotated[
        str | None,
        typer.Option("--to", help="Include the entire local date (YYYY-MM-DD)."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        filters = parse_search_filters(since=since, from_date=from_date, to_date=to_date)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    search_results = core_from_context(ctx).search(query, limit, context, filters=filters)
    results = [meeting_search_result_payload(result) for result in search_results.results]
    if json_output:
        print_json(results)
        return
    print_search_results(results)


def print_search_results(results: list[dict[str, object]]) -> None:
    for index, result in enumerate(results):
        if index:
            console.print()
        print_search_meeting_header(result)
        matched_tags = cast("list[str]", result["matched_tags"])
        if matched_tags:
            console.print(f"matched tag: {', '.join(matched_tags)}")
        for evidence in cast("list[dict[str, object]]", result["evidence"]):
            print_search_excerpt(evidence)


def print_search_meeting_header(result: dict[str, object]) -> None:
    meeting_id = result["meeting_id"]
    meeting = cast("dict[str, object]", result["meeting"])
    date = compact_date(meeting.get("updated_at") or meeting.get("created_at"))
    suffix = f" ({date})" if date else ""
    console.print(f"#{meeting_id} {meeting['title']}{suffix}")
    console.print(f"open: mm open {meeting_id}")


def print_search_excerpt(result: dict[str, object]) -> None:
    excerpt = cast("dict[str, object]", result["excerpt"])
    source_parts = [
        f"chunk #{excerpt.get('chunk_external_id') or excerpt['ordinal']}",
    ]
    if excerpt.get("timestamp_label"):
        source_parts.insert(0, str(excerpt["timestamp_label"]))
    if result.get("is_context"):
        source_parts.append("context")
    console.print(" | ".join(source_parts))
    text = str(excerpt["text"])
    if excerpt.get("speaker"):
        text = f"{excerpt['speaker']}: {text}"
    console.print(text)
    console.print()


@app.command("c", hidden=True)
def context(
    ctx: typer.Context,
    question: str,
    limit: Annotated[int, typer.Option("--limit", "-n")] = DEFAULT_CONTEXT_LIMIT,
    context: Annotated[
        int,
        typer.Option("--context", help="Adjacent chunks around each lexical match."),
    ] = 0,
) -> None:
    bundle = core_from_context(ctx).build_context(question, limit, context=context)
    print_text_block(ContextRenderer().render(bundle))


@app.command("t", hidden=True)
def topic_memory(
    ctx: typer.Context,
    query: str,
    alias: Annotated[
        list[str] | None,
        typer.Option("--alias", help="Add an alias for this topic."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    core = core_from_context(ctx)
    if alias:
        alias_result = core.add_topic_alias(query, alias)
        if json_output:
            print_json(topic_alias_payload(alias_result))
            return
        for added_alias in alias_result.added_aliases:
            print_text_block(f"alias added: {added_alias} -> {alias_result.topic.title}")
    memory = topic_memory_payload(core.topic(query, limit))
    if json_output:
        print_json(memory)
        return
    memory["ui_language"] = ui_language_from_context(ctx)
    print_topic_memory(memory)


app.command("topic", hidden=True)(topic_memory)


@app.command("open")
def open_command(
    ctx: typer.Context,
    meeting_id: str,
    source: Annotated[bool, typer.Option("--source", help="Open the indexed source path.")] = False,
    print_path: Annotated[
        bool,
        typer.Option("--print-path", help="Print the selected path without opening it."),
    ] = False,
) -> None:
    """Open the original meeting folder."""
    repo = IndexRepository(ctx.obj["index_path"])
    meeting = repo.get_meeting(meeting_id)
    if not meeting:
        message = f"Meeting not found: {meeting_id}"
        raise typer.BadParameter(message)
    path = meeting.get("source_path") if source else meeting.get("folder_path")
    path = path or meeting.get("folder_path") or meeting.get("source_path")
    if not path:
        message = f"Meeting has no path: {meeting_id}"
        raise typer.BadParameter(message)
    if print_path:
        print_text_block(str(path))
        return
    open_path(Path(path))
