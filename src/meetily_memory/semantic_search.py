import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import sqlite_vec
from sqlite_vec import serialize_float32

from meetily_memory.config.paths import app_config_path, semantic_config_path
from meetily_memory.config.settings import SemanticSettings, load_app_settings, update_app_settings
from meetily_memory.db.schema import index_connection
from meetily_memory.json_codec import loads_json

EMBEDDING_DIMENSIONS = 128
Row = dict[str, object]
EMBEDDING_MODEL = "local-hash-v1"
DEFAULT_EMBEDDING_PROVIDER = "ollama"
DEFAULT_OLLAMA_MODEL = "nomic-embed-text"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_TIMEOUT_SECONDS = 30.0
OLLAMA_BATCH_SIZE = 16
DEFAULT_SEMANTIC_INDEX_BATCH_SIZE = 128
VECTOR_TABLE_HASH_LENGTH = 12
TOKEN_RE = re.compile(r"\w+", re.UNICODE)
SQL_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FEATURE_HASH_SIZE = 8
HASH_BUCKET_BYTES = 4
LOAD_EXTENSION_DISABLED = False
LOAD_EXTENSION_ENABLED = True
MIN_STEM_TOKEN_LENGTH = 3
ZERO_VECTOR_FALLBACK = 1.0
TOKEN_WEIGHT = 1.0
BIGRAM_WEIGHT = 1.25


class EmbeddingProvider(Protocol):
    name: str
    model: str
    dims: int | None

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class SemanticSearchConfig:
    provider: str | None = None
    ollama_model: str | None = None
    ollama_url: str | None = None

    def as_payload(self) -> dict[str, str]:
        payload: dict[str, str] = {}
        if self.provider:
            payload["provider"] = self.provider
        if self.ollama_model:
            payload["model"] = self.ollama_model
        if self.ollama_url:
            payload["ollama_url"] = self.ollama_url
        return payload


@dataclass(frozen=True)
class LocalHashEmbeddingProvider:
    name: str = "hash"
    model: str = EMBEDDING_MODEL
    dims: int | None = EMBEDDING_DIMENSIONS

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [embed_text(text) for text in texts]


