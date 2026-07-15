import hashlib
import math
import shutil
import sqlite3
import subprocess
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any, ClassVar

from meetily_memory.db.repository import IndexRepository
from meetily_memory.json_codec import dumps_json, dumps_json_bytes, loads_json

EVALUATION_SCHEMA_VERSION = "meetily-memory.eval.v1"
PRIMARY_RELEVANCE = 2
HIT_AT_THREE = 3
EVALUATION_CUTOFF = 5
TASK_CLASSES = frozenset(
    {
        "exact_match",
        "fuzzy_recall",
        "decision",
        "task",
        "risk",
        "person",
        "project",
        "neighbor_context",
    }
)


@dataclass(frozen=True)
class ExpectedEvidence:
    evidence_id: str
    relevance: int

    def __post_init__(self) -> None:
        if not self.evidence_id:
            message = "expected evidence_id must not be empty"
            raise ValueError(message)
        if self.relevance not in {1, 2}:
            message = "expected relevance must be 1 or 2"
            raise ValueError(message)


@dataclass(frozen=True)
class EvaluationTask:
    id: str
    query: str
    task_class: str
    critical: bool
    critical_reason: str | None
    expected: tuple[ExpectedEvidence, ...]

    def __post_init__(self) -> None:
        if not self.id or not self.query:
            message = "evaluation task id and query must not be empty"
            raise ValueError(message)
        if self.task_class not in TASK_CLASSES:
            message = f"unsupported evaluation task class: {self.task_class}"
            raise ValueError(message)
        if self.critical and not self.critical_reason:
            message = f"critical task {self.id!r} requires critical_reason"
            raise ValueError(message)
        if not self.expected:
            message = f"evaluation task {self.id!r} requires expected evidence"
            raise ValueError(message)
        evidence_ids = [item.evidence_id for item in self.expected]
        if len(evidence_ids) != len(set(evidence_ids)):
            message = f"evaluation task {self.id!r} has duplicate evidence_id values"
            raise ValueError(message)


@dataclass(frozen=True)
class EvaluationDataset:
    schema_version: str
    dataset_version: str
    name: str
    tasks: tuple[EvaluationTask, ...]

    def __post_init__(self) -> None:
        if self.schema_version != EVALUATION_SCHEMA_VERSION:
            message = f"unsupported evaluation schema: {self.schema_version}"
            raise ValueError(message)
        if not self.dataset_version or not self.name or not self.tasks:
            message = "dataset_version, name, and tasks are required"
            raise ValueError(message)
        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            message = "evaluation task ids must be unique"
            raise ValueError(message)

    @property
    def fingerprint(self) -> str:
        return sha256_payload(dataset_payload(self))


@dataclass(frozen=True)
class RetrievedEvidence:
    evidence_id: str
    meeting_external_id: str
    relevance: int
    rank: int


@dataclass(frozen=True)
class ObservedTask:
    id: str
    task_class: str
    critical: bool
    success: bool
    hit_at_1: float
    hit_at_3: float
    hit_at_5: float
    reciprocal_rank: float
    ndcg: float
    source_accurate: bool
    source_openings: int
    latency_ms: float
    retrieved: tuple[RetrievedEvidence, ...]

    @classmethod
    def for_tests(
        cls,
        task_id: str,
        task_class: str,
        *,
        success: bool,
        ndcg: float,
        critical: bool = False,
    ) -> "ObservedTask":
        return cls(
            id=task_id,
            task_class=task_class,
            critical=critical,
            success=success,
            hit_at_1=float(success),
            hit_at_3=float(success),
            hit_at_5=float(success),
            reciprocal_rank=float(success),
            ndcg=ndcg,
            source_accurate=success,
            source_openings=1 if success else 0,
            latency_ms=0.0,
            retrieved=(),
        )


@dataclass(frozen=True)
class EvaluationMetrics:
    hit_at_1: float
    hit_at_3: float
    hit_at_5: float
    mrr: float
    ndcg: float
    source_accuracy: float
    median_source_openings: float
    empty_result_rate: float
    median_latency_ms: float
    p95_latency_ms: float


