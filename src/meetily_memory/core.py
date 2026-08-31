from pathlib import Path
from typing import Any

from meetily_memory.domain import (
    Meeting,
    MeetingRef,
    MeetingSearchFilters,
    Person,
    SearchResults,
    StructuredEntities,
    TaskStatusResult,
    Topic,
    TopicAliasResult,
    TopicMemory,
)
from meetily_memory.local_memory import (
    ranked_excerpt_from_row,
    structured_entity_kind,
    structured_signal_from_row,
)
from meetily_memory.repositories.index import IndexRepository, meeting_from_row, optional_str
from meetily_memory.retrieval import (
    LexicalRetrievalStrategy,
    LexicalTagMeetingRetrievalStrategy,
    MeetingRetrievalStrategy,
    RetrievalStrategy,
    TagRetrievalStrategy,
    search_meetings_with_builtin_snapshot,
)
from meetily_memory.tagging import TagRepository


class TaskNotFoundError(LookupError):
    pass


class MeetilyMemoryCore:
    def __init__(
        self,
        index_path: Path,
        *,
        state_path: Path | None = None,
        retrieval_strategy: RetrievalStrategy | None = None,
        meeting_retrieval_strategy: MeetingRetrievalStrategy | None = None,
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
        lexical = retrieval_strategy or LexicalRetrievalStrategy(repository)
        tag_repository = TagRepository.open_existing(repository.state_path)
        tag_retrieval = TagRetrievalStrategy(tag_repository)
        self._meeting_retrieval = meeting_retrieval_strategy or LexicalTagMeetingRetrievalStrategy(
            repository=repository,
            lexical=lexical,
            tags=tag_retrieval,
        )

    def search(
        self,
        query: str,
        limit: int = 10,
        context: int = 0,
        *,
        filters: MeetingSearchFilters | None = None,
    ) -> SearchResults:
        results = search_meetings_with_builtin_snapshot(
            self._repository,
            self._meeting_retrieval,
            query,
            limit,
            context,
            filters=filters,
        )
        return SearchResults(query=query, context=context, results=results)

    def meetings(self, limit: int = 20, person: str | None = None) -> tuple[Meeting, ...]:
        return tuple(
            meeting_from_row(row)
            for row in self._repository.list_meetings(limit=limit, person=person)
        )

    def get_meeting(self, external_id: str) -> Meeting | None:
        row = self._repository.get_meeting(external_id)
        return meeting_from_row(row) if row is not None else None

    def get_meeting_by_ref(self, meeting_ref: MeetingRef) -> Meeting | None:
        row = self._repository.get_meeting_by_ref(meeting_ref)
        return meeting_from_row(row) if row is not None else None

    def topic(self, query: str, limit: int = 10) -> TopicMemory:
        payload = self._repository.topic_memory(query, limit)
        return TopicMemory(
            topic=topic_from_row(payload["topic"]),
            language=optional_str(payload.get("language")),
            query_terms=tuple(str(term) for term in payload["query_terms"]),
            meetings=tuple(ranked_excerpt_from_row(row) for row in payload["meetings"]),
            evidence=tuple(ranked_excerpt_from_row(row) for row in payload["evidence"]),
            structured_signals=tuple(
                structured_signal_from_row(row) for row in payload["structured_signals"]
            ),
            related_people=tuple(person_from_row(row) for row in payload["related_people"]),
        )

    def topics(self, limit: int = 100) -> tuple[Topic, ...]:
        return tuple(topic_from_row(row) for row in self._repository.list_topics(limit))

    def add_topic_alias(self, query: str, aliases: list[str]) -> TopicAliasResult:
        payload = self._repository.add_topic_aliases(query, aliases)
        return TopicAliasResult(
            topic=topic_from_row(payload),
            added_aliases=tuple(str(alias) for alias in payload["added_aliases"]),
        )

    def structured_entities(
        self,
        kind: str,
        limit: int = 20,
        *,
        status: str = "all",
    ) -> StructuredEntities:
        entity_kind = structured_entity_kind(kind)
        rows = self._repository.list_structured_entity_details(kind, limit, status=status)
        return StructuredEntities(
            entity_kind=entity_kind,
            status=status,
            entities=tuple(structured_signal_from_row(row) for row in rows),
        )

    def set_task_status(
        self,
        task_id: int,
        status: str,
        *,
        note: str | None = None,
    ) -> TaskStatusResult:
        try:
            row = self._writer_repository().set_task_status(task_id, status, note=note)
        except ValueError as exc:
            if str(exc).startswith("Task not found:"):
                raise TaskNotFoundError(str(exc)) from exc
            raise
        return TaskStatusResult(
            id=int(row["id"]),
            text=str(row["text"]),
            status=str(row["status"]),
            status_note=optional_str(row.get("status_note")),
            status_source=str(row["status_source"]),
            status_updated_at=str(row["status_updated_at"]),
        )

    def _writer_repository(self) -> IndexRepository:
        return IndexRepository(self._index_path, state_path=self._state_path)


def topic_from_row(row: dict[str, Any]) -> Topic:
    return Topic(
        id=int(row["id"]),
        stable_key=str(row["stable_key"]),
        title=str(row["title"]),
        aliases=tuple(str(alias) for alias in row["aliases"]),
    )


def person_from_row(row: dict[str, Any]) -> Person:
    return Person(id=int(row["id"]), display_name=str(row["display_name"]))
