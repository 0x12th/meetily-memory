#!/usr/bin/env python3
import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from meetily_memory.db.schema import index_connection
from meetily_memory.evaluation import (
    EvaluationReport,
    EvaluationRetrievalConfig,
    ObservedTask,
    compare_reports,
    evaluate_hybrid_gate,
    evaluate_retrieval,
    load_dataset,
    save_report,
)
from meetily_memory.json_codec import dumps_json, dumps_json_bytes
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.retrieval import (
    HYBRID_CANDIDATE_MULTIPLIER,
    RRF_K,
    HybridRetrievalStrategy,
    LexicalRetrievalStrategy,
    SemanticRetrievalStrategy,
    TagRetrievalStrategy,
)
from meetily_memory.semantic_search import (
    OllamaEmbeddingProvider,
    index_semantic_embeddings,
    load_sqlite_vec,
    semantic_index_coverage,
)
from meetily_memory.tagging import TagRepository

DEFAULT_MODELS = ("qwen3-embedding:0.6b", "bge-m3")
SEMANTIC_CLASSES = frozenset({"semantic", "paraphrase"})
TOP_ONE_PROTECTED_CLASSES = frozenset({"exact_match", "tag"})
MIN_SEMANTIC_IMPROVEMENTS = 3
ALLOWED_COMPARISON_DRIFT = {
    "retrieval_mode",
    "retrieval_parameters",
    "semantic_provider",
    "semantic_model",
    "semantic_dimension",
    "semantic_model_digest",
    "semantic_query_instruction",
    "semantic_document_instruction",
    "semantic_index_fingerprint",
    "chunk_fingerprint",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the frozen offline multilingual semantic-search spike."
    )
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", action="append", dest="models")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset = load_dataset(args.dataset)
    baseline_path = output_dir / "lexical.json"
    baseline = evaluate_retrieval(
        dataset,
        args.index,
        limit=args.limit,
        config=EvaluationRetrievalConfig(warmup=True),
    )
    save_report(baseline, baseline_path)

    state_path = args.index.with_name("state.sqlite")
    if state_path.exists():
        shutil.copy2(state_path, output_dir / "state.sqlite")

    available_models = ollama_models(args.ollama_url)
    candidates: list[dict[str, object]] = []
    for requested_model in args.models or DEFAULT_MODELS:
        model = resolve_model(available_models, requested_model)
        candidate_path = output_dir / f"{model_slug(requested_model)}.sqlite"
        shutil.copy2(args.index, candidate_path)
        strip_semantic_index(candidate_path)
        lexical_index_size = candidate_path.stat().st_size

        provider = OllamaEmbeddingProvider(
            model=requested_model,
            base_url=args.ollama_url,
            model_digest=str(model["digest"]),
        )
        dimensions = len(provider.embed(["dimension probe"], role="document")[0])
        chunks_digest = chunk_fingerprint(candidate_path)
        index_fingerprint = semantic_index_fingerprint(provider, dimensions, chunks_digest)
        started = perf_counter()
        indexed = index_semantic_embeddings(candidate_path, embedding_provider=provider)
        refresh_ms = (perf_counter() - started) * 1000
        coverage = semantic_index_coverage(candidate_path, provider)
        if not coverage.complete:
            message = f"semantic index is incomplete for {requested_model}: {coverage.as_payload()}"
            raise RuntimeError(message)
        semantic_size = max(0, candidate_path.stat().st_size - lexical_index_size)

        repository = IndexRepository(candidate_path)
        strategy = HybridRetrievalStrategy(
            repository=repository,
            lexical=LexicalRetrievalStrategy(repository),
            semantic=SemanticRetrievalStrategy(repository, provider),
            tags=TagRetrievalStrategy(TagRepository(repository.state_path)),
            semantic_provider=provider,
        )
        report = evaluate_retrieval(
            dataset,
            candidate_path,
            limit=args.limit,
            config=EvaluationRetrievalConfig(
                meeting_strategy=strategy,
                mode="hybrid_rrf",
                parameters={
                    "rrf_k": RRF_K,
                    "candidate_multiplier": HYBRID_CANDIDATE_MULTIPLIER,
                    "fts_weight": 1.0,
                    "semantic_weight": 1.0,
                    "tag_weight": 1.0,
                },
                semantic_provider=provider.name,
                semantic_model=provider.model,
                semantic_dimension=dimensions,
                semantic_model_digest=provider.model_digest,
                semantic_query_instruction=provider.effective_query_instruction,
                semantic_document_instruction=provider.document_instruction,
                semantic_index_fingerprint=index_fingerprint,
                chunk_fingerprint=chunks_digest,
                semantic_refresh_ms=refresh_ms,
                semantic_index_size_bytes=semantic_size,
                warmup=True,
            ),
        )
        report_path = output_dir / f"{model_slug(requested_model)}.json"
        save_report(report, report_path)
        candidates.append(candidate_summary(baseline, report, report_path, indexed))

    summary = {
        "scope": (
            "offline spike on the pre-existing 47-task dataset; not the real-failure product gate"
        ),
        "dataset": str(args.dataset),
        "baseline_report": str(baseline_path),
        "candidates": candidates,
        "decision": "keep experimental",
        "decision_reason": (
            "The required 15 real lexical failures and three repeated real-use wins do not exist; "
            "offline metrics cannot authorize adoption."
        ),
    }
    summary_path = output_dir / "summary.json"
    write_immutable(summary_path, summary)
    sys.stdout.write(dumps_json(summary))
    sys.stdout.write("\n")


