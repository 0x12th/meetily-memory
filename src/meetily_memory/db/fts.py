import re

FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
MAX_FTS_QUERY_TOKENS = 16
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
    tokens = [token.casefold() for token in FTS_TOKEN_RE.findall(text)]
    unique_tokens = list(
        dict.fromkeys(token for token in tokens if len(token) > 1 and token not in FTS_STOPWORDS)
    )
    return " OR ".join(f'"{token}"' for token in unique_tokens[:MAX_FTS_QUERY_TOKENS])
