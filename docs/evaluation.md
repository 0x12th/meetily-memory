# Retrieval evaluation

Meetily Memory includes a reproducible FTS5 evaluation runner. The public fixture in
`tests/fixtures/evaluation/synthetic_dataset.json` verifies the dataset format, stable evidence
keys, metrics, and comparison rules in CI. It is synthetic and must not be used as evidence of
retrieval quality.

Real queries, relevance labels, reports, and manual reviews belong under the ignored
`.docs/eval/` directory. A real dataset should contain 30–50 tasks from actual use, cover all
supported task classes, allow multiple primary (`2`) and supporting (`1`) evidence fragments,
and record a reason for every critical task before any candidate strategy is evaluated.

Run the unchanged FTS5 path against an existing index:

```bash
uv run scripts/evaluate-retrieval.py \
  .docs/eval/tasks.v1.json \
  --index .docs/eval/index.sqlite \
  --output .docs/eval/baseline.json
```

To evaluate the explicit neighboring-context mode without changing default retrieval, add
`--context 1` and compare with `--allow-drift retrieval_parameters`. Lexical matches keep their
original order; adjacent chunks are appended afterward, so the experiment cannot silently
replace or reorder the standard top results.

Compare a candidate with a compatible baseline:

```bash
uv run scripts/evaluate-retrieval.py \
  .docs/eval/tasks.v1.json \
  --index .docs/eval/index.sqlite \
  --baseline .docs/eval/baseline.json \
  --output .docs/eval/candidate.json
```

Reports are immutable: the runner refuses to overwrite an existing output path. Automatic
comparison is rejected when the dataset, corpus, index schema, retrieval mode or parameters,
or semantic provider/model/dimension differ. Code commits and dirty-tree state remain recorded
for traceability but do not by themselves make two retrieval runs incompatible.

An intentional migration can be analyzed with an explicit drift field, for example
`--allow-drift index_schema_version`. The comparison records the allowed mismatch; this mode is
for migration analysis and must not be presented as an ordinary compatible comparison.

The report includes hit@1/3/5, MRR, nDCG, source accuracy, source openings, empty-result rate,
median and p95 latency, per-task observations, paired improvements/ties/regressions, success
transitions, class-level counts, and critical regressions. Failed real tasks still require a
manual source review before a retrieval change can be accepted.
