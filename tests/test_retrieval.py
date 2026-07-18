from dataclasses import dataclass
from pathlib import Path

from meetily_memory.context_builder import ContextRenderer
from meetily_memory.core import CORE_V2_VERSION, MeetilyMemoryCore
from meetily_memory.domain import ContextBundle, SearchHit
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner


@dataclass(frozen=True)
class FixedRetrievalStrategy:
    hits: tuple[SearchHit, ...]

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        meeting_id: int | None = None,
        context: int = 0,
    ) -> tuple[SearchHit, ...]:
        del query, meeting_id, context
        return self.hits[:limit]


def test_selected_strategy_drives_v2_search_and_context_bundle(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    lexical_hit = MeetilyMemoryCore(index_path).search_hits("pricing decision")[0]
    core = MeetilyMemoryCore(
        index_path,
        retrieval_strategy=FixedRetrievalStrategy((lexical_hit,)),
    )

    search = core.search("query ignored by strategy", contract_version=CORE_V2_VERSION)
    bundle = core.context_bundle("question ignored by strategy")
    context = core.build_context(
        "question ignored by strategy",
        contract_version=CORE_V2_VERSION,
    )

    assert search.data["results"] == [lexical_hit.as_payload()]
    assert bundle.evidence == (lexical_hit,)
    assert context.data == bundle.as_payload()


def test_context_renderer_uses_context_bundle_without_storage_rows(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    hit = MeetilyMemoryCore(index_path).search_hits("migration risks")[0]
    bundle = ContextBundle(
        question="Who owns migration risks?",
        evidence=(hit,),
        entities=(),
    )

    markdown = ContextRenderer().render(bundle)

    assert markdown.startswith("# Question\n\nWho owns migration risks?")
    assert "## Meeting: Dobrynya Follow-up" in markdown
    assert "Source: meeting-2 / transcript-2" in markdown
    assert "Dobrynya agreed to send migration risks by Friday." in markdown
    assert markdown.endswith("# Question\n\nWho owns migration risks?\n")
