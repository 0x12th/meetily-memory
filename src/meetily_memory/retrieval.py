from dataclasses import dataclass
from typing import Protocol

from meetily_memory.domain import SearchHit
from meetily_memory.repositories.index import IndexRepository


class RetrievalStrategy(Protocol):
    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        meeting_id: int | None = None,
        context: int = 0,
    ) -> tuple[SearchHit, ...]: ...


@dataclass(frozen=True)
class LexicalRetrievalStrategy:
    repository: IndexRepository

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        meeting_id: int | None = None,
        context: int = 0,
    ) -> tuple[SearchHit, ...]:
        return self.repository.search_hits(
            query,
            limit,
            meeting_id=meeting_id,
            context=context,
        )
