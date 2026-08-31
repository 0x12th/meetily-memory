# Meetily Memory

Search past Meetily meetings locally, verify the relevant transcript excerpt, and open the original.
Meetily Memory is for Meetily users who need to recover what was actually discussed without sending
meeting data to another service.

## Install

On macOS:

```bash
brew tap 0x12th/meetily-memory
brew install meetily-memory
```

The CLI is available as `mm` and `meetily-memory`.

## Quick Start

```bash
mm init
mm s "migration risk"
mm open SOURCE_UUID/EXTERNAL_ID
```

Search results include transcript excerpts and a complete `mm open ...` command. Copy that command
from the result to open the original meeting; do not substitute the displayed result number.

## Refresh

After Meetily has new meetings, refresh the local index manually:

```bash
mm refresh
```

## Lexical Search

Search is lexical: it works best with words, names, identifiers, and phrases that appear in the
transcript. It may miss paraphrases or questions phrased differently from the discussion.

Use neighboring transcript chunks or date filters when needed:

```bash
mm s "migration risk" --context 2
mm s "product integration" --since 7d
mm s "product integration" --from 2026-08-17 --to 2026-08-23
```

Run `mm s --help` for the current search options.

## Privacy

Meetily Memory reads the Meetily source database without modifying it. Indexing and search run
locally, and meeting data is not sent to a cloud service.

## Optional Organization

Search works without manual organization. To add or remove meeting tags, start with:

```bash
mm tag --help
```

Obsidian sync is optional and manual. It writes managed notes only under `Meetings/` and `Tags/`:

```bash
mm obsidian init --vault /path/to/vault
mm obsidian sync
mm obsidian status
```

Run `mm obsidian
--help` for the current commands.

## Troubleshooting and Help

If Meetily cannot be discovered or the local setup is unhealthy:

```bash
mm doctor
```

Use `mm --help` or `mm COMMAND --help` as the command reference.

## Development

See [docs/development.md](docs/development.md).

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).
