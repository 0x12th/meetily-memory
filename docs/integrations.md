# Integrations

Meetily Memory retains Obsidian as an optional advanced integration. The command group is hidden from
top-level `mm --help`; search and meeting lookup remain the visible local CLI workflow.

## Obsidian Sync

Obsidian is a sync integration, not a one-file export.

Hidden advanced commands:

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
stable object key, readable filename plus deterministic 80-bit suffix, relative path, display label,
alias wikilink, and versioned identity marker. Example:

```text
Meetings/Launch Planning--m-3fa912abcdef12345678.md
[[Launch Planning--m-3fa912abcdef12345678|Launch Planning]]
<!-- meetily-memory:managed:v1:ENCODED_OBJECT_KEY -->
```

Meeting keys contain `source_uuid` plus `external_id`. Entity keys contain source UUID, meeting
external ID, stable evidence ID, entity kind, and a stable normalized-content fingerprint computed by
the adapter. Topics use their persistent stable key, and people use their normalized domain key.
Opaque identity strings are preserved exactly; only display labels and readable path components are
NFC-normalized.

Filenames are NFC-normalized and sanitize Windows, macOS, Linux, and Obsidian-reserved filename or
wikilink characters. Each complete `.md` filename is at most 255 UTF-8 bytes; truncation reserves
space for the suffix and extension and never splits a code point. The suffix keeps duplicate titles,
sanitization collisions, and equal readable prefixes distinct while alias links remain readable.

Generated meeting-note open commands use
`mm open --source-uuid UUID --external-id ID`. The persisted command payload always carries the full
source-aware `MeetingRef`.

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

## Optional Post-Refresh Sync

If enabled during `mm obsidian init`, Obsidian sync runs as a follow-up phase of an explicitly invoked
`mm refresh`:

```text
mm refresh
obsidian sync, if configured
```
