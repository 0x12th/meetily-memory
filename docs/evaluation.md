# Retrieval evaluation

The repository includes a reproducible developer-only evaluation runner for the current lexical
meeting-search path. Its implementation lives under `scripts/` and is not part of the installed
`meetily_memory` package. The public synthetic fixture verifies dataset parsing, stable meeting and
evidence identities, metrics, and report comparison behavior; it is not evidence of real retrieval
quality.

Private real-corpus inputs and reports belong under the ignored `.docs/eval/` directory.

Run a baseline against an existing index:

```bash
uv run scripts/evaluate-retrieval.py \
  .docs/eval/cases.json \
  --index .docs/eval/index.sqlite \
  --output .docs/eval/baseline.json
```

Evaluate explicit neighboring transcript context without changing the product default:

```bash
uv run scripts/evaluate-retrieval.py \
  .docs/eval/cases.json \
  --index .docs/eval/index.sqlite \
  --context 1 \
  --output .docs/eval/context-1.json
```

Compare a candidate with a compatible baseline:

```bash
uv run scripts/evaluate-retrieval.py \
  .docs/eval/cases.json \
  --index .docs/eval/index.sqlite \
  --baseline .docs/eval/baseline.json \
  --output .docs/eval/candidate.json
```

Reports are immutable: the runner refuses to overwrite an existing output path. Comparisons reject
incompatible datasets, corpora, tag state, index schema, retrieval mode, or retrieval parameters
unless the changed field is explicitly allowed with `--allow-drift`.

The report includes ranking, source accuracy, source openings, empty-result rate, latency, paired
changes, and critical regressions. Real failures still require manual review against the source.
