# Meetily Memory

Private local memory for your Meetily meetings.

Meetily Memory indexes your local Meetily database into a separate SQLite
knowledge layer, then helps you find decisions, tasks, risks, questions, people,
projects, and paste-ready context across many meetings.

It does not replace Meetily, record meetings, upload data, call an LLM, or
modify the Meetily database.

## What It Helps With

- **Search past meetings** with fast local FTS: `mm s "pricing decision"`.
- **Build LLM context** you can paste into ChatGPT or Claude: `mm c "what did we decide about pricing?"`.
- **Recover source-backed memory** for topics, projects, people, decisions, tasks, risks, and questions.
- **Track unresolved work locally** with task status overrides that keep source evidence.
- **Export knowledge** to Obsidian, Markdown, Gbrain-style JSONL, Spotlight, or a task-tracker draft.
- **Expose local memory to agents** through a thin MCP server over the same Core API.

## Why

Recording meetings is easy. Remembering what happened months later is not.

After enough meetings, you need answers like:

- What did we decide about the migration?
- What did Vladimir promise last week?
- Which risks keep coming up?
- What context should I give an AI agent before asking it to help?
- Where did this claim come from?

Meetily Memory keeps the workflow local and evidence-first:

```text
Meetily history
        |
        v
Local SQLite index
        |
        v
Search / knowledge / exports / agents
```

## Install

On macOS:

```bash
brew tap 0x12th/meetily-memory
brew install meetily-memory
```

The CLI is available as both:

```bash
meetily-memory --help
mm --help
```

## First Run

Meetily Memory discovers common Meetily database locations automatically.

```bash
mm doctor
mm update
```

If your Meetily database is elsewhere:

```bash
mm update --source /path/to/meeting_minutes.sqlite
```

Then try:

```bash
mm s "migration risk"
mm c "what risks did we discuss for the migration?"
mm topic "migration"
mm tasks
```

## Core Workflows

### Search And Context

```bash
mm s "pricing decision"
mm c "what did we decide about pricing?"
```

`mm s` is for fast lookup. `mm c` builds Markdown context with citations, ready
to paste into ChatGPT, Claude, or another LLM.

### Local Knowledge

```bash
mm decisions
mm tasks
mm risks
mm questions
mm topic "migration"
mm person "Vladimir"
mm project "migration"
mm graph "migration" --json
```

Structured results are heuristic signals, not an automatic truth model. The CLI
keeps meeting and chunk evidence visible so you can verify important claims.

### Exports

```bash
mm export obsidian "migration" --output ~/Obsidian/Meetily
mm export markdown "migration" --output ./migration.md
mm export gbrain "migration" --output ./migration.gbrain.jsonl
mm export task-draft "migration risks" --output ./task.md
mm spotlight export
```

Exports are file adapters over the Core API. They do not sync in the background
or write back to Meetily, Jira, GitHub, or other external systems.

### Agent Access

```bash
mm mcp serve
```

The MCP server is a thin adapter over the same source-backed Core API used by
the CLI and exports.

## Principles

- **Local-first**: no required cloud services.
- **Private by default**: no uploads and no required LLM calls.
- **Read-only upstream**: the Meetily database is never modified.
- **Evidence-first**: useful output should point back to meeting and chunk sources.
- **Minimal infrastructure**: SQLite, FTS5, optional sqlite-vec, no external graph database.

## Documentation

- [Getting started](docs/getting-started.md)
- [Command reference](docs/commands.md)
- [Knowledge layer and Core API](docs/knowledge-layer.md)
- [Exports and integrations](docs/integrations.md)
- [Semantic search](docs/semantic-search.md)
- [Development](docs/development.md)

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).
