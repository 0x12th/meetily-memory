# Development

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

Enable pre-commit hooks:

```bash
uv run pre-commit install
```