@dataclass(frozen=True)
class EvaluationManifest:
    run_id: str
    created_at: str
    code_commit: str
    working_tree_dirty: bool
    dataset_fingerprint: str
    corpus_fingerprint: str
    index_schema_version: int
    retrieval_mode: str
    retrieval_parameters: dict[str, Any]
    semantic_provider: str | None = None
    semantic_model: str | None = None
    semantic_dimension: int | None = None

    COMPATIBILITY_FIELDS: ClassVar[tuple[str, ...]] = (
        "dataset_fingerprint",
        "corpus_fingerprint",
        "index_schema_version",
        "retrieval_mode",
        "retrieval_parameters",
        "semantic_provider",
        "semantic_model",
        "semantic_dimension",
    )

    @classmethod
    def compatible_for_tests(cls, *, corpus_fingerprint: str = "corpus") -> "EvaluationManifest":
        return cls(
            run_id="test-run",
            created_at="2026-01-01T00:00:00Z",
            code_commit="test",
            working_tree_dirty=False,
            dataset_fingerprint="dataset",
            corpus_fingerprint=corpus_fingerprint,
            index_schema_version=1,
            retrieval_mode="fts5",
            retrieval_parameters={"limit": 5},
        )

    def compatibility_mismatches(self, other: "EvaluationManifest") -> list[str]:
        return [
            field
            for field in self.COMPATIBILITY_FIELDS
            if getattr(self, field) != getattr(other, field)
        ]


@dataclass(frozen=True)
class EvaluationReport:
    manifest: EvaluationManifest
    dataset_name: str
    dataset_version: str
    metrics: EvaluationMetrics
    tasks: tuple[ObservedTask, ...]

    @classmethod
    def for_tests(
        cls, manifest: EvaluationManifest, tasks: list[ObservedTask]
    ) -> "EvaluationReport":
        observed = tuple(tasks)
        return cls(manifest, "test", "1", aggregate_metrics(observed), observed)

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ComparisonCount:
    improvements: int
    ties: int
    regressions: int


@dataclass(frozen=True)
class EvaluationComparison:
    baseline_run_id: str
    candidate_run_id: str
    improvements: int
    ties: int
    regressions: int
    transitions: list[str]
    by_class: dict[str, ComparisonCount]
    critical_regressions: list[str]
    allowed_manifest_drift: list[str]

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


def load_dataset(path: Path) -> EvaluationDataset:
    payload = loads_json(Path(path).read_text())
    if not isinstance(payload, dict):
        message = "evaluation dataset must be a JSON object"
        raise TypeError(message)
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        message = "evaluation dataset tasks must be a list"
        raise TypeError(message)
    tasks = tuple(task_from_payload(item) for item in raw_tasks)
    return EvaluationDataset(
        schema_version=str(payload.get("schema_version") or ""),
        dataset_version=str(payload.get("dataset_version") or ""),
        name=str(payload.get("name") or ""),
        tasks=tasks,
    )


def task_from_payload(payload: object) -> EvaluationTask:
    if not isinstance(payload, dict):
        message = "evaluation task must be a JSON object"
        raise TypeError(message)
    raw_expected = payload.get("expected")
    if not isinstance(raw_expected, list):
        message = "evaluation task expected must be a list"
        raise TypeError(message)
    expected = tuple(expected_from_payload(item) for item in raw_expected)
    critical_reason = payload.get("critical_reason")
    return EvaluationTask(
        id=str(payload.get("id") or ""),
        query=str(payload.get("query") or ""),
        task_class=str(payload.get("class") or ""),
        critical=payload.get("critical") is True,
        critical_reason=str(critical_reason) if critical_reason is not None else None,
        expected=expected,
    )


def expected_from_payload(payload: object) -> ExpectedEvidence:
    if not isinstance(payload, dict):
        message = "expected evidence must be a JSON object"
        raise TypeError(message)
    relevance = payload.get("relevance")
    if not isinstance(relevance, int) or isinstance(relevance, bool):
        message = "expected evidence relevance must be an integer"
        raise TypeError(message)
    return ExpectedEvidence(
        evidence_id=str(payload.get("evidence_id") or ""),
        relevance=relevance,
    )


