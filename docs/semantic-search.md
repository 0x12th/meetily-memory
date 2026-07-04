# Semantic Search

Semantic search is optional and experimental.

Ollama is not required for ordinary FTS search with `mm s` or context building
with `mm c`.

Configure a local embedding model once:

```bash
ollama pull nomic-embed-text

mm semantic setup \
  --provider ollama \
  --model nomic-embed-text
```

Then search semantically:

```bash
mm semantic index
mm sem "migration blockers"
```

You can inspect the saved configuration at any time:

```bash
mm semantic setup --show
```

To use another local Ollama model:

```bash
ollama pull mxbai-embed-large

mm semantic setup \
  --provider ollama \
  --model mxbai-embed-large
```

A deterministic `hash` provider is also available for diagnostics and testing:

```bash
mm semantic setup --provider hash
```

`hash` is a dependency-free baseline and is not expected to outperform FTS5.

FTS remains the default retrieval layer. Semantic search is experimental until
it consistently improves retrieval quality on real-world query sets.

Environment variables remain available for automation and debugging:

- `MM_EMBEDDING_PROVIDER`
- `MM_OLLAMA_URL`
- `MM_OLLAMA_MODEL`

For interactive use, `mm semantic setup` is the recommended workflow.
