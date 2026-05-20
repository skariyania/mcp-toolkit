# mcp-toolkit

> One control panel for **Model Context Protocol** (MCP) configs across **Windows + WSL** and across **Windsurf, LM Studio, VS Code, and Devin**.
>
> Diagnose what's broken, push your master config to every tool, and keep them in sync on a schedule — with dry-runs, diffs, backups, and desktop notifications.

Stdlib-only Python. No `pip install`. Cross-platform.

> **Note (Sahil):** the old path `~/dev/personal/tools/mcp/` now resolves into this repo via a Windows junction / WSL symlink — old commands keep working.

---

## Why this exists

If you use MCP servers (Atlassian, GitHub, Postgres, custom servers, …) across multiple AI tools and OSes, you'll eventually hit some combination of:

- **5 different config files** with **2 different schemas** (Windsurf/Cursor/Claude/Devin use `mcpServers`, VS Code uses `servers`).
- **Secrets scattered** across `secrets.env`, `.env`, inline JSON `env` blocks, and OS-level env vars.
- **Wrapper scripts** on Linux (the `bash mcp-foo.sh` pattern) that don't run on Windows-native.
- **Drift** — you change one config, forget the others, and your friend asks "why doesn't your `mcp-doctor` setup work on my machine?"
- **Silent failures** — Windsurf says "no MCP access" with no clue why; toasts get suppressed by an unregistered AppId; `npx` package cold-start exceeds the IDE's discovery timeout.

`mcp-toolkit` solves all of that with four small Python scripts that compose into one safe, interactive tool.

---

## What you get

| Script | One-line summary |
|---|---|
| **`mcp.py`** | Front-door interactive menu (or subcommand). Always shows dry-run + diff before applying. Recommended default entry point. |
| `mcp_doctor.py` | Smoke-test every MCP server in your config. Auto-detects Windsurf/Cursor/Claude/VS Code/Devin configs across Windows/macOS/Linux/WSL. |
| `mcp_sync.py` | Translate-and-distribute one master config to all tools. Unwraps bash wrapper scripts. Skips servers it can't translate. Backs up before every write. |
| `mcp_sync_daemon.py` | One-shot scheduled wrapper (cron / Task Scheduler). Markdown reports + desktop toasts. Skips silently if not due. |

`mcp.py` invokes the others under the hood. Power users can call any script directly.

---

## Quick start

### 1) From a fresh check-out, run the menu

```bash
# Inside the repo:
uv run mcp.py
# or, if you don't have uv:
python3 mcp.py
```

You'll see:

```
=========================================
  MCP — main menu
=========================================
Pick an action:
  1) Diagnose       — test which MCPs are working right now
  2) Sync           — push master config to other tools (safe: dry-run first)
  3) Schedule       — set up periodic sync
  4) Topology       — show where MCP files live across OSes
  q) Quit
```

### 2) Common one-liners

```bash
# Find out what's working now
uv run mcp.py doctor

# Translate-and-distribute (interactive, dry-run -> confirm -> apply)
uv run mcp.py sync

# Show the env-var config + cron / schtasks command
uv run mcp.py schedule

# Where does everything live?
uv run mcp.py topology
```

### 3) Drop a daily auto-sync into cron / Task Scheduler

```cmd
:: Windows (run once)
schtasks /create /tn "MCP Sync" /sc daily /st 09:00 /tr ^
  "uv run \"%USERPROFILE%\dev\personal\mcp-toolkit\mcp_sync_daemon.py\""
```

```bash
# WSL (crontab -e), default frequency = 1 day
0 9 * * *  /home/$USER/.local/bin/uv run /home/$USER/dev/personal/mcp-toolkit/mcp_sync_daemon.py >> ~/.local/state/mcp-sync/cron.log 2>&1
```

The daemon de-bounces — running it more frequently just causes silent skips until `MCP_SYNC_FREQUENCY` elapses.

---

## How the four scripts compose

```
                 +-------------------+
   user  ─►      │     mcp.py        │  interactive menu / subcommands
                 +---------+---------+
                           │
              ┌────────────┼────────────────────────┐
              ▼            ▼                        ▼
      mcp_doctor.py  mcp_sync.py         mcp_sync_daemon.py
      (diagnose)     (translator +        (scheduler wrapper:
                      distributor)         calls mcp_sync.py,
                                           writes report,
                                           fires toast)
```

Each script is **self-contained, stdlib-only, and re-runnable**. They share no state in memory — only files (config JSON, secrets `.env`, state file, reports).

---

## Detailed flows

### Diagnose — "which MCPs are actually working?"

```bash
uv run mcp.py doctor
```

