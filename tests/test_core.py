import json
from pathlib import Path

from typer.testing import CliRunner

from meetily_memory.cli.app import app
from meetily_memory.context_builder import ContextRenderer
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.domain import StructuredEntities, TopicGraph, TopicMemory
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.serializers import topic_memory_payload


def test_core_exposes_data_only_context_contract(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path)

    bundle = core.build_context("Who owns migration risks?", limit=3)

    assert bundle.question == "Who owns migration risks?"
    assert bundle.evidence[0].meeting.external_id == "meeting-2"
    assert "Source: meeting-2 / transcript-2" in ContextRenderer().render(bundle)


def test_core_exposes_typed_topic_graph_and_structured_contracts(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path)

    topic = core.topic("migration")
    assert isinstance(topic, TopicMemory)
    assert topic.topic.title == "migration"
    assert topic.structured_signals[0].meeting_external_id == "meeting-2"

    graph = core.graph("migration")
    assert isinstance(graph, TopicGraph)
    assert {node.type for node in graph.nodes} >= {"Topic", "Task"}

    tasks = core.structured_entities("action_items", status="open")
    assert isinstance(tasks, StructuredEntities)
    assert tasks.entity_kind == "action_items"
    assert tasks.entities[0].status == "open"


def test_cli_topic_json_uses_boundary_serializer(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()
    scan = runner.invoke(app, ["--index", str(index_path), "scan", "--source", str(meetily_db)])
    assert scan.exit_code == 0

    cli_topic = runner.invoke(app, ["--index", str(index_path), "t", "migration", "--json"])
    assert cli_topic.exit_code == 0

    core_topic = topic_memory_payload(MeetilyMemoryCore(index_path).topic("migration"))
    assert json.loads(cli_topic.stdout) == core_topic
