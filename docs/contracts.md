# Core contract and persistent user state

`MeetilyMemoryCore` exposes one immutable, typed contract. `search()` always returns
meeting-level `SearchResults`: the query and requested neighbor count plus ranked
`MeetingSearchResult` values with match sources, evidence, and matched tags. Search versions and
compatibility payloads do not exist. `Meeting` contains one frozen
`MeetingRef(source_uuid, external_id)` plus curated meeting data; source paths, fingerprints, and
raw storage JSON remain inside the repository layer. Integer meeting and chunk IDs are explicitly
local to one disposable index generation.

`build_context()` and `build_meeting_context()` return the same data-only `ContextBundle`.
Markdown is a CLI presentation concern and is rendered by `ContextRenderer`. `MemoryEntity`
values use the canonical kinds `decision`, `task`, `risk`, and `question`, point directly to their
source excerpt, and are marked non-authoritative. Extractor confidence remains internal
diagnostics and is not part of the domain contract or generated Obsidian notes. The heuristic
task extractor requires an explicit action verb or assignment phrase; generic mentions of a task
or what one "can do" are not treated as established action items.

All Core operations return domain or operation models rather than transport dictionaries.
Explicit serializers in `meetily_memory.serializers` are called only by CLI, MCP, and integration
adapters. The MCP adapter has only `search_meetings` and `get_meeting`, keeps the `{kind, data}`
envelope, and invokes the same canonical meeting-level search path and date filters as `mm s`.
`get_meeting` accepts the stable `source_uuid` plus `external_id`; serialized meetings and excerpts
carry the same nested ref while generation-local IDs are labelled as local. It has no
contract-version selector. Optional meeting lookups return `None`; required meeting and evidence
resolution raise specialized `LookupError` subclasses. Bare external-ID lookup is allowed only
when exactly one indexed source matches and otherwise raises an ambiguity error.

`RetrievalStrategy` accepts only a query and candidate limit and returns ranked `SearchHit`
values. Meeting scope, neighboring excerpts, bundle limits, and `MemoryEntity` attachment are
owned by `ContextBundleBuilder`. The full `SearchHit` resolves through
`MeetilyMemoryCore.resolve_search_hit()` with the same stable evidence ID; a missing ID is an
integrity error, not an empty successful result. Context expansion and entity hydration batch-resolve
that stable ID inside one explicit SQLite read transaction, keep its snapshot through the final
context/entity SELECT that consumes `source_chunk_id`, verify the stored `MeetingRef`, and use only
the newly resolved chunk ID. Nested callers reuse an existing transaction; owned read snapshots are
rolled back on success or failure. Missing or mismatched evidence fails closed with an explicit
retry-search error; a generation-local integer is never accepted as identity.

Index schema v7 materializes every `chunks.evidence_id` as `TEXT NOT NULL UNIQUE` during ingestion.
The `evidence:` prefix, source/meeting/chunk payload, canonical JSON encoding, and content-fingerprint
fallback are unchanged. Search SQL returns the complete `Meeting`, `SourceExcerpt`, source UUID,
local chunk ID, and evidence ID in one row; row-to-domain conversion performs no database or state
lookup. Evidence resolution is one unique-index lookup. Context windows reuse one open connection
and bounded SQL batches, while context entities are selected directly by `source_chunk_id IN (...)`
in bounded batches.

`MeetilyMemoryCore`, CLI search/context/open/list operations, MCP reads, and offline retrieval reads
open an already-existing current index and state with SQLite `mode=ro`. This mode never creates a
directory or database, migrates a schema, registers a source, heals a projection, or projects topic
aliases. A missing or legacy disposable index fails with refresh/scan guidance. A missing
`state.sqlite` instead requires restoring that authoritative database: refresh alone cannot recover
a UUID already projected by a current index. An intentional identity reset first moves/removes the
disposable index and then explicitly runs init/scan, with the understood loss of manual tags, task
statuses, and task notes. Writer setup, scan/refresh, rebind, manual tag/task mutations, and the
still-experimental topic materialization path use the explicit writer repository. Making topic/graph
reads non-materializing remains separate topic work.

## User state migration

Schema v4 makes every structured entity require `source_chunk_id` and changes deletion to
`ON DELETE CASCADE`. Before an existing v3 index is upgraded, task status overrides and notes
are copied to sibling `state.sqlite` using this strict identity:

```text
source UUID + meeting external ID + chunk external ID + task kind + normalized text fingerprint
```

