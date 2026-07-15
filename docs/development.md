# Development

See [Retrieval evaluation](evaluation.md) for the reproducible FTS5 quality baseline and paired
comparison workflow.

See [Core contracts and persistent user state](contracts.md) for v1/v2 compatibility and the
schema-v4 user-state migration boundary.

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
