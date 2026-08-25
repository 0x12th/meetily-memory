from pathlib import Path
from typing import Any, cast

import pytest
from mcp.server.fastmcp import FastMCP

from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.mcp_server import MCP_TOOL_NAMES, create_mcp_server
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.serializers import search_results_payload

Payload = dict[str, Any]


async def call_payload(server: FastMCP, name: str, arguments: dict[str, object]) -> Payload:
    _, structured = await server.call_tool(name, arguments)
    return cast("Payload", structured)


@pytest.mark.anyio
async def test_mcp_server_exposes_v7_toolset(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)

    server = create_mcp_server(index_path)
    tools = await server.list_tools()

    assert {tool.name for tool in tools} == set(MCP_TOOL_NAMES)


@pytest.mark.anyio
async def test_mcp_tools_are_thin_core_adapters(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    server = create_mcp_server(index_path)

    search = await call_payload(server, "search", {"query": "migration risks", "limit": 3})
    assert search["kind"] == "search"
    assert "contract_version" not in search
    assert search["data"] == search_results_payload(
        MeetilyMemoryCore(index_path).search("migration risks", limit=3)
    )
    assert search["data"]["results"][0]["meeting"]["external_id"] == "meeting-2"

    context = await call_payload(
        server,
        "build_context",
        {"question": "Who owns migration risks?", "limit": 3},
    )
    assert context["kind"] == "context"
    assert "markdown" not in context["data"]
    assert context["data"]["evidence"][0]["meeting"]["external_id"] == "meeting-2"

    tasks = await call_payload(server, "get_tasks", {"limit": 3, "status": "open"})
    assert tasks["kind"] == "structured_entities"
    assert tasks["data"]["entity_kind"] == "action_items"


@pytest.mark.anyio
async def test_mcp_search_and_context_have_no_contract_version_argument(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    tools = await create_mcp_server(index_path).list_tools()
    schemas = {tool.name: tool.inputSchema for tool in tools}

    assert "contract_version" not in schemas["search"]["properties"]
    assert "contract_version" not in schemas["build_context"]["properties"]
