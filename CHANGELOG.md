# Changelog

## Unreleased

- Split the root README into a concise product overview and detailed docs under `docs/`.
- Add export-only integration adapters for Obsidian notes, Gbrain JSONL, Markdown bundles, and generic task tracker drafts.
- Add a local MCP server that exposes Core API tools over FastMCP.
- Add a versioned Core API for retrieval, context, memory, topic, graph, and task-status operations.
- Add SQLite knowledge nodes/edges, topic memory, topic graph projection, and local task status overrides.
- License the project under Apache License 2.0.
- Up perfomance.

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
