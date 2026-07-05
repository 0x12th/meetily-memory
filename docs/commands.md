# Command Reference

This page describes the public command model after scope narrowing.

## Core

| Command | Public role |
|---|---|
| `mm init` | First-run setup. Discovers the Meetily DB, creates `index.sqlite`, runs the first refresh, and asks before enabling automatic index refreshes. |
| `mm refresh` | Main manual index refresh. Reads the Meetily DB, updates the local index, and rebuilds structured memory. If semantic search or Obsidian are configured, it also refreshes those derived layers. |
| `mm update` | Updates the installed `meetily-memory` utility through Homebrew. |
| `mm status` | Short system state: Meetily DB path, index path, last refresh, autosync, Obsidian, LLM, and semantic status. |
| `mm doctor` | Diagnostics only. Checks Meetily DB access/schema, SQLite/FTS5/sqlite-vec support, index permissions, and config. It does not change state. |

## Search And Context

| Command | Public role |
|---|---|
| `mm s "migration risk"` | Fast FTS search over indexed meetings. Returns meeting id, title, chunk id, timestamp/source, and enough evidence to open the source. |
| `mm open 12` | Opens the meeting folder so the original Meetily record can be inspected. |
| `mm open 12 --source` | Opens the indexed source file/path. |
| `mm open 12 --print-path` | Prints the default meeting folder path without opening it. |
| `mm c "what did we decide about migration?"` | Builds paste-ready Markdown context with sources for ChatGPT, Claude, Codex, or another LLM. Use when you want to copy context elsewhere. |
| `mm t "migration"` | Shows what is known about a topic: related meetings, heuristic decisions/tasks/risks/questions, people, and source links. It is a source-backed dossier, not an LLM answer. Headings follow indexed meeting language metadata when available. |

## Optional: Semantic Search

| Command | Public role |
|---|---|
| `mm semantic init` | Configures the embedding provider, such as Ollama or a deterministic hash diagnostic baseline. Settings are stored in the main app settings file. |
| `mm semantic index` | Explicitly builds or refreshes embeddings for chunks. `mm refresh` also updates embeddings once semantic search is configured. |
| `mm sem "migration blockers"` | Semantic search. If embeddings are missing, it asks the user to run `mm semantic index`. |

## Optional: LLM Setup

| Command | Public role |
|---|---|
| `mm llm init` | Configures optional local LLM settings. Initial modes are `manual` and `ollama`; `agent` is reserved for later. |

The answer path is not part of the everyday CLI yet. Prefer `mm c` for
source-backed prompt/context handoff until local answering has a reliable
provider setup, source-grounding checks, and a clear difference from context
export.

## Optional: Obsidian

| Command | Public role |
|---|---|
| `mm obsidian init` | Configures the vault path, folder, and whether to sync after every `mm refresh`. |
| `mm obsidian sync` | Creates or updates the managed Obsidian note network. |
| `mm obsidian status` | Shows Obsidian settings and the last sync state. |

The default vault path is `~/Documents/Obsidian`. The default folder is
`Meetily Memory`.

`mm obsidian sync` should maintain:

```text
Topics/
Meetings/
People/
Tasks/
Decisions/
Risks/
Questions/
```

Managed notes use:

```html
<!-- meetily-memory:managed -->
```

## Automatic Refreshes

| Command | Public role |
|---|---|
| `mm autosync start` | Installs or starts the background refresh job. On macOS this is launchd; on Linux it is systemd when available. |
| `mm autosync stop` | Disables automatic refreshes and removes generated launchd/systemd files when present. |
| `mm autosync status` | Shows whether automatic refreshes are enabled and when they last ran. |

The background cycle runs `mm refresh`, then semantic indexing if configured, then
Obsidian sync if configured.

There should be no separate public watch command.

## Low-Level And Advanced

| Command | Role |
|---|---|
| `mm scan` | Low-level Meetily DB indexing for debugging and tests. Ordinary users use `mm refresh`. |
| `mm analyze` | Rebuilds structured memory manually for debug or repair. |
| `mm db status` | Shows schema version and local index internals. |
| `mm ask ...` | Hidden experimental compatibility command. In manual mode it prints a prompt, which overlaps with `mm c`; do not treat it as a core user workflow yet. |
| `mm mcp serve` | Experimental MCP adapter for external agents. It is optional for pip/uv installs via `meetily-memory[mcp]`. |
