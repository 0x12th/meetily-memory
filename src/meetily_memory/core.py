from pathlib import Path

from meetily_memory.domain import (
    Meeting,
    MeetingRef,
    MeetingSearchFilters,
    SearchResults,
)
from meetily_memory.repositories.index import IndexRepository, meeting_from_row
from meetily_memory.retrieval import MeetingSearchService
from meetily_memory.tagging import TagRepository


class MeetilyMemoryCore:
    def __init__(
        self,
        index_path: Path,
        *,
        state_path: Path | None = None,
    ) -> None:
        self._index_path = Path(index_path)
        self._state_path = (
            Path(state_path)
            if state_path is not None
            else self._index_path.with_name("state.sqlite")
        )
        repository = IndexRepository.open_existing(
            self._index_path,
            state_path=self._state_path,
        )
        self._repository = repository
        tag_repository = TagRepository.open_existing(repository.state_path)
        self._meeting_search = MeetingSearchService(repository, tag_repository)

    def search(
        self,
        query: str,
        limit: int = 10,
        context: int = 0,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> SearchResults:
        results = self._meeting_search.search(query, limit, context, filters=filters)
        return SearchResults(query=query, context=context, results=results)

    def meetings(self, limit: int = 20) -> tuple[Meeting, ...]:
        return tuple(meeting_from_row(row) for row in self._repository.list_meetings(limit=limit))

    def get_meeting_by_ref(self, meeting_ref: MeetingRef) -> Meeting | None:
        row = self._repository.get_meeting_by_ref(meeting_ref)
        return meeting_from_row(row) if row is not None else None
