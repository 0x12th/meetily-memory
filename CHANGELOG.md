# Changelog

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
