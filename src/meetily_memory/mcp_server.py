from functools import wraps
from pathlib import Path
from typing import Annotated, cast

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from meetily_memory.cli.search_commands import parse_search_filters
from meetily_memory.config.paths import default_index_path
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.serializers import (
    envelope,
    meeting_payload,
    search_results_payload,
)

MCP_TOOL_NAMES = (
    "search_meetings",
    "get_meeting",
)


def create_mcp_server(index_path: Path | None = None) -> FastMCP:
    core = MeetilyMemoryCore(index_path or default_index_path())
    server = FastMCP("Meetily Memory", json_response=True)

    def search_meetings_schema(
        query: str,
        limit: int = 10,
        since: str | None = None,
        from_date: Annotated[str | None, Field(alias="from")] = None,
        to_date: Annotated[str | None, Field(alias="to")] = None,
    ) -> dict[str, object]:
        """Search local, source-backed meetings; results may include private meeting data."""
        raise NotImplementedError

    # FastMCP uses aliases in the generated schema and when invoking the function. Keep the
    # public JSON keys `from` and `to` while the wrapper accepts those reserved Python words.
    @server.tool(name="search_meetings")
    @wraps(search_meetings_schema)
    def search_meetings(**arguments: object) -> dict[str, object]:
        query = cast("str", arguments["query"])
        limit = cast("int", arguments["limit"])
        since = cast("str | None", arguments["since"])
        from_date = cast("str | None", arguments["from"])
        to_date = cast("str | None", arguments["to"])
        filters = parse_search_filters(since=since, from_date=from_date, to_date=to_date)
        return envelope(
            "search",
            search_results_payload(core.search(query, limit, filters=filters)),
        )

    @server.tool()
    def get_meeting(meeting_id: str) -> dict[str, object]:
        """Get an indexed meeting by internal or external id."""
        meeting = core.get_meeting(meeting_id)
        return envelope(
            "meeting",
            {"meeting": meeting_payload(meeting) if meeting is not None else None},
        )

    return server


def run_mcp_server(index_path: Path | None = None) -> None:
    create_mcp_server(index_path).run(transport="stdio")