Steps:
1. Auto-detects all known MCP config locations on this machine.
2. Lists them; you pick one (or accept the first detected).
3. Optionally narrows to a single server.
4. Spawns each server, sends JSON-RPC `initialize` + `tools/list`, reports:
   - `OK <name> (<latency>ms, <N> tools)` — server started and listed its tools
   - `SLOW <name>` — responded but past the slow threshold (cold start)
   - `FAIL <name>: <reason or first stderr line>` — wrapper exited or never responded
5. Pre-flight checks `docker`, `npx`, `uvx`, `python3` so a missing binary on the launcher's PATH is surfaced immediately.

### Sync — "push my master to everywhere else, safely"

```bash
uv run mcp.py sync
```

Walks you through:
1. **Direction** — WSL master → Windows tools, or Windows master → WSL tools, or same-OS, or custom.
2. **Source path** — pre-filled with the right default for the chosen direction.
3. **Targets** — toggleable multi-select for `windsurf`, `lmstudio`, `vscode`, `devin`, `master`.
4. **Mirror secrets?** — explicit yes/no (the warning about plaintext crossing OS boundaries is shown).
5. **Dry-run preview** — full unified diff for each target file, plus skipped servers and any missing secret keys.
6. **Confirm** — `Apply these changes now? [y/N]`. Default is **N**. Press Enter to cancel.
7. **Apply** — only after explicit `y`. Each target file is backed up to `<file>.bak.<timestamp>` before being overwritten.

#### What "translation" means

When syncing WSL → Windows (or any direction crossing the OS boundary), the translator handles:

| Source pattern | Target translation |
|---|---|
| `bash /home/.../wrappers/mcp-foo.sh` | Unwrapped to inline `docker run --env-file <target secrets path>` (or `npx`/`uvx`) |
| `secrets.env` referenced by a wrapper | Path translated to the target OS's secrets file (`.env` on Windows) |
| Schema `mcpServers` | Stays as `mcpServers`, except VS Code targets get `servers` |
| VS Code-specific `type: "stdio"` | Auto-injected for VS Code targets |
| Devin's other top-level fields (permissions, theme, agent) | Preserved (only `mcpServers` block is replaced) |

Servers that **cannot** be safely translated — for example, Postgres MCPs that depend on the `mcp-tool-rename.py` shim (POSIX-only) — are **skipped** with a clear reason. No silently-broken configs ever get written.

### Schedule — "keep them in sync automatically"

```bash
uv run mcp.py schedule
```

Shows your resolved env-var config (`MCP_SYNC_*`) and prints the OS-native install command for either Windows Task Scheduler or WSL cron. **Nothing is installed automatically** — you copy the command yourself so you stay in control.

The daemon (`mcp_sync_daemon.py`):
- Reads config from env vars (with sensible defaults).
- Skips silently if the last successful run was within `MCP_SYNC_FREQUENCY`.
- Otherwise calls `mcp_sync.py` with the configured flags.
- Writes a Markdown report (`sync-<ts>.md` + `latest.md`) to `MCP_SYNC_REPORT_DIR`.
- Updates state **only on non-fatal exits**, so persistent failures keep retrying on each scheduled invocation (and keep firing notifications until you fix the cause).
- Sends a desktop toast on error / partial-success / configurable.

### Topology — "remind me where everything lives"

```bash
uv run mcp.py topology
```

Prints the complete cross-OS map of master configs, secrets, wrappers, reports, and where each tool reads its config from.

---

## Configuration (env vars)

All env vars are **optional**; sensible defaults come from your current OS.

| Variable | Default | Purpose |
|---|---|---|
| `MCP_SYNC_USER` | current user | Used in report headers / greetings |
| `MCP_SYNC_FREQUENCY` | `1d` | Min interval between scheduled syncs (`60s`, `30m`, `12h`, `1d`, `1w`) |
| `MCP_SYNC_SOURCE` | OS-aware master path | WSL: `~/.codeium/windsurf/mcp_config.json`; Windows: `~/.config/mcp/servers.json` |
| `MCP_SYNC_TARGET_OS` | opposite of current OS | `windows` \| `wsl` |
| `MCP_SYNC_TARGETS` | `windsurf,lmstudio,vscode,devin` | Comma list or `all` |
| `MCP_SYNC_APPLY` | `yes` | Periodic sync should sync. Set `no` for a recurring dry-run alarm. |
| `MCP_SYNC_MIRROR_SECRETS` | `no` | Plaintext tokens crossing OS boundary requires explicit opt-in. |
| `MCP_SYNC_NOTIFY_ON` | `error` | `error` \| `always` \| `never` |
| `MCP_SYNC_REPORT_DIR` | OS-aware | Windows: `%LOCALAPPDATA%\mcp-sync\reports`; Linux: `~/.local/share/mcp-sync/reports` |
| `MCP_SYNC_STATE_DIR` | OS-aware | `%LOCALAPPDATA%\mcp-sync` / `~/.local/state/mcp-sync` |

