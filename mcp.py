# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""mcp - Unified, interactive entry point for MCP ops on Windows + WSL.

This is the front door for the four lower-level scripts in this folder:

    mcp_doctor.py        - diagnose MCP server availability
    mcp_sync.py          - translate-and-distribute master config
    mcp_sync_daemon.py   - scheduled wrapper for sync (with reports + toasts)
    mcp_memory_sync.py   - mirror the Memory MCP knowledge-graph across OSes

`mcp.py` provides:
  - An interactive menu for users who'd rather pick options than memorise flags.
  - Subcommands for power users / scripts.
  - A SAFE sync flow that always runs dry-run first, shows the diff, and asks
    `[y/N]` before applying anything. No way to accidentally clobber a config.

USAGE
    # Interactive menu (default if no subcommand):
    uv run mcp.py

    # Direct subcommand:
    uv run mcp.py doctor          # smoke-test current MCPs
    uv run mcp.py sync            # interactive sync (dry-run -> confirm -> apply)
    uv run mcp.py sync --auto     # interactive sync, but accept defaults non-interactively
    uv run mcp.py schedule        # interactive scheduling helper
    uv run mcp.py memory          # mirror Memory MCP graph across OSes (safe)
    uv run mcp.py topology        # show where MCP files live (Win + WSL)
    uv run mcp.py menu            # explicit menu (same as no args)

The unified entry point is stdlib-only and works identically on Windows + WSL.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import os
import platform
import shlex
import subprocess
import sys
from pathlib import Path

# --- UTF-8 stdout fix for Windows ---
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

THIS_DIR = Path(__file__).resolve().parent
DOCTOR  = THIS_DIR / "mcp_doctor.py"
SYNC    = THIS_DIR / "mcp_sync.py"
DAEMON  = THIS_DIR / "mcp_sync_daemon.py"
MEMSYNC = THIS_DIR / "mcp_memory_sync.py"


# ============================================================================
# Shared helpers: hardlink-safe write + conservative JSON auto-repair
# ============================================================================
#
# These live in mcp.py rather than a separate module so the repo keeps its
# "one job per script + a front door" shape.  Both are pure functions; no CLI
# flow lives here.  Anyone (this file, mcp_doctor.py, future tools) can
# import them via `from mcp import write_in_place, try_repair_simple_json`.

def write_in_place(path: Path, text: str, *, encoding: str = "utf-8") -> Path | None:
    """Write `text` to `path`, preserving the file's inode (and therefore any
    hardlinks pointing at it).

    Method: open in mode 'r+' (existing file) or 'w' (new file), seek(0),
    truncate(), write(), flush(), fsync().  This is open-truncate-write in
    place -- NEVER save-by-rename.

    Before any mutation of an existing file, writes a `.bak.<ts>` next to it.
    Returns the backup path (or None if the file didn't exist).

    Asserts that `st_nlink` is unchanged across the write; raises RuntimeError
    if not.  This catches future regressions that would silently break the
    NTFS hardlink chain that [[MCP Setup]] depends on.
    """
    path = Path(path)
    bak: Path | None = None
    nlink_before: int | None = None
    if path.exists():
        nlink_before = os.stat(path).st_nlink
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = path.with_name(path.name + f".bak.{ts}")
        bak.write_bytes(path.read_bytes())
        # Open existing file for read+write so we can truncate in place.
        with open(path, "r+", encoding=encoding, newline="") as f:
            f.seek(0)
            f.truncate()
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except (OSError, AttributeError):
                pass  # fsync not available on all platforms / for all fds
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except (OSError, AttributeError):
                pass
    if nlink_before is not None:
        nlink_after = os.stat(path).st_nlink
        if nlink_after != nlink_before:
            raise RuntimeError(
                f"write_in_place: hardlink count changed "
                f"({nlink_before} -> {nlink_after}) for {path}. "
                f"The hardlink chain has been broken. Backup at: {bak}"
            )
    return bak