def evaluate_retrieval(
    dataset: EvaluationDataset,
    index_path: Path,
    *,
    limit: int = 5,
    context: int = 0,
    repository_root: Path | None = None,
) -> EvaluationReport:
    if limit < EVALUATION_CUTOFF:
        message = "evaluation limit must be at least 5"
        raise ValueError(message)
    index_path = Path(index_path)
    repo = IndexRepository(index_path)
    observed: list[ObservedTask] = []
    for task in dataset.tasks:
        started = perf_counter()
        rows = repo.search(task.query, limit, context=context)
        latency_ms = (perf_counter() - started) * 1000
        observed.append(observe_task(task, rows, latency_ms))
    manifest = build_manifest(
        dataset,
        index_path,
        limit=limit,
        context=context,
        repository_root=repository_root,
    )
    tasks = tuple(observed)
    return EvaluationReport(
        manifest=manifest,
        dataset_name=dataset.name,
        dataset_version=dataset.dataset_version,
        metrics=aggregate_metrics(tasks),
        tasks=tasks,
    )


def observe_task(
    task: EvaluationTask, rows: list[dict[str, Any]], latency_ms: float
) -> ObservedTask:
    relevance_by_id = {item.evidence_id: item.relevance for item in task.expected}
    retrieved = tuple(
        RetrievedEvidence(
            evidence_id=evidence_id_from_row(row),
            meeting_external_id=str(row["meeting_external_id"]),
            relevance=relevance_by_id.get(evidence_id_from_row(row), 0),
            rank=rank,
        )
        for rank, row in enumerate(rows, start=1)
    )
    primary_ranks = [item.rank for item in retrieved if item.relevance == PRIMARY_RELEVANCE]
    first_primary_rank = min(primary_ranks, default=None)
    expected_meetings = {
        evidence.evidence_id.split("/", 1)[0]
        for evidence in task.expected
        if evidence.relevance > 0
    }
    source_accurate = bool(retrieved and retrieved[0].meeting_external_id in expected_meetings)
    return ObservedTask(
        id=task.id,
        task_class=task.task_class,
        critical=task.critical,
        success=first_primary_rank is not None,
        hit_at_1=float(first_primary_rank is not None and first_primary_rank <= 1),
        hit_at_3=float(first_primary_rank is not None and first_primary_rank <= HIT_AT_THREE),
        hit_at_5=float(first_primary_rank is not None and first_primary_rank <= EVALUATION_CUTOFF),
        reciprocal_rank=1 / first_primary_rank if first_primary_rank else 0.0,
        ndcg=ndcg_at_five(retrieved, task.expected),
        source_accurate=source_accurate,
        source_openings=source_openings(retrieved, first_primary_rank),
        latency_ms=latency_ms,
        retrieved=retrieved,
    )


def aggregate_metrics(tasks: tuple[ObservedTask, ...]) -> EvaluationMetrics:
    if not tasks:
        return EvaluationMetrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    latencies = sorted(task.latency_ms for task in tasks)
    openings = [task.source_openings for task in tasks]
    return EvaluationMetrics(
        hit_at_1=mean(task.hit_at_1 for task in tasks),
        hit_at_3=mean(task.hit_at_3 for task in tasks),
        hit_at_5=mean(task.hit_at_5 for task in tasks),
        mrr=mean(task.reciprocal_rank for task in tasks),
        ndcg=mean(task.ndcg for task in tasks),
        source_accuracy=mean(float(task.source_accurate) for task in tasks),
        median_source_openings=float(median(openings)),
        empty_result_rate=mean(float(not task.retrieved) for task in tasks),
        median_latency_ms=float(median(latencies)),
        p95_latency_ms=nearest_rank_percentile(latencies, 0.95),
    )


