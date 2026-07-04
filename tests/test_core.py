import json
from pathlib import Path

from typer.testing import CliRunner

from meetily_memory.cli.app import app
from meetily_memory.core import CONTRACT_VERSION, MeetilyMemoryCore
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner


def test_core_exposes_versioned_context_contract(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path)

    response = core.build_context("Who owns migration risks?", limit=3)
    payload = response.as_payload()

    assert payload["contract_version"] == CONTRACT_VERSION
    assert payload["kind"] == "context"
    data = payload["data"]
    assert data["question"] == "Who owns migration risks?"
    assert "Source: meeting-2 / transcript-2" in data["markdown"]
    assert data["evidence"][0]["meeting_external_id"] == "meeting-2"


def test_core_exposes_topic_graph_and_structured_contracts(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path)

    topic = core.topic("migration").as_payload()
    assert topic["contract_version"] == CONTRACT_VERSION
    assert topic["kind"] == "topic"
    assert topic["data"]["topic"]["title"] == "migration"
    assert topic["data"]["structured_signals"][0]["meeting_external_id"] == "meeting-2"

    graph = core.graph("migration").as_payload()
    assert graph["contract_version"] == CONTRACT_VERSION
    assert graph["kind"] == "graph"
    assert {node["type"] for node in graph["data"]["nodes"]} >= {"Topic", "Task"}

    tasks = core.structured_entities("action_items", status="open").as_payload()
    assert tasks["contract_version"] == CONTRACT_VERSION
    assert tasks["kind"] == "structured_entities"
    assert tasks["data"]["entity_kind"] == "action_items"
    assert tasks["data"]["entities"][0]["status"] == "open"


def test_cli_topic_json_uses_core_data_contract(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()
    scan = runner.invoke(app, ["--index", str(index_path), "scan", "--source", str(meetily_db)])
    assert scan.exit_code == 0

    cli_topic = runner.invoke(app, ["--index", str(index_path), "topic", "migration", "--json"])
    assert cli_topic.exit_code == 0

    core_topic = MeetilyMemoryCore(index_path).topic("migration").data
    assert json.loads(cli_topic.stdout) == core_topic
