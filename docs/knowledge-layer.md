# Knowledge Layer And Core API

Meetily Memory turns many meetings into a local knowledge layer while preserving
source evidence.

```text
Meetily history
        |
        v
Local index.sqlite
        |
        v
Knowledge nodes, structured signals, relations
        |
        v
CLI, exports, MCP, future integrations
```

## Data Model

Meetily is always treated as a read-only upstream source.

Meetily Memory never modifies the Meetily database. Instead, it builds and
maintains its own local `index.sqlite`.

The local index contains:

- normalized meetings;
- searchable transcript chunks;
- people metadata, best effort;
- structured meeting entities;
- knowledge nodes and source-backed relation edges;
- topic aliases;
- local task status overrides;
- local scan history;
- local semantic embeddings;
- SQLite FTS5 and sqlite-vec indexes.

Structured meeting entities currently include:

- decisions;
- action items;
- risks;
- open questions.

These are structured signals extracted by local heuristics, not a verified fact
database. Use the source evidence shown by the CLI when accuracy matters.

## Trust Model

The knowledge layer is a SQLite projection over source-backed records, not a
standalone graph database. `mm topic` and `mm graph` create local topic links
from matching cited signals, while complex inferred relations remain out of the
fact model until they have explicit evidence or manual review.

Current priorities:

- source-backed output over automatic truth;
- simple local SQLite storage over an external graph database;
- candidates for inferred relations before facts;
- stable JSON contracts before heavier integrations.

## Core API

The CLI uses `meetily_memory.core.MeetilyMemoryCore` for retrieval, context, and
memory commands. This keeps CLI rendering separate from the stable data contract
future adapters use.

Each core response has:

- `contract_version`;
- `kind`;
- `data`.

The current contract version is:

```text
meetily-memory.core.v1
```

Example:

```python
from pathlib import Path

from meetily_memory.core import MeetilyMemoryCore

core = MeetilyMemoryCore(Path("index.sqlite"))
payload = core.build_context("Who owns migration risks?").as_payload()
print(payload["data"]["markdown"])
```

The Core API is intentionally local-only and does not start MCP, plugin, or
external integration runtimes.

## MCP Server

Meetily Memory includes a local MCP server as a thin adapter over the Core API.
It does not keep a separate database or implement its own retrieval logic.

Run it over stdio:

```bash
mm mcp serve
```

Use another index if needed:

```bash
mm --index /path/to/index.sqlite mcp serve
```

Initial MCP tools:

- `search`
- `get_meeting`
- `build_context`
- `get_person`
- `get_project`
- `get_topic`
- `get_related`
- `get_timeline`
- `get_decisions`
- `get_tasks`
- `get_risks`
- `get_questions`

Every tool returns the same versioned Core API envelope with source-backed
payloads where evidence is available.