The source UUID lives in `state.sqlite`; its authoritative `current_path` can change without
changing the UUID. `projected_path` records the last index projection confirmed by state, while a
monotonic revision doubles as the opaque pending-claim/compensation token. A claim changes
`current_path` and persists its pending revision without overwriting `projected_path`; therefore a
same-target retry after process death still knows the actual legacy/current index path it must heal.
Index schema v6 introduced `sources.source_uuid` as a `NOT NULL UNIQUE` projection supplied by the
scanner after state registration and one stable `generation_id` marker with alias ownership. Schema
v7 preserves both and adds the materialized evidence identity. The generation identifier remains
independent of every source UUID.

Existing populated v1-v6 indexes are rebuilt side by side from a complete state-owned source
snapshot, not only the requested source. Before backup and replacement, the active SQLite family is
recovered, any WAL is checkpointed, `journal_mode=DELETE` is forced and verified, and an exclusive
transaction is held through the final sidecar check and swap. A live or uncheckpointable WAL fails
closed without replacing the active generation. A clean binding requires the same canonical current and
projected path. A pending binding may map the exact old legacy projection to its validated canonical
current path; this is the only path mismatch accepted during rebuild. Every current source must have
an available, valid Meetily DB and an unambiguous state-owned UUID. The requested source is scanned
last so its counts remain the command result. A fresh generation always reconstructs the full
derived entity snapshot for every source, regardless of an incremental scan's analysis flag.
Unavailable, unmapped, ambiguous, non-canonical, or concurrently changed bindings abort before
replacement; UUIDs are never backfilled from mutable legacy paths.

New input paths are canonicalized with `expanduser().resolve(strict=True)` before registration or
rebind. Automatic scan or selection reuses a UUID only when `state.current_path` already equals
that canonical string exactly. Re-resolving a stored relative path or symlink can reveal a
collision, but never proves identity: automatic reuse and duplicate registration both fail closed
and require explicit rebind. This prevents a changed working directory or retargeted symlink from
assigning an old UUID to another database. Legacy settings migration is lookup-only and read-only: `source_path` may select an existing UUID
only by exact raw equality with `state.current_path`, or with `state.projected_path` while that source
has a pending claim. A pending match then uses authoritative `current_path`; paths are never
re-resolved as identity evidence, and settings can never create or backfill a UUID. Before a
populated v1-v6 index is opened through migration-capable code, scanner
preflight verifies every legacy source against state. A missing identity fails with explicit
`mm config source`/`--rebind --source-uuid` recovery guidance and leaves the index generation
unchanged. Empty/new index flows may still register a source through explicit init/config/scan.
App settings select the UUID and do not keep a second authoritative path after the legacy
`source_path` setting migrates. On an ordinary v7 scan, any persisted pending binding is projected
to both `sources.path` and every related `meetings.source_path` in one index transaction before
unchanged meeting fingerprints can short-circuit content upserts. Opening a current v7 writer
checks every pending claim represented by the index, including secondary sources, and finalizes all
verified claims with one state transaction/CAS. A legacy rebuild projects the same complete pending
snapshot into the replacement and batch-finalizes it only after the verified swap. A successful
refresh therefore heals process death before projection, after projection, during multi-source
finalization, or after a v5/v6-to-v7 swap.

`mm config source NEW_PATH --rebind [--source-uuid UUID]` is the explicit user assertion that a
state-owned UUID now belongs to the validated target DB. The optional UUID can repair a
non-selected secondary source; it must already exist, and success makes it selected in settings.
Without the option, rebind uses the selected UUID. Legacy settings that contain only `source_path`
may recover an identity solely through one exact raw `state.current_path` match, or an exact raw
`state.projected_path` match while that source has a pending claim. Missing or ambiguous exact
matches stop with instructions to pass `--source-uuid`; the stored path is never re-resolved as
automatic identity evidence.

Ordinary source selection validates and registers only the newly supplied canonical path; it does
not migrate or resolve an unrelated previous settings binding. Rebind validates UUID existence,
source kind, and target ownership using state alone before opening or migrating the index. The
ownership check and authoritative `state.current_path` update are one `BEGIN IMMEDIATE`
transaction. Every claim, including a same-target retry, advances `sources.revision` while retaining
the persisted old projection. Finalization and compensation compare both paths and the pending
revision. Compensation first persists a fresh reverse claim (`current_path=old`,
`projected_path=actual new`), then rolls back the index projection under that token, and only then
finalizes state. A restart can heal either crash boundary without reviving an old token (ABA).

