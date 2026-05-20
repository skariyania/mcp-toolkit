# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""mcp - Unified, interactive entry point for MCP ops on Windows + WSL.

This is the front door for the three lower-level scripts in this folder:

    mcp_doctor.py        - diagnose MCP server availability
    mcp_sync.py          - translate-and-distribute master config
    mcp_sync_daemon.py   - scheduled wrapper for sync (with reports + toasts)

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
    uv run mcp.py topology        # show where MCP files live (Win + WSL)
    uv run mcp.py menu            # explicit menu (same as no args)

The unified entry point is stdlib-only and works identically on Windows + WSL.
"""
from __future__ import annotations

import argparse
import io
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


def default_source_for(os_kind: str) -> str:
    if os_kind == "wsl":
        if current_os() == "wsl":
            return os.path.expanduser("~/.codeium/windsurf/mcp_config.json")
        # cross from Windows: UNC
        return "\\\\wsl.localhost\\Ubuntu\\home\\skariyania\\.codeium\\windsurf\\mcp_config.json"
    # windows
    if current_os() == "windows":
        return os.path.expandvars(r"%USERPROFILE%\.config\mcp\servers.json")
    # cross from WSL: /mnt/c
    return "/mnt/c/Users/Sahil.Kariyania/.config/mcp/servers.json"


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
        (".", ""),
        ("WSL master config",      "/home/skariyania/.codeium/windsurf/mcp_config.json"),
        ("WSL secrets",            "/home/skariyania/.codeium/windsurf/secrets.env (chmod 600)"),
        ("WSL wrappers",           "/home/skariyania/.codeium/windsurf/wrappers/"),
        ("WSL VS Code",            "/home/skariyania/.config/Code/User/mcp.json"),
        ("WSL Devin",              "/home/skariyania/.config/devin/config.json"),
        ("WSL reports/state",      "~/.local/share/mcp-sync/  +  ~/.local/state/mcp-sync/"),
        (".", ""),
        ("Skills (WSL workspace)", "~/dev/github.com/.windsurf/{rules,workflows}/mcp-*.md"),
        ("Reference doc (Obsidian)", "@work/MCP Setup.md"),
        ("Memory entity",          "mcp-topology  (memory MCP graph; seed at ~/.codeium/windsurf/mcp-topology-seed.json)"),
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
        print("  3) Schedule       — set up periodic sync")
        print("  4) Topology       — show where MCP files live across OSes")
        print("  q) Quit")
        try:
            v = input("\n  Choose [1-4 / q]: ").strip().lower()
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
            cmd_schedule_interactive()
        elif v == "4":
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
    sub.add_parser("schedule", help="Show env config + install commands for periodic sync.")
    sub.add_parser("topology", help="Show where MCP files live on Windows + WSL.")
    args = p.parse_args(argv)

    if args.cmd is None or args.cmd == "menu":
        return menu()
    if args.cmd == "doctor":
        return cmd_doctor_interactive()
    if args.cmd == "sync":
        return cmd_sync_interactive(auto=getattr(args, "auto", False))
    if args.cmd == "schedule":
        return cmd_schedule_interactive()
    if args.cmd == "topology":
        return cmd_topology()
    return 0


if __name__ == "__main__":
    sys.exit(main())
