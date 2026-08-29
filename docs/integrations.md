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

Every generated object is represented by one typed note reference. That reference produces the
stable object key, readable filename plus deterministic 80-bit suffix, relative path, display
label, alias wikilink, and versioned identity marker. Example:

```text
Meetings/Launch Planning--m-3fa912abcdef12345678.md
[[Launch Planning--m-3fa912abcdef12345678|Launch Planning]]
<!-- meetily-memory:managed:v1:ENCODED_OBJECT_KEY -->
```

Meeting keys contain `source_uuid` plus `external_id`. Entity keys contain source UUID, meeting
external ID, stable chunk evidence ID, entity kind, and a stable normalized-content fingerprint
computed by the adapter; the repository entity fingerprint and local meeting, chunk, entity, topic,
or person row IDs never participate. Topics use their persistent stable key, and people use their
normalized domain key. Opaque UUID, external-ID, evidence-ID, and stable-key strings are preserved
exactly in identity; only display labels and readable path components are NFC-normalized.

Filenames are NFC-normalized and sanitize Windows, macOS, Linux, and Obsidian-reserved filename or
wikilink characters. Each complete `.md` filename is at most 255 UTF-8 bytes; truncation reserves
space for the suffix and extension and never splits a code point. The suffix keeps duplicate titles,
sanitization collisions, and equal readable prefixes distinct while alias links remain readable.

Generated meeting-note open commands use
`mm open --source-uuid UUID --external-id ID`. The persisted command payload always carries the
source-aware `MeetingRef` and never a generation-local integer meeting ID.

Sync builds and validates the complete, deterministically ordered note plan before creating a
directory, writing a file, or removing a stale file. Duplicate object identities or cross-platform
paths, any symlinked managed-directory component, managed destination collisions, and filesystem
preflight errors therefore leave the vault unchanged. Marker ownership requires canonical JSON and
base64 plus the exact kind-specific v1 field set with non-empty strings; missing, extra, or wrong-typed
fields are foreign. Unmanaged files and markers from another format/version are never overwritten or
removed. A full sync uses the object identity marker to remove a previous readable path after rename
and to remove stale notes whose managed identities disappeared. Case-only rename uses portable path
equivalence and filesystem identity so the newly written destination is never unlinked on a
case-insensitive filesystem, while a genuinely distinct old path is removed on a case-sensitive one. If an unmanaged destination blocks a
rename, the prior managed note is retained. A limited sync has an incomplete expected set and never
performs destructive reconciliation. Text and JSON results report written, skipped, and removed file
counts.

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
