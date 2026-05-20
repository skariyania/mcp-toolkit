# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""mcp_sync_daemon - Run mcp_sync.py on a schedule, write a report, notify on errors.

This is a *one-shot* script (not a long-running daemon). You schedule it
with the OS-native mechanism (Windows Task Scheduler, WSL cron / systemd
timer). On each invocation it:

  1. Reads config from env vars (with sensible defaults).
  2. Checks the last-run timestamp; SKIPS if too recent (unless --force).
  3. Invokes mcp_sync.py with the configured source / targets / flags.
  4. Writes a Markdown report to ${MCP_SYNC_REPORT_DIR}.
  5. Updates the last-run timestamp ONLY on success (so persistent failures
     keep retrying on each scheduled invocation).
  6. Sends a desktop notification on error (or always, configurable).

ENVIRONMENT VARIABLES (all optional)
    MCP_SYNC_USER           Username (used in greetings / report headers).
                            Default: current user (USERPROFILE / $USER).
    MCP_SYNC_FREQUENCY      Min interval between syncs. Suffixes: s/m/h/d/w.
                            Examples: "1d" (default), "12h", "30m", "1w".
    MCP_SYNC_SOURCE         Path to source MCP config.
                            Default: ~/.codeium/windsurf/mcp_config.json on WSL,
                                     ~/.config/mcp/servers.json on Windows.
    MCP_SYNC_TARGET_OS      "windows" | "wsl". Default: opposite of current OS
                            (so the typical use is WSL master -> Windows tools).
    MCP_SYNC_TARGETS        Comma list or "all". Default: "windsurf,lmstudio,vscode,devin".
    MCP_SYNC_APPLY          "yes" | "no". Default: "yes" (periodic sync should sync).
    MCP_SYNC_MIRROR_SECRETS "yes" | "no". Default: "no" (plaintext tokens crossing
                            OS boundaries requires explicit opt-in).
    MCP_SYNC_NOTIFY_ON      "error" | "always" | "never". Default: "error".
    MCP_SYNC_REPORT_DIR     Where to write reports.
                            Default: ~/.local/share/mcp-sync/reports/
    MCP_SYNC_STATE_DIR      Where to keep last-run state.
                            Default: ~/.local/state/mcp-sync/

USAGE
    # Run if due (skips silently if last run was within frequency window):
    uv run mcp_sync_daemon.py

    # Force run even if not due:
    uv run mcp_sync_daemon.py --force

    # Show resolved config and exit:
    uv run mcp_sync_daemon.py --show-config

    # Print the most recent report path:
    uv run mcp_sync_daemon.py --latest-report

SCHEDULING
    Easiest: run `mcp.py schedule` — it prints the OS-specific install command
    with the *actual* path to this script filled in.

    Or write it yourself, replacing <abs/path> with your install location:

      Windows Task Scheduler:
          schtasks /create /tn "MCP Sync" /sc daily /st 09:00 /tr ^
              "uv run \"<abs\\path\\to>\\mcp_sync_daemon.py\""

      WSL / Linux cron (`crontab -e`):
          0 9 * * *  /usr/bin/python3 <abs/path/to>/mcp_sync_daemon.py \\
                     >> ~/.local/state/mcp-sync/cron.log 2>&1

    Either way the script handles its own debouncing — running it more often
    than MCP_SYNC_FREQUENCY just causes silent skips.

EXIT CODES
    0   no work needed (skipped) OR sync ran successfully
    1   sync ran but had pending changes (only when MCP_SYNC_APPLY=no)
    2   configuration error
    3   sync ran but skipped some servers (translation gaps)
    4   sync FAILED (notification fired)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# --- UTF-8 stdout fix for Windows ---
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

THIS_DIR = Path(__file__).resolve().parent
SYNC_SCRIPT = THIS_DIR / "mcp_sync.py"


# --- Env helpers ---

def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default

def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "yes", "true", "y", "on")