To set them persistently on Windows: `setx VAR value`. On WSL: append `export VAR=value` to `~/.bashrc` or `~/.profile`.

---

## File locations the tools create / read

| Path (Windows) | Path (WSL/Linux) | Owner |
|---|---|---|
| `%LOCALAPPDATA%\mcp-sync\last-run.json` | `~/.local/state/mcp-sync/last-run.json` | daemon (state) |
| `%LOCALAPPDATA%\mcp-sync\reports\sync-<ts>.md` | `~/.local/share/mcp-sync/reports/sync-<ts>.md` | daemon (report) |
| `%LOCALAPPDATA%\mcp-sync\reports\latest.md` | `~/.local/share/mcp-sync/reports/latest.md` | daemon (latest report copy) |
| `%TEMP%\mcp_sync_toast.ps1` | n/a | daemon (notification helper) |
| `<target file>.bak.<ts>` | (same) | sync (backup before overwrite) |

The toolkit **never** writes to `secrets.env` / `.env` automatically — only when you pass `--mirror-secrets` and confirm `yes` at the prompt.

---

## Limitations (read this!)

These are real, by-design boundaries — not bugs.

### Sync is one-way **replace**, not merge

Sync from a 2-server WSL master to a 3-server Windows Windsurf config will produce a **2-server** result on Windows. The 3rd server (which only existed on Windows) is **gone from the live file** but **recoverable from the `.bak.<ts>` backup**.

Practical implication: if you have an MCP server that only makes sense on one OS, either:
- Add it to the master and accept that translation will skip it (e.g. `mcp-tool-rename.py`-shimmed Postgres MCPs naturally do this), **or**
- Use `--targets <subset>` to exclude the target file that has the OS-specific server, **or**
- Restore from `.bak.<ts>` if you sync and lose something.

There's currently no `"sync_only_to": ["windows"]` per-server marker. It could be added later if needed.

### Bash wrapper unwrapping recognizes specific patterns only

The translator handles wrappers that follow this exact shape:

```bash
#!/usr/bin/env bash
set -euo pipefail
set -a; source "$(dirname "$0")/../secrets.env"; set +a
export VAR1="literal"   # zero or more of these
exec docker run …       # or npx / uvx / python3
```

If your wrapper does anything else (multiple `exec` lines, custom logic, conditional `if` blocks, etc.), it will be **skipped** with the reason `unsupported wrapper: …`. Skipping is the safe default — never silently emits broken config.

### `mcp-tool-rename.py`-shimmed servers can't translate to Windows

This shim is a Linux-only Python helper that lives next to the wrapper. The translator detects it and skips with the reason `uses mcp-tool-rename.py shim (POSIX-only)`. To use those servers on Windows, you'd need to port the shim or use a different collision-avoidance strategy (e.g. running each Postgres MCP in its own container with separate names).

### Cross-OS path translation assumes a specific user-directory layout

For UNC paths (`\\wsl.localhost\Ubuntu\home\<user>`) and `/mnt/c/Users/<user>` translation, the toolkit picks the first user dir it finds (or the literal user `skariyania` as fallback). On a fresh machine with a different username, the cross-OS path expansion logic may need a small tweak (`expand_for_os` in `mcp_sync.py` and `_default_*` helpers).

For same-OS sync, this isn't an issue — `${USERPROFILE}` / `$HOME` resolve normally.

### macOS / Linux native (non-WSL) support

- **`mcp_doctor.py`** — fully supported. Auto-detects `~/Library/Application Support/Claude/...` (macOS), `~/.config/Claude/...` (Linux), VS Code, Cursor, Windsurf, Devin.
- **`mcp_sync.py`** — works for same-OS sync (e.g. Linux master → Linux Windsurf). Cross-OS translation is currently Windows ↔ WSL specific.
- **`mcp_sync_daemon.py`** — works on Linux (notifications via `notify-send`) and macOS (notifications via `osascript`).

### Windows toast notifications need an AppId registered in `HKCU` (handled automatically)

Newer Windows silently dismisses toasts whose `AppUserModelID` isn't registered. The daemon's first call writes:

```
HKCU\Software\Classes\AppUserModelId\MCP.Sync
  └─ DisplayName = "MCP Sync"
```

This is a single registry value, no admin needed. If you ever wipe `HKCU` (unusual) or your friend doesn't see notifications, run `uv run mcp_sync_daemon.py --dry-notify` once to re-register.

If the toast still doesn't show, check:
1. Settings → System → Notifications → "MCP Sync" is allowed
2. Focus Assist / Do Not Disturb is off
3. Notifications are globally enabled

### Frequency parser is simple

