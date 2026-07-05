# Changelog

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
