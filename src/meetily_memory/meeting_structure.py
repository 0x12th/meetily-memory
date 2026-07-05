import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

ENTITY_KINDS = ("decisions", "action_items", "risks", "open_questions")

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
ENTITY_PATTERNS = {
    "decisions": re.compile(
        r"\b(decision|decided|agreed|approved|confirmed|resolved)\b|"
        r"(решили|решение|согласовали|утвердили|подтвердил|подтвердили|подтвержден)",
        re.IGNORECASE,
    ),
    "risks": re.compile(
        r"\b(risk|risks|blocker|blocked|issue|concern|dependency)\b|"
        r"(риск|риски|блокер|проблема|зависимость)",
        re.IGNORECASE,
    ),
}
ACTION_ITEM_PATTERNS = (
    re.compile(
        r"\b(action item|todo|to do|follow up)\b|"
        r"\b(agreed to|owns|owner)\s+\w+|"
        r"\b(send|prepare|share|write|create|update|review|check|clarify)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(задача|сделать|отправить|подготовить|написать|создать|обновить|"
        r"проверить|уточнить|согласовать|прислать|скинуть|собрать)",
        re.IGNORECASE,
    ),
    re.compile(r"(нужно|надо)\s+\w*(подготов|сдел|отправ|напис|созд|обнов|провер|уточн)"),
)
ACTION_ITEM_EXCLUDE_RE = re.compile(
    r"(надо|нужно)\s+(подумать|понять|разобраться)|"
    r"должен\s+будет|"
    r"\b(should|need to|needs to)\s+(think|understand|figure out)\b",
    re.IGNORECASE,
)
OPEN_QUESTION_PATTERNS = (
    re.compile(r"\b(open question|unknown|clarify|tbd)\b", re.IGNORECASE),
    re.compile(r"(открытый вопрос|уточнить|неясно|непонятно|под вопросом)", re.IGNORECASE),
)


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
            for kind in ENTITY_KINDS:
                if not is_entity_sentence(kind, sentence):
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


def is_entity_sentence(kind: str, sentence: str) -> bool:
    if kind == "action_items":
        return is_action_item(sentence)
    if kind == "open_questions":
        return is_open_question(sentence)
    pattern = ENTITY_PATTERNS.get(kind)
    return bool(pattern and pattern.search(sentence))


def is_action_item(sentence: str) -> bool:
    if is_open_question(sentence):
        return False
    if ACTION_ITEM_EXCLUDE_RE.search(sentence):
        return False
    return any(pattern.search(sentence) for pattern in ACTION_ITEM_PATTERNS)


def is_open_question(sentence: str) -> bool:
    return any(pattern.search(sentence) for pattern in OPEN_QUESTION_PATTERNS)


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
