# Changelog

All notable changes to mcp-toolkit are recorded here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-05-21

### Fixed
- `mcp_memory_sync.py`: dry-run summary now reflects the chosen direction.
  One-way modes (`wsl-to-windows`, `windows-to-wsl`) explicitly enumerate
  what data the target loses, not just what it gains, with `!!`-prefixed
  warnings and a suggestion to switch to `bidirectional`. The previous
  wording could imply "+5 gain" while silently overwriting 34 unique
  target-side entities. (Exit codes were already correct; this changes
  the wording and adds an overlap-stats block to the dry-run output.)
- `mcp_memory_sync.py`: one-way modes now correctly flag pending changes
  even when the target loses data with zero gains. Previously
  `--direction wsl-to-windows` against an empty WSL graph would report
  "no changes needed" while `--apply` would have wiped the target.
- `mcp_memory_sync.py`: `--windows-file` / `--wsl-file` overrides are now
  used as-is (paths this process can open directly), not run through the
  cross-OS path translator that assumes config-declared native syntax.

### Added
- `mcp_memory_sync.py --self-test`: inline assertions for the direction-
  accurate output (T1, T2, T2b, T2c). Run via `uv run mcp_memory_sync.py
  --self-test`. No new dependencies, no `tests/` directory.
- `mcp.py repair <path>` subcommand: dry-run / `--apply` conservative
  auto-fix for common JSON syntax errors (missing comma between sibling
  values, trailing comma before `}` / `]`). Shows source-window context
  with a caret on the failing column, classification, and a unified diff
  before writing. Refuses to guess on anything else.
- `mcp.py` internal helpers (also re-importable):
    - `write_in_place(path, text)` — open-truncate-write in place
      (mode `r+` for existing, `w` for new), creates `.bak.<ts>`
      before mutation, asserts `st_nlink` is unchanged across the write.
      Catches future regressions that would silently break the NTFS
      hardlink chain that the master MCP config depends on.
    - `try_repair_simple_json(text)` — pure function returning
      `(repaired_text, summary)` or `(None, reason)`; never guesses.
- `mcp.py --self-test`: inline assertions for `write_in_place` and
  `try_repair_simple_json` (T3, T4, T4b, T5, T5b). Includes hardlink
  preservation verification on a tmpfs file with link count 2.

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
