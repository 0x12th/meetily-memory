import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, override

from meetily_memory.db.repository import IndexRepository
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.semantic_search import (
    LocalHashEmbeddingProvider,
    OllamaEmbeddingProvider,
    SemanticSearchConfig,
    embed_text,
    resolve_embedding_provider,
    semantic_search,
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
    embeddings_indexed = results[0]["embeddings_indexed"]
    assert isinstance(embeddings_indexed, int)
    assert embeddings_indexed > 0

    repo = IndexRepository(index_path)
    assert repo.stats()["chunks"] >= 4


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
