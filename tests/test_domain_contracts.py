from pathlib import Path

from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.domain import MeetingRef, SearchHit, SearchResults
from meetily_memory.repositories.index import IndexRepository
from tests.index_helpers import publish_fresh_index


def test_meeting_ref_has_one_canonical_round_trip() -> None:
    meeting_ref = MeetingRef("source-uuid", "meeting/path")

    assert str(meeting_ref) == "source-uuid/meeting/path"
    assert MeetingRef.parse(str(meeting_ref)) == meeting_ref


def test_search_has_one_meeting_level_contract(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)

    search = MeetilyMemoryCore(index_path).search("migration risks", limit=3)

    assert isinstance(search, SearchResults)
    assert search.query == "migration risks"
    assert search.context == 0
    assert search.results[0].meeting.external_id == "meeting-2"
    assert search.results[0].match_sources
    assert search.results[0].evidence


def test_search_hit_identity_and_source_survive_index_rebuild(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    state_path = tmp_path / "state.sqlite"
    publish_fresh_index(index_path, meetily_db, state_path=state_path)
    first = IndexRepository(index_path, state_path=state_path).search_hits("pricing decision")[0]

    index_path.unlink()
    publish_fresh_index(index_path, meetily_db, state_path=state_path)
    repository = IndexRepository(index_path, state_path=state_path)
    second = repository.search_hits("pricing decision")[0]
    resolved = repository.get_search_hit(first.id)

    assert isinstance(first, SearchHit)
    assert first.id == second.id
    assert first.meeting.external_id == second.meeting.external_id
    assert first.excerpt.text == "Alice confirmed the launch checklist and pricing decision."
    assert resolved == second