def try_repair_simple_json(text: str, *, max_repairs: int = 50) -> tuple[str | None, str]:
    """Attempt safe, conservative repair on common JSON syntax errors.

    Handles only:
      - missing comma between two sibling values inside objects/arrays
        (boundaries: }<ws>" / ]<ws>" / }<ws>{ / ]<ws>[ / "<ws>" / ...)
      - trailing comma: ,<ws>} / ,<ws>]

    Anything else: returns (None, "no safe repair available -- manual fix required").
    Never guesses.

    Returns (repaired_text, summary) on success.  Repairs are applied
    one-at-a-time and the result is re-parsed after each, up to `max_repairs`
    rounds, to handle multiple similar errors in one file.
    """
    if not isinstance(text, str):
        return None, "input is not a string"

    summary: list[str] = []
    current = text
    for _ in range(max_repairs):
        try:
            json.loads(current)
            if summary:
                return current, "; ".join(summary)
            return current, "already valid"
        except json.JSONDecodeError as e:
            pos = int(e.pos)
            msg = e.msg or ""
            n = len(current)

            # Walk back from pos to the previous non-whitespace char.
            j = pos - 1
            while j >= 0 and current[j] in " \t\r\n":
                j -= 1
            prev = current[j] if j >= 0 else ""

            # Case A: missing comma between two siblings.
            # Trigger: prev is a value-closer ('}', ']', '"', or a digit, or 'e', 'l' from true/false/null)
            # AND the char at pos is a value-opener ('"', '{', '[', digit, sign, t, f, n).
            here = current[pos] if pos < n else ""
            value_closer = prev in ("}", "]", '"') or prev.isdigit() or prev in ("e", "l")
            value_opener = (here == '"' or here in "{[" or here.isdigit()
                            or here in "-+tfn")
            if (value_closer and value_opener
                    and ("delimiter" in msg or "Expecting" in msg)):
                current = current[: j + 1] + "," + current[j + 1:]
                summary.append(f"inserted ',' between sibling values at char {j+1}")
                continue

            # Case B: trailing comma before a closer.
            # Trigger: char at pos is '}' or ']' AND the previous non-ws char is ','.
            if (here in ("}", "]") and prev == ","):
                # Remove the trailing comma at position j.
                current = current[:j] + current[j+1:]
                summary.append(f"removed trailing comma at char {j}")
                continue

            return None, ("no safe repair available -- manual fix required "
                          f"(error at line {e.lineno} col {e.colno}: {msg})")
    return None, f"giving up after {max_repairs} repair attempts"


# ============================================================================
# Prompt helpers (stdin-based, no curses / rich)
# ============================================================================

