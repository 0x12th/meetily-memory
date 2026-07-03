import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

ENTITY_KINDS = ("decisions", "action_items", "risks", "open_questions")

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
ENTITY_PATTERNS = {
    "decisions": re.compile(
        r"\b(decision|decided|agreed|approved|confirmed|resolved)\b|"
        r"(решили|решение|согласовали|утвердили)",
        re.IGNORECASE,
    ),
    "action_items": re.compile(
        r"\b(action item|todo|to do|will|should|must|need to|needs to|"
        r"agreed to|owns|owner|send|prepare|follow up)\b|"
        r"(задача|нужно|надо|должен|сделать|отправить|подготовить)",
        re.IGNORECASE,
    ),
    "risks": re.compile(
        r"\b(risk|risks|blocker|blocked|issue|concern|dependency)\b|"
        r"(риск|риски|блокер|проблема|зависимость)",
        re.IGNORECASE,
    ),
    "open_questions": re.compile(
        r"\?|"
        r"\b(open question|question|unknown|clarify|tbd)\b|"
        r"(вопрос|уточнить|неясно)",
        re.IGNORECASE,
    ),
}


@dataclass(frozen=True)
class StructuredEntity:
    kind: str
    source_chunk_id: int | None
    ordinal: int
    text: str
    source: str
    confidence: float
    fingerprint: str
    raw_metadata_json: str | None = None


def extract_structured_entities(chunks: Sequence[Mapping[str, object]]) -> list[StructuredEntity]:
    counters = dict.fromkeys(ENTITY_KINDS, 0)
    seen: set[tuple[str, str]] = set()
    entities: list[StructuredEntity] = []

    for chunk in chunks:
        source_chunk_id = chunk_id(chunk)
        for sentence in split_sentences(str(chunk.get("text") or "")):
            normalized = sentence.casefold()
            for kind, pattern in ENTITY_PATTERNS.items():
                if not pattern.search(sentence):
                    continue
                dedupe_key = (kind, normalized)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                ordinal = counters[kind]
                counters[kind] += 1
                entities.append(
                    StructuredEntity(
                        kind=kind,
                        source_chunk_id=source_chunk_id,
                        ordinal=ordinal,
                        text=sentence,
                        source="heuristic",
                        confidence=0.55,
                        fingerprint=entity_fingerprint(kind, source_chunk_id, sentence),
                    )
                )

    return entities


def empty_entity_counts() -> dict[str, int]:
    return dict.fromkeys(ENTITY_KINDS, 0)


def count_entities(entities: Sequence[StructuredEntity]) -> dict[str, int]:
    counts = empty_entity_counts()
    for entity in entities:
        counts[entity.kind] += 1
    return counts


def split_sentences(text: str) -> list[str]:
    return [sentence.strip() for sentence in SENTENCE_SPLIT_RE.split(text) if sentence.strip()]


def chunk_id(chunk: Mapping[str, object]) -> int | None:
    value = chunk.get("id")
    return value if isinstance(value, int) else None


def entity_fingerprint(kind: str, source_chunk_id: int | None, text: str) -> str:
    payload = f"{kind}\0{source_chunk_id or ''}\0{text.casefold()}".encode()
    return hashlib.sha256(payload).hexdigest()
