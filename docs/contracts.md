# Core contracts and persistent user state

`meetily-memory.core.v1` remains the default response contract for the CLI and MCP server.
Search and context payloads keep their existing dictionary and Markdown shapes. Internal domain
types are serialized back through the dedicated v1 adapter, and
`tests/fixtures/core_v1_contract.json` guards the field-level contract.

Python consumers can explicitly request `meetily-memory.core.v2` from `search()` and
`build_context()`. The v2 search result is a `SearchHit` with a stable public evidence ID,
`MeetingRef`, an untruncated `SourceExcerpt`, and an explicit `is_context` role. The v2 context
payload is a data-only
`ContextBundle`; it does not contain Markdown. `MemoryEntity` values use the canonical kinds
`decision`, `task`, `risk`, and `question`, point directly to their source excerpt, and are
marked non-authoritative. Extractor confidence remains internal diagnostics and is not part of
the domain contract or generated Obsidian notes. The heuristic task extractor requires an
explicit action verb or assignment phrase; generic mentions of a task or what one "can do" are
not treated as established action items.

MCP `search` and `build_context` use `core.v1` unless the caller explicitly passes
`contract_version="meetily-memory.core.v2"`. They delegate version selection and validation to
`MeetilyMemoryCore`; the MCP adapter does not implement retrieval, ranking, ID resolution, or
context assembly.

`CompactSearchHit` is an explicit preview projection. Its `truncated`, `preview_length`,
`projection_version`, and `is_context` fields are always present. Changing the preview length
does not change retrieval or the evidence ID. The full `SearchHit` resolves through
`MeetilyMemoryCore.resolve_search_hit()` with the same ID; a missing ID is an integrity error,
not an empty successful result.

## User state migration

Schema v4 makes every structured entity require `source_chunk_id` and changes deletion to
`ON DELETE CASCADE`. Before an existing v3 index is upgraded, task status overrides and notes
are copied to sibling `state.sqlite` using this strict identity:

```text
source UUID + meeting external ID + chunk external ID + task kind + normalized text fingerprint
```

The source UUID lives in `state.sqlite`; its current source path can change without changing the
UUID. A legacy record without the complete strict identity is retained as an orphan and is never
attached by fuzzy matching. `mm db status` reports the latest migration counts. After migration,
`index.sqlite` can be deleted and rebuilt without losing task statuses or notes.
