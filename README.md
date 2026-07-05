# Meetily Memory

Search.
Context.
Ask.

Meetily Memory turns local Meetily meetings into a private, source-backed memory
you can search, understand, and ask.

It is for the moment when:

> "I remember we discussed this."

...is not enough.

You need the original decision, the remaining risks, the people involved, and
the meeting that proved it.

Meetily Memory never modifies the Meetily database and never requires a cloud
service.

---

## What You Can Do

Everyday flow:

Find an old discussion:

```bash
mm s "migration risk"
```

Build clean context for ChatGPT, Claude, or Codex:

```bash
mm c "what did we decide about the migration?"
```

Ask your meeting history directly:

```bash
mm ask "what is still open?"
```

Open the original meeting folder:

```bash
mm open 12
```

Refresh the local index from Meetily:

```bash
mm refresh
```

Advanced views and integrations:

See everything known about a topic:

```bash
mm topic "migration"
```

Sync a managed Obsidian knowledge vault:

```bash
mm obsidian init
```

---

## Install

On macOS:

```bash
brew tap 0x12th/meetily-memory
brew install meetily-memory
```

The CLI is available as:

```bash
mm
meetily-memory
```

---

## First Run

```bash
mm init
```

`mm init` automatically:

- discovers the local Meetily database;
- creates the private search index;
- performs the first refresh;
- offers to enable automatic index refreshes.

Nothing optional is enabled without asking.

If automatic refreshes are enabled, your local index stays up to date
automatically.

If discovery fails:

```bash
mm doctor
```

---

## Everyday Usage

Most users only need a few commands.

Search:

```bash
mm s "migration risk"
```

Use this when you want source snippets and meeting ids.

Build LLM context:

```bash
mm c "what context matters for the migration plan?"
```

Use this when you want Markdown you can paste into ChatGPT, Claude, or Codex.

Ask meeting memory:

```bash
mm ask "what remains unresolved?"
```

Use this when you want Meetily Memory to prepare the context and answer through
the configured provider. In manual mode it prints the prompt instead.

Explore a topic:

```bash
mm topic "migration"
```

Use this when you want an advanced dossier: related meetings, decisions, tasks,
risks, questions, people, and sources.

Open the original meeting:

```bash
mm open 12
```

If automatic refreshes are disabled, refresh the index manually:

```bash
mm refresh
```

Update the installed utility itself:

```bash
mm update
```

---

## Obsidian

Meetily Memory can maintain a managed Obsidian knowledge vault.

Configure it once:

```bash
mm obsidian init
```

After setup, notes can be synchronized automatically after index refreshes.

Manual synchronization is also available:

```bash
mm obsidian sync
```

The vault contains managed notes such as:

```text
Topics/
Meetings/
People/
Tasks/
Decisions/
Risks/
Questions/
```

Managed notes include:

```html
<!-- meetily-memory:managed -->
```

Only managed files are updated.
Your own notes are never overwritten.

---

## LLM Integration

Configure an LLM once:

```bash
mm llm init
```

Supported providers:

- Manual (prepare context for ChatGPT or Claude)
- Ollama (local models)

Then simply ask:

```bash
mm ask "what is still open?"
```

Meetily Memory retrieves the relevant evidence first, then sends only the
selected context to the configured provider.

---

## Principles

- Local-first.
- Private by default.
- Read-only Meetily database.
- Evidence before summaries.
- Small public CLI.
- Search → Context → Ask.

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
      CLI          Obsidian      LLM / Ask
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

The internal knowledge layer powers `mm topic`, `mm ask`, Obsidian
synchronization, and the experimental MCP adapter.

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
