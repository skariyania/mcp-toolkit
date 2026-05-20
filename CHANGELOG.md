# Changelog

All notable changes to mcp-toolkit are recorded here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-05-21

### Added
- `mcp_memory_sync.py` — mirror the Memory MCP knowledge-graph JSONL
  between Windows and WSL. Default behaviour is a bidirectional union
  merge: entities are deduped by `name` (observations merged preserving
  order), relations by the triple `(from, to, relationType)`. One-way
  modes `--direction wsl-to-windows` / `windows-to-wsl` are supported.
  Each side is backed up to `.bak.<ts>` before any overwrite. Dry-run
  by default; pass `--apply` to write. Exit codes: 0 no-op, 1 changes
  pending, 2 error, 3 missing `MEMORY_FILE_PATH`.
- `mcp.py memory` — interactive subcommand that wraps `mcp_memory_sync.py`
  with the same dry-run-then-confirm UX the other flows use.
- `.gitignore` — ignore Devin local state (`.devin/`).

### Changed
- README updated with a Memory sync section (rationale + flows + exit
  codes + caveats) and the 5-script overview table.
- `mcp.py` topology output gained a row for the memory-graph file
  locations on each OS.

## [1.0.0] - 2026-05-20

Initial release.

- `mcp.py` — unified interactive entry point + subcommands.
- `mcp_doctor.py` — diagnose MCP server availability across Windsurf,
  Cursor, Claude Desktop, VS Code, Devin on Windows/macOS/Linux/WSL.
- `mcp_sync.py` — translate-and-distribute master MCP config across
  tools and OS boundaries. Skips servers that can't be safely translated.
  Backs up to `.bak.<ts>` before every overwrite.
- `mcp_sync_daemon.py` — scheduled wrapper for `mcp_sync.py` with
  Markdown reports and desktop toasts.