def _current_user() -> str:
    return (os.environ.get("USER") or os.environ.get("USERNAME")
            or (os.environ.get("USERPROFILE") or "").rsplit(os.sep, 1)[-1]
            or "user")

def _current_os() -> str:
    """Return 'windows' or 'wsl' (we treat any Linux-like as 'wsl' for path purposes)."""
    if sys.platform == "win32":
        return "windows"
    return "wsl"

def _default_source() -> str:
    if _current_os() == "wsl":
        return "~/.codeium/windsurf/mcp_config.json"
    return os.path.expandvars("${USERPROFILE}/.config/mcp/servers.json")

def _default_target_os() -> str:
    return "windows" if _current_os() == "wsl" else "wsl"

def _default_state_dir() -> str:
    if _current_os() == "windows":
        return os.path.expandvars("${LOCALAPPDATA}/mcp-sync")
    return "~/.local/state/mcp-sync"

def _default_report_dir() -> str:
    if _current_os() == "windows":
        return os.path.expandvars("${LOCALAPPDATA}/mcp-sync/reports")
    return "~/.local/share/mcp-sync/reports"


# --- Frequency parsing ---

_FREQ_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_FREQ_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

def parse_frequency(spec: str) -> int:
    m = _FREQ_RE.match(spec)
    if not m:
        raise ValueError(f"unrecognized frequency: {spec!r} (try 1d, 12h, 30m, 60s, 1w)")
    return int(m.group(1)) * _FREQ_MULT[m.group(2).lower()]


# --- Resolved config ---

@dataclass
class Config:
    user: str
    frequency: str
    frequency_seconds: int
    source: Path
    target_os: str
    targets: str
    apply: bool
    mirror_secrets: bool
    notify_on: str   # error | always | never
    report_dir: Path
    state_dir: Path

def resolve_config() -> Config:
    user = _env("MCP_SYNC_USER", _current_user())
    freq = _env("MCP_SYNC_FREQUENCY", "1d")
    freq_secs = parse_frequency(freq)
    source = Path(os.path.expanduser(_env("MCP_SYNC_SOURCE", _default_source()))).resolve()
    target_os = _env("MCP_SYNC_TARGET_OS", _default_target_os()).lower()
    targets = _env("MCP_SYNC_TARGETS", "windsurf,lmstudio,vscode,devin")
    apply_ = _env_bool("MCP_SYNC_APPLY", True)
    mirror = _env_bool("MCP_SYNC_MIRROR_SECRETS", False)
    notify_on = _env("MCP_SYNC_NOTIFY_ON", "error").lower()
    report_dir = Path(os.path.expanduser(_env("MCP_SYNC_REPORT_DIR", _default_report_dir())))
    state_dir = Path(os.path.expanduser(_env("MCP_SYNC_STATE_DIR", _default_state_dir())))

    if target_os not in ("windows", "wsl"):
        raise ValueError(f"MCP_SYNC_TARGET_OS must be 'windows' or 'wsl', got {target_os!r}")
    if notify_on not in ("error", "always", "never"):
        raise ValueError(f"MCP_SYNC_NOTIFY_ON must be 'error'|'always'|'never', got {notify_on!r}")

    return Config(user=user, frequency=freq, frequency_seconds=freq_secs,
                  source=source, target_os=target_os, targets=targets,
                  apply=apply_, mirror_secrets=mirror, notify_on=notify_on,
                  report_dir=report_dir, state_dir=state_dir)


# --- State (last-run timestamp) ---

def state_file(state_dir: Path) -> Path:
    return state_dir / "last-run.json"

def read_last_run(state_dir: Path) -> _dt.datetime | None:
    f = state_file(state_dir)
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return _dt.datetime.fromisoformat(data["timestamp"])
    except Exception:
        return None

