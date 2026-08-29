# Integrations

Meetily Memory keeps one full integration in the near-term public product:
Obsidian.

Other adapters can exist internally or experimentally, but they should not shape
the first public workflow until they clearly strengthen Search, Context, or Ask.

## Experimental MCP Adapter

MCP is an optional, local stdio adapter for one selected client. Install it with
`meetily-memory[mcp]` and configure the client to run:

```bash
mm mcp serve
```

It exposes only two tools:

- `search_meetings` accepts `query`, `limit`, `since`, `from`, and `to`; it uses the
  same meeting-level search and source evidence as `mm s`;
- `get_meeting` retrieves one indexed meeting by the stable `source_uuid` and `external_id`
  returned by search.

There is no SSE or streamable HTTP mode. The server does not expose context generation,
topics, people, projects, graph projections, or heuristic structured entities. Returned
data is local and source-backed, and it may contain private meetings.

This adapter remains experimental until a two-week pilot records at least five real uses
from the selected client. At least three must begin with a natural-language question, and
their top meetings and evidence must match a manual `mm s` check. If that gate is not met,
the adapter and its optional dependency should be removed.

## Obsidian Sync

Obsidian is a sync integration, not a one-file export.

Public commands:

```bash
mm obsidian init
mm obsidian sync
mm obsidian status
```

`mm obsidian init` asks for:

- vault path, defaulting to `~/Documents/Obsidian`;
- folder, defaulting to `Meetily Memory`;
- whether to run sync after every `mm refresh`.

`mm obsidian sync` creates and updates a managed note network:

```text
Topics/
Meetings/
People/
Tasks/
Decisions/
Risks/
Questions/
```

This gives Obsidian a useful graph without exposing Meetily Memory's internal
graph projection as a user-facing command.

Managed files must include:

```html
<!-- meetily-memory:managed -->
```

Generated meeting-note open commands use
`mm open --source-uuid UUID --external-id ID`. The persisted command payload always carries the
source-aware `MeetingRef` and never a generation-local integer meeting ID; title-based note naming
and wikilinks are unchanged here.

The sync command may update managed files, but it must not overwrite unrelated
user notes in the vault. A full sync also removes stale generated paths after a
meeting is deleted or renamed. Removal is restricted to the integration's own
directories and files containing the exact managed-marker line; unmarked notes
and notes with a changed marker are preserved. Text and JSON results report
written, skipped, and removed file counts.

With an explicit `--index`, Obsidian configuration is stored in the workspace
`settings.json` next to that index. Without it, commands use the global settings
scope. `init`, `sync`, `status`, and post-refresh sync always use the same scope.

## Automatic Post-Refresh Sync

If enabled during `mm obsidian init`, Obsidian sync runs after `mm refresh`.

The post-refresh flow is:

```text
mm refresh
obsidian sync, if Obsidian post-refresh sync is enabled
```

## Not In The Public Integration Layer

The following are out of the first public integration surface:

- Gbrain JSONL export;
- generic Markdown bundle export;
- task tracker draft export;
- one-off Obsidian topic export.

If these remain in the repository, they should be internal or experimental
until a repeated workflow proves they are worth stabilizing.
