# Command Reference

Meetily Memory is centered on `mm s → source evidence → mm open`. Meetily SQLite is read-only;
`index.sqlite` is derived; explicit tags and source identity are stored separately in `state.sqlite`.

## Main workflow

| Command | Role |
|---|---|
| `mm init [--source PATH]` | Discover or select a Meetily database and create the local index. |
| `mm refresh [--source PATH]` | Refresh the index and optionally run configured Obsidian sync. |
| `mm s QUERY` | Search meetings by lexical evidence and manual tags. |
| `mm open --source-uuid UUID --external-id ID` | Open a meeting by stable `MeetingRef`. |
| `mm doctor [--source PATH]` | Check source access, SQLite/FTS5, index/state health, and refresh state. |
| `mm status` | Show index/state/source status, language, last refresh, and Obsidian configuration. |
| `mm update` | Update a Homebrew installation. |

`mm open --external-id ID` remains a convenience lookup only when the external ID is unambiguous.
Generation-local integer IDs are not accepted by `mm open` and must not be persisted in scripts,
notes, or commands.

## Search

```bash
mm s "migration risk"
mm s "migration risk" --context 2
mm s "product integration" --since 7d
mm s "product integration" --from 2026-08-17 --to 2026-08-23
```

`--context N` adds neighboring source chunks to each lexical match. It does not invoke a separate
context-generation product. `--since Nd` uses a rolling N-day window. `--from` and `--to` use local
calendar dates, with the complete `--to` day included. `--since` and `--from` are mutually exclusive.

Every displayed open command uses the stable form:

```bash
mm open --source-uuid UUID --external-id ID
```

## Manual tags

Tag mutations require stable meeting references. Repeat `--external-id` to mutate several meetings
from the same source atomically.

```bash
mm tag add "migration,system design" \
  --source-uuid UUID \
  --external-id EXTERNAL_ID_A \
  --external-id EXTERNAL_ID_B

mm tag remove migration \
  --source-uuid UUID \
  --external-id EXTERNAL_ID_A

mm tag list
mm tag list --source-uuid UUID --external-id EXTERNAL_ID_A
mm tag suggest --source-uuid UUID --external-id EXTERNAL_ID_A
```

Tags live in `state.sqlite` by `MeetingRef(source_uuid, external_id)` and survive disposable index
rebuilds. A batch containing a missing meeting fails before any tag is changed.

## Source and settings maintenance

| Command | Role |
|---|---|
| `mm config language en|ru|auto` | Set or clear the UI language. |
| `mm config source NEW_PATH` | Select a validated Meetily database as a source identity. |
| `mm config source NEW_PATH --rebind [--source-uuid UUID]` | Explicitly repair the path of an existing source UUID. |
| `mm db status` | Inspect index/state schema and migration status without modifying databases. |
| `mm scan --source PATH` | Low-level indexing command for debugging and tests. |

Refresh, source selection, and rebind share `refresh.lock`; a second writer fails instead of writing
concurrently.

## Obsidian

```bash
mm obsidian init
mm obsidian sync
mm obsidian status
```

Obsidian is an optional, hidden advanced command group: it does not appear in top-level `mm --help`,
but the commands above can be invoked directly. Generated meeting open commands always contain a
full `MeetingRef`. Sync currently emits the existing rich managed note network described in
[integrations.md](integrations.md).

## Offline semantic evaluation

Semantic retrieval is not a CLI or refresh strategy. The optional `semantic` dependency exists only
for reproducible offline experiments through `scripts/evaluate-semantic-search.py`.
