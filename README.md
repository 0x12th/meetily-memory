# Meetily Memory

Find past Meetily discussions, verify them against source excerpts, and open
the original meeting.

Meetily Memory reads the local Meetily database, builds a private local index,
and never modifies the source database or sends meeting data to a cloud
service.

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

Each result includes the meeting, a matching source excerpt, and a stable command for opening the
original. Use the generated `MeetingRef` command rather than an index-local numeric identifier:

```bash
mm open --source-uuid 8fd43c7b-4e82-4e91-aab1-6bd92131bc20 --external-id EXTERNAL_ID
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
mm s "product integration" --since 7d
mm s "product integration" --from 2026-08-17 --to 2026-08-23
```

`--since Nd` searches from the current moment minus N days through now. Calendar filters use
local dates: `--from` includes the start date and `--to` includes the entire final day.
`--since` and `--from` are mutually exclusive.

Use `--context N` when a matching excerpt needs adjacent transcript chunks:

```bash
mm s "migration risk" --context 2
```

## Keep the Index Fresh

Refresh the local index after new meetings:

```bash
mm refresh
```


Update the installed CLI:

```bash
mm update
```

## Optional: Organize With Tags

Search works without manual organization. Add a tag only when several meetings
belong to a project or topic that is not named consistently in their
transcripts:

```bash
mm tag add migration \
  --source-uuid 8fd43c7b-4e82-4e91-aab1-6bd92131bc20 \
  --external-id EXTERNAL_ID
mm s "migration"
```

See the [command reference](docs/commands.md) for listing, suggesting, and
removing tags.

## Privacy

- Meetily remains the read-only source of truth.
- `index.sqlite` contains derived data and can be rebuilt.
- Explicit user state is kept separately from the disposable index.
- Search and indexing run locally.

## Advanced Commands

Obsidian, database diagnostics, and source-rebinding commands remain available as hidden advanced
commands outside the main workflow. Semantic retrieval is isolated to offline evaluation tooling.
See [docs/commands.md](docs/commands.md) and [docs/integrations.md](docs/integrations.md).

## Development

See [docs/development.md](docs/development.md).

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).
