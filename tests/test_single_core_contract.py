from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

import pytest

from meetily_memory.core import (
    EvidenceNotFoundError,
    MeetilyMemoryCore,
    MeetingNotFoundError,
)
from meetily_memory.domain import ContextBundle, Meeting, MeetingRef, SearchResults
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner


def test_search_and_context_are_single_typed_contract(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path)

    search = core.search("migration risks", limit=3)
    context = core.build_context("migration risks", limit=3, context=1)

    assert isinstance(search, SearchResults)
    assert search.query == "migration risks"
    assert search.context == 0
    assert search.results[0].meeting.external_id == "meeting-2"
    assert isinstance(context, ContextBundle)
    assert context.question == "migration risks"
    assert any(hit.is_context for hit in context.evidence)
    assert not hasattr(core, "repo")


def test_public_core_results_are_transitively_typed(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path)

    results = (
        core.search("migration"),
        core.build_context("migration"),
        core.meetings(),
        core.latest_meeting(),
        core.get_meeting("meeting-1"),
        core.meeting_chunks(1),
        core.summary(),
        core.timeline(),
        core.project("migration"),
        core.person("Dobrynya"),
        core.topic("migration"),
        core.add_topic_alias("migration", ["move"]),
        core.graph("migration"),
        core.structured_entities("action_items"),
    )

    for result in results:
        assert_transitively_typed(result)

    for name, member in MeetilyMemoryCore.__dict__.items():
        if name.startswith("_") or not callable(member):
            continue
        return_type = get_type_hints(member).get("return")
        assert return_type is not None, name
        assert_annotation_has_no_generic_mapping(return_type)


def test_optional_and_required_lookups_have_specific_semantics(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    core = MeetilyMemoryCore(index_path)

    meeting = core.get_meeting("meeting-1")
    assert isinstance(meeting, Meeting)
    assert core.get_meeting("missing") is None
    with pytest.raises(MeetingNotFoundError, match="Meeting not found"):
        core.build_meeting_context("migration", MeetingRef("missing-source", "missing"))
    with pytest.raises(EvidenceNotFoundError, match="Evidence not found"):
        core.resolve_search_hit("evidence:missing")


def assert_transitively_typed(value: object) -> None:
    assert not isinstance(value, (dict, list))
    if isinstance(value, tuple):
        for item in value:
            assert_transitively_typed(item)
        return
    if is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            assert_transitively_typed(getattr(value, field.name))


def assert_annotation_has_no_generic_mapping(annotation: object) -> None:
    assert annotation is not Any
    origin = get_origin(annotation)
    assert origin not in {dict, list}
    for argument in get_args(annotation):
        assert_annotation_has_no_generic_mapping(argument)
