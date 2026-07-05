import re

FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
CYRILLIC_RE = re.compile(r"[\u0430-\u044f\u0451]", re.IGNORECASE)
MAX_FTS_QUERY_TOKENS = 16
MIN_STRICT_FTS_QUERY_TOKENS = 2
MAX_STRICT_FTS_QUERY_TOKENS = 4
MIN_CYRILLIC_IA_TOKEN_LENGTH = 4
MIN_CYRILLIC_A_TOKEN_LENGTH = 3
CYRILLIC_A = "\u0430"
CYRILLIC_IA = "\u0438\u044f"
CYRILLIC_II = "\u0438\u0438"
CYRILLIC_IU = "\u0438\u044e"
CYRILLIC_IEI = "\u0438\u0435\u0439"
CYRILLIC_Y = "\u044b"
CYRILLIC_E = "\u0435"
CYRILLIC_U = "\u0443"
CYRILLIC_OI = "\u043e\u0439"
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
    return " OR ".join(f'"{token}"' for token in expanded_fts_query_tokens(text))


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


def expanded_fts_query_tokens(text: str) -> list[str]:
    expanded: list[str] = []
    for token in fts_query_tokens(text):
        expanded.extend(cyrillic_case_variants(token))
    return list(dict.fromkeys(expanded))[:MAX_FTS_QUERY_TOKENS]


def cyrillic_case_variants(token: str) -> list[str]:
    if CYRILLIC_RE.search(token) is None:
        return [token]
    if token.endswith(CYRILLIC_IA) and len(token) > MIN_CYRILLIC_IA_TOKEN_LENGTH:
        stem = token[:-2]
        return [token, f"{stem}{CYRILLIC_II}", f"{stem}{CYRILLIC_IU}", f"{stem}{CYRILLIC_IEI}"]
    if token.endswith(CYRILLIC_A) and len(token) > MIN_CYRILLIC_A_TOKEN_LENGTH:
        stem = token[:-1]
        return [
            token,
            f"{stem}{CYRILLIC_Y}",
            f"{stem}{CYRILLIC_E}",
            f"{stem}{CYRILLIC_U}",
            f"{stem}{CYRILLIC_OI}",
        ]
    return [token]
