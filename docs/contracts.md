# Runtime contracts and persistent identity

## Product path

The supported path is `mm s → source-backed evidence → mm open`. Meetily Memory is not a meeting
source of truth or a general knowledge platform.

## Identity

`MeetingRef(source_uuid, external_id)` is the only user-facing meeting identity. Generated commands,
tag mutations, and managed meeting notes use the full stable reference. Integer meeting and chunk IDs
are local handles inside one disposable index generation and are not accepted by `mm open` or tag
commands.

Every lexical excerpt has a stable `evidence_id`. Repository lookups may resolve that identity inside
a pinned read transaction, but the public Core no longer exposes a general evidence-resolution or
context-bundle API.

## Core surface

`MeetilyMemoryCore.search()` is the supported public Core path. It returns meeting-level
`SearchResults` with match sources, source-backed evidence, and matched manual tags. Additional rich
topic and structured methods remain internal dependencies of the current hidden Obsidian integration;
they are not exposed as CLI workflows. Semantic retrieval is available only through offline
evaluation tooling.

## Data ownership

- Meetily SQLite is opened read-only and remains the source of truth.
- `index.sqlite` contains derived searchable projections.
- `state.sqlite` owns source UUIDs, manual tags, and other explicit user state that cannot be rebuilt.
- Tag assignments are keyed by `(source_uuid, meeting_external_id)` and may remain orphaned until a
  meeting reappears.

A read path opens existing compatible databases without creating or migrating them. Mutations use an
explicit writer repository. Refresh, source selection, and rebind share one interprocess writer lock.

## Current refresh contract

The current runtime publishes an incremental schema-v7 projection and retains its legacy migration,
recovery, topic, structured-entity, and source-path projection machinery. A failed projection does not
delete the previously searchable state. An optional post-publish Obsidian failure does not turn a
successfully published index into a failed index.

## Current Obsidian contract

The hidden Obsidian adapter builds the existing rich managed network and uses managed marker version
`v1`. Meeting object keys and generated open commands use stable source-aware identity. Sync completes
a full preflight before changing the vault; unmanaged, malformed, and foreign-version files remain
untouched.
