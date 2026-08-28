from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from meetily_memory.cli.search_commands import parse_search_filters
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.mcp_server import MCP_TOOL_NAMES, create_mcp_server
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.serializers import meeting_payload, search_results_payload

Payload = dict[str, Any]


async def call_payload(server: FastMCP, name: str, arguments: Mapping[str, object]) -> Payload:
    _, structured = await server.call_tool(name, dict(arguments))
    return cast("Payload", structured)


@pytest.mark.anyio
async def test_mcp_server_exposes_only_meeting_search_and_lookup(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)

    server = create_mcp_server(index_path)
    tools = await server.list_tools()

    assert {tool.name for tool in tools} == set(MCP_TOOL_NAMES)
    assert set(MCP_TOOL_NAMES) == {"search_meetings", "get_meeting"}
    descriptions = {tool.name: tool.description for tool in tools}
    search_description = descriptions["search_meetings"] or ""
    assert "local" in search_description.casefold()
    assert "source-backed" in search_description.casefold()
    assert "private" in search_description.casefold()


@pytest.mark.anyio
async def test_mcp_search_matches_core_with_time_filters(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    server = create_mcp_server(index_path)

    arguments = {
        "query": "migration risks",
        "limit": 3,
        "from": "2026-07-02",
        "to": "2026-07-02",
    }
    search = await call_payload(server, "search_meetings", arguments)
    assert search["kind"] == "search"
    assert "contract_version" not in search
    filters = parse_search_filters(from_date="2026-07-02", to_date="2026-07-02")
    assert search["data"] == search_results_payload(
        MeetilyMemoryCore(index_path).search("migration risks", limit=3, filters=filters)
    )
    assert search["data"]["results"][0]["meeting"]["external_id"] == "meeting-2"

    core_meeting = MeetilyMemoryCore(index_path).get_meeting("meeting-2")
    assert core_meeting is not None
    meeting = await call_payload(server, "get_meeting", {"meeting_id": "meeting-2"})
    assert meeting == {
        "kind": "meeting",
        "data": {"meeting": meeting_payload(core_meeting)},
    }
    missing = await call_payload(server, "get_meeting", {"meeting_id": "missing"})
    assert missing == {"kind": "meeting", "data": {"meeting": None}}


@pytest.mark.anyio
async def test_mcp_search_schema_has_cli_filters_without_legacy_contract_argument(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    tools = await create_mcp_server(index_path).list_tools()
    schemas = {tool.name: tool.inputSchema for tool in tools}

    assert set(schemas["search_meetings"]["properties"]) == {
        "query",
        "limit",
        "since",
        "from",
        "to",
    }
    assert "contract_version" not in schemas["search_meetings"]["properties"]


@pytest.mark.anyio
async def test_mcp_since_filter_uses_the_same_validation_as_cli(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)

    with pytest.raises(ToolError, match="positive number of days"):
        await call_payload(
            create_mcp_server(index_path),
            "search_meetings",
            {"query": "migration", "since": "0d"},
        )