Accepts `<int><suffix>` with suffix in `s|m|h|d|w`. No "every weekday", no "9:30 PM", no cron expressions. The OS-native scheduler handles the recurrence pattern; this script just de-bounces.

### Memory MCP needs an explicit `MEMORY_FILE_PATH`

If `MEMORY_FILE_PATH` is empty (the default of `@modelcontextprotocol/server-memory`), the graph is silently ephemeral and resets on restart. The toolkit assumes you set it explicitly (master configs in this setup do; bare `npx` invocations elsewhere may not).

---

## Troubleshooting

### "Windsurf says no MCP access"

Run `uv run mcp.py doctor` from inside WSL (or wherever the IDE is running). The most common causes (in order):

1. Docker Desktop down or WSL2 integration off → wrappers that use `docker run` all silently fail.
2. PATH problem — Windsurf launches wrappers with a minimal env; `docker` / `npx` / `uvx` aren't visible if they live in `nvm` / `pyenv` / `~/.local/bin`.
3. Cold-start latency on many servers exceeds the IDE's MCP discovery timeout. Reduce the number of enabled servers or pre-pull packages (`npx -y <pkg> --help`, `docker pull <image>`).
4. `set -euo pipefail` in a wrapper exits on a typo in `secrets.env`.

### "Sync says my server was skipped"

Read the skip reason. It will be one of:

- `unsupported wrapper: uses mcp-tool-rename.py shim (POSIX-only)` — Linux-only by design; can't translate to Windows.
- `unsupported wrapper: expected exactly one 'exec' line, found 0` — your wrapper doesn't have a recognizable `exec` invocation. Edit the wrapper to fit the pattern, or sync this server manually.
- `unsupported wrapper: exec command 'foo' not in (docker|npx|uvx|python3)` — the toolkit only knows how to inline these four. PR the others if needed.
- `command not found` — the source command isn't on PATH for your sync session.

### "Translated config doesn't actually start the server in the target IDE"

Run `uv run mcp.py doctor` against the **target** config (not the source). The doctor's failure hints will pinpoint PATH / Docker / auth issues that are specific to the target machine.

### "I want to undo a sync"

Each target file has a `<file>.bak.<timestamp>` next to it. Just rename the most recent backup back over the live file:

```bash
cd ~/.codeium/windsurf
mv mcp_config.json.bak.20260520-205530 mcp_config.json
```

### "Sync overwrote a server I wanted to keep"

Same fix as above. To prevent it: either add the server to the master, or omit the target from `--targets` next time.

---

## Conventions used by this toolkit

- **Stdlib only.** No `pip install`, no `requirements.txt`, no `pyproject.toml`. Each script has a PEP 723 header so `uv run <script>.py` works out of the box (with no deps to actually fetch — keeps `uv run` fast and consistent across machines).
- **Dry-run by default.** Every destructive action is preceded by a diff and a `[y/N]` prompt with `N` as default.
- **Backup before every write.** No exceptions.
- **Skip > silently break.** When in doubt, `mcp_sync.py` skips with a reason rather than emitting config the target can't actually run.
- **State advances only on non-fatal exits.** Persistent failures stay loud (next scheduled run retries + notifies again).
- **No secrets in scripts or commits.** Secrets live in `secrets.env` (POSIX) / `.env` (Windows). Tools reference them via `--env-file` or wrapper sourcing — never hard-coded.
- **One job per script.** Composition is via subprocess; state is via files.

---

## Repo layout

```
mcp-toolkit/
├── mcp.py                  # unified entry point (interactive + subcommands)
├── mcp_doctor.py           # diagnose
├── mcp_sync.py             # translate-and-distribute master config
├── mcp_sync_daemon.py      # scheduled wrapper for sync
├── README.md               # this file
├── LICENSE                 # MIT
└── .gitignore
```

---

## Sharing with a friend / putting on a fresh machine

1. Clone or copy this repo.
2. Make sure they have **Python 3.10+** and (optionally) **uv**.
3. Tell them to run `uv run mcp.py` (or `python3 mcp.py`) and pick from the menu.

That's it. There is no install step. Stdlib-only Python means nothing to compile and nothing to lock down to a version.

---

## Roadmap (maybe-someday)

- `"sync_only_to": ["windows"]` per-server marker for OS-specific MCPs.
- Bidirectional / merge sync (currently strictly source-of-truth replace).
- Auto-install OS scheduler tasks (currently prints commands; you copy).
- Pluggable wrapper-pattern parser for non-bash wrappers.
- macOS / Linux-native cross-OS path translation.
- `--save-as <name>` to bookmark a sync recipe (source + targets + flags) for one-flag invocation.

---

## License

MIT — see [LICENSE](./LICENSE). Use, fork, modify, share. No warranty.
