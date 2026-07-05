# Meetily Memory

Search.
Context.
Verify.

Meetily Memory turns local Meetily meetings into a private, source-backed memory
you can search, explore, and turn into LLM-ready context.

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

- discovers the local Meetily database;
- creates a private search index;
- performs the initial refresh;
- offers to enable automatic refreshes.

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

Build LLM-ready context:

```bash
mm c "what did we decide about the migration?"
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

### Context

```bash
mm c "what did we decide about the migration?"
```

Build clean Markdown context ready to paste into ChatGPT, Claude, or Codex.

### Topics

```bash
mm t "migration"
```

Explore everything known about a topic, including related meetings,
heuristically extracted decisions, risks, tasks, questions, people, and
supporting evidence.

This is an evidence view rather than an LLM-generated answer.

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

---

## Optional Features

### Obsidian

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

- Topics
- Meetings
- People
- Tasks
- Decisions
- Risks
- Questions

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

- Manual (prepare context for ChatGPT or Claude)
- Ollama (local models)

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

`mm mcp serve` exposes Meetily Memory to external agents.

MCP support is optional for `pip` and `uv` installs via
`meetily-memory[mcp]`.

If the extra is not installed, the command prints installation
instructions instead of starting a server.

---

## Principles

- Local-first.
- Private by default.
- Read-only Meetily database.
- Evidence before summaries.
- Small public CLI.
- Search → Context → Verify.

---

## Architecture

```text
                Meetily SQLite
                      │
                      ▼
          Meetily Memory index.sqlite
                      │
      ┌───────────────┼────────────────┐
      │               │                │
      ▼               ▼                ▼
   FTS Search   Structured Memory   Semantic Search
                      │
                      ▼
              Topic Knowledge Layer
                      │
        ┌─────────────┼──────────────┐
        ▼             ▼              ▼
     CLI       Optional Obsidian   LLM Answering
                                     │
                                     ▼
                              Experimental MCP
```

Meetily Memory stores only derived local state:

- normalized meetings and chunks;
- SQLite FTS index;
- decisions, action items, risks, and questions;
- topic relationships;
- optional semantic embeddings;
- local application settings.

The knowledge layer powers `mm t`, Obsidian synchronization, LLM workflows,
and the MCP adapter.

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
