import re

FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
MAX_FTS_QUERY_TOKENS = 16
MIN_STRICT_FTS_QUERY_TOKENS = 2
MAX_STRICT_FTS_QUERY_TOKENS = 4
NO_MATCH_FTS_QUERY = '"meetilymemorynomatchtoken"'
FTS_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "and",
        "are",
        "by",
        "did",
        "do",
        "for",
        "how",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "was",
        "we",
        "what",
        "when",
        "where",
        "who",
        "why",
        "как",
        "кто",
        "мы",
        "на",
        "по",
        "про",
        "что",
    }
)


def build_fts_query(text: str) -> str:
    return " OR ".join(f'"{token}"' for token in fts_query_tokens(text))


def build_strict_fts_query(text: str) -> str:
    tokens = fts_query_tokens(text)
    if not MIN_STRICT_FTS_QUERY_TOKENS <= len(tokens) <= MAX_STRICT_FTS_QUERY_TOKENS:
        return ""
    return " AND ".join(f'"{token}"' for token in tokens)


def fts_query_tokens(text: str) -> list[str]:
    tokens = [token.casefold() for token in FTS_TOKEN_RE.findall(text)]
    return list(
        dict.fromkeys(token for token in tokens if len(token) > 1 and token not in FTS_STOPWORDS)
    )[:MAX_FTS_QUERY_TOKENS]
