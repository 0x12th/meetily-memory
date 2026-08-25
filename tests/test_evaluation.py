import sqlite3
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from meetily_memory.domain import SearchHit
from meetily_memory.evaluation import (
    EvaluationDataset,
    EvaluationManifest,
    EvaluationReport,
    EvaluationRetrievalConfig,
    EvaluationTask,
    ExpectedEvidence,
    ObservedTask,
    compare_reports,
    evaluate_hybrid_gate,
    evaluate_retrieval,
    load_dataset,
)
from meetily_memory.json_codec import loads_json
from meetily_memory.repositories.index import IndexRepository
from meetily_memory.retrieval import (
    LexicalRetrievalStrategy,
    LexicalTagMeetingRetrievalStrategy,
    TagRetrievalStrategy,
)
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.semantic_search import LocalHashEmbeddingProvider, index_semantic_embeddings
from meetily_memory.tagging import TagRepository, TagService
from tests.semantic_helpers import requires_sqlite_vec


@dataclass(frozen=True)
class FixedEvaluationStrategy:
    hits: tuple[SearchHit, ...]

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        meeting_id: int | None = None,
        context: int = 0,
    ) -> tuple[SearchHit, ...]:
        del query, meeting_id, context
        return self.hits[:limit]


def test_synthetic_evaluation_dataset_is_valid() -> None:
    dataset = load_dataset(Path("tests/fixtures/evaluation/synthetic_dataset.json"))

    assert dataset.schema_version == "meetily-memory.eval.v2"
    assert {evidence.relevance for task in dataset.tasks for evidence in task.expected} == {1, 2}
    assert all(task.critical_reason for task in dataset.tasks if task.critical)
    assert all(task.expected_meetings for task in dataset.tasks)
    assert next(task for task in dataset.tasks if task.task_class == "tag").expected == ()


def test_v1_dataset_is_migrated_to_meeting_level_on_read(tmp_path: Path) -> None:
    dataset_path = tmp_path / "legacy-v1.json"
    dataset_path.write_text(
        """
        {
          "schema_version": "meetily-memory.eval.v1",
          "dataset_version": "1",
          "name": "legacy",
          "tasks": [{
            "id": "legacy",
            "query": "pricing",
            "class": "exact_match",
            "critical": false,
            "critical_reason": null,
            "expected": [
              {"evidence_id": "meeting-1/transcript-1", "relevance": 2},
              {"evidence_id": "meeting-1/summary:meeting-1", "relevance": 1}
            ]
          }]
        }
        """
    )

    dataset = load_dataset(dataset_path)

    assert dataset.schema_version == "meetily-memory.eval.v2"
    assert dataset.tasks[0].expected_meetings == ("meeting-1",)


def test_v2_dataset_accepts_tag_only_and_semantic_classes(tmp_path: Path) -> None:
    dataset_path = tmp_path / "meeting-v2.json"
    dataset_path.write_text(
        """
        {
          "schema_version": "meetily-memory.eval.v2",
          "dataset_version": "2",
          "name": "meeting-level",
          "tasks": [
            {
              "id": "tag-only",
              "query": "private-label",
              "class": "tag",
              "critical": false,
              "critical_reason": null,
              "expected_meetings": ["meeting-2"],
              "expected": []
            },
            {
              "id": "paraphrase",
              "query": "who handles the move",
              "class": "paraphrase",
              "critical": false,
              "critical_reason": null,
              "expected_meetings": ["meeting-2"],
              "expected": []
            }
          ]
        }
        """
    )

    dataset = load_dataset(dataset_path)

    assert [task.task_class for task in dataset.tasks] == ["tag", "paraphrase"]
    assert dataset.tasks[0].expected == ()


