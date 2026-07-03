from dataclasses import dataclass
from datetime import UTC, datetime

from meetily_memory.db.repository import IndexRepository
from meetily_memory.meeting_structure import extract_structured_entities


@dataclass
class StructureAnalysisResult:
    meetings_analyzed: int = 0
    decisions: int = 0
    action_items: int = 0
    risks: int = 0
    open_questions: int = 0

    def add_counts(self, counts: dict[str, int]) -> None:
        self.decisions += counts["decisions"]
        self.action_items += counts["action_items"]
        self.risks += counts["risks"]
        self.open_questions += counts["open_questions"]

    def add_result(self, other: "StructureAnalysisResult") -> None:
        self.meetings_analyzed += other.meetings_analyzed
        self.decisions += other.decisions
        self.action_items += other.action_items
        self.risks += other.risks
        self.open_questions += other.open_questions

    def as_payload(self) -> dict[str, int]:
        return {
            "meetings_analyzed": self.meetings_analyzed,
            "decisions": self.decisions,
            "action_items": self.action_items,
            "risks": self.risks,
            "open_questions": self.open_questions,
        }


class StructureAnalyzer:
    def __init__(self, repo: IndexRepository) -> None:
        self.repo = repo

    def analyze_meeting(self, meeting_id: int) -> StructureAnalysisResult:
        chunks = self.repo.get_chunks_for_meeting(meeting_id)
        entities = extract_structured_entities(chunks)
        counts = self.repo.replace_structured_entities(meeting_id, entities, utc_now())
        result = StructureAnalysisResult(meetings_analyzed=1)
        result.add_counts(counts)
        return result

    def analyze_all(self) -> StructureAnalysisResult:
        result = StructureAnalysisResult()
        for meeting_id in self.repo.list_meeting_ids():
            meeting_result = self.analyze_meeting(meeting_id)
            result.add_result(meeting_result)
        return result


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
