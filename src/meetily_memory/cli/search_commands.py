from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Annotated

import typer

from meetily_memory.cli.common import (
    compact_date,
    console,
    core_from_context,
    make_typer,
    open_path,
    print_json,
    print_text_block,
    read_repository_from_context,
)
from meetily_memory.domain import MeetingRef, MeetingSearchFilters, MeetingSearchResult, SearchHit
from meetily_memory.open_commands import stable_meeting_open_command
from meetily_memory.serializers import meeting_search_result_payload

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
    if json_output:
        print_json([meeting_search_result_payload(result) for result in search_results.results])
        return
    print_search_results(search_results.results)


def print_search_results(results: tuple[MeetingSearchResult, ...]) -> None:
    for index, result in enumerate(results):
        if index:
            console.print()
        print_search_meeting_header(result)
        if result.matched_tags:
            console.print(f"matched tag: {', '.join(result.matched_tags)}")
        for evidence in result.evidence:
            print_search_excerpt(evidence)


def print_search_meeting_header(result: MeetingSearchResult) -> None:
    meeting = result.meeting
    date = compact_date(meeting.updated_at or meeting.created_at)
    suffix = f" ({date})" if date else ""
    console.print(f"#{result.meeting_id} {meeting.title}{suffix}")
    print_text_block("open: " + stable_meeting_open_command(meeting.ref))


def print_search_excerpt(result: SearchHit) -> None:
    excerpt = result.excerpt
    source_parts = [f"chunk #{excerpt.chunk_external_id or excerpt.ordinal}"]
    if excerpt.timestamp_label:
        source_parts.insert(0, excerpt.timestamp_label)
    if result.is_context:
        source_parts.append("context")
    console.print(" | ".join(source_parts))
    text = excerpt.text
    if excerpt.speaker:
        text = f"{excerpt.speaker}: {text}"
    console.print(text)
    console.print()


@app.command("open")
def open_command(
    ctx: typer.Context,
    meeting_ref: Annotated[
        str,
        typer.Argument(help="Canonical meeting reference: SOURCE_UUID/EXTERNAL_ID."),
    ],
) -> None:
    """Open the original meeting folder."""
    try:
        ref = MeetingRef.parse(meeting_ref)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    meeting = read_repository_from_context(ctx).get_meeting_by_ref(ref)
    if meeting is None:
        message = f"Meeting not found: {ref}"
        raise typer.BadParameter(message)
    folder_path = meeting.get("folder_path")
    if not folder_path:
        message = f"Meeting has no source-native folder: {ref}"
        raise typer.BadParameter(message)
    open_path(Path(str(folder_path)))
