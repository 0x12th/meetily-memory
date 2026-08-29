# Development

See [Retrieval evaluation](evaluation.md) for the reproducible FTS5 quality baseline and paired
comparison workflow.

See [Core contracts and persistent user state](contracts.md) for v1/v2 compatibility and the
schema-v4 user-state migration boundary.

An explicit `--index` without `MEETILY_MEMORY_DATA_DIR` uses `settings.json` beside that index.
This keeps temporary CLI workspaces and tests from changing the default desktop configuration.
Set `MEETILY_MEMORY_DATA_DIR` when an alternate index should intentionally share a data directory.

Development setup:

```bash
uv sync
```

Run the test suite:

```bash
uv run pytest -q
```

Run all quality checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check --error all
```

Build the package:

```bash
uv build
```

Build and validate a macOS release archive:

```bash
scripts/build-binary.sh
scripts/package-release-asset.sh v0.7.0 macos-arm64
scripts/smoke-release-asset.py \
  target/release-assets/meetily-memory-v0.7.0-macos-arm64.tar.gz \
  v0.7.0
```

The release smoke always tests the exact `.tar.gz`, not `dist/`. It extracts into an empty
system temporary directory, runs `mm` from a clean working directory without the checkout or
virtual-environment import paths, and uses a synthetic SQLite source plus an isolated
`MEETILY_MEMORY_DATA_DIR`. The adjacent `.tar.gz.smoke.json` file records the machine-readable
result. Generate and upload checksums only after this command succeeds. Release CI preserves the
architecture-specific smoke JSON even when smoke fails, but a failed smoke blocks checksum
creation, release-asset upload, and the publish job.

Repository boundary:

`IndexRepository` is a compatibility facade for the public core API and legacy
call sites. New low-level persistence behavior should live in concrete
repositories such as search, meetings, knowledge, entities, or task status.
New user-facing workflows should be added in `core`, not as pass-through
methods on the facade.

Enable pre-commit hooks:

```bash
uv run pre-commit install
```