@dataclass(frozen=True)
class OllamaEmbeddingProvider:
    name: str = "ollama"
    model: str = DEFAULT_OLLAMA_MODEL
    base_url: str = DEFAULT_OLLAMA_URL
    timeout_seconds: float = DEFAULT_OLLAMA_TIMEOUT_SECONDS
    dims: int | None = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        embeddings: list[list[float]] = []
        for offset in range(0, len(texts), OLLAMA_BATCH_SIZE):
            embeddings.extend(self._embed_batch(texts[offset : offset + OLLAMA_BATCH_SIZE]))
        return embeddings

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.model, "input": texts}).encode()
        endpoint = ollama_embed_endpoint(self.base_url)
        request = Request(  # noqa: S310
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                data = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            message = (
                "Ollama embedding provider is unavailable. Start Ollama and run "
                f"`ollama pull {self.model}`, or pass `--provider hash` "
                "for the diagnostic baseline."
            )
            raise RuntimeError(message) from exc

        raw_embeddings = data.get("embeddings") or data.get("embedding")
        if (
            isinstance(raw_embeddings, list)
            and raw_embeddings
            and isinstance(raw_embeddings[0], int | float)
        ):
            raw_embeddings = [raw_embeddings]
        if not isinstance(raw_embeddings, list) or len(raw_embeddings) != len(texts):
            message = "Ollama returned an unexpected embedding response shape."
            raise RuntimeError(message)
        return [normalize_embedding(parse_embedding(values)) for values in raw_embeddings]


def semantic_search(
    index_path: Path,
    query: str,
    limit: int = 10,
    *,
    embedding_provider: EmbeddingProvider | None = None,
) -> list[Row]:
    provider = embedding_provider or resolve_embedding_provider()
    query_embedding = provider.embed([query])[0]
    dimensions = len(query_embedding)
    vector_table = vector_table_name(provider, dimensions)
    with index_connection(index_path) as conn:
        load_sqlite_vec(conn)
        ensure_semantic_schema(conn, vector_table, dimensions)
        if count_indexed_embeddings(conn, provider, dimensions) == 0:
            message = "Semantic index is empty. Run: mm semantic index"
            raise RuntimeError(message)
        semantic_sql = f"""
            WITH matches AS (
              SELECT rowid AS chunk_id, distance
              FROM {vector_table}
              WHERE embedding MATCH ?
              ORDER BY distance
              LIMIT ?
            )
            SELECT
              m.id AS meeting_id,
              m.external_id AS meeting_external_id,
              m.title AS title,
              m.created_at AS created_at,
              m.updated_at AS updated_at,
              m.folder_path AS folder_path,
              c.id AS chunk_id,
              c.external_id AS chunk_external_id,
              c.kind AS kind,
              c.text AS text,
              c.speaker AS speaker,
              c.starts_at_seconds AS starts_at_seconds,
              c.ends_at_seconds AS ends_at_seconds,
              c.timestamp_label AS timestamp_label,
              matches.distance AS distance,
              e.embedding_provider AS embedding_provider,
              e.embedding_model AS embedding_model,
              e.embedding_dimensions AS embedding_dimensions
            FROM matches
            JOIN chunks c ON c.id = matches.chunk_id
            JOIN meetings m ON m.id = c.meeting_id
            JOIN chunk_embeddings e
              ON e.chunk_id = c.id
             AND e.embedding_provider = ?
             AND e.embedding_model = ?
             AND e.embedding_dimensions = ?
            ORDER BY matches.distance
            """
        rows = conn.execute(
            semantic_sql,
            (
                serialize_float32(query_embedding),
                limit,
                provider.name,
                provider.model,
                dimensions,
            ),
        ).fetchall()
        return [dict(row) for row in rows]


def index_semantic_embeddings(
    index_path: Path,
    *,
    embedding_provider: EmbeddingProvider | None = None,
    batch_size: int = DEFAULT_SEMANTIC_INDEX_BATCH_SIZE,
) -> int:
    provider = embedding_provider or resolve_embedding_provider()
    dimensions = provider.dims
    if dimensions is None:
        dimensions = len(provider.embed([""])[0])
    vector_table = vector_table_name(provider, dimensions)
    with index_connection(index_path) as conn:
        load_sqlite_vec(conn)
        ensure_semantic_schema(conn, vector_table, dimensions)
        return index_missing_embeddings(
            conn,
            provider,
            vector_table,
            dimensions,
            batch_size=batch_size,
        )


def resolve_embedding_provider(
    provider_name: str | None = None,
    *,
    ollama_model: str | None = None,
    ollama_url: str | None = None,
    config: SemanticSearchConfig | None = None,
    env: Mapping[str, str] | None = None,
) -> EmbeddingProvider:
    config = config if config is not None else load_semantic_config()
    environment = os.environ if env is None else env
    name = (
        provider_name
        or environment.get("MM_EMBEDDING_PROVIDER")
        or environment.get("MEETILY_MEMORY_EMBEDDING_PROVIDER")
        or config.provider
        or DEFAULT_EMBEDDING_PROVIDER
    ).casefold()
    if name in {"local-hash", "hash", "diagnostic"}:
        return LocalHashEmbeddingProvider()
    if name == "ollama":
        return OllamaEmbeddingProvider(
            model=ollama_model
            or environment.get("MM_OLLAMA_MODEL")
            or environment.get("MEETILY_MEMORY_OLLAMA_MODEL")
            or config.ollama_model
            or DEFAULT_OLLAMA_MODEL,
            base_url=ollama_url
            or environment.get("MM_OLLAMA_URL")
            or environment.get("MEETILY_MEMORY_OLLAMA_URL")
            or config.ollama_url
            or DEFAULT_OLLAMA_URL,
        )
    message = "Unknown embedding provider. Use `ollama` or `hash`."
    raise RuntimeError(message)


def load_semantic_config() -> SemanticSearchConfig:
    settings = load_app_settings()
    if settings.semantic.provider:
        return SemanticSearchConfig(
            provider=settings.semantic.provider,
            ollama_model=settings.semantic.model,
            ollama_url=settings.semantic.ollama_url,
        )
    return load_legacy_semantic_config()


def load_legacy_semantic_config() -> SemanticSearchConfig:
    path = semantic_config_path()
    if not path.exists():
        return SemanticSearchConfig()
    payload = loads_json(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return SemanticSearchConfig()
    return SemanticSearchConfig(
        provider=optional_str(payload.get("provider")),
        ollama_model=optional_str(payload.get("model")),
        ollama_url=optional_str(payload.get("ollama_url")),
    )


def save_semantic_config(config: SemanticSearchConfig) -> Path:
    update_app_settings(
        semantic=SemanticSettings(
            provider=config.provider,
            model=config.ollama_model,
            ollama_url=config.ollama_url,
        )
    )
    return app_config_path()


def optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def load_sqlite_vec(conn: sqlite3.Connection) -> None:
    try:
        conn.enable_load_extension(LOAD_EXTENSION_ENABLED)
        sqlite_vec.load(conn)
    except (AttributeError, RuntimeError, sqlite3.Error) as exc:
        message = (
            "sqlite-vec is unavailable for this Python/SQLite runtime. "
            "Use a Python build with SQLite extension loading enabled."
        )
        raise RuntimeError(message) from exc
    finally:
        with suppress(AttributeError, sqlite3.Error):
            conn.enable_load_extension(LOAD_EXTENSION_DISABLED)


def ensure_semantic_schema(
    conn: sqlite3.Connection,
    vector_table: str,
    dimensions: int,
) -> None:
    vector_table = assert_safe_identifier(vector_table)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunk_embeddings (
          chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
          embedding_provider TEXT NOT NULL DEFAULT 'hash',
          embedding_model TEXT NOT NULL,
          embedding_dimensions INTEGER NOT NULL DEFAULT 128,
          chunk_fingerprint TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (
            chunk_id, embedding_provider, embedding_model, embedding_dimensions
          )
        )
        """
    )
    ensure_embedding_metadata_columns(conn)
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {vector_table}
        USING vec0(embedding float[{dimensions}])
        """
    )
    conn.commit()


def index_missing_embeddings(
    conn: sqlite3.Connection,
    provider: EmbeddingProvider,
    vector_table: str,
    dimensions: int,
    *,
    batch_size: int = DEFAULT_SEMANTIC_INDEX_BATCH_SIZE,
) -> int:
    vector_table = assert_safe_identifier(vector_table)
    if batch_size < 1:
        message = "Semantic index batch size must be at least 1."
        raise ValueError(message)
    cleanup_orphaned_vector_rows(conn, vector_table)
    indexed = 0
    while True:
        rows = conn.execute(
            """
            SELECT c.id, c.text, c.fingerprint
            FROM chunks c
            LEFT JOIN chunk_embeddings e
              ON e.chunk_id = c.id
             AND e.embedding_provider = ?
             AND e.embedding_model = ?
             AND e.embedding_dimensions = ?
            WHERE e.chunk_id IS NULL
               OR e.chunk_fingerprint != c.fingerprint
            ORDER BY c.id
            LIMIT ?
            """,
            (provider.name, provider.model, dimensions, batch_size),
        ).fetchall()
        if not rows:
            conn.commit()
            return indexed
        embeddings = provider.embed([str(row["text"] or "") for row in rows])
        for row, embedding in zip(rows, embeddings, strict=True):
            chunk_id = int(row["id"])
            conn.execute(
                """
                DELETE FROM chunk_embeddings
                WHERE chunk_id = ?
                  AND embedding_provider = ?
                  AND embedding_model = ?
                  AND embedding_dimensions = ?
                """,
                (chunk_id, provider.name, provider.model, len(embedding)),
            )
            conn.execute(f"DELETE FROM {vector_table} WHERE rowid = ?", (chunk_id,))
            conn.execute(
                """
                INSERT INTO chunk_embeddings (
                  chunk_id, embedding_provider, embedding_model, embedding_dimensions,
                  chunk_fingerprint, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (chunk_id, provider.name, provider.model, len(embedding), row["fingerprint"]),
            )
            conn.execute(
                f"INSERT INTO {vector_table}(rowid, embedding) VALUES (?, ?)",
                (chunk_id, serialize_float32(embedding)),
            )
        conn.commit()
        indexed += len(rows)


def cleanup_orphaned_vector_rows(conn: sqlite3.Connection, vector_table: str) -> None:
    vector_table = assert_safe_identifier(vector_table)
    conn.execute(
        f"""
        DELETE FROM {vector_table}
        WHERE rowid NOT IN (SELECT id FROM chunks)
        """
    )


def ensure_embedding_metadata_columns(conn: sqlite3.Connection) -> None:
    existing_columns = {
        str(row["name"]) for row in conn.execute("PRAGMA table_info(chunk_embeddings)").fetchall()
    }
    if "embedding_provider" not in existing_columns:
        conn.execute(
            "ALTER TABLE chunk_embeddings "
            "ADD COLUMN embedding_provider TEXT NOT NULL DEFAULT 'hash'"
        )
    if "embedding_dimensions" not in existing_columns:
        conn.execute(
            "ALTER TABLE chunk_embeddings "
            f"ADD COLUMN embedding_dimensions INTEGER NOT NULL DEFAULT {EMBEDDING_DIMENSIONS}"
        )


def vector_table_name(provider: EmbeddingProvider, dimensions: int) -> str:
    model_key = f"{provider.name}:{provider.model}:{dimensions}"
    digest = hashlib.sha256(model_key.encode()).hexdigest()[:VECTOR_TABLE_HASH_LENGTH]
    return assert_safe_identifier(f"chunk_embeddings_vec_{dimensions}_{digest}")


def assert_safe_identifier(value: str) -> str:
    if SQL_IDENTIFIER_RE.fullmatch(value) is None:
        message = f"Unsafe SQL identifier: {value}"
        raise ValueError(message)
    return value


def count_indexed_embeddings(
    conn: sqlite3.Connection,
    provider: EmbeddingProvider,
    dimensions: int,
) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM chunk_embeddings
            WHERE embedding_provider = ?
              AND embedding_model = ?
              AND embedding_dimensions = ?
            """,
            (provider.name, provider.model, dimensions),
        ).fetchone()[0]
    )


