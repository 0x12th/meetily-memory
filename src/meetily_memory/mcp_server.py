from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from meetily_memory.config.paths import default_index_path
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.serializers import (
    context_bundle_payload,
    envelope,
    meeting_payload,
    person_memory_payload,
    project_memory_payload,
    search_results_payload,
    structured_entities_payload,
    timeline_memory_payload,
    topic_graph_payload,
    topic_memory_payload,
)

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
    ) -> dict[str, object]:
        """Search local Meetily memory with source-backed results."""
        return envelope("search", search_results_payload(core.search(query, limit)))

    @server.tool()
    def get_meeting(meeting_id: str) -> dict[str, object]:
        """Get an indexed meeting by internal or external id."""
        meeting = core.get_meeting(meeting_id)
        return envelope(
            "meeting",
            {"meeting": meeting_payload(meeting) if meeting is not None else None},
        )

    @server.tool()
    def build_context(
        question: str,
        limit: int = 8,
    ) -> dict[str, object]:
        """Build source-backed data-only context for an LLM question."""
        return envelope("context", context_bundle_payload(core.build_context(question, limit)))

    @server.tool()
    def get_person(name: str, limit: int = 10) -> dict[str, object]:
        """Get source-backed memory for a person."""
        return envelope("person", person_memory_payload(core.person(name, limit)))

    @server.tool()
    def get_project(query: str, limit: int = 10) -> dict[str, object]:
        """Get source-backed memory for a project or project-like topic."""
        return envelope("project", project_memory_payload(core.project(query, limit)))

    @server.tool()
    def get_topic(query: str, limit: int = 10) -> dict[str, object]:
        """Get source-backed topic memory."""
        return envelope("topic", topic_memory_payload(core.topic(query, limit)))

    @server.tool()
    def get_related(query: str, limit: int = 50) -> dict[str, object]:
        """Get the local graph projection for a topic."""
        return envelope("graph", topic_graph_payload(core.graph(query, limit)))

    @server.tool()
    def get_timeline(query: str | None = None, limit: int = 20) -> dict[str, object]:
        """Get source-backed timeline signals, optionally filtered by topic."""
        return envelope("timeline", timeline_memory_payload(core.timeline(query, limit)))

    @server.tool()
    def get_decisions(limit: int = 20) -> dict[str, object]:
        """List heuristic decision signals with source evidence."""
        return envelope(
            "structured_entities",
            structured_entities_payload(core.structured_entities("decisions", limit)),
        )

    @server.tool()
    def get_tasks(limit: int = 20, status: str = "open") -> dict[str, object]:
        """List heuristic task signals with source evidence and local status."""
        return envelope(
            "structured_entities",
            structured_entities_payload(
                core.structured_entities("action_items", limit, status=status)
            ),
        )

    @server.tool()
    def get_risks(limit: int = 20) -> dict[str, object]:
        """List heuristic risk signals with source evidence."""
        return envelope(
            "structured_entities",
            structured_entities_payload(core.structured_entities("risks", limit)),
        )

    @server.tool()
    def get_questions(limit: int = 20) -> dict[str, object]:
        """List heuristic open-question signals with source evidence."""
        return envelope(
            "structured_entities",
            structured_entities_payload(core.structured_entities("open_questions", limit)),
        )

    return server


def run_mcp_server(
    index_path: Path | None = None,
    *,
    transport: MCPTransport = "stdio",
) -> None:
    create_mcp_server(index_path).run(transport=transport)
