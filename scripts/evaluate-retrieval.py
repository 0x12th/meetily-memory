#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

from meetily_memory.evaluation import (
    EvaluationRetrievalConfig,
    compare_reports,
    evaluate_retrieval,
    load_dataset,
    load_report,
    save_report,
)
from meetily_memory.json_codec import dumps_json
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.retrieval import (
    HYBRID_CANDIDATE_MULTIPLIER,
    RRF_K,
    HybridRetrievalStrategy,
    LexicalRetrievalStrategy,
    SemanticRetrievalStrategy,
)
from meetily_memory.semantic_search import resolve_embedding_provider


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Meetily Memory retrieval.")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument(
        "--allow-drift",
        action="append",
        choices=(
            "dataset_fingerprint",
            "corpus_fingerprint",
            "index_schema_version",
            "retrieval_mode",
            "retrieval_parameters",
            "semantic_provider",
            "semantic_model",
            "semantic_dimension",
        ),
        default=[],
        help="Explicitly allow one incompatible manifest field for drift analysis.",
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--retrieval",
        choices=("lexical", "hybrid"),
        default="lexical",
        help="Retrieval strategy to evaluate; hybrid requires an existing semantic index.",
    )
    parser.add_argument(
        "--embedding-provider",
        choices=("ollama", "hash"),
        help="Embedding provider for explicit hybrid evaluation.",
    )
    parser.add_argument("--embedding-model", help="Ollama embedding model for hybrid evaluation.")
    parser.add_argument("--ollama-url", help="Ollama base URL for hybrid evaluation.")
    parser.add_argument(
        "--context",
        type=int,
        default=0,
        help="Include this many adjacent chunks around each lexical match.",
    )
    args = parser.parse_args()

    strategy = None
    retrieval_mode = "fts5"
    retrieval_parameters: dict[str, object] = {}
    semantic_provider = None
    semantic_model = None
    semantic_dimension = None
    if args.retrieval == "hybrid":
        provider = resolve_embedding_provider(
            args.embedding_provider,
            ollama_model=args.embedding_model,
            ollama_url=args.ollama_url,
        )
        repository = IndexRepository(args.index)
        strategy = HybridRetrievalStrategy(
            lexical=LexicalRetrievalStrategy(repository),
            semantic=SemanticRetrievalStrategy(repository, provider),
        )
        retrieval_mode = "hybrid_rrf"
        retrieval_parameters = {
            "rrf_k": RRF_K,
            "candidate_multiplier": HYBRID_CANDIDATE_MULTIPLIER,
        }
        semantic_provider = provider.name
        semantic_model = provider.model
        semantic_dimension = provider.dims or len(provider.embed([""])[0])

    report = evaluate_retrieval(
        load_dataset(args.dataset),
        args.index,
        limit=args.limit,
        context=args.context,
        config=EvaluationRetrievalConfig(
            strategy=strategy,
            mode=retrieval_mode,
            parameters=retrieval_parameters,
            semantic_provider=semantic_provider,
            semantic_model=semantic_model,
            semantic_dimension=semantic_dimension,
        ),
    )
    save_report(report, args.output)
    payload: dict[str, object] = {"report": report.as_payload()}
    if args.baseline:
        payload["comparison"] = compare_reports(
            load_report(args.baseline),
            report,
            allow_manifest_drift=set(args.allow_drift),
        ).as_payload()
    sys.stdout.write(dumps_json(payload))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
