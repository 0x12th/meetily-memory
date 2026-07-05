# Integrations

Meetily Memory keeps one full integration in the near-term public product:
Obsidian.

Other adapters can exist internally or experimentally, but they should not shape
the first public workflow until they clearly strengthen Search, Context, or Ask.

## Obsidian Sync

Obsidian is a sync integration, not a one-file export.

Public commands:

```bash
mm obsidian init
mm obsidian sync
mm obsidian status
```

`mm obsidian init` asks for:

- vault path, defaulting to `~/Documents/Obsidian`;
- folder, defaulting to `Meetily Memory`;
- whether to run sync after every `mm update`.

`mm obsidian sync` creates and updates a managed note network:

```text
Topics/
Meetings/
People/
Tasks/
Decisions/
Risks/
Questions/
```

This gives Obsidian a useful graph without exposing Meetily Memory's internal
graph projection as a user-facing command.

Managed files must include:

```html
<!-- meetily-memory:managed -->
```

The sync command may update managed files, but it must not overwrite unrelated
user notes in the vault.

## Automatic Post-Update Sync

If enabled during `mm obsidian init`, Obsidian sync runs after `mm update`.

The post-update flow is:

```text
mm update
semantic index, if semantic search is configured
obsidian sync, if Obsidian post-update sync is enabled
```

## Not In The Public Integration Layer

The following are out of the first public integration surface:

- Gbrain JSONL export;
- generic Markdown bundle export;
- task tracker draft export;
- one-off Obsidian topic export.

If these remain in the repository, they should be internal or experimental
until a repeated workflow proves they are worth stabilizing.