def write_last_run(state_dir: Path, when: _dt.datetime, exit_code: int, report_path: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file(state_dir).write_text(
        json.dumps({
            "timestamp": when.isoformat(),
            "exit_code": exit_code,
            "report": str(report_path),
        }, indent=2),
        encoding="utf-8",
    )


# --- Sync invocation ---

@dataclass
class SyncResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    cmd: list[str]

def run_sync(cfg: Config) -> SyncResult:
    cmd = [
        sys.executable, str(SYNC_SCRIPT),
        "--source", str(cfg.source),
        "--target-os", cfg.target_os,
        "--targets", cfg.targets,
    ]
    if cfg.apply:
        cmd.append("--apply")
    if cfg.mirror_secrets:
        cmd.append("--mirror-secrets")

    # Force unbuffered to capture diff output cleanly
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    t0 = _dt.datetime.now()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, env=env,
            timeout=600, encoding="utf-8", errors="replace",
        )
        dur = (_dt.datetime.now() - t0).total_seconds()
        return SyncResult(exit_code=proc.returncode, stdout=proc.stdout,
                          stderr=proc.stderr, duration_s=dur, cmd=cmd)
    except subprocess.TimeoutExpired:
        dur = (_dt.datetime.now() - t0).total_seconds()
        return SyncResult(exit_code=124, stdout="",
                          stderr=f"mcp_sync.py timed out after {dur:.0f}s",
                          duration_s=dur, cmd=cmd)
    except FileNotFoundError as e:
        return SyncResult(exit_code=2, stdout="",
                          stderr=f"sync script not found: {e}",
                          duration_s=0.0, cmd=cmd)


# --- Report (Markdown) ---

def write_report(cfg: Config, when: _dt.datetime, result: SyncResult,
                 last_run: _dt.datetime | None) -> Path:
    cfg.report_dir.mkdir(parents=True, exist_ok=True)
    ts = when.strftime("%Y%m%d-%H%M%S")
    path = cfg.report_dir / f"sync-{ts}.md"

    status = _interpret_exit(result.exit_code)
    last_run_str = last_run.isoformat() if last_run else "(never)"

    summary_lines = []
    skipped_lines = []
    diff_lines = []
    cur_section = "summary"
    for line in result.stdout.splitlines():
        if line.startswith("Skipped"):
            cur_section = "skipped"; skipped_lines.append(line); continue
        if line.startswith("--- Target:"):
            cur_section = "diff"
        if cur_section == "diff":
            diff_lines.append(line)
        elif cur_section == "skipped" and (line.startswith("  - ") or line.strip() == ""):
            skipped_lines.append(line)
            if not line.strip():
                cur_section = "summary"
        else:
            summary_lines.append(line)

    body = []
    body.append(f"---")
    body.append(f"timestamp: {when.isoformat()}")
    body.append(f"user: {cfg.user}")
    body.append(f"host: {platform.node()}")
    body.append(f"platform: {platform.system()} {platform.release()}")
    body.append(f"frequency: {cfg.frequency}")
    body.append(f"exit_code: {result.exit_code}")
    body.append(f"status: {status}")
    body.append(f"duration_s: {result.duration_s:.1f}")
    body.append(f"---\n")

    body.append(f"# MCP Sync Report — {when.strftime('%Y-%m-%d %H:%M:%S')}")
    body.append("")
    body.append(f"**Status:** {status}  ")
    body.append(f"**User:** `{cfg.user}` on `{platform.node()}` ({platform.system()})  ")
    body.append(f"**Last run:** {last_run_str}  ")
    body.append(f"**Duration:** {result.duration_s:.1f}s  ")
    body.append("")
    body.append("## Configuration")
    body.append("")
    body.append(f"| Key | Value |")
    body.append(f"|---|---|")
    body.append(f"| Source | `{cfg.source}` |")
    body.append(f"| Target OS | `{cfg.target_os}` |")
    body.append(f"| Targets | `{cfg.targets}` |")
    body.append(f"| Apply | `{cfg.apply}` |")
    body.append(f"| Mirror secrets | `{cfg.mirror_secrets}` |")
    body.append(f"| Frequency | `{cfg.frequency}` ({cfg.frequency_seconds}s) |")
    body.append(f"| Notify on | `{cfg.notify_on}` |")
    body.append("")

    body.append("## Summary")
    body.append("")
    body.append("```")
    body.extend(summary_lines)
    body.append("```")
    body.append("")

    if skipped_lines:
        body.append("## Skipped servers")
        body.append("")
        body.append("```")
        body.extend(skipped_lines)
        body.append("```")
        body.append("")

    if diff_lines:
        body.append("## Per-target diffs")
        body.append("")
        body.append("```diff")
        body.extend(diff_lines)
        body.append("```")
        body.append("")

    if result.stderr.strip():
        body.append("## Errors / warnings (stderr)")
        body.append("")
        body.append("```")
        body.append(result.stderr.rstrip())
        body.append("```")
        body.append("")

    body.append("## Invocation")
    body.append("")
    body.append("```")
    body.append(" ".join(_quote(a) for a in result.cmd))
    body.append("```")

    path.write_text("\n".join(body), encoding="utf-8")

    # Also keep latest.md (copy, not symlink, so Windows is fine).
    (cfg.report_dir / "latest.md").write_bytes(path.read_bytes())
    return path