def compare_reports(
    baseline: EvaluationReport,
    candidate: EvaluationReport,
    *,
    allow_manifest_drift: set[str] | None = None,
) -> EvaluationComparison:
    allowed = allow_manifest_drift or set()
    unknown_fields = allowed - set(EvaluationManifest.COMPATIBILITY_FIELDS)
    if unknown_fields:
        message = f"unknown manifest drift fields: {', '.join(sorted(unknown_fields))}"
        raise ValueError(message)
    mismatches = baseline.manifest.compatibility_mismatches(candidate.manifest)
    incompatible = [field for field in mismatches if field not in allowed]
    if incompatible:
        message = f"incompatible evaluation manifests: {', '.join(incompatible)}"
        raise ValueError(message)
    baseline_tasks = {task.id: task for task in baseline.tasks}
    candidate_tasks = {task.id: task for task in candidate.tasks}
    if baseline_tasks.keys() != candidate_tasks.keys():
        message = "incompatible evaluation reports: task ids differ"
        raise ValueError(message)
    outcomes = [
        classify_change(baseline_tasks[task_id], candidate_tasks[task_id])
        for task_id in baseline_tasks
    ]
    transitions: list[str] = []
    critical_regressions: list[str] = []
    by_class_outcomes: dict[str, list[str]] = {}
    for task_id, outcome in zip(baseline_tasks, outcomes, strict=True):
        before = baseline_tasks[task_id]
        after = candidate_tasks[task_id]
        by_class_outcomes.setdefault(before.task_class, []).append(outcome)
        if before.success != after.success:
            transitions.append(
                f"{task_id}: {'success' if before.success else 'failure'} -> "
                f"{'success' if after.success else 'failure'}"
            )
        if before.critical and outcome == "regression":
            critical_regressions.append(task_id)
    counts = count_outcomes(outcomes)
    return EvaluationComparison(
        baseline_run_id=baseline.manifest.run_id,
        candidate_run_id=candidate.manifest.run_id,
        improvements=counts.improvements,
        ties=counts.ties,
        regressions=counts.regressions,
        transitions=transitions,
        by_class={key: count_outcomes(value) for key, value in by_class_outcomes.items()},
        critical_regressions=critical_regressions,
        allowed_manifest_drift=sorted(field for field in mismatches if field in allowed),
    )


