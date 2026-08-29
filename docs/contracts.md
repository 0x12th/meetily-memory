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

`MeetilyMemoryCore`, CLI search/context/open/list/topic operations, Core graph reads, MCP reads, and
offline retrieval reads open an already-existing current index and state with SQLite `mode=ro`. This
mode never creates a directory or database, migrates a schema, registers a source, heals a
projection, or projects topic aliases. Topic and graph resolve canonical titles and aliases from
state, then resolve the canonical stable key and compute current structured matches on one pinned
index connection inside one explicit read snapshot; a generation-local topic ID never crosses an
index connection or rebuild boundary. Topic term searches share that snapshot and deduplicate only
by stable `evidence_id`, never by local chunk ID. An unknown query creates neither a topic row nor an
edge. A missing or legacy disposable index fails with refresh/scan guidance. A missing
`state.sqlite` instead requires restoring that authoritative database: refresh alone cannot recover
a UUID already projected by a current index. An intentional identity reset first moves/removes the
disposable index and then explicitly runs init/scan, with the understood loss of manual tags, task
statuses, task notes, and topic aliases. Scan/refresh, rebind, and manual tag/task mutations use an
explicit writer repository. Topic alias mutations reopen only the already-validated physical state
identity with SQLite `mode=rw`: they do not create a directory/database or migrate a schema. A
missing, replaced, or retargeted state path fails with restore-state guidance before commit instead
of writing a new database or another workspace/global state.

## Obsidian note identity

The Obsidian adapter builds one complete, immutable note plan before filesystem mutation. Each typed
`NoteRef` owns the canonical object key, NFC-normalized byte-bounded readable stem, deterministic
80-bit suffix, relative path, display label, alias wikilink, and versioned identity marker; path and
link policy cannot diverge. Meeting identity is `(source_uuid, external_id)`. Entity identity is
`(source_uuid, meeting_external_id, chunk_evidence_id, entity_kind,
stable_content_fingerprint)` and never contains a local row ID. Topics use their persistent stable
key; people use a normalized domain key. Opaque source UUIDs, external IDs, evidence IDs, and stable
domain keys are preserved byte-for-byte in the canonical object key; NFC applies only to display
labels and readable path components.

The complete `.md` component is at most 255 UTF-8 bytes with suffix and extension space reserved;
truncation does not split code points. Windows reserved basenames, control characters, trailing dots
or spaces, filesystem separators, and Obsidian link delimiters are sanitized. Planned object keys and
NFC/case-folded relative paths must both be unique, and plan order is deterministic.

Vault preflight rejects every symlink at a managed directory component, then reads all destinations
and recognized markers before the first `mkdir`, write, or remove. Any duplicate, symlink, or
managed-object collision fails with zero filesystem mutation. Marker ownership requires canonical
JSON/base64 plus the exact non-empty typed v1 schema for its kind; missing, extra, or wrong-typed
fields are foreign. A full sync reconciles by decoded object identity: rename removes only prior paths
for that identity, and deleted identities remove only valid v1 managed notes. Case-only rename checks
portable path equivalence and filesystem `samefile` after writing, so it never unlinks a destination
that is the same filesystem object while still removing a distinct old path on case-sensitive
filesystems. Unmanaged files and foreign or malformed marker formats are preserved. If an unmanaged destination blocks a rename, its prior managed path is also
preserved. A limited sync never removes files because its expected identity set is incomplete.

## Atomic refresh publication

An ordinary schema-v7 refresh publishes one incremental projection transaction; it does not copy or
rebuild the complete index. Constructing `IndexRepository` never heals or commits pending source
paths. The scanner first commits a durable `scan_runs.status = 'running'` row. It then uses one
connection and one `BEGIN IMMEDIATE` unit of work for every pending source-path projection,
meeting and chunk upserts, FTS, people, structured entities, knowledge nodes and edges, deletion
reconciliation, state-owned topic-alias projection, and the same run's `completed` status and
statistics. There are no per-meeting commits in this unit of work. An exception before commit rolls
back all derived changes and is followed by a separate durable `failed` update with the safe local
phase; the exception text is never persisted.

The unit of work uses transient WAL when the index normally uses `DELETE` journaling. This lets
read-only search ignore uncommitted frames after abrupt process death and continue to see the old
completed snapshot. A durable sibling marker records that WAL is temporary. Clean success or
handled rollback checkpoints WAL, restores `DELETE`, and removes the marker and SQLite sidecars. If
the process dies, the marker and WAL may remain temporarily; search still sees the old commit, and
the next refresh recovers, retries, restores `DELETE`, and removes all temporary files without manual
cleanup. An explicitly pre-existing persistent WAL mode has no transient marker and is preserved,
which keeps task-08 long-lived read snapshots compatible. Readers that overlap a normal refresh see
one coherent old or new SQLite snapshot, never an in-progress projection. Meetily source databases
remain opened read-only.

