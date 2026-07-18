from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from meetily_memory.domain import ContextBundle, SearchHit

DEFAULT_CONTEXT_LIMIT = 8


@dataclass
class ContextMeeting:
    external_id: str
    title: str
    date: str
    best_rank: float
    excerpts: list[Mapping[str, object]]


@dataclass(frozen=True)
class ContextRenderer:
    def render(self, bundle: ContextBundle) -> str:
        return render_context_markdown(
            bundle.question,
            group_hits_by_meeting(bundle.evidence),
        )


def build_context_markdown(
    question: str,
    search_results: Sequence[Mapping[str, object]],
) -> str:
    return render_context_markdown(question, group_results_by_meeting(search_results))


def render_context_markdown(question: str, meetings: Sequence[ContextMeeting]) -> str:
    lines = [
        "# Question",
        "",
        question.strip(),
        "",
        "# Relevant meetings",
    ]

    if not meetings:
        lines.extend(["", "No relevant excerpts found."])
        return finish_with_question(lines, question)

    for meeting in meetings:
        lines.extend(
            [
                "",
                f"## Meeting: {meeting.title}",
                "",
                f"Date: {meeting.date}",
                f"Meeting ID: {meeting.external_id}",
                "",
                "### Relevant excerpt",
            ]
        )
        for excerpt in meeting.excerpts:
            lines.extend(["", format_excerpt(excerpt)])

    return finish_with_question(lines, question)


def group_hits_by_meeting(hits: Sequence[SearchHit]) -> list[ContextMeeting]:
    meetings_by_id: dict[str, ContextMeeting] = {}
    for rank, hit in enumerate(hits):
        meeting = meetings_by_id.get(hit.meeting.external_id)
        if meeting is None:
            meeting = ContextMeeting(
                external_id=hit.meeting.external_id,
                title=hit.meeting.title,
                date=hit.meeting.updated_at or hit.meeting.created_at or "unknown",
                best_rank=float(rank),
                excerpts=[],
            )
            meetings_by_id[hit.meeting.external_id] = meeting
        meeting.excerpts.append(search_hit_as_context_row(hit))
    return list(meetings_by_id.values())


def search_hit_as_context_row(hit: SearchHit) -> dict[str, object]:
    return {
        "meeting_external_id": hit.meeting.external_id,
        "chunk_external_id": hit.excerpt.chunk_external_id,
        "text": hit.excerpt.text,
        "speaker": hit.excerpt.speaker,
        "timestamp_label": hit.excerpt.timestamp_label,
    }


def group_results_by_meeting(
    search_results: Sequence[Mapping[str, object]],
) -> list[ContextMeeting]:
    meetings_by_id: dict[int, ContextMeeting] = {}
    for row in search_results:
        meeting_id_value = row["meeting_id"]
        if not isinstance(meeting_id_value, int):
            message = "Context search result has non-integer meeting_id."
            raise TypeError(message)
        meeting_id = meeting_id_value
        meeting = meetings_by_id.get(meeting_id)
        if meeting is None:
            meeting = ContextMeeting(
                external_id=str(row["meeting_external_id"]),
                title=str(row["title"]),
                date=str(row.get("updated_at") or row.get("created_at") or "unknown"),
                best_rank=rank_sort_value(row.get("rank")),
                excerpts=[],
            )
            meetings_by_id[meeting_id] = meeting
        meeting.excerpts.append(row)

    return sorted(
        meetings_by_id.values(),
        key=lambda meeting: (
            meeting.best_rank,
            meeting.date,
            meeting.title,
        ),
    )


def format_excerpt(row: Mapping[str, object]) -> str:
    prefix_parts = []
    if row.get("speaker"):
        prefix_parts.append(str(row["speaker"]))
    if row.get("timestamp_label"):
        prefix_parts.append(str(row["timestamp_label"]))
    prefix = f"**{' | '.join(prefix_parts)}**: " if prefix_parts else ""
    source = format_source(row)
    return f"{source}\n> {prefix}{normalize_excerpt(str(row['text']))}"


def format_source(row: Mapping[str, object]) -> str:
    meeting_id = row.get("meeting_external_id") or row.get("meeting_id") or "unknown-meeting"
    chunk_id = row.get("chunk_external_id") or row.get("chunk_id") or "unknown-chunk"
    return f"Source: {meeting_id} / {chunk_id}"


def normalize_excerpt(text: str) -> str:
    return "\n> ".join(line.strip() for line in text.splitlines() if line.strip())


def finish_with_question(lines: list[str], question: str) -> str:
    lines.extend(["", "# Question", "", question.strip(), ""])
    return "\n".join(lines)


def rank_sort_value(rank: object) -> float:
    return float(rank) if isinstance(rank, (int, float)) else 0.0