def save_report(report: EvaluationReport, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x") as stream:
            stream.write(dumps_json(report.as_payload()))
            stream.write("\n")
    except FileExistsError as exc:
        message = f"evaluation report is immutable and already exists: {path}"
        raise ValueError(message) from exc


def load_report(path: Path) -> EvaluationReport:
    payload = loads_json(Path(path).read_text())
    if not isinstance(payload, dict):
        message = "evaluation report must be a JSON object"
        raise TypeError(message)
    manifest = EvaluationManifest(**payload["manifest"])
    metrics = EvaluationMetrics(**payload["metrics"])
    tasks = tuple(observed_task_from_payload(task) for task in payload["tasks"])
    return EvaluationReport(
        manifest=manifest,
        dataset_name=str(payload["dataset_name"]),
        dataset_version=str(payload["dataset_version"]),
        metrics=metrics,
        tasks=tasks,
    )


def observed_task_from_payload(payload: dict[str, Any]) -> ObservedTask:
    retrieved = tuple(
        RetrievedEvidence(
            evidence_id=str(item["evidence_id"]),
            meeting_external_id=str(item["meeting_external_id"]),
            relevance=int(item["relevance"]),
            rank=int(item["rank"]),
        )
        for item in payload["retrieved"]
    )
    return ObservedTask(
        id=str(payload["id"]),
        task_class=str(payload["task_class"]),
        critical=bool(payload["critical"]),
        success=bool(payload["success"]),
        hit_at_1=float(payload["hit_at_1"]),
        hit_at_3=float(payload["hit_at_3"]),
        hit_at_5=float(payload["hit_at_5"]),
        reciprocal_rank=float(payload["reciprocal_rank"]),
        ndcg=float(payload["ndcg"]),
        source_accurate=bool(payload["source_accurate"]),
        source_openings=int(payload["source_openings"]),
        latency_ms=float(payload["latency_ms"]),
        retrieved=retrieved,
    )


def build_manifest(
    dataset: EvaluationDataset,
    index_path: Path,
    *,
    limit: int,
    context: int,
    repository_root: Path | None,
) -> EvaluationManifest:
    root = repository_root or Path.cwd()
    commit = git_output(root, "rev-parse", "HEAD") or "unknown"
    dirty = bool(git_output(root, "status", "--porcelain"))
    with sqlite3.connect(index_path) as conn:
        schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    return EvaluationManifest(
        run_id=str(uuid.uuid4()),
        created_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        code_commit=commit,
        working_tree_dirty=dirty,
        dataset_fingerprint=dataset.fingerprint,
        corpus_fingerprint=corpus_fingerprint(index_path),
        index_schema_version=schema_version,
        retrieval_mode="fts5",
        retrieval_parameters={"limit": limit, "context": context},
    )


def corpus_fingerprint(index_path: Path) -> str:
    with sqlite3.connect(index_path) as conn:
        rows = conn.execute(
            """
            SELECT s.kind, s.path, m.external_id, m.fingerprint, c.external_id,
                   c.kind, c.ordinal, c.fingerprint
            FROM chunks c
            JOIN meetings m ON m.id = c.meeting_id
            JOIN sources s ON s.id = m.source_id
            ORDER BY s.kind, s.path, m.external_id, c.kind, c.ordinal
            """
        ).fetchall()
    return sha256_payload([list(row) for row in rows])


def evidence_id_from_row(row: dict[str, Any]) -> str:
    meeting_id = str(row["meeting_external_id"])
    chunk_external_id = row.get("chunk_external_id")
    if chunk_external_id:
        return f"{meeting_id}/{chunk_external_id}"
    fallback = sha256_payload(
        {
            "kind": row["kind"],
            "ordinal": row["ordinal"],
            "text": row["text"],
        }
    )
    return f"{meeting_id}/{row['kind']}:{row['ordinal']}:{fallback}"


def dataset_payload(dataset: EvaluationDataset) -> dict[str, Any]:
    return {
        "schema_version": dataset.schema_version,
        "dataset_version": dataset.dataset_version,
        "name": dataset.name,
        "tasks": [
            {
                "id": task.id,
                "query": task.query,
                "class": task.task_class,
                "critical": task.critical,
                "critical_reason": task.critical_reason,
                "expected": [asdict(item) for item in task.expected],
            }
            for task in dataset.tasks
        ],
    }


def ndcg_at_five(
    retrieved: tuple[RetrievedEvidence, ...], expected: tuple[ExpectedEvidence, ...]
) -> float:
    actual = [item.relevance for item in retrieved[:EVALUATION_CUTOFF]]
    ideal = sorted((item.relevance for item in expected), reverse=True)[:EVALUATION_CUTOFF]
    ideal_score = discounted_gain(ideal)
    return discounted_gain(actual) / ideal_score if ideal_score else 0.0


def discounted_gain(relevances: list[int]) -> float:
    return sum(
        (2**relevance - 1) / math.log2(rank + 1) for rank, relevance in enumerate(relevances, 1)
    )


def source_openings(
    retrieved: tuple[RetrievedEvidence, ...], first_primary_rank: int | None
) -> int:
    relevant_slice = retrieved[:first_primary_rank] if first_primary_rank else retrieved
    return len({item.meeting_external_id for item in relevant_slice})


def classify_change(before: ObservedTask, after: ObservedTask) -> str:
    before_score = (int(before.success), before.ndcg)
    after_score = (int(after.success), after.ndcg)
    if after_score > before_score:
        return "improvement"
    if after_score < before_score:
        return "regression"
    return "tie"


def count_outcomes(outcomes: list[str]) -> ComparisonCount:
    return ComparisonCount(
        improvements=outcomes.count("improvement"),
        ties=outcomes.count("tie"),
        regressions=outcomes.count("regression"),
    )


def nearest_rank_percentile(values: list[float], quantile: float) -> float:
    rank = max(1, math.ceil(quantile * len(values)))
    return float(values[rank - 1])


def mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items)


def sha256_payload(payload: object) -> str:
    return hashlib.sha256(dumps_json_bytes(payload)).hexdigest()


def git_output(root: Path, *args: str) -> str:
    git = shutil.which("git")
    if git is None:
        return ""
    result = subprocess.run(  # noqa: S603
        [git, *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""