def test_evaluation_calculates_ranked_and_product_metrics(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    dataset = load_dataset(Path("tests/fixtures/evaluation/synthetic_dataset.json"))
    dataset = replace(
        dataset,
        tasks=tuple(task for task in dataset.tasks if task.task_class != "tag"),
    )

    report = evaluate_retrieval(dataset, index_path, limit=5)

    assert report.metrics.hit_at_1 == 1.0
    assert report.metrics.hit_at_3 == 1.0
    assert report.metrics.hit_at_5 == 1.0
    assert report.metrics.mrr == 1.0
    assert report.metrics.ndcg == 1.0
    assert report.metrics.source_accuracy == 1.0
    assert report.metrics.median_source_openings == 1.0
    assert report.metrics.empty_result_rate == 0.0
    assert report.metrics.median_latency_ms >= 0
    assert report.metrics.p95_latency_ms >= report.metrics.median_latency_ms
    assert all(task.success for task in report.tasks)
    assert report.tasks[0].retrieved[0].meeting_external_id == "meeting-1"
    assert report.tasks[0].retrieved[0].evidence_ids


def test_evaluation_supports_tag_only_meeting_results_and_fingerprints_tag_state(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    repository = IndexRepository(index_path)
    dataset = EvaluationDataset(
        schema_version="meetily-memory.eval.v2",
        dataset_version="2",
        name="tag-only",
        tasks=(
            EvaluationTask(
                id="tag-only",
                query="private-label",
                task_class="tag",
                critical=False,
                critical_reason=None,
                expected=(),
                expected_meetings=("meeting-2",),
            ),
        ),
    )
    before = evaluate_retrieval(dataset, index_path, limit=5)
    TagService(IndexRepository(index_path)).assign(("2",), ("private-label",))

    report = evaluate_retrieval(
        dataset,
        index_path,
        limit=5,
        config=EvaluationRetrievalConfig(
            meeting_strategy=LexicalTagMeetingRetrievalStrategy(
                repository=repository,
                lexical=LexicalRetrievalStrategy(repository),
                tags=TagRetrievalStrategy(TagRepository(repository.state_path)),
            ),
            mode="fts5_tags",
        ),
    )

    assert report.tasks[0].success is True
    assert report.tasks[0].retrieved[0].meeting_external_id == "meeting-2"
    assert report.tasks[0].retrieved[0].evidence_ids == ()
    assert before.manifest.tag_state_fingerprint != report.manifest.tag_state_fingerprint


def test_evaluation_records_explicit_neighbor_context_parameter(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    dataset = load_dataset(Path("tests/fixtures/evaluation/synthetic_dataset.json"))

    report = evaluate_retrieval(dataset, index_path, limit=5, context=1)

    assert report.manifest.retrieval_parameters == {"limit": 5, "context": 1}


def test_evaluation_records_explicit_hybrid_strategy(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    dataset = load_dataset(Path("tests/fixtures/evaluation/synthetic_dataset.json"))
    hit = IndexRepository(index_path).search_hits("pricing decision", 1)[0]

    report = evaluate_retrieval(
        dataset,
        index_path,
        limit=5,
        config=EvaluationRetrievalConfig(
            strategy=FixedEvaluationStrategy((hit,)),
            mode="hybrid_rrf",
            parameters={"rrf_k": 60},
            semantic_provider="ollama",
            semantic_model="nomic-embed-text",
            semantic_dimension=768,
        ),
    )

    assert report.manifest.retrieval_mode == "hybrid_rrf"
    assert report.manifest.retrieval_parameters == {"limit": 5, "context": 0, "rrf_k": 60}
    assert report.manifest.semantic_provider == "ollama"
    assert report.manifest.semantic_model == "nomic-embed-text"
    assert report.manifest.semantic_dimension == 768


def test_evaluation_records_cold_start_before_warm_latency_measurements(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    dataset = load_dataset(Path("tests/fixtures/evaluation/synthetic_dataset.json"))
    hit = IndexRepository(index_path).search_hits("pricing decision", 1)[0]

    report = evaluate_retrieval(
        dataset,
        index_path,
        limit=5,
        config=EvaluationRetrievalConfig(
            strategy=FixedEvaluationStrategy((hit,)),
            warmup=True,
        ),
    )

    assert report.cold_start_latency_ms is not None
    assert report.cold_start_latency_ms >= 0
    assert report.manifest.retrieval_parameters["warmed_up"] is True


@requires_sqlite_vec
def test_evaluation_script_runs_explicit_hybrid_mode(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    output_path = tmp_path / "hybrid.json"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    index_semantic_embeddings(
        index_path,
        embedding_provider=LocalHashEmbeddingProvider(),
    )

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/evaluate-retrieval.py",
            "tests/fixtures/evaluation/synthetic_dataset.json",
            "--index",
            str(index_path),
            "--output",
            str(output_path),
            "--retrieval",
            "hybrid",
            "--embedding-provider",
            "hash",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    report = loads_json(output_path.read_text())
    assert report["manifest"]["retrieval_mode"] == "hybrid_rrf"
    assert report["manifest"]["semantic_provider"] == "hash"
    assert report["manifest"]["semantic_model"] == "local-hash-v1"
    assert report["manifest"]["retrieval_parameters"]["warmed_up"] is True
    assert report["cold_start_latency_ms"] >= 0


@requires_sqlite_vec
def test_evaluation_script_rejects_incomplete_hybrid_index(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.sqlite"
    output_path = tmp_path / "hybrid.json"
    MeetilySQLiteScanner(index_path).scan(meetily_db)
    index_semantic_embeddings(
        index_path,
        embedding_provider=LocalHashEmbeddingProvider(),
    )
    with sqlite3.connect(index_path) as conn:
        conn.execute(
            """
            DELETE FROM chunk_embeddings
            WHERE chunk_id = (SELECT MIN(chunk_id) FROM chunk_embeddings)
            """
        )
        conn.commit()

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/evaluate-retrieval.py",
            "tests/fixtures/evaluation/synthetic_dataset.json",
            "--index",
            str(index_path),
            "--output",
            str(output_path),
            "--retrieval",
            "hybrid",
            "--embedding-provider",
            "hash",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "semantic index is incomplete" in result.stderr
    assert not output_path.exists()


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


def test_hybrid_gate_requires_semantic_wins_without_exact_regressions() -> None:
    manifest = replace(
        EvaluationManifest.compatible_for_tests(),
        retrieval_parameters={"limit": 5, "warmed_up": True},
    )
    baseline = EvaluationReport.for_tests(
        manifest,
        [
            ObservedTask.for_tests("exact", "exact_match", success=True, ndcg=1.0),
            ObservedTask.for_tests("semantic", "semantic", success=False, ndcg=0.0),
        ],
    )
    candidate = EvaluationReport.for_tests(
        manifest,
        [
            ObservedTask.for_tests("exact", "exact_match", success=True, ndcg=1.0),
            ObservedTask.for_tests("semantic", "semantic", success=True, ndcg=1.0),
        ],
    )
    comparison = compare_reports(baseline, candidate)

    passed = evaluate_hybrid_gate(baseline, candidate, comparison)
    regressed = EvaluationReport.for_tests(
        manifest,
        [
            ObservedTask.for_tests("exact", "exact_match", success=False, ndcg=0.0),
            ObservedTask.for_tests("semantic", "semantic", success=True, ndcg=1.0),
        ],
    )
    failed = evaluate_hybrid_gate(
        baseline,
        regressed,
        compare_reports(baseline, regressed),
    )

    assert passed.passed is True
    assert passed.failures == ()
    assert failed.passed is False
    assert "exact-query regressions: 1" in failed.failures


def test_report_comparison_rejects_incompatible_manifests() -> None:
    baseline = EvaluationReport.for_tests(EvaluationManifest.compatible_for_tests(), [])
    changed = EvaluationManifest.compatible_for_tests(corpus_fingerprint="changed")
    candidate = EvaluationReport.for_tests(changed, [])

    with pytest.raises(ValueError, match="incompatible evaluation manifests"):
        compare_reports(baseline, candidate)


def test_report_comparison_rejects_tag_state_drift() -> None:
    baseline_manifest = EvaluationManifest.compatible_for_tests()
    candidate_manifest = replace(
        baseline_manifest,
        run_id="candidate",
        tag_state_fingerprint="changed",
    )

    with pytest.raises(ValueError, match="tag_state_fingerprint"):
        compare_reports(
            EvaluationReport.for_tests(baseline_manifest, []),
            EvaluationReport.for_tests(candidate_manifest, []),
        )


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
            schema_version="meetily-memory.eval.v2",
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


def test_dataset_accepts_open_question_tasks() -> None:
    task = EvaluationTask(
        id="question-team-composition",
        query="open team composition question",
        task_class="question",
        critical=False,
        critical_reason=None,
        expected=(ExpectedEvidence("meeting/chunk", 2),),
    )

    assert task.task_class == "question"


def test_evaluation_uses_external_evidence_identity_after_index_rebuild(
    meetily_db: Path, tmp_path: Path
) -> None:
    index_path = tmp_path / "index.sqlite"
    scanner = MeetilySQLiteScanner(index_path)
    scanner.scan(meetily_db)
    dataset = load_dataset(Path("tests/fixtures/evaluation/synthetic_dataset.json"))
    first = evaluate_retrieval(dataset, index_path, limit=5)

    index_path.unlink()
    scanner = MeetilySQLiteScanner(index_path)
    scanner.scan(meetily_db)
    second = evaluate_retrieval(dataset, index_path, limit=5)

    assert first.tasks[0].retrieved[0].evidence_ids[0] == "meeting-1/transcript-1"
    assert second.tasks[0].retrieved[0].evidence_ids[0] == "meeting-1/transcript-1"
