# Core contract and persistent user state

`MeetilyMemoryCore` exposes one immutable, typed contract. `search()` always returns
meeting-level `SearchResults`: the query and requested neighbor count plus ranked
`MeetingSearchResult` values with match sources, evidence, and matched tags. Search versions and
compatibility payloads do not exist. `Meeting` contains product identity and curated meeting
data; source IDs, paths, fingerprints, and raw storage JSON remain inside the repository layer.

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
It has no contract-version selector. Optional meeting lookups return `None`; required meeting and
evidence resolution raise specialized `LookupError` subclasses.

`RetrievalStrategy` accepts only a query and candidate limit and returns ranked `SearchHit`
values. Meeting scope, neighboring excerpts, bundle limits, and `MemoryEntity` attachment are
owned by `ContextBundleBuilder`. The full `SearchHit` resolves through
`MeetilyMemoryCore.resolve_search_hit()` with the same stable evidence ID; a missing ID is an
integrity error, not an empty successful result.

## User state migration

Schema v4 makes every structured entity require `source_chunk_id` and changes deletion to
`ON DELETE CASCADE`. Before an existing v3 index is upgraded, task status overrides and notes
are copied to sibling `state.sqlite` using this strict identity:

```text
source UUID + meeting external ID + chunk external ID + task kind + normalized text fingerprint
```

The source UUID lives in `state.sqlite`; its current source path can change without changing the
UUID. App settings select that UUID and do not keep a second authoritative path after the legacy
`source_path` setting migrates. `mm config source NEW_PATH --rebind` preserves the UUID only
after the new Meetily DB passes schema validation and shares at least one stable meeting ID with
the indexed source. Ordinary source selection and scanning never infer a move. A legacy record
without the complete strict identity is retained as an orphan and is never
attached by fuzzy matching. `mm db status` reports the latest migration counts. After migration,
`index.sqlite` can be deleted and rebuilt without losing task statuses or notes.
