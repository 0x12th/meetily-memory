# Runtime contracts and persistent identity

## Product path

The supported path is `mm s → transcript excerpt → mm open`. Meetily Memory is a local lexical
search tool, not a meeting source of truth or a general knowledge platform.

## Identity

`MeetingRef(source_uuid, external_id)` is the user-facing meeting identity. Generated open commands,
tag mutations, and managed meeting notes use the full stable reference. Integer meeting and chunk IDs
are local handles and are not accepted by `mm open` or tag commands.

Every lexical excerpt has a stable evidence ID for internal retrieval and evaluation contracts.

## Core surface

`MeetilyMemoryCore.search()` is the supported public Core path. It returns meeting-level results with
source-backed transcript evidence and matched manual tags.

## Data ownership

- Meetily SQLite is opened read-only and remains the source of truth.
- The searchable index is derived and replaceable.
- Persistent state owns source UUIDs, manual tags, application settings, and Obsidian sync metadata.
- Tag assignments use `(source_uuid, meeting_external_id)`.

Read paths open existing compatible data without creating it. Refresh and other writers share one
interprocess lock.

## Refresh contract

A refresh publishes a complete replacement index atomically. Readers continue to use the previous
completed index until publication succeeds. Obsidian is never synced by refresh.

## Obsidian contract

Obsidian is a manual adapter with `init`, `sync`, and `status` commands. A sync manages only
`Meetings/` and `Tags/` notes. It completes preflight before changing the vault, identifies owned
notes with managed markers, and leaves unmanaged or foreign-version files untouched.
