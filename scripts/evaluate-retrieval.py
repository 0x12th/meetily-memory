#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

from meetily_memory.evaluation import (
    compare_reports,
    evaluate_retrieval,
    load_dataset,
    load_report,
    save_report,
)
from meetily_memory.json_codec import dumps_json


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
        "--context",
        type=int,
        default=0,
        help="Include this many adjacent chunks around each lexical match.",
    )
    args = parser.parse_args()

    report = evaluate_retrieval(
        load_dataset(args.dataset),
        args.index,
        limit=args.limit,
        context=args.context,
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
