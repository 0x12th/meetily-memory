from dataclasses import replace
from pathlib import Path

import pytest

from meetily_memory.evaluation import (
    EvaluationDataset,
    EvaluationManifest,
    EvaluationReport,
    EvaluationTask,
    ExpectedEvidence,
    ObservedTask,
    compare_reports,
    evaluate_retrieval,
    load_dataset,
)
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner


def test_synthetic_evaluation_dataset_is_valid() -> None:
    dataset = load_dataset(Path("eval/synthetic_dataset.json"))

    assert dataset.schema_version == "meetily-memory.eval.v1"
    assert {evidence.relevance for task in dataset.tasks for evidence in task.expected} == {1, 2}
    assert all(task.critical_reason for task in dataset.tasks if task.critical)


def test_evaluation_calculates_ranked_and_product_metrics(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    dataset = load_dataset(Path("eval/synthetic_dataset.json"))

    report = evaluate_retrieval(dataset, index_path, limit=5)

    assert report.metrics.hit_at_1 == 1.0
    assert report.metrics.hit_at_3 == 1.0
    assert report.metrics.hit_at_5 == 1.0
    assert report.metrics.mrr == 1.0
    assert report.metrics.ndcg == pytest.approx(0.9131173286)
    assert report.metrics.source_accuracy == 1.0
    assert report.metrics.median_source_openings == 1.0
    assert report.metrics.empty_result_rate == 0.0
    assert report.metrics.median_latency_ms >= 0
    assert report.metrics.p95_latency_ms >= report.metrics.median_latency_ms
    assert all(task.success for task in report.tasks)


def test_evaluation_records_explicit_neighbor_context_parameter(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    dataset = load_dataset(Path("eval/synthetic_dataset.json"))

    report = evaluate_retrieval(dataset, index_path, limit=5, context=1)

    assert report.manifest.retrieval_parameters == {"limit": 5, "context": 1}


def test_report_comparison_is_paired_by_task_and_class() -> None:
    manifest = EvaluationManifest.compatible_for_tests()
    baseline = EvaluationReport.for_tests(
        manifest,
        [
            ObservedTask.for_tests("exact", "exact_match", success=True, ndcg=1.0),
            ObservedTask.for_tests("risk", "risk", success=False, ndcg=0.0, critical=True),
        ],
    )
    candidate = EvaluationReport.for_tests(
        manifest,
        [
            ObservedTask.for_tests("exact", "exact_match", success=True, ndcg=1.0),
            ObservedTask.for_tests("risk", "risk", success=True, ndcg=0.8, critical=True),
        ],
    )

    comparison = compare_reports(baseline, candidate)

    assert comparison.improvements == 1
    assert comparison.ties == 1
    assert comparison.regressions == 0
    assert comparison.transitions == ["risk: failure -> success"]
    assert comparison.by_class["risk"].improvements == 1
    assert comparison.critical_regressions == []


def test_report_comparison_rejects_incompatible_manifests() -> None:
    baseline = EvaluationReport.for_tests(EvaluationManifest.compatible_for_tests(), [])
    changed = EvaluationManifest.compatible_for_tests(corpus_fingerprint="changed")
    candidate = EvaluationReport.for_tests(changed, [])

    with pytest.raises(ValueError, match="incompatible evaluation manifests"):
        compare_reports(baseline, candidate)


def test_report_comparison_allows_explicit_index_schema_drift() -> None:
    baseline_manifest = EvaluationManifest.compatible_for_tests()
    candidate_manifest = replace(
        baseline_manifest,
        run_id="candidate",
        index_schema_version=4,
    )
    baseline = EvaluationReport.for_tests(baseline_manifest, [])
    candidate = EvaluationReport.for_tests(candidate_manifest, [])

    comparison = compare_reports(
        baseline,
        candidate,
        allow_manifest_drift={"index_schema_version"},
    )

    assert comparison.allowed_manifest_drift == ["index_schema_version"]


def test_dataset_rejects_critical_task_without_predeclared_reason() -> None:
    with pytest.raises(ValueError, match="critical_reason"):
        EvaluationDataset(
            schema_version="meetily-memory.eval.v1",
            dataset_version="1",
            name="invalid",
            tasks=(
                EvaluationTask(
                    id="risk",
                    query="risk",
                    task_class="risk",
                    critical=True,
                    critical_reason=None,
                    expected=(ExpectedEvidence("meeting/chunk", 2),),
                ),
            ),
        )


def test_evaluation_uses_external_evidence_identity_after_index_rebuild(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    scanner = MeetilySQLiteScanner(index_path)
    scanner.scan(meetily_db)
    dataset = load_dataset(Path("eval/synthetic_dataset.json"))
    first = evaluate_retrieval(dataset, index_path, limit=5)

    index_path.unlink()
    scanner = MeetilySQLiteScanner(index_path)
    scanner.scan(meetily_db)
    second = evaluate_retrieval(dataset, index_path, limit=5)

    assert first.tasks[0].retrieved[0].evidence_id == "meeting-1/transcript-1"
    assert second.tasks[0].retrieved[0].evidence_id == "meeting-1/transcript-1"