def ollama_embed_endpoint(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        message = "Ollama URL must be an http(s) URL."
        raise RuntimeError(message)
    return f"{base_url.rstrip('/')}/api/embed"


def parse_embedding(values: object) -> list[float]:
    if not isinstance(values, Sequence) or isinstance(values, str | bytes | bytearray):
        message = "Embedding provider returned a non-vector value."
        raise TypeError(message)
    parsed: list[float] = []
    for value in values:
        if not isinstance(value, int | float):
            message = "Embedding provider returned a non-numeric vector value."
            raise TypeError(message)
        parsed.append(float(value))
    return parsed


def normalize_embedding(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        message = "Embedding provider returned a zero vector."
        raise RuntimeError(message)
    return [value / norm for value in vector]


def embed_text(text: str) -> list[float]:
    tokens = tokenize(text)
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for feature, weight in weighted_features(tokens):
        add_feature(vector, feature, weight)
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        vector[0] = ZERO_VECTOR_FALLBACK
        return vector
    return [value / norm for value in vector]


def tokenize(text: str) -> list[str]:
    return [normalize_token(match.group(0)) for match in TOKEN_RE.finditer(text.casefold())]


def normalize_token(token: str) -> str:
    if len(token) > MIN_STEM_TOKEN_LENGTH and token.endswith("s"):
        return token[:-1]
    return token


def weighted_features(tokens: list[str]) -> Iterable[tuple[str, float]]:
    for token in tokens:
        yield f"tok:{token}", TOKEN_WEIGHT
    for left, right in pairwise(tokens):
        yield f"big:{left}:{right}", BIGRAM_WEIGHT


def add_feature(vector: list[float], feature: str, weight: float) -> None:
    digest = hashlib.blake2b(feature.encode(), digest_size=FEATURE_HASH_SIZE).digest()
    bucket = int.from_bytes(digest[:HASH_BUCKET_BYTES], "big") % EMBEDDING_DIMENSIONS
    sign = 1.0 if digest[HASH_BUCKET_BYTES] % 2 else -1.0
    vector[bucket] += sign * weight
