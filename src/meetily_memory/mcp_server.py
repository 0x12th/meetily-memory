from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from meetily_memory.config.paths import default_index_path
from meetily_memory.core import CORE_V1_VERSION, MeetilyMemoryCore

MCP_TOOL_NAMES = (
    "search",
    "get_meeting",
    "build_context",
    "get_person",
    "get_project",
    "get_topic",
    "get_related",
    "get_timeline",
    "get_decisions",
    "get_tasks",
    "get_risks",
    "get_questions",
)
MCPTransport = Literal["stdio", "sse", "streamable-http"]


def create_mcp_server(index_path: Path | None = None) -> FastMCP:  # noqa: C901
    core = MeetilyMemoryCore(index_path or default_index_path())
    server = FastMCP("Meetily Memory", json_response=True)

    @server.tool()
    def search(
        query: str,
        limit: int = 10,
        contract_version: str = CORE_V1_VERSION,
    ) -> dict[str, object]:
        """Search local Meetily memory with source-backed results."""
        return core.search(query, limit, contract_version=contract_version).as_payload()

    @server.tool()
    def get_meeting(meeting_id: str) -> dict[str, object]:
        """Get an indexed meeting by internal or external id."""
        return core.get_meeting(meeting_id).as_payload()

    @server.tool()
    def build_context(
        question: str,
        limit: int = 8,
        contract_version: str = CORE_V1_VERSION,
    ) -> dict[str, object]:
        """Build source-backed Markdown context for an LLM question."""
        return core.build_context(
            question,
            limit,
            contract_version=contract_version,
        ).as_payload()

    @server.tool()
    def get_person(name: str, limit: int = 10) -> dict[str, object]:
        """Get source-backed memory for a person."""
        return core.person(name, limit).as_payload()

    @server.tool()
    def get_project(query: str, limit: int = 10) -> dict[str, object]:
        """Get source-backed memory for a project or project-like topic."""
        return core.project(query, limit).as_payload()

    @server.tool()
    def get_topic(query: str, limit: int = 10) -> dict[str, object]:
        """Get source-backed topic memory."""
        return core.topic(query, limit).as_payload()

    @server.tool()
    def get_related(query: str, limit: int = 50) -> dict[str, object]:
        """Get the local graph projection for a topic."""
        return core.graph(query, limit).as_payload()

    @server.tool()
    def get_timeline(query: str | None = None, limit: int = 20) -> dict[str, object]:
        """Get source-backed timeline signals, optionally filtered by topic."""
        return core.timeline(query, limit).as_payload()

    @server.tool()
    def get_decisions(limit: int = 20) -> dict[str, object]:
        """List heuristic decision signals with source evidence."""
        return core.structured_entities("decisions", limit).as_payload()

    @server.tool()
    def get_tasks(limit: int = 20, status: str = "open") -> dict[str, object]:
        """List heuristic task signals with source evidence and local status."""
        return core.structured_entities("action_items", limit, status=status).as_payload()

    @server.tool()
    def get_risks(limit: int = 20) -> dict[str, object]:
        """List heuristic risk signals with source evidence."""
        return core.structured_entities("risks", limit).as_payload()

    @server.tool()
    def get_questions(limit: int = 20) -> dict[str, object]:
        """List heuristic open-question signals with source evidence."""
        return core.structured_entities("open_questions", limit).as_payload()

    return server


def run_mcp_server(
    index_path: Path | None = None,
    *,
    transport: MCPTransport = "stdio",
) -> None:
    create_mcp_server(index_path).run(transport=transport)
