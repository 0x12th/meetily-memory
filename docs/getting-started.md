# Getting Started

Meetily Memory builds a private local index from your Meetily meeting database.
It treats Meetily as a read-only upstream source and stores derived state in its
own `index.sqlite`.

## Install

On macOS:

```bash
brew tap 0x12th/meetily-memory
brew install meetily-memory
```

The CLI is available as both:

```bash
meetily-memory
mm
```

Verify the installation:

```bash
mm --help
```

## First Run

Meetily Memory automatically discovers the Meetily database in common locations
when possible.

Start with:

```bash
mm doctor
mm update
```

If autodiscovery does not find your database, specify it explicitly:

```bash
mm doctor --source /path/to/meeting_minutes.sqlite
mm update --source /path/to/meeting_minutes.sqlite
```

Once the local index is built, try:

```bash
mm s "pricing decision"
mm c "what did we decide about pricing?"
mm topic "migration"
mm tasks
```

## Source Discovery

`--source` is optional.

By default, `mm doctor`, `mm update`, and `mm scan` search common application
data locations for the Meetily database.

The source may be:

- `meeting_minutes.sqlite`;
- legacy `meeting_minutes.db`;
- a directory containing one of those files.

You can configure the source once:

```bash
export MEETILY_MEMORY_SOURCE=/path/to/meeting_minutes.sqlite
```

Meetily Memory stores its own search index separately using the platform default
data directory. Override the index location when needed:

```bash
mm --index /path/to/index.sqlite scan
```

## Example Workflow

```bash
mm doctor
mm update

mm s "migration risk"
mm c "what risks did we discuss for the migration?"

mm decisions
mm topic "migration"
mm graph "migration" --json
mm project "migration"
mm person "Vladimir"

mm open 2
```
