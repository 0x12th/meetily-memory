from collections.abc import Iterable
from typing import Any

from meetily_memory.db.fts import cyrillic_case_variants, fts_query_tokens


def normalize_key(value: str) -> str:
    return " ".join(value.casefold().split())


def topic_stable_key(title: str) -> str:
    return f"topic:{normalize_key(title)}"


def entity_stable_key(row: dict[str, Any]) -> str:
    return f"{row['kind']}:{row['id']}"


def row_matches_terms(row: dict[str, Any], terms: Iterable[str]) -> bool:
    haystack = normalize_key(
        " ".join(
            str(row.get(key) or "")
            for key in (
                "text",
                "meeting_title",
                "meeting_external_id",
                "chunk_external_id",
                "chunk_speaker",
            )
        )
    )
    return any(term_matches_haystack(term, haystack) for term in terms if normalize_key(term))


def term_matches_haystack(term: str, haystack: str) -> bool:
    normalized = normalize_key(term)
    if normalized in haystack:
        return True
    variants: list[str] = []
    for token in fts_query_tokens(normalized):
        variants.extend(cyrillic_case_variants(token))
    return any(variant in haystack for variant in variants)


def without_added_aliases(topic: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in topic.items() if key != "added_aliases"}