def candidate_summary(
    baseline: EvaluationReport,
    candidate: EvaluationReport,
    report_path: Path,
    indexed: int,
) -> dict[str, object]:
    comparison = compare_reports(
        baseline,
        candidate,
        allow_manifest_drift=ALLOWED_COMPARISON_DRIFT,
    )
    metric_gate = evaluate_hybrid_gate(
        baseline,
        candidate,
        comparison,
        warm_p95_limit_ms=150.0,
    )
    before = {task.id: task for task in baseline.tasks}
    after = {task.id: task for task in candidate.tasks}
    semantic_improvements = sum(
        count.improvements
        for name, count in comparison.by_class.items()
        if name in SEMANTIC_CLASSES
    )
    protected_top_one_regressions = [
        task_id
        for task_id, baseline_task in before.items()
        if (baseline_task.critical or baseline_task.task_class in TOP_ONE_PROTECTED_CLASSES)
        and baseline_task.hit_at_1 == 1.0
        and after[task_id].hit_at_1 == 0.0
    ]
    changed_queries = [
        {
            "id": task_id,
            "class": before[task_id].task_class,
            "baseline_rank": first_relevant_rank(before[task_id]),
            "candidate_rank": first_relevant_rank(after[task_id]),
        }
        for task_id in before
        if first_relevant_rank(before[task_id]) != first_relevant_rank(after[task_id])
    ]
    offline_gate_failures = list(metric_gate.failures)
    if semantic_improvements < MIN_SEMANTIC_IMPROVEMENTS:
        offline_gate_failures.append(
            "semantic/paraphrase improvements below "
            f"{MIN_SEMANTIC_IMPROVEMENTS}: {semantic_improvements}"
        )
    if protected_top_one_regressions:
        offline_gate_failures.append(
            "protected top-1 regressions: " + ", ".join(protected_top_one_regressions)
        )
    return {
        "model": candidate.manifest.semantic_model,
        "report": str(report_path),
        "indexed_chunks": indexed,
        "metrics": asdict(candidate.metrics),
        "comparison": comparison.as_payload(),
        "changed_queries": changed_queries,
        "offline_metric_gate_passed": not offline_gate_failures,
        "offline_metric_gate_failures": offline_gate_failures,
        "product_gate_passed": False,
        "product_gate_failure": "0/15 real lexical failures and 0/3 repeated real-use wins",
    }


def first_relevant_rank(task: ObservedTask) -> int | None:
    return min((item.rank for item in task.retrieved if item.relevance > 0), default=None)


def ollama_models(base_url: str) -> list[dict[str, object]]:
    endpoint = f"{base_url.rstrip('/')}/api/tags"
    try:
        with urlopen(endpoint, timeout=10) as response:  # noqa: S310
            payload = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        message = f"Ollama is unavailable at {base_url}."
        raise RuntimeError(message) from exc
    models = payload.get("models")
    if not isinstance(models, list):
        message = "Ollama returned an unexpected model-list response."
        raise TypeError(message)
    return [model for model in models if isinstance(model, dict)]


def resolve_model(models: list[dict[str, object]], requested: str) -> dict[str, object]:
    aliases = {requested, f"{requested}:latest"}
    for model in models:
        if model.get("name") in aliases or model.get("model") in aliases:
            if not model.get("digest"):
                break
            return model
    message = f"Ollama model is not available: {requested}. Run `ollama pull {requested}`."
    raise RuntimeError(message)


def strip_semantic_index(index_path: Path) -> None:
    with index_connection(index_path) as conn:
        load_sqlite_vec(conn)
        vector_tables = [
            str(row[0])
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name GLOB 'chunk_embeddings_vec_*'
                  AND sql LIKE 'CREATE VIRTUAL TABLE%'
                """
            ).fetchall()
        ]
        for table in vector_tables:
            conn.execute(f'DROP TABLE "{table}"')
        conn.execute("DROP TABLE IF EXISTS chunk_embeddings")
        conn.commit()
        conn.execute("VACUUM")


def chunk_fingerprint(index_path: Path) -> str:
    with sqlite3.connect(index_path) as conn:
        rows = conn.execute(
            """
            SELECT m.external_id, c.external_id, c.kind, c.ordinal, c.fingerprint
            FROM chunks c
            JOIN meetings m ON m.id = c.meeting_id
            ORDER BY m.external_id, c.kind, c.ordinal, c.external_id
            """
        ).fetchall()
    return hashlib.sha256(dumps_json_bytes([list(row) for row in rows])).hexdigest()


def semantic_index_fingerprint(
    provider: OllamaEmbeddingProvider,
    dimensions: int,
    chunks_digest: str,
) -> str:
    payload = {
        "provider": provider.name,
        "model": provider.model,
        "model_digest": provider.model_digest,
        "dimensions": dimensions,
        "query_instruction": provider.effective_query_instruction,
        "document_instruction": provider.document_instruction,
        "chunk_fingerprint": chunks_digest,
    }
    return hashlib.sha256(dumps_json_bytes(payload)).hexdigest()


def model_slug(model: str) -> str:
    return "".join(character if character.isalnum() else "-" for character in model).strip("-")


def write_immutable(path: Path, payload: object) -> None:
    try:
        with path.open("x") as stream:
            stream.write(dumps_json(payload))
            stream.write("\n")
    except FileExistsError as exc:
        message = f"evaluation artifact is immutable and already exists: {path}"
        raise ValueError(message) from exc


if __name__ == "__main__":
    main()