def _prompt(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        v = input(f"{msg}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return v or default


def prompt_yn(msg: str, default: bool = False) -> bool:
    while True:
        d = "Y/n" if default else "y/N"
        try:
            v = input(f"{msg} [{d}]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not v:
            return default
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False
        print("  please answer y or n.")


def prompt_choice(msg: str, options: list[tuple[str, str]], default_index: int = 0) -> int:
    """Show a numbered list, return chosen index. Options is list of (label, hint)."""
    print(msg)
    for i, (label, hint) in enumerate(options, 1):
        marker = " *" if (i - 1) == default_index else "  "
        if hint:
            print(f"  {i}){marker} {label}  — {hint}")
        else:
            print(f"  {i}){marker} {label}")
    while True:
        try:
            v = input(f"  Choose [1-{len(options)}, default {default_index + 1}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return default_index
        if not v:
            return default_index
        if v.isdigit():
            n = int(v)
            if 1 <= n <= len(options):
                return n - 1
        print("  invalid selection.")


def prompt_multi(msg: str, options: list[tuple[str, str, bool]]) -> list[int]:
    """Toggle-able list. Options is list of (label, hint, default_on).
    Returns list of selected indices.
    User enters comma-separated numbers to toggle, or empty to accept current.
    """
    state = [bool(d) for (_, _, d) in options]
    while True:
        print(msg)
        for i, (label, hint, _) in enumerate(options, 1):
            mark = "[x]" if state[i - 1] else "[ ]"
            line = f"  {mark} {i}) {label}"
            if hint:
                line += f"  — {hint}"
            print(line)
        try:
            v = input("  Toggle (e.g. '1,3' to flip), 'a'=all, 'n'=none, Enter=continue: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not v:
            break
        if v == "a":
            state = [True] * len(state); continue
        if v == "n":
            state = [False] * len(state); continue
        bad = False
        for tok in v.split(","):
            tok = tok.strip()
            if not tok.isdigit():
                bad = True; break
            n = int(tok)
            if not (1 <= n <= len(state)):
                bad = True; break
            state[n - 1] = not state[n - 1]
        if bad:
            print("  invalid input — try again.")
    return [i for i, on in enumerate(state) if on]


def section(title: str) -> None:
    bar = "=" * max(20, min(70, len(title) + 4))
    print()
    print(bar)
    print(f"  {title}")
    print(bar)


# ============================================================================
# Subprocess wrapper (so we can stream + capture)
# ============================================================================

def run_script(script: Path, args: list[str], *, capture: bool = False,
               env_overlay: dict | None = None) -> tuple[int, str, str]:
    """Run a sibling script with the same Python. Returns (rc, stdout, stderr).
    When capture=False, output streams to terminal in real time.
    """
    if not script.is_file():
        return 2, "", f"script not found: {script}"
    cmd = [sys.executable, str(script), *args]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if env_overlay:
        env.update({k: str(v) for k, v in env_overlay.items()})
    if capture:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                                  encoding="utf-8", errors="replace", timeout=600)
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"
    else:
        proc = subprocess.run(cmd, env=env)
        return proc.returncode, "", ""


# ============================================================================
# Direction / source / target helpers
# ============================================================================

def current_os() -> str:
    return "windows" if sys.platform == "win32" else "wsl"


def _detect_wsl_distro() -> str:
    r"""Detect the WSL distribution name (e.g. 'Ubuntu', 'Debian', 'kali-linux').

    Resolution order:
      1) MCP_WSL_DISTRO env var if set
      2) WSL_DISTRO_NAME (set automatically inside WSL)
      3) First subdirectory under \\wsl.localhost\ when probing from Windows
      4) 'Ubuntu' as last-resort fallback
    """
    env = os.environ.get("MCP_WSL_DISTRO")
    if env:
        return env
    in_wsl = os.environ.get("WSL_DISTRO_NAME")
    if in_wsl:
        return in_wsl
    if sys.platform == "win32":
        wsl_root = Path("\\\\wsl.localhost")
        if wsl_root.is_dir():
            kids = [c.name for c in wsl_root.iterdir() if c.is_dir() and not c.name.startswith("$")]
            if kids:
                return kids[0]
    return "Ubuntu"


def _detect_wsl_user() -> str:
    r"""Best-effort: pick a username under \\wsl.localhost\<distro>\home\.
    Honors MCP_WSL_USER env var first."""
    env = os.environ.get("MCP_WSL_USER")
    if env:
        return env
    distro = _detect_wsl_distro()
    unc = Path(f"\\\\wsl.localhost\\{distro}\\home")
    if unc.is_dir():
        kids = [c.name for c in unc.iterdir() if c.is_dir()]
        if kids:
            return kids[0]
    return os.environ.get("USER", os.environ.get("USERNAME", "user"))


def _detect_windows_user() -> str:
    """Best-effort: pick a username under /mnt/c/Users/.
    Honors MCP_WINDOWS_USER env var first."""
    env = os.environ.get("MCP_WINDOWS_USER")
    if env:
        return env
    mnt = Path("/mnt/c/Users")
    if mnt.is_dir():
        kids = [c.name for c in mnt.iterdir() if c.is_dir() and c.name not in ("Public", "Default", "All Users", "Default User")]
        if kids:
            return kids[0]
    return os.environ.get("USERNAME", os.environ.get("USER", "user"))


def default_source_for(os_kind: str) -> str:
    if os_kind == "wsl":
        if current_os() == "wsl":
            return os.path.expanduser("~/.codeium/windsurf/mcp_config.json")
        # cross from Windows: UNC
        distro = _detect_wsl_distro()
        user = _detect_wsl_user()
        return f"\\\\wsl.localhost\\{distro}\\home\\{user}\\.codeium\\windsurf\\mcp_config.json"
    # windows
    if current_os() == "windows":
        return os.path.expandvars(r"%USERPROFILE%\.config\mcp\servers.json")
    # cross from WSL: /mnt/c
    user = _detect_windows_user()
    return f"/mnt/c/Users/{user}/.config/mcp/servers.json"


# ============================================================================
# Sync flow (the safe, always-dry-run-first one)
# ============================================================================

ALL_TARGETS = [
    ("windsurf", "Windsurf IDE config"),
    ("lmstudio", "LM Studio MCP config"),
    ("vscode",   "VS Code (schema rewrite)"),
    ("devin",    "Devin (merges into existing config.json)"),
    ("master",   "Canonical master file (Windows .config/mcp/servers.json)"),
]


def cmd_sync_interactive(auto: bool = False) -> int:
    section("Sync — translate-and-distribute master MCP config")
    print("This walks you through:")
    print("  1) Pick the SOURCE (which config is the master).")
    print("  2) Pick the TARGET OS + targets to update.")
    print("  3) Decide on secrets mirroring.")
    print("  4) See a DRY-RUN diff of every change.")
    print("  5) Confirm before any file is written.\n")

    # 1. Direction
    direction_idx = prompt_choice(
        "Sync direction:",
        [
            ("WSL master  → Windows tools", "WSL is source-of-truth"),
            ("Windows master → WSL tools", "Windows is source-of-truth"),
            ("Same OS (sync within current OS)", f"both source + targets are {current_os().upper()}"),
            ("Custom source path", "you'll be prompted for a file path"),
        ],
        default_index=0 if current_os() == "windows" else 1,
    )

    if direction_idx == 0:
        source_os, target_os = "wsl", "windows"
        source_path = _prompt("Source file", default_source_for("wsl"))
    elif direction_idx == 1:
        source_os, target_os = "windows", "wsl"
        source_path = _prompt("Source file", default_source_for("windows"))
    elif direction_idx == 2:
        source_os = target_os = current_os()
        source_path = _prompt("Source file", default_source_for(source_os))
    else:
        source_path = _prompt("Source file", "")
        if not source_path:
            print("Cancelled."); return 0
        # Guess source OS from the path
        s = source_path
        source_os = "wsl" if (s.startswith("/") or s.startswith("\\\\wsl") or s.startswith("/mnt/")) else "windows"
        target_idx = prompt_choice("Target OS:", [("Windows", ""), ("WSL", "")],
                                   default_index=0 if source_os == "wsl" else 1)
        target_os = "windows" if target_idx == 0 else "wsl"

    print(f"\n  source: {source_path}  (source_os={source_os})")
    print(f"  target_os: {target_os}\n")
    if not Path(os.path.expandvars(os.path.expanduser(source_path))).exists():
        print(f"  WARNING: source path doesn't exist or isn't reachable: {source_path}")
        if not prompt_yn("  continue anyway?", default=False):
            return 0

    # 2. Targets
    target_indices = prompt_multi(
        "Targets to update:",
        [(label, hint, label != "master") for label, hint in ALL_TARGETS],  # all on except master by default
    )
    if not target_indices:
        print("No targets selected — nothing to do."); return 0
    targets = [ALL_TARGETS[i][0] for i in target_indices]
    print(f"\n  selected targets: {', '.join(targets)}\n")

    # 3. Secrets
    mirror_secrets = prompt_yn(
        "Mirror missing secrets from source secrets file to target? "
        "(plaintext tokens cross the OS boundary)",
        default=False,
    )

    # 4. DRY-RUN preview
    section("Dry-run preview (no files will be modified)")
    dry_args = [
        "--source", source_path,
        "--source-os", source_os,
        "--target-os", target_os,
        "--targets", ",".join(targets),
    ]
    if mirror_secrets:
        dry_args.append("--mirror-secrets")
    rc, out, err = run_script(SYNC, dry_args, capture=True)
    print(out)
    if err.strip():
        print("--- stderr ---")
        print(err)
    print(f"(dry-run exit code: {rc})")

    if rc == 2:
        print("\nDry-run errored — won't proceed."); return rc

    # Decide what "no work" means
    if rc == 0:
        print("\nNo changes needed. Nothing to apply."); return 0

    # 5. Confirm + apply
    section("Confirm")
    print("Above is the DRY-RUN. The following will happen on apply:")
    print(f"  - {len(targets)} target(s) will be backed up to .bak.<timestamp> then overwritten.")
    if mirror_secrets:
        print("  - Missing secrets will be prompted for (handled by mcp_sync.py).")
    print()
    if not prompt_yn("Apply these changes now?", default=False):
        print("Cancelled. No files modified."); return 0

    # 6. Apply
    section("Applying")
    apply_args = dry_args + ["--apply"]
    rc2, out2, err2 = run_script(SYNC, apply_args, capture=False)
    if rc2 not in (0, 3):
        print(f"\nApply failed with exit code {rc2}.")
    return rc2


# ============================================================================
# Doctor flow
# ============================================================================

def cmd_doctor_interactive() -> int:
    section("Diagnose — smoke-test MCP servers")
    print("Detects MCP configs on this machine, then probes each enabled server.\n")
    rc, out, err = run_script(DOCTOR, ["--list"], capture=True)
    print(out)
    if rc != 0 and not out:
        print(err); return rc
    cfg = _prompt("Path to config to test (Enter = first detected)", "")
    args = []
    if cfg:
        args.extend(["--config", cfg])
    if prompt_yn("Test only one specific server?", default=False):
        name = _prompt("Server name", "")
        if name:
            args.extend(["--server", name])
    section("Probing")
    rc, _, _ = run_script(DOCTOR, args, capture=False)
    return rc


# ============================================================================
# Memory-sync flow (mirror the Memory MCP knowledge-graph across OSes)
# ============================================================================

def cmd_memory_interactive() -> int:
    section("Memory MCP graph sync")
    print("Mirror the Memory MCP knowledge-graph file (the .json the memory")
    print("MCP server reads + writes) between Windows and WSL.\n")
    print("Default is a bidirectional union merge: entities are deduped by")
    print("name (observations merged), relations by (from, to, relationType).\n")

    # 1. Direction
    direction_idx = prompt_choice(
        "Direction:",
        [
            ("Bidirectional union merge", "both sides receive everything new from the other"),
            ("WSL  -> Windows (one-way)", "WSL graph overwrites Windows graph"),
            ("Windows -> WSL (one-way)", "Windows graph overwrites WSL graph"),
        ],
        default_index=0,
    )
    direction = ["bidirectional", "wsl-to-windows", "windows-to-wsl"][direction_idx]

    # 2. Dry-run preview
    section("Dry-run preview (no files will be modified)")
    rc, out, err = run_script(MEMSYNC, ["--direction", direction], capture=True)
    print(out)
    if err.strip():
        print("--- stderr ---")
        print(err)
    print(f"(dry-run exit code: {rc})")

    if rc == 2:
        print("\nDry-run errored -- won't proceed."); return rc
    if rc == 3:
        print("\nOne or both sides missing MEMORY_FILE_PATH. Fix that, then re-run."); return rc
    if rc == 0:
        print("\nNo changes needed. Nothing to apply."); return 0

    # 3. Confirm + apply
    section("Confirm")
    print("Above is the DRY-RUN. The following will happen on apply:")
    if direction == "bidirectional":
        print("  - Both Windows and WSL memory files will be backed up to .bak.<timestamp>")
        print("    then overwritten with the merged graph.")
    elif direction == "wsl-to-windows":
        print("  - Windows memory file will be backed up to .bak.<timestamp>")
        print("    then overwritten with the WSL graph.")
    else:
        print("  - WSL memory file will be backed up to .bak.<timestamp>")
        print("    then overwritten with the Windows graph.")
    print()
    if not prompt_yn("Apply these changes now?", default=False):
        print("Cancelled. No files modified."); return 0

    section("Applying")
    rc2, _, _ = run_script(MEMSYNC, ["--direction", direction, "--apply"], capture=False)
    return rc2


# ============================================================================
# Schedule flow
# ============================================================================

def cmd_schedule_interactive() -> int:
    section("Schedule — periodic sync via OS scheduler")
    print("Steps: (1) review env config, (2) print install command for your OS.")
    print("Nothing is installed automatically — you copy the command yourself.\n")

    rc, out, _ = run_script(DAEMON, ["--show-config"], capture=True)
    print(out)
    print()
    if not prompt_yn("Show install commands for your OS?", default=True):
        return 0

    if current_os() == "windows":
        section("Windows Task Scheduler — install command")
        cmd = (
            'schtasks /create /tn "MCP Sync" /sc daily /st 09:00 /tr '
            f'"uv run \\"{DAEMON}\\""'
        )
        print("Run this in cmd.exe (or PowerShell):\n")
        print(f"  {cmd}\n")
        print("To remove later:  schtasks /delete /tn \"MCP Sync\" /f")
    else:
        section("WSL — cron command")
        print("Add this line to crontab (run `crontab -e`):\n")
        log = os.path.expanduser("~/.local/state/mcp-sync/cron.log")
        print(f"  0 9 * * *  {sys.executable} {DAEMON} >> {log} 2>&1\n")
        print("To list:    crontab -l")
        print("To remove:  crontab -e  (then delete the line)")

    print()
    print("Override defaults via env vars (set persistently before scheduling):")
    print("  MCP_SYNC_FREQUENCY        e.g. 1d, 12h, 30m, 1w")
    print("  MCP_SYNC_TARGETS          e.g. windsurf,vscode")
    print("  MCP_SYNC_APPLY            yes | no")
    print("  MCP_SYNC_MIRROR_SECRETS   yes | no")
    print("  MCP_SYNC_NOTIFY_ON        error | always | never")
    return 0


# ============================================================================
# Topology
# ============================================================================

def cmd_topology() -> int:
    section("MCP topology — where everything lives")
    print(f"Running on: {platform.system()} {platform.release()}  (treated as: {current_os().upper()})")
    print()
    locations = [
        ("Windows master config",  "${USERPROFILE}\\.config\\mcp\\servers.json"),
        ("Windows secrets",        "${USERPROFILE}\\.config\\mcp\\.env"),
        ("Windows Windsurf",       "${USERPROFILE}\\.codeium\\windsurf\\mcp_config.json (hardlink)"),
        ("Windows LM Studio",      "${USERPROFILE}\\.lmstudio\\mcp.json (hardlink)"),
        ("Windows VS Code",        "%APPDATA%\\Code\\User\\mcp.json (separate schema)"),
        ("Windows Devin",          "%APPDATA%\\devin\\config.json (merged)"),
        ("Windows reports/state",  "%LOCALAPPDATA%\\mcp-sync\\"),
        ("Windows memory graph",   "(MEMORY_FILE_PATH from Windsurf mcp_config.json, typically ${USERPROFILE}\\.config\\mcp\\memory.json)"),
        (".", ""),
        ("WSL master config",      "~/.codeium/windsurf/mcp_config.json"),
        ("WSL secrets",            "~/.codeium/windsurf/secrets.env (chmod 600)"),
        ("WSL wrappers",           "~/.codeium/windsurf/wrappers/"),
        ("WSL VS Code",            "~/.config/Code/User/mcp.json"),
        ("WSL Devin",              "~/.config/devin/config.json"),
        ("WSL reports/state",      "~/.local/share/mcp-sync/  +  ~/.local/state/mcp-sync/"),
        ("WSL memory graph",       "(MEMORY_FILE_PATH from WSL Windsurf mcp_config.json, typically ~/.codeium/windsurf/memory-graph.json)"),
    ]
    for k, v in locations:
        if k == ".":
            print()
            continue
        print(f"  {k:<26} {v}")
    print()
    print("Tools in this folder:")
    print("  mcp.py                 — this unified entry point (interactive menu + subcommands)")
    print("  mcp_doctor.py          — diagnose")
    print("  mcp_sync.py            — translate-and-distribute (always dry-run unless --apply)")
    print("  mcp_sync_daemon.py     — scheduled wrapper (env-configurable)")
    print("  mcp_memory_sync.py     — mirror Memory MCP knowledge-graph across OSes (dry-run unless --apply)")
    return 0


# ============================================================================
# Repair flow (auto-fix common JSON syntax errors, hardlink-safe)
# ============================================================================

def _render_parse_error_context(text: str, lineno: int, colno: int,
                                msg: str, *, ctx: int = 3) -> str:
    """Pretty-print the parse error: file window with caret + classification."""
    lines = text.splitlines()
    lo = max(1, lineno - ctx)
    hi = min(len(lines), lineno + ctx)
    width = len(str(hi))
    out: list[str] = [f"  line {lineno}, col {colno}: {msg}", ""]
    for i in range(lo, hi + 1):
        prefix = f"  {str(i).rjust(width)} | "
        out.append(prefix + (lines[i - 1] if i - 1 < len(lines) else ""))
        if i == lineno:
            out.append(" " * (len(prefix) + max(0, colno - 1)) + "^")
    return "\n".join(out)


def cmd_repair(path_str: str, apply: bool = False) -> int:
    """Dry-run or --apply a safe auto-repair on a JSON file.
    Hardlink-safe: uses write_in_place(), preserves NTFS inode + .bak.<ts>.

    Exit codes:
      0   file already parses (no work needed), or --apply succeeded
      1   repair available but not applied (dry-run with pending fix)
      2   no safe repair (manual fix required) or apply failed
    """
    import difflib
    path = Path(path_str).expanduser()
    section(f"Repair — {path}")
    if not path.is_file():
        print(f"  ERROR: not a file: {path}", file=sys.stderr)
        return 2
    text = path.read_text(encoding="utf-8")
    try:
        json.loads(text)
        print("  already valid JSON; no repair needed.")
        return 0
    except json.JSONDecodeError as e:
        print(_render_parse_error_context(text, e.lineno, e.colno, e.msg or ""))
        print()
    repaired, summary = try_repair_simple_json(text)
    if repaired is None:
        print(f"  {summary}")
        print()
        print("  No automated repair available. Open the file in an editor and")
        print("  fix the error manually, then re-run this command to verify.")
        return 2
    print(f"  proposed fix: {summary}")
    print()
    diff = "".join(difflib.unified_diff(
        text.splitlines(keepends=True),
        repaired.splitlines(keepends=True),
        fromfile=f"{path} (current)", tofile=f"{path} (repaired)", n=3,
    ))
    print(diff or "(no textual diff -- check encoding/newlines)")
    print()
    if not apply:
        print("(dry-run -- pass --apply to write the fix in place)")
        return 1
    try:
        bak = write_in_place(path, repaired)
    except Exception as e:
        print(f"  apply failed: {e}", file=sys.stderr)
        return 2
    if bak:
        print(f"  backed up original -> {bak}")
    print(f"  wrote {path}")
    return 0


# ============================================================================
# Main menu
# ============================================================================

def menu() -> int:
    while True:
        section("MCP — main menu")
        print("Pick an action:")
        print("  1) Diagnose       — test which MCPs are working right now")
        print("  2) Sync           — push master config to other tools (safe: dry-run first)")
        print("  3) Memory sync    — mirror Memory MCP knowledge-graph across OSes")
        print("  4) Schedule       — set up periodic sync")
        print("  5) Topology       — show where MCP files live across OSes")
        print("  q) Quit")
        try:
            v = input("\n  Choose [1-5 / q]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if v in ("q", "quit", "exit"):
            return 0
        if v == "1":
            cmd_doctor_interactive()
        elif v == "2":
            cmd_sync_interactive()
        elif v == "3":
            cmd_memory_interactive()
        elif v == "4":
            cmd_schedule_interactive()
        elif v == "5":
            cmd_topology()
        else:
            print("  invalid choice.")


# ============================================================================
# CLI
# ============================================================================

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Unified, interactive entry point for MCP ops.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("menu", help="Interactive menu (default if no subcommand).")
    sub.add_parser("doctor", help="Diagnose MCP server availability.")
    s_sync = sub.add_parser("sync", help="Translate-and-distribute master config (safe).")
    s_sync.add_argument("--auto", action="store_true",
                        help="Use sensible defaults non-interactively (still asks before apply).")
    sub.add_parser("memory", help="Mirror the Memory MCP knowledge-graph across OSes (safe).")
    sub.add_parser("schedule", help="Show env config + install commands for periodic sync.")
    sub.add_parser("topology", help="Show where MCP files live on Windows + WSL.")
    s_repair = sub.add_parser("repair",
                              help="Auto-fix common JSON syntax errors (missing/trailing comma).")
    s_repair.add_argument("path", help="Path to the JSON file to repair.")
    s_repair.add_argument("--apply", action="store_true",
                          help="Write the fix in place (default is dry-run).")
    args = p.parse_args(argv)

    if args.cmd is None or args.cmd == "menu":
        return menu()
    if args.cmd == "doctor":
        return cmd_doctor_interactive()
    if args.cmd == "sync":
        return cmd_sync_interactive(auto=getattr(args, "auto", False))
    if args.cmd == "memory":
        return cmd_memory_interactive()
    if args.cmd == "schedule":
        return cmd_schedule_interactive()
    if args.cmd == "topology":
        return cmd_topology()
    if args.cmd == "repair":
        return cmd_repair(args.path, apply=args.apply)
    return 0


# ============================================================================
# Self-tests (run via `python mcp.py --self-test`)
# ============================================================================

def _self_test() -> int:
    """Inline assertions for write_in_place + try_repair_simple_json (T3-T5)."""
    import tempfile
    # T3: write_in_place preserves inode + link count on a hardlinked file.
    with tempfile.TemporaryDirectory() as td:
        a = Path(td) / "a.json"
        b = Path(td) / "b.json"
        a.write_text('{"v": 1}', encoding="utf-8")
        os.link(a, b)
        ino_before = os.stat(a).st_ino
        nl_before = os.stat(a).st_nlink
        assert nl_before == 2, f"setup FAIL: link count {nl_before} != 2"
        bak = write_in_place(a, '{"v": 2, "added": "yes -- longer than before"}')
        ino_after = os.stat(a).st_ino
        nl_after = os.stat(a).st_nlink
        assert ino_before == ino_after, f"T3 FAIL: inode changed {ino_before} -> {ino_after}"
        assert nl_before == nl_after, f"T3 FAIL: link count {nl_before} -> {nl_after}"
        assert b.read_text(encoding="utf-8") == '{"v": 2, "added": "yes -- longer than before"}', \
            "T3 FAIL: hardlinked sibling didn't see the new content"
        assert bak is not None and bak.exists(), "T3 FAIL: .bak.<ts> not created"
        assert bak.read_text(encoding="utf-8") == '{"v": 1}', "T3 FAIL: backup content wrong"
        print("T3 PASS  write_in_place preserves inode + link count + creates .bak")

    # T4: try_repair_simple_json fixes the canonical missing-comma case
    # (the actual bug we hit in this session: missing comma between two
    # object members).
    broken_missing_comma = '{\n  "a": 1\n  "b": 2\n}\n'
    fixed, summary = try_repair_simple_json(broken_missing_comma)
    assert fixed is not None, f"T4 FAIL: repair returned None for missing-comma; summary={summary}"
    obj = json.loads(fixed)
    assert obj == {"a": 1, "b": 2}, f"T4 FAIL: repaired parsed wrong: {obj}"
    assert "inserted ','" in summary, f"T4 FAIL: unexpected summary: {summary}"
    print(f"T4 PASS  missing-comma repair works ({summary})")

    # T4b: trailing comma repair.
    broken_trailing = '{\n  "a": 1,\n  "b": 2,\n}\n'
    fixed, summary = try_repair_simple_json(broken_trailing)
    assert fixed is not None, f"T4b FAIL: trailing-comma not repaired; {summary}"
    assert json.loads(fixed) == {"a": 1, "b": 2}, f"T4b FAIL: wrong content"
    assert "removed trailing comma" in summary, f"T4b FAIL: unexpected summary: {summary}"
    print(f"T4b PASS trailing-comma repair works ({summary})")

    # T5: try_repair_simple_json refuses to guess on truly invalid input.
    truly_bad = '{"a": "unterminated string'
    fixed, summary = try_repair_simple_json(truly_bad)
    assert fixed is None, f"T5 FAIL: repaired something it shouldn't have; got: {fixed!r}"
    assert "no safe repair" in summary, f"T5 FAIL: unexpected summary: {summary}"
    print(f"T5 PASS  unfixable input returns (None, ...) -- no guessing")

    # T5b: garbage input (not even JSON-shaped) -> refuse cleanly.
    fixed, summary = try_repair_simple_json("hello world")
    assert fixed is None, f"T5b FAIL: repaired non-JSON: {fixed!r}"
    print(f"T5b PASS non-JSON input returns (None, ...)")

    print("\nAll self-tests passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