A completed projection is committed before settings, Obsidian, or other optional integrations.
Those operations still run while the caller holds `refresh.lock`, but their failures cannot change the
run back to `failed`. Instead the completed row records a sanitized `post_publish_*_failed` phase,
`index_status = completed`, a separate post-publish status, the source UUID and canonical source
path, and a structured supported retry command. Raw integration exceptions are neither chained nor
persisted. `mm status` exposes the latest unresolved event as `last_post_publish_error`; a successful
operation resolves only events with the same source UUID and phase. A completed refresh for another
source, or a refresh that does not attempt the failed Obsidian phase, leaves the event visible.

### Local design comparison (2026-08-29)

Both candidates were measured on the same generated Meetily fixture and current schema v7. The
fixture contained 500 meetings, ten transcripts per meeting plus summaries and notes (6,000 indexed
chunks initially). The incremental refresh changed 40 meetings, deleted 10, added 10, and ran real
structured-entity and knowledge projection. The generated source was 1,007,616 bytes. The baseline
index was 11,010,048 bytes and the published index was 11,603,968 bytes. Seven runs per candidate
were alternated in the same local macOS workspace; times below are wall-clock medians, with the
observed min/max range, so absolute time is environment-specific.

| Candidate | Refresh time | Peak index family | Extra disk / baseline | Rollback and recovery complexity |
|---|---:|---:|---:|---|
| One projection transaction with transient WAL | 3.528 s (3.511–3.532) | 17,017,495 B (`1.546×` baseline) | `0.546×` | SQLite rollback for handled faults; one durable transient-WAL marker makes hard-crash retry and cleanup explicit. No index copy, backup, validation swap, or generation handoff on an ordinary refresh. |
| Copy to staging, update, validate, fsync, retain previous copy, atomically replace | 3.614 s (3.577–3.635) | 33,624,064 B (`3.054×` baseline) | `2.054×` | Must recover/validate `.next`, coordinate schema-v7 generation/state ownership, fsync stage and directory, retain/expire the previous index, handle swap boundaries and stale artifacts, and reconcile the durable run across two files. |

Staging was 2.4% slower in this workload and used about 3.76 times as much additional disk. The
single-transaction design is therefore the schema-v7 incremental path. Side-by-side rebuild and swap
remain limited to incompatible format upgrades, preserving the task-07 safety contract; these
measurements do not justify a full rebuild or full-file copy on each incremental refresh.

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
digest are rechecked before the state transaction commits. Manual add and delete mutate only state;
they do not commit a derived topic node, alias row, or `belongs_to` edge. Canonical topic keys and
aliases share one casefold/whitespace-normalized namespace. Resolution checks canonical identity
before aliases, and `BEGIN IMMEDIATE` prevalidates the complete alias batch before creating a topic
or alias row. A cross-owner conflict adds nothing atomically; duplicate/empty/failed additions do
not leave an orphan topic definition. Unicode casefold behavior, including `Straße`/`STRASSE`, is
unchanged. When only an index canonical exists, reads preserve its title, stable key, metadata, and
timestamps; an explicit alias add seeds state from that exact canonical descriptor rather than the
query's casing or synthetic null metadata.

Topic and graph resolution read canonical ownership from state and compute current matches without
`ensure_topic()`, relinking, or commits. The index `topic_aliases` table remains a derived
compatibility projection that is updated only by scan/rebuild lifecycle work. Topic listings include
an index topic only when it still has a current knowledge relationship or a current state definition;
an alias-only node left by deletion is ignored, matching a full rebuild. Rebuilds recreate the
projection from state and verify every topic/alias metadata row exactly before replacement, so
deletion, projection failure, and recreation of `index.sqlite` cannot resurrect a stale alias.

The experimental topic contract uses the projected generation-local topic ID when one exists. A
non-materialized topic receives a deterministic nonzero negative request-local ID derived from its
canonical stable key. IDs are allocated in a range disjoint from computed edge IDs and checked for
collisions against every topic ID in that result; a deterministic salted retry resolves a collision.
Computed `belongs_to` graph edges use request-local negative IDs and `created_at = null`, and every
edge endpoint names one unique node in the same result. All such IDs are presentation handles, not
persistent identity, and must not be stored across calls.

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
