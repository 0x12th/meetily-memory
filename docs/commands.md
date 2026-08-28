# Command Reference

This page describes the focused public workflow and the advanced commands that remain
available for compatibility and experiments.

## Core

| Command | Public role |
|---|---|
| `mm init` | First-run setup. Discovers the Meetily DB, creates `index.sqlite`, runs the first refresh, and asks before enabling automatic index refreshes. |
| `mm refresh` | Main manual index refresh. Reads the Meetily DB and updates the lexical index. If Obsidian post-refresh sync is configured, it also syncs that projection. |
| `mm update` | Updates the installed `meetily-memory` utility through Homebrew. |
| `mm status` | Short system state: Meetily DB path, index path, UI language, last refresh, actual autosync scheduler state, and Obsidian. |
| `mm doctor` | Diagnostics only. Checks Meetily DB access/schema, SQLite/FTS5 support, index permissions, and config. It does not change state. |
| `mm tag add 10 11 migration` | Assigns an explicit tag to one or more meetings. Tags are user state stored separately from the rebuildable index. |
| `mm tag list [MEETING_ID]` | Lists all active tags or the tags assigned to one meeting. |
| `mm tag suggest MEETING_ID` | Suggests up to five existing tags without changing assignments. |
| `mm tag remove 10 11 migration` | Removes a tag assignment from one or more meetings. |
| `mm config language ru` | Stores the stable CLI UI language. Supported values are `en`, `ru`, and `auto`. |
| `mm config source NEW_PATH` | Selects a validated Meetily DB as a new source identity. |
| `mm config source NEW_PATH --rebind` | Explicitly moves the selected source identity after matching its Meetily schema and meeting IDs. |

## Search And Context

`mm s QUERY` returns one ranked result per meeting. Exact tag matches rank first, followed by
lexical transcript matches and token-level tag matches. Each result includes source evidence
when available. `--context N` appends adjacent chunks around matching excerpts; the default
remains `--context 0`.

Date filters use the meeting timestamp `started_at`, falling back to `created_at`, `updated_at`,
and then `indexed_at`. `--since Nd` includes meetings from exactly N days ago through now.
`--from YYYY-MM-DD` includes the start of that date in the local timezone, while
`--to YYYY-MM-DD` includes the entire local calendar date. `--since` and `--from` cannot be
combined; `--to` can be used by itself or as an upper bound.

| Command | Public role |
|---|---|
| `mm s "migration risk"` | Meeting-level search over explicit tags and indexed transcript text. Returns the meeting, matched tags, source excerpts, and an `mm open` command. |
| `mm s "product integration" --since 7d` | Searches only the last seven 24-hour periods, including the exact lower boundary and the current moment. |
| `mm s "product integration" --from 2026-08-17 --to 2026-08-23` | Searches an inclusive range of local calendar dates; the whole final day is included. |
| `mm s "migration risk" --context 2` | Expands each hit with neighboring chunks before and after the match. Use this when the matching snippet is too short. |
| `mm open 12` | Opens the meeting folder so the original Meetily record can be inspected. |
| `mm open 12 --source` | Opens the indexed source file/path. |
| `mm open 12 --print-path` | Prints the default meeting folder path without opening it. |
| `mm c "what did we decide about migration?"` | Builds paste-ready Markdown context with sources for ChatGPT, Claude, Codex, or another LLM. Use when you want to copy context elsewhere. |
| `mm c "what did we decide?" --context 2` | Explicitly adds two neighboring chunks around each lexical match. |
| `mm t "migration"` | Experimental source-backed topic dossier. It starts from search evidence, labels heuristic matches as possible decisions/tasks/risks/questions, and still shows evidence when structured memory is empty. It is not an LLM answer. |

`mm t` expands stored topic aliases. Add aliases explicitly with repeated
`--alias` options, for example:

```bash
mm t "kafka" --alias "кафка" --alias "broker"
```

There is no built-in domain dictionary for specific terms. Future expansion
should come from user aliases, indexed aliases, extracted aliases, or semantic
retrieval.

`mm c` uses direct lexical matches by default. `--context N` explicitly appends neighboring
chunks, marks their evidence role, and caps the resulting evidence bundle at 20 excerpts.

## Offline Semantic Research

Semantic retrieval did not pass its product gate and is no longer a CLI or
refresh surface. Install the `semantic` extra only when reproducing an offline
experiment, then use `scripts/evaluate-semantic-search.py` as documented in
[evaluation.md](evaluation.md). Standard installation, `mm s`, `mm refresh`,
and autosync do not import or require `sqlite-vec` or Ollama.

## Optional Experimental: Obsidian

Obsidian is experimental but intentionally visible and usable because it is a
requested workflow.

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
| `mm autosync start` | Installs and activates the background refresh job. On macOS this is launchd; on Linux it is systemd when available. |
| `mm autosync stop` | Stops automatic refreshes and removes generated launchd/systemd files when present. |
| `mm autosync status` | Verifies saved configuration, scheduler installation, runtime registration, and the last successful refresh. |

The background cycle runs `mm refresh`, then Obsidian sync if configured.

Autosync status is `enabled` only when configuration, scheduler files, and runtime registration
agree. A `misconfigured` status can be repaired by rerunning `mm autosync start`. Background
stdout and stderr are stored in the Meetily Memory data directory.

There should be no separate public watch command.

## Low-Level And Advanced

| Command | Role |
|---|---|
| `mm scan` | Low-level Meetily DB indexing for debugging and tests. Ordinary users use `mm refresh`. |
| `mm db status` | Shows schema version and local index internals. |
| `mm mcp serve` | Experimental stdio-only MCP adapter with meeting search and lookup. It is optional for pip/uv installs via `meetily-memory[mcp]`. |