def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


def _interpret_exit(rc: int) -> str:
    return {
        0: "✓ OK (no changes / fully applied)",
        1: "⚠ pending (dry-run had changes)",
        2: "✗ ERROR (config / unreachable source)",
        3: "⚠ partial (some servers skipped)",
        4: "✗ FAILED",
        124: "✗ TIMEOUT",
    }.get(rc, f"? unknown ({rc})")


# --- Notifications (cross-platform, stdlib only) ---

def notify(title: str, message: str, urgent: bool = False) -> None:
    """Best-effort cross-platform desktop notification. Never raises."""
    try:
        if sys.platform == "win32":
            _notify_windows(title, message)
        elif sys.platform == "darwin":
            _notify_macos(title, message)
        else:
            # Linux native or WSL
            ok = _notify_linux(title, message)
            if not ok:
                # WSL: fall back to Windows toast via powershell.exe
                _notify_windows(title, message, posh="powershell.exe")
    except Exception as e:
        # Last resort: stderr only
        print(f"[notify failed: {e}] {title}: {message}", file=sys.stderr)


_PS_TOAST_SCRIPT = r'''param(
    [Parameter(Mandatory=$true)][string]$Title,
    [Parameter(Mandatory=$true)][string]$Message
)
# Register an AppUserModelID under HKCU so newer Windows actually displays
# the toast (unregistered AppIds are silently dismissed). Idempotent.
$appId = "MCP.Sync"
$regPath = "HKCU:\Software\Classes\AppUserModelId\$appId"
if (-not (Test-Path $regPath)) {
    New-Item -Path $regPath -Force | Out-Null
    Set-ItemProperty -Path $regPath -Name "DisplayName" -Value "MCP Sync"
}

[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null

$titleEnc   = [System.Security.SecurityElement]::Escape($Title)
$messageEnc = [System.Security.SecurityElement]::Escape($Message)

$xmlString = @"
<toast>
  <visual>
    <binding template="ToastGeneric">
      <text>$titleEnc</text>
      <text>$messageEnc</text>
    </binding>
  </visual>
</toast>
"@

$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($xmlString)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
'''

def _notify_windows(title: str, message: str, posh: str = "powershell") -> None:
    """Fire a Windows toast notification reliably.

    Uses a tempfile-backed .ps1 (avoids -Command quoting hell) and registers
    a HKCU AppUserModelID on first run so newer Windows actually surfaces
    the toast (unregistered AppIds are silently swallowed).
    """
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "mcp_sync_toast.ps1"
    if not tmp.is_file() or tmp.read_text(encoding="utf-8") != _PS_TOAST_SCRIPT:
        tmp.write_text(_PS_TOAST_SCRIPT, encoding="utf-8")
    subprocess.run(
        [posh, "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(tmp), "-Title", title, "-Message", message],
        timeout=10, capture_output=True, check=False,
    )


def _notify_macos(title: str, message: str) -> None:
    title = title.replace('"', '\\"')
    message = message.replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
        timeout=10, capture_output=True, check=False,
    )


