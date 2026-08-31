# Changelog

## Unreleased

- Remove MCP, background autosync, and the context/topic command families from the installed runtime.
- Require stable source-aware meeting references for generated open commands and tag mutations;
  generation-local numeric IDs are no longer accepted by those interfaces.
- Keep lexical search, neighboring-chunk expansion, date filters, manual tags, and the hidden advanced
  Obsidian integration on the current incremental index architecture.

## 0.7.0 - 2026-08-29

- Make meeting and evidence identity source-aware, canonicalize source paths, and rebuild disposable
  indexes safely when adopting the new source UUID and materialized evidence formats.
- Make search, context, topic, graph, status, and doctor use bounded read-only snapshots without
  creating databases, migrating schemas, or mutating user state.
- Persist manual topic aliases and source/task/tag ownership in `state.sqlite` so explicit user
  intent survives index deletion, incompatible rebuilds, interruptions, and concurrent retries.
- Publish incremental refreshes atomically, keep readers on the previous completed snapshot until
  commit, and report settings or Obsidian failures separately from a successful index update.
- Give generated Obsidian notes stable typed identities, collision-resistant filenames, exact
  managed markers, and safe preflight reconciliation across duplicate and Unicode titles.
- Add an exact-archive release smoke for both macOS architectures that proves version, SQLite/FTS,
  `init`, source-backed search, and read-only diagnostics before checksums or GitHub upload.

## 0.6.0 - 2026-08-28

- Collapse Core search to one typed meeting-level contract and add consistent `--since`,
  `--from`, and `--to` date filters across CLI and MCP search.
- Serialize refresh writers, record honest scan-run failures, and reconcile meetings deleted
  from the read-only Meetily source so stale search results disappear.
- Remove unreachable and unproven CLI surfaces, including local LLM answering and product
  semantic commands; keep semantic retrieval isolated as optional offline research tooling.
- Narrow experimental MCP to local stdio `search_meetings` and `get_meeting` tools backed by the
  same search contract as `mm s`.
- Keep Obsidian settings in the selected global or workspace scope and reconcile deleted or
  renamed managed notes without touching unmarked user files.

## 0.5.0 - 2026-07-28

- Focus the public workflow on meeting search, opening original records, and explicit
  meeting tags.
- Add persistent tags in `state.sqlite`, including add, remove, list, and non-mutating
  suggestions based on existing tags.
- Return one ranked result per meeting from `mm s`, combining exact tag matches, lexical
  evidence, and token-level tag matches with bounded source context.
- Add the opt-in Core v3 meeting-result contract while preserving the existing Core v1 and
  v2 payloads for API and MCP consumers.
- Extend retrieval evaluation with meeting-level and tag-only tasks, reproducible fingerprints,
  and strict semantic-index coverage checks.
- Keep hybrid lexical/semantic retrieval experimental: it remains disabled until its promotion
  gate demonstrates improvements on semantic and paraphrase tasks without regressions.

## 0.4.1 - 2026-07-20

- Prevent commands using an explicit temporary `--index` from replacing the selected source
  in the default user settings.
- Make `mm autosync start` install and activate the launchd or systemd scheduler instead of
  only storing an enabled flag.
- Make `mm init --autosync`, `mm autosync stop`, and both status commands manage and report
  the real scheduler lifecycle.
- Pin background refreshes to their index and data directory, keep the executable path stable
  across Homebrew upgrades, and write dedicated autosync stdout/stderr logs.
- Report `autosync: misconfigured` when saved configuration, scheduler files, and runtime state
  disagree.

## 0.4.0 - 2026-07-18

- Add `mm config source NEW_PATH` for selecting a validated Meetily database and
  `--rebind` for explicitly preserving source identity after a verified move or restore.
- Persist source identity and task status in dedicated user state so index migrations and
  rebuilds do not discard user-owned state.
- Add stable, versioned evidence and context contracts for retrieval consumers, with an
  explicit opt-in Core v2 path for MCP clients.
- Keep `mm c` lexical and source-ranked by default while allowing bounded neighboring
  excerpts through explicit `--context N`.
- Add reproducible retrieval evaluation datasets, manifests, comparison gates, and reports
  so experimental strategies can be measured without changing the stable search path.
- Tighten heuristic task extraction and document the measured product gates before task
  workflows can be promoted beyond experimental status.

## 0.3.2 - 2026-07-06

- Add `mm s --context/-C` to expand search hits with neighboring chunks.
- Rework `mm t` as a source-backed topic dossier with supporting excerpts even
  when structured signals are empty.
- Make topic sections use cautious "possible" labels for heuristic decisions,
  tasks, risks, and questions.
- Add stable CLI UI language configuration via `mm config language en|ru|auto`.
- Keep topic alias expansion data-driven through stored aliases instead of
  built-in term dictionaries.
- Update README and command docs around the search-first stable contract and
  experimental topic/Obsidian/LLM/MCP surfaces.

## 0.3.1 - 2026-07-05

- Replace the public topic command with the shorter `mm t` while keeping the
  old `mm topic` path hidden for compatibility.
- Demote experimental LLM answering from the everyday README/help path and keep
  `mm c` as the recommended LLM context handoff.
- Make topic output clearer as a source-backed "what we know" dossier with
  heuristic signal labels and meeting-language-aware headings.
- Improve Cyrillic topic matching for simple inflected forms such as
  `миграция` / `миграции`.
- Refresh test fixtures and examples to use Dobrynya consistently.

## 0.3.0 - 2026-07-05

- Narrow the public CLI around init, update, status, search, context, topic memory, semantic search, ask, Obsidian sync, autosync, doctor, and advanced diagnostics.
- Split the 1500+ line CLI and overloaded index repository into focused command, repository, memory, and database modules.
- Keep the graph as an internal knowledge layer for topic memory, ask, Obsidian, and MCP instead of a public command surface.
- Replace export-style Obsidian behavior with managed sync notes for meetings, topics, people, tasks, decisions, risks, and questions.
- Add `mm semantic init` and `mm llm init` as the consistent setup entry points.
- Move MCP dependencies behind the `mcp` optional extra for pip installs.
- Remove Spotlight and broad export leftovers from the focused core API.

## 0.2.0 - 2026-07-04

- Add Spotlight-friendly Markdown export and clean commands.

## 0.1.0 - 2026-07-04

Initial public release of Meetily Memory.

- Add local-first Meetily SQLite scanner and incremental index.
- Add SQLite FTS5 search with `mm s`.
- Add context builder with `mm c`.
- Add meeting structure extraction for decisions, action items, risks, and open questions.
- Add actionable CLI output with meeting ids, chunk ids, and `mm open` hints.
- Add best-effort person lookup with Cyrillic-safe FTS matching.
- Add macOS binary release workflow and Homebrew tap formula support.
