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

A changed refresh publishes a complete replacement index atomically. Readers continue to use the
previous completed index until publication succeeds. The published index owns a versioned source
fingerprint covering the Meetily SQLite file and its WAL/journal metadata. An unchanged fingerprint
is a successful no-op: `last_update_at` advances while `index.sqlite` remains untouched. `--force`
bypasses the no-op.

`refresh --sync-obsidian` keeps the refresh lock through the downstream Obsidian sync. Obsidian still
runs after an index no-op because state-owned tags and vault contents may have changed. If refresh
fails, Obsidian does not run. If Obsidian fails after a successful refresh, the index and refresh
timestamp remain committed and the command exits non-zero.

## Obsidian contract

Obsidian is a downstream adapter with `init`, `sync`, and `status` commands. `init` stores the target
and performs the first sync immediately. A sync manages only
`Meetings/` and `Tags/` notes. It completes preflight before changing the vault, identifies owned
notes with managed markers, and leaves unmanaged or foreign-version files untouched.

## Autosync contract

Autosync is macOS-only and consists of one user LaunchAgent. The plist and `launchctl` are its source
of truth; scheduler state is not copied into `state.sqlite`. The job runs the stable installed `mm`
executable with `--index PATH refresh --sync-obsidian` on calendar minutes 0, 15, 30, and 45, without
`RunAtLoad`. One user job serves at most one index. Replacing another workspace requires explicit
`autosync enable --replace`; `disable` refuses to stop a job owned by another workspace.