def _notify_linux(title: str, message: str) -> bool:
    if not shutil.which("notify-send"):
        return False
    subprocess.run(["notify-send", "-a", "MCP Sync", title, message],
                   timeout=10, capture_output=True, check=False)
    return True


# --- Main ---

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Run mcp_sync.py on a schedule, write a report, notify on errors.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--force", action="store_true",
                   help="Run even if last run was within MCP_SYNC_FREQUENCY.")
    p.add_argument("--show-config", action="store_true",
                   help="Print resolved config and exit.")
    p.add_argument("--latest-report", action="store_true",
                   help="Print path to the most recent report and exit.")
    p.add_argument("--dry-notify", action="store_true",
                   help="Send a test notification and exit.")
    args = p.parse_args(argv)

    try:
        cfg = resolve_config()
    except Exception as e:
        print(f"mcp_sync_daemon: config error: {e}", file=sys.stderr)
        return 2

    if args.show_config:
        print(f"User:           {cfg.user}")
        print(f"Frequency:      {cfg.frequency} ({cfg.frequency_seconds}s)")
        print(f"Source:         {cfg.source}")
        print(f"Target OS:      {cfg.target_os}")
        print(f"Targets:        {cfg.targets}")
        print(f"Apply:          {cfg.apply}")
        print(f"Mirror secrets: {cfg.mirror_secrets}")
        print(f"Notify on:      {cfg.notify_on}")
        print(f"Report dir:     {cfg.report_dir}")
        print(f"State dir:      {cfg.state_dir}")
        last = read_last_run(cfg.state_dir)
        print(f"Last run:       {last.isoformat() if last else '(never)'}")
        return 0

    if args.latest_report:
        latest = cfg.report_dir / "latest.md"
        if latest.is_file():
            print(latest)
            return 0
        print("(no reports yet)", file=sys.stderr)
        return 2

    if args.dry_notify:
        notify("MCP Sync — test", f"Hello {cfg.user}, notifications work.")
        print("test notification fired.")
        return 0

    if not SYNC_SCRIPT.is_file():
        print(f"mcp_sync_daemon: sync script not found at {SYNC_SCRIPT}", file=sys.stderr)
        notify("MCP Sync — error", f"sync script missing at {SYNC_SCRIPT}", urgent=True)
        return 2

    # Due check
    last_run = read_last_run(cfg.state_dir)
    now = _dt.datetime.now()
    if last_run and not args.force:
        elapsed = (now - last_run).total_seconds()
        remaining = cfg.frequency_seconds - elapsed
        if remaining > 0:
            mins = int(remaining // 60)
            print(f"mcp_sync_daemon: skipped (last run {last_run.isoformat()}; "
                  f"due in ~{mins}m). Use --force to override.")
            return 0

    # Run sync
    print(f"mcp_sync_daemon: running sync (target_os={cfg.target_os}, targets={cfg.targets}, apply={cfg.apply}) ...")
    result = run_sync(cfg)
    report_path = write_report(cfg, now, result, last_run)

    # Decide outcome
    if result.exit_code in (0, 1, 3):
        # 0=ok, 1=dry-run pending, 3=partial — these are non-fatal; advance state.
        write_last_run(cfg.state_dir, now, result.exit_code, report_path)

    fatal = result.exit_code not in (0, 1, 3)
    status = _interpret_exit(result.exit_code)
    print(f"mcp_sync_daemon: {status}  report -> {report_path}")

    # Notification
    should_notify = (
        cfg.notify_on == "always"
        or (cfg.notify_on == "error" and fatal)
    )
    # Also notify on partial / pending if always requested:
    if cfg.notify_on == "error" and result.exit_code == 3:
        should_notify = True   # skipped servers are worth surfacing

    if should_notify:
        title = f"MCP Sync — {status}"
        msg = f"{Path(report_path).name} | targets={cfg.targets}"
        notify(title, msg, urgent=fatal)

    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
