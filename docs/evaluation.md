# Retrieval evaluation

Meetily Memory includes a reproducible meeting-level retrieval evaluation runner. The public
fixture in `tests/fixtures/evaluation/synthetic_dataset.json` verifies the v2 dataset format,
stable meeting and evidence keys, tag-only tasks, metrics, and comparison rules in CI. It is
synthetic and must not be used as evidence of retrieval quality. Existing v1 datasets are
migrated to v2 when read by deriving meetings from their evidence IDs.

Real queries, relevance labels, reports, and manual reviews belong under the ignored
`.docs/eval/` directory. A real dataset should contain 30–50 tasks from actual use, cover all
supported task classes, declare `expected_meetings`, optionally allow multiple primary (`2`) and
supporting (`1`) evidence fragments, and record a reason for every critical task before any
candidate strategy is evaluated. A tag-only task has one or more `expected_meetings` and an
empty `expected` evidence list.

Run the unchanged FTS5 path against an existing index:

```bash
uv run scripts/evaluate-retrieval.py \
  .docs/eval/tasks.v2.json \
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
  .docs/eval/tasks.v2.json \
  --index .docs/eval/index.sqlite \
  --baseline .docs/eval/baseline.json \
  --output .docs/eval/candidate.json
```

Evaluate the explicit hybrid RRF experiment only after semantic embeddings have been indexed:

```bash
uv run scripts/evaluate-retrieval.py \
  .docs/eval/tasks.v2.json \
  --index .docs/eval/index.sqlite \
  --baseline .docs/eval/baseline.json \
  --output .docs/eval/hybrid.json \
  --retrieval hybrid \
  --embedding-provider ollama \
  --allow-drift retrieval_mode \
  --allow-drift retrieval_parameters \
  --allow-drift semantic_provider \
  --allow-drift semantic_model \
  --allow-drift semantic_dimension
```

The semantic CLI has been removed from the product. Offline runners verify that every current
chunk has matching metadata and a vector row for the selected provider, model, and dimensions.
They reject an incomplete or stale semantic index instead of allowing the regression gate to
certify an FTS-plus-tags fallback as hybrid.

Hybrid evaluation never creates embeddings during a query. It records the provider, model,
dimension, RRF constant, candidate multiplier, FTS/semantic/tag weights, and warmup in the
manifest. Fusion happens after each source is collapsed to meetings; source ranks and the fused
score remain diagnostic trace data and are not added to `MeetingSearchResult`. When a compatible
baseline is supplied, the runner includes the regression-gate result in its JSON output. A
successful single comparison does not change standard search or expose hybrid controls in the
public CLI.

For the frozen multilingual offline spike, first pull the two local candidates and then run one
command:

```bash
ollama pull qwen3-embedding:0.6b
ollama pull bge-m3
uv run scripts/evaluate-semantic-search.py \
  .docs/eval/tasks.v2.json \
  --index .docs/eval/index.sqlite \
  --output-dir .docs/eval/semantic-offline-YYYYMMDD
```

This runner keeps each incompatible embedding configuration in a separate copied index. It uses
the Qwen model-card query instruction and no document instruction; BGE-M3 uses identity transforms
for both explicit roles because its model card says query instructions are not required. Candidate
manifests include the Ollama model digest, dimensions, both role instructions, chunk fingerprint,
strict semantic-index fingerprint, full embedding refresh time, and vector-index size. The fixed
RRF weights are not tuned on the evaluated queries. The runner refuses to overwrite reports and
always labels this old-dataset run as an offline spike rather than the real-failure product gate.

Reports are immutable: the runner refuses to overwrite an existing output path. Automatic
comparison is rejected when the dataset, corpus, tag-state fingerprint, index schema, retrieval
mode or parameters, semantic provider/model/dimension/digest, role instructions, semantic-index
fingerprint, or chunk fingerprint differ. Code commits and dirty-tree state remain recorded for
traceability but do not by themselves make two retrieval runs incompatible.

An intentional migration can be analyzed with an explicit drift field, for example
`--allow-drift index_schema_version`. The comparison records the allowed mismatch; this mode is
for migration analysis and must not be presented as an ordinary compatible comparison.

The report includes hit@1/3/5, MRR, nDCG, source accuracy, source openings, empty-result rate,
median and p95 latency, per-task observations, paired improvements/ties/regressions, success
transitions, class-level counts, and critical regressions. Failed real tasks still require a
manual source review before a retrieval change can be accepted.
