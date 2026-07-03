# Meetily Memory

Local-first CLI for indexing and searching Meetily meeting history. Meetily
Memory is a local memory layer on top of Meetily: it imports meeting history,
normalizes it into a private SQLite index, and provides fast search through a
small command-line interface.

It does not replace Meetily, record audio, transcribe meetings, or mutate the
Meetily database.

## Principles

- Local-first: no required cloud services.
- Read-only upstream: Meetily data is never modified.
- Minimal infrastructure: no Docker, Postgres, queues, or background services.
- One local CLI: `mm`.
- Fast local search with SQLite and FTS5.
- Plugin/MCP-ready architecture for later versions.

## Data Contract

Meetily is treated as an upstream source. Meetily Memory opens the source
database read-only and writes all derived state into its own `index.sqlite`.

```text
source.kind = meetily_sqlite
source.path = /path/to/meeting_minutes.sqlite
meeting.external_id = Meetily meetings.id
chunk.external_id = Meetily transcripts.id
fingerprint = hash(normalized Meetily row data)
```

Derived data stored in `index.sqlite` includes normalized meetings, searchable
chunks, best-effort people metadata, artifacts, scan history, the FTS5 index,
and future embeddings or plugin state.

## Source Discovery

Use `--source` when the Meetily database is not in a default app-data location:

```bash
mm doctor --source /path/to/meeting_minutes.sqlite
mm scan --source /path/to/meeting_minutes.sqlite
```

The source may be a Meetily SQLite file or a directory that contains
`meeting_minutes.sqlite` or the legacy `meeting_minutes.db`. Discovery is
best-effort and platform-aware; explicit `--source` is the stable path.

## v1 Quick Start

```bash
uv sync
uv run mm doctor --source /path/to/meeting_minutes.sqlite
uv run mm scan --source /path/to/meeting_minutes.sqlite
uv run mm s "pricing decision"
uv run mm c "what did we decide about pricing?"
uv run mm ls
uv run mm last
uv run mm last --person Robert
uv run mm p Robert
```

By default, Meetily Memory stores its index in the platform data directory via
`platformdirs`. You can override it for any command:

```bash
uv run mm --index /path/to/index.sqlite scan --source /path/to/meeting_minutes.sqlite
```

## CLI

```bash
mm doctor
mm scan
mm s "query"
mm c "question"
mm ls
mm last
mm last --person "Robert"
mm p "Robert"
mm open <meeting-id>
```

`people` support in v1 is best-effort. It uses available speaker and metadata
values, and may fall back to text search when Meetily does not provide reliable
participant identity.

## Development

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run ty check --error all
uv run pytest -q
uv build
```

Pre-commit hooks are configured in `.pre-commit-config.yaml`. Install them after
initializing the git repository:

```bash
uv run pre-commit install
```
