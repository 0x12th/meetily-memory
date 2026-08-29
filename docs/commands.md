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
| `mm config source NEW_PATH --rebind [--source-uuid UUID]` | Explicitly asserts that a state-owned source UUID moved to a validated target, canonicalizes state/index paths, and preserves the UUID even when meeting-ID overlap is zero. Pass `--source-uuid` to repair a non-selected secondary source; targets owned by another UUID are rejected. |

Ordinary `mm config source NEW_PATH` validates and selects only `NEW_PATH`; it does not migrate or
resolve the previous settings binding, so an unrelated legacy alias cannot block creation of a new
source identity. A legacy settings-only `source_path` is migrated to UUID only on an exact raw
`state.current_path` match, or on an exact raw pending `state.projected_path` match whose
`current_path` remains authoritative. It never creates an identity by itself. A
populated v1-v5 index without that state binding stops before index migration and instructs the user
to register the source explicitly with `mm config source PATH` or repair an existing UUID with
`--rebind --source-uuid`.

Without `--source-uuid`, rebind uses the selected UUID. For legacy settings that contain only
`source_path`, it may recover the UUID only from an exact raw `state.current_path` match or the
exact old `projected_path` of a pending claim; it never re-resolves that stored path as identity
evidence. A pending match immediately selects its authoritative `current_path`. If no unique exact identity is available, the
command stops and asks for `--source-uuid UUID`. Before constructing a mutable repository, rebind
resolves its identity read-only from the explicit UUID, the selected settings UUID, or that exact raw
legacy path match. Missing state, no selection, or no exact match fails without state/index creation
or migration. UUID/kind and target ownership are then checked in state. A successful rebind makes the repaired UUID the
selected source in settings. Refresh/rebuild, ordinary source selection, and rebind share one
interprocess `refresh.lock`. Unknown UUIDs and targets owned by another UUID are rejected from state
before the index is opened. A valid rebind first completes any legacy v3 task-state transfer and the
safe in-place upgrade through v5, then claims path ownership with a monotonic pending revision.
State retains the last confirmed projected path until the v5/v6/v7 projection succeeds, settings
are written, and that exact revision is finalized. A failed step first persists a fresh reverse pending
claim (`current_path=old`, `projected_path=actual new`), then rolls back the index under that token,
and only then clears pending state. Settings are never restored by comparing UUID/path values alone,
so an older failure cannot undo a newer same-target selection; an incomplete rollback reports an
explicit recovery error. After process death at any point around the forward or reverse
claim/projection, a refresh or repeated same-target `--rebind` uses the persisted paths to repair
legacy or source-aware index paths before clearing pending metadata. A current v7 writer open checks
every pending source, including secondary sources, and finalizes all verified claims in one state
transaction.

Read-only local diagnostics pin the physical `index.sqlite` and `state.sqlite` targets as one pair at
the start of each snapshot. A shared logical parent is resolved once, then both child/file targets
are repeatedly resolved as `index,state,index,state` until the pair is stable; other layouts use the
same bounded pair-stability rule. Schema, migration-report, tag, and meeting-ref reads reuse those targets and
immutable read-only connections. Every read is guarded independently against active
`-wal`/`-journal` sidecars; a sidecar that appears after initial schema inspection makes the affected
database and cross-database details explicitly unavailable instead of returning stale data.
Retargeting a logical directory or child file symlink cannot mix databases within a report. Orphaned
tag assignments are unavailable unless both index and user-state schemas are current and readable.

## Search And Context

`mm s QUERY` returns one ranked result per meeting. Exact tag matches rank first, followed by
lexical transcript matches and token-level tag matches. Each result includes source evidence
when available. `--context N` appends adjacent chunks around matching excerpts; the default
remains `--context 0`. Search, context, evidence resolution, open, tag listing, and MCP reads require
an existing schema-v7 index and state and open both with SQLite `mode=ro`. They never create or
migrate databases. A missing or legacy disposable index reports that `mm refresh` or
`mm scan --source PATH` is required. Missing authoritative `state.sqlite` is different: restore it
from backup. `mm refresh` alone cannot recover the UUID already projected by a current index. For an
intentional identity reset, first move or remove `index.sqlite`, then run `mm init` or
`mm scan --source PATH`; manual tags, task statuses, and task notes cannot be recovered without the
original state database.

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
| `mm open 12 --print-path` | Prints the default meeting folder path using a generation-local integer shortcut. |
| `mm open --source-uuid UUID --external-id ID` | Opens a meeting by its stable source-aware reference; digit-only external IDs are supported. |
| `mm open --external-id ID` | Convenience lookup for an external ID only when exactly one source matches. |
| `mm c "what did we decide about migration?"` | Builds paste-ready Markdown context with sources for ChatGPT, Claude, Codex, or another LLM. Use when you want to copy context elsewhere. |
| `mm c "what did we decide?" --context 2` | Explicitly adds two neighboring chunks around each lexical match. |
| `mm t "migration"` | Experimental source-backed topic dossier. It starts from search evidence, labels heuristic matches as possible decisions/tasks/risks/questions, and still shows evidence when structured memory is empty. It is not an LLM answer. |

Context and entity hydration never carry a generation-local chunk ID into another SQLite
snapshot. Every hit is batch-resolved by stable `evidence_id` inside one explicit read transaction
that remains open through the final context/entity SELECT, and its `MeetingRef` must still match. A
missing or mismatched hit fails closed and asks the caller to repeat the search instead of
substituting whichever chunk currently owns the old integer ID.

`mm t` expands stored topic aliases. Manual aliases are authoritative in `state.sqlite`; the index
table is only a derived retrieval projection, so aliases survive full index deletion and rebuild.
Legacy v5 and old unmarked v6 aliases are imported once under a locked, digest-checked snapshot.
Every current v7 generation has a stable generation marker; fresh and rebuilt generations are
registered as state-owned before projection and never import their derived aliases back. Add aliases
explicitly with repeated `--alias` options, for example:

```bash
mm t "kafka" --alias "кафка" --alias "broker"
```

There is no built-in domain dictionary for specific terms. Future expansion
should come from user aliases, indexed aliases, extracted aliases, or semantic
retrieval.

`mm c` uses direct lexical matches by default. `--context N` explicitly appends neighboring
chunks, marks their evidence role, and caps the resulting evidence bundle at 20 excerpts. Evidence
IDs are materialized during scan with the unchanged stable-ID algorithm; resolving one uses the
unique `chunks.evidence_id` index. Structured entities for the bundle are selected only for those
chunk IDs in bounded SQL batches.

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

A generated meeting note persists its open command with the stable meeting reference:
`mm open --source-uuid UUID --external-id ID`. Obsidian output never persists a generation-local
integer meeting ID in that command.

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
| `mm db status` | Read-only inspection of missing, legacy, current, or incompatible index/state schemas, migration status, and orphaned tag assignments. It never creates or upgrades a database. |
| `mm mcp serve` | Experimental stdio-only MCP adapter with meeting search and lookup. It is optional for pip/uv installs via `meetily-memory[mcp]`. |