Refresh/rebuild, ordinary source selection, rebind, and their source-selection settings writes share
one interprocess `refresh.lock`. The explicit UUID candidate, selected settings UUID, or exact raw
legacy `source_path` match against current/pending state is resolved through the existing state file
read-only before a migration-capable repository is constructed. Missing state, no selected identity,
or no exact raw match fails without state/index creation or migration. Rebind then validates UUID/kind and target ownership from state before it
opens the index. For legacy v3, it completes the durable task-state transfer and safe in-place
upgrade through v5 before creating the new path claim, so the old projection maps to the selected
UUID. Rejected unknown or already-owned targets never open or migrate the index. Rebind then updates
the existing v5, v6, or v7 index projection without turning the operation into ordinary source
selection. For v7, the current claim revision is checked
before and inside the index write transaction, the source row is selected by authoritative UUID,
target ownership is checked, and the actual projected path is compare-and-set to the claimed path
while every `meetings.source_path` is updated. This permits a same-target rebind to repair a stale
projection left by process death without accepting a stale concurrent claim. The revision is checked
again immediately before commit. The v5 branch keeps its stricter path-based mapping: only a unique
legacy path collision may identify the projection row to repair, and it never auto-maps or merges
UUIDs. A target already projected by another UUID is rejected. Settings are written only after state
and index agree. If a derived projection or settings update fails, state
and projection are restored only while the exact claim revision is still current. The reverse claim
remains authoritative and recoverable if the process dies before or after index rollback. Settings receive
no unsafe UUID/path-only compensation: a write that completed before reporting failure may leave the
same UUID selected, which remains resolvable through the rolled-back authoritative state. A newer
claim and selection are never overwritten, and an incomplete compensation is reported as an
explicit recovery failure rather than success.
Meeting-ID overlap is reported and
can validate the assertion when available, but it is not required: an explicit rebind may
legitimately report zero matches, including for a source with no indexed meetings. Rebuild errors
for unavailable or non-canonical legacy sources identify the legacy path, known state UUIDs, and
the corresponding `--rebind --source-uuid` recovery command. Ordinary source selection and
scanning never infer a move. A legacy record without the complete strict identity is retained as an
orphan and is never attached by fuzzy matching. A v3 transfer also never creates a source UUID: it
reuses only an exact existing state binding, otherwise preserving the full status/note/source
provenance as an orphan with `source_uuid = NULL`. `mm db status` reports the latest migration counts
and, only when both databases are current and readable, counts orphaned tag assignment rows rather
than distinct meetings. The count is unavailable when either index or user state is missing, legacy,
incompatible, or unreadable. After migration,
`index.sqlite` can be deleted and rebuilt without losing task statuses or notes.

Manual topic aliases are also authoritative in `state.sqlite`. The additive state migration creates
stable topic descriptors, normalized alias rows, a generation/path ownership ledger, and a
composite generation/path import ledger atomically. Fresh and side-by-side v7 generations receive a
stable marker unrelated to source UUIDs and are registered as state-owned before their first alias
projection; a state-owned generation never imports its derived rows back. Legacy v5 and old
unmarked v6 aliases are imported once while a normal read-write connection holds `BEGIN IMMEDIATE`:
hot rollback recovery and future-version rejection happen before state writes, and exact count plus
digest are rechecked before the state transaction commits. Manual add and delete mutate state first,
and topic resolution reads state first. The index `topic_aliases` table remains a derived
compatibility projection. Rebuilds recreate it from state and verify every topic/alias metadata row
exactly before replacement, so deletion, projection failure, and recreation of `index.sqlite` cannot
resurrect a stale alias.

Read-only local diagnostics pin the physical index and state targets together before the first
schema read. The same pinned targets and immutable read-only connections serve schema,
migration-report, tag-assignment, and indexed-meeting-ref reads; helpers never re-resolve the
logical symlink. WAL/rollback-journal guards run around each read phase. If a sidecar appears after
initial inspection, the affected database becomes explicitly incompatible and detail fields become
unavailable rather than reporting stale success. A logical symlink retarget during the snapshot
therefore cannot combine diagnostics from one physical database pair with details from another.

Persistent integration output also treats `MeetingRef(source_uuid, external_id)` as the meeting
identity boundary. In particular, generated Obsidian meeting-note commands use
`mm open --source-uuid UUID --external-id ID` and never persist a generation-local integer ID.
Title-derived note names and wikilinks remain outside this command-payload contract.
