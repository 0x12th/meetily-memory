# Meetily Memory

Search.
Context.
Verify.

Meetily Memory turns local Meetily meetings into a private, source-backed memory
for search, context, and verification.

It is for the moment when:

> "I remember we discussed this."

...is not enough.

You need the original decision, the remaining risks, the people involved, and
the meeting that proved it.

Meetily Memory never modifies the Meetily database and never requires a cloud
service.

---

## Install

On macOS:

```bash
brew tap 0x12th/meetily-memory
brew install meetily-memory
```

The CLI is available as:

```text
mm
meetily-memory
```

---

## Quick Start

Initialize Meetily Memory:

```bash
mm init
```

`mm init` automatically:

* discovers the local Meetily database;
* creates a private search index;
* performs the initial refresh;
* offers to enable automatic refreshes.

Nothing optional is enabled without asking.

If discovery fails:

```bash
mm doctor
```

Typical workflow:

Search meetings:

```bash
mm s "migration risk"
```

Example output:

```text
#10 Meeting 2026-07-06

12:56:36 | chunk #3863 | open: mm open 10
If I write to the database, I must also publish to Kafka...

12:56:42
Pattern outbox.
```

Show neighboring context around each hit:

```bash
mm s "migration risk" --context 2
```

Build LLM-ready context:

```bash
mm c "what did we decide about the migration?"
```

Add neighboring excerpts only when the direct matches are insufficient:

```bash
mm c "what did we decide about the migration?" --context 2
```

Explore a topic:

```bash
mm t "migration"
```

Open the original meeting:

```bash
mm open 12
```

If automatic refreshes are disabled:

```bash
mm refresh
```

Update the installed utility:

```bash
mm update
```

---

## Core Commands

### Search

```bash
mm s "migration risk"
```

Search indexed meetings and return matching source snippets.

Use `--context N` when the matching snippet is too short:

```bash
mm s "migration risk" --context 2
```

### Context

```bash
mm c "what did we decide about the migration?"
```

Build clean Markdown context ready to paste into ChatGPT, Claude, or Codex.
Neighbor expansion is explicit through `--context N`; direct lexical matches are the default.

### Topics

```bash
mm t "migration"
```

Experimental. Explore a topic as search results grouped into an evidence-backed
dossier: summary, related meetings, possible decisions, possible risks,
possible tasks, possible questions, and supporting excerpts.

Topic output is not a knowledge-graph oracle or an LLM-generated answer. If
structured memory has no matches, it still shows relevant evidence from search.
Topic expansion uses stored aliases, not built-in term dictionaries:

```bash
mm t "kafka" --alias "кафка" --alias "broker"
```

The CLI language is stable across commands. Configure it explicitly when needed:

```bash
mm config language ru
mm config language auto
```

### Open

```bash
mm open 12
```

Open the original Meetily meeting.

### Refresh

```bash
mm refresh
```

Refresh the local index when automatic refreshes are disabled.

### Move Or Restore The Meetily Database

Select a different validated Meetily database as a new source:

```bash
mm config source /path/to/meeting_minutes.sqlite
```

If the same database was moved or restored from a copy, preserve its source identity and
user-owned state explicitly:

```bash
mm config source /path/to/meeting_minutes.sqlite --rebind
```

Rebinding succeeds only after the new database passes schema validation and shares stable
meeting IDs with the selected source.

---

## Optional Features

### Obsidian (Experimental)

Meetily Memory can maintain a managed Obsidian knowledge vault.

Configure it once:

```bash
mm obsidian init
```

Synchronize manually when needed:

```bash
mm obsidian sync
```

Managed notes include:

* Topics
* Meetings
* People
* Tasks
* Decisions
* Risks
* Questions

Managed files contain:

```html
<!-- meetily-memory:managed -->
```

Only managed notes are updated.

Your own notes are never overwritten.

---

### LLM Answering (Experimental)

Configure a provider:

```bash
mm llm init
```

Supported providers:

* Manual (prepare context for ChatGPT or Claude)
* Ollama (local models)

The `mm ask` command is intentionally hidden while this workflow matures.

For now, `mm c` is the recommended interface.

Compatibility command:

```bash
mm ask "what is still open?"
```

Meetily Memory always retrieves supporting evidence before preparing or sending
context to an LLM.

---

### MCP (Experimental)

```bash
mm mcp serve
```

Expose Meetily Memory to external agents.

MCP support is optional for `pip` and `uv` installs via
`meetily-memory[mcp]`.

If the extra is not installed, the command prints installation instructions
instead of starting a server.

---

## Principles

* Search-first public CLI.
* Evidence before summaries.
* Search → Context → Verify.
* Local-first.
* Private by default.
* Read-only Meetily database.

---

## Architecture

```text
                Meetily SQLite
                      │
                      ▼
          Meetily Memory index.sqlite
                      │
      ┌───────────────┼────────────────────┐
      │               │                    │
      ▼               ▼                    ▼
   FTS Search   Source Context      Structured Signals
    stable         stable              experimental
      │                                   │
      ├───────────────┬───────────────────┤
      ▼               ▼                   ▼
     CLI       Topic Summary       Semantic Search
    stable      experimental        experimental
                      │
        ┌─────────────┼──────────────┐
        ▼             ▼              ▼
  Obsidian       LLM Answering      MCP
 experimental    experimental   experimental
```

Meetily Memory stores only derived local state:

* normalized meetings and chunks;
* SQLite FTS index;
* experimental decisions, action items, risks, and questions;
* experimental topic relationships;
* optional semantic embeddings;
* local application settings.

The stable path is search, context, and source verification. The experimental
knowledge layers power `mm t`, Obsidian synchronization, LLM workflows, and the
MCP adapter.

---

## Development

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run ty check --error all
uv run pytest -q
uv build
```

---

## License

Licensed under the Apache License 2.0.

See [LICENSE](LICENSE).
