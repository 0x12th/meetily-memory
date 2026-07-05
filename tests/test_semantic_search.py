import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, override

import pytest

from meetily_memory.db.repository import IndexRepository
from meetily_memory.db.schema import index_connection
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.semantic_search import (
    LocalHashEmbeddingProvider,
    OllamaEmbeddingProvider,
    SemanticSearchConfig,
    assert_safe_identifier,
    embed_text,
    ensure_semantic_schema,
    index_missing_embeddings,
    index_semantic_embeddings,
    load_semantic_config,
    load_sqlite_vec,
    resolve_embedding_provider,
    semantic_search,
    vector_table_name,
)


class StubEmbeddingProvider:
    name = "stub"
    model = "3d"
    dims: int | None = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        embeddings = []
        for text in texts:
            normalized = text.casefold()
            if "migration" in normalized or "vladimir" in normalized:
                embeddings.append([1.0, 0.0, 0.0])
            elif "pricing" in normalized or "launch" in normalized:
                embeddings.append([0.0, 1.0, 0.0])
            else:
                embeddings.append([0.0, 0.0, 1.0])
        return embeddings


class RecordingBatchEmbeddingProvider:
    name = "recording"
    model = "3d"
    dims: int | None = 3

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]


class RecordingOllamaHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        content_length = int(self.headers["Content-Length"])
        body = self.rfile.read(content_length)
        self.requests.append(
            {
                "path": self.path,
                "content_type": self.headers["Content-Type"],
                "body": json.loads(body),
            }
        )
        payload = {
            "embeddings": [
                [0.1, 0.2, 0.3],
                [0.4, 0.5, 0.6],
            ]
        }
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    @override
    def log_message(self, format: str, *args: Any) -> None:
        return None


def test_semantic_search_accepts_dynamic_embedding_provider(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    indexed = index_semantic_embeddings(index_path, embedding_provider=StubEmbeddingProvider())
    assert indexed > 0

    results = semantic_search(
        index_path,
        "migration blocker",
        5,
        embedding_provider=StubEmbeddingProvider(),
    )

    assert results[0]["meeting_external_id"] == "meeting-2"
    assert results[0]["embedding_provider"] == "stub"
    assert results[0]["embedding_model"] == "3d"
    assert results[0]["embedding_dimensions"] == 3
    repo = IndexRepository(index_path)
    assert repo.stats()["chunks"] >= 4


def test_semantic_index_batches_missing_embeddings(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    provider = RecordingBatchEmbeddingProvider()

    indexed = index_semantic_embeddings(index_path, embedding_provider=provider, batch_size=2)

    assert indexed > 2
    assert [len(call) for call in provider.calls] == [2, 2, 2]


def test_semantic_index_cleans_orphaned_vector_rows(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    provider = StubEmbeddingProvider()
    index_semantic_embeddings(index_path, embedding_provider=provider)

    vector_table = vector_table_name(provider, 3)
    with index_connection(index_path) as conn:
        load_sqlite_vec(conn)
        conn.execute("DELETE FROM chunks WHERE id = (SELECT MIN(id) FROM chunks)")
        conn.commit()
        ensure_semantic_schema(conn, vector_table, 3)

        indexed = index_missing_embeddings(conn, provider, vector_table, 3, batch_size=2)
        orphaned = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM {vector_table}
            WHERE rowid NOT IN (SELECT id FROM chunks)
            """  # noqa: S608
        ).fetchone()[0]

    assert indexed == 0
    assert orphaned == 0


def test_semantic_search_does_not_index_missing_embeddings(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)

    with pytest.raises(RuntimeError, match="Semantic index is empty"):
        semantic_search(
            index_path,
            "migration blocker",
            5,
            embedding_provider=StubEmbeddingProvider(),
        )


def test_assert_safe_identifier_rejects_dynamic_sql_names() -> None:
    assert assert_safe_identifier("chunk_embeddings_vec_128_deadbeef") == (
        "chunk_embeddings_vec_128_deadbeef"
    )
    with pytest.raises(ValueError, match="Unsafe SQL identifier"):
        assert_safe_identifier("chunk_embeddings_vec; DROP TABLE chunks")


def test_local_hash_provider_remains_explicit_baseline() -> None:
    provider = LocalHashEmbeddingProvider()

    embeddings = provider.embed(["migration risks", "pricing decision"])

    assert provider.name == "hash"
    assert provider.model == "local-hash-v1"
    assert provider.dims == 128
    assert len(embeddings) == 2
    assert len(embeddings[0]) == 128


def test_local_hash_provider_does_not_encode_synonyms() -> None:
    risk = embed_text("risk")
    blocker = embed_text("blocker")

    assert sum(left * right for left, right in zip(risk, blocker, strict=True)) == 0.0


def test_resolve_embedding_provider_reads_explicit_environment() -> None:
    provider = resolve_embedding_provider(
        env={
            "MM_EMBEDDING_PROVIDER": "ollama",
            "MM_OLLAMA_MODEL": "nomic-embed-text",
            "MM_OLLAMA_URL": "http://ollama.test",
        }
    )

    assert isinstance(provider, OllamaEmbeddingProvider)
    assert provider.name == "ollama"
    assert provider.model == "nomic-embed-text"
    assert provider.base_url == "http://ollama.test"


def test_resolve_embedding_provider_reads_persisted_config() -> None:
    config = SemanticSearchConfig(
        provider="ollama",
        ollama_model="mxbai-embed-large",
        ollama_url="http://configured.test:11434",
    )

    provider = resolve_embedding_provider(config=config, env={})

    assert isinstance(provider, OllamaEmbeddingProvider)
    assert provider.model == "mxbai-embed-large"
    assert provider.base_url == "http://configured.test:11434"


def test_load_semantic_config_reads_legacy_config_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEETILY_MEMORY_DATA_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "provider": "hash",
                "model": "local-hash-v1",
            }
        ),
        encoding="utf-8",
    )

    config = load_semantic_config()

    assert config.provider == "hash"
    assert config.ollama_model == "local-hash-v1"


def test_ollama_embedding_provider_uses_current_embed_endpoint() -> None:
    RecordingOllamaHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingOllamaHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        provider = OllamaEmbeddingProvider(
            base_url=f"http://127.0.0.1:{server.server_port}/",
            model="nomic-embed-text",
            timeout_seconds=1.5,
        )

        embeddings = provider.embed(["first", "second"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert provider.name == "ollama"
    assert provider.model == "nomic-embed-text"
    assert provider.dims is None
    assert embeddings[0] == [
        0.2672612419124244,
        0.5345224838248488,
        0.8017837257372731,
    ]
    assert embeddings[1] == [
        0.4558423058385518,
        0.5698028822981898,
        0.6837634587578276,
    ]
    assert RecordingOllamaHandler.requests == [
        {
            "path": "/api/embed",
            "content_type": "application/json",
            "body": {
                "model": "nomic-embed-text",
                "input": ["first", "second"],
            },
        }
    ]
