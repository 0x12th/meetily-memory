# Meetily Memory

Local search over your Meetily meeting history, with source excerpts you can
verify in the original meeting.

Meetily Memory reads the local Meetily database, builds a private local index,
and never modifies the source database.

## Install

On macOS:

```bash
brew tap 0x12th/meetily-memory
brew install meetily-memory
```

The CLI is available as `mm` and `meetily-memory`.

## Quick Start

Initialize the local index:

```bash
mm init
```

Search meeting history:

```bash
mm s "migration risk"
```

Each result includes the meeting, matching source excerpt, and command for
opening the original:

```text
#10 Meeting 2026-07-06
open: mm open 10

12:56:36 | chunk #3863
If I write to the database, I must also publish to Kafka...
```

Open the meeting:

```bash
mm open 10
```

Group related meetings with a tag and find them through the same search:

```bash
mm tag add 10 11 migration
mm s "migration"
```

If Meetily cannot be discovered automatically:

```bash
mm doctor
```

## Search

`mm s` is the main product interface:

```bash
mm s "owner_worker_id"
mm s "what did we decide about migration?"
```

Use `--context N` when a matching excerpt needs adjacent transcript chunks:

```bash
mm s "migration risk" --context 2
```

## Tags

Assign one or more tags to several meetings:

```bash
mm tag add 10 11 migration,backend
mm tag add 10 11 "system design"
```

List active tags or tags for one meeting:

```bash
mm tag list
mm tag list 10
```

Remove assignments:

```bash
mm tag remove 10 11 migration
```

Tags are normalized for matching, survive index rebuilds, and participate in
ordinary `mm s` searches even when the tag text is absent from the transcript.

## Refresh

Refresh the local index after new meetings:

```bash
mm refresh
```

Automatic refresh is optional:

```bash
mm autosync start
mm autosync status
```

Update the installed CLI:

```bash
mm update
```

## Privacy

- Meetily remains the read-only source of truth.
- `index.sqlite` contains derived data and can be rebuilt.
- Explicit user state, including tags, is kept in `state.sqlite` separately
  from the disposable index.
- Search and indexing run locally.

## Advanced Commands

Context, topics, semantic search, Obsidian, LLM, MCP, database diagnostics, and
source-rebinding commands remain available but are not part of the main
workflow. See [docs/commands.md](docs/commands.md) and
[docs/integrations.md](docs/integrations.md).

## Development

See [docs/development.md](docs/development.md).

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).
