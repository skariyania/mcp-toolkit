# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""mcp_doctor - Diagnose MCP (Model Context Protocol) server availability.

Smoke-tests every MCP server in a config file by spawning it the same way
an IDE would, sending JSON-RPC `initialize` + `tools/list`, and reporting
per-server status (OK / SLOW / FAIL) with diagnostic hints.

Auto-detects common config locations: Windsurf, Cursor, Claude Desktop,
VS Code, and Devin (Windows + macOS + Linux).

USAGE
    # Auto-detect and test the first config found:
    uv run mcp_doctor.py

    # List configs detected on this machine without testing:
    uv run mcp_doctor.py --list

    # Test a specific config file:
    uv run mcp_doctor.py --config ~/.codeium/windsurf/mcp_config.json

    # Test a specific server only:
    uv run mcp_doctor.py --server git

    # Tweak timeouts (defaults: 15s timeout, 5000ms slow threshold):
    uv run mcp_doctor.py --timeout 30 --slow-ms 8000

    # Machine-readable output:
    uv run mcp_doctor.py --json

EXIT CODES
    0   all enabled servers OK or SLOW
    1   one or more servers FAIL
    2   no usable config found / preflight error
"""
from __future__ import annotations

import argparse
import io
import json
import os
import platform
import select
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

# Force UTF-8 stdout on Windows so non-ASCII (arrows, emojis, foreign chars)
# don't crash on cp1252.
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

DEFAULT_TIMEOUT_S = float(os.environ.get("MCP_DOCTOR_TIMEOUT", "15"))
DEFAULT_SLOW_MS = int(os.environ.get("MCP_DOCTOR_SLOW_MS", "5000"))


# ----- Config discovery -----

def _candidates() -> list[tuple[str, Path]]:
    """Return (label, path) pairs for known MCP config locations on this OS."""
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", "")) if sys.platform == "win32" else None
    out: list[tuple[str, Path]] = []

    # Windsurf
    out.append(("windsurf", home / ".codeium" / "windsurf" / "mcp_config.json"))

    # Cursor
    out.append(("cursor", home / ".cursor" / "mcp.json"))

    # Claude Desktop
    if sys.platform == "darwin":
        out.append(("claude-desktop", home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"))
    elif sys.platform == "win32" and appdata:
        out.append(("claude-desktop", appdata / "Claude" / "claude_desktop_config.json"))
    else:
        out.append(("claude-desktop", home / ".config" / "Claude" / "claude_desktop_config.json"))

    # VS Code
    if sys.platform == "darwin":
        out.append(("vscode", home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"))
    elif sys.platform == "win32" and appdata:
        out.append(("vscode", appdata / "Code" / "User" / "mcp.json"))
    else:
        out.append(("vscode", home / ".config" / "Code" / "User" / "mcp.json"))

    # Devin
    if sys.platform == "win32" and appdata:
        out.append(("devin-user", appdata / "devin" / "config.json"))
    out.append(("devin-xdg", home / ".config" / "devin" / "config.json"))

    return out


def detect_configs() -> list[tuple[str, Path]]:
    """Return existing configs only."""
    return [(label, p) for label, p in _candidates() if p.is_file()]


def _print_parse_error(path: Path, e: json.JSONDecodeError) -> None:
    """Print a JSON parse error with source-window context + caret +
    classification + suggested fix command.  Replaces the previous bare
    `Expecting ',' delimiter: line X col Y` one-liner that gave the user
    no way to know what to actually do next."""
    text = ""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        pass
    lines = text.splitlines()
    lineno = int(e.lineno)
    colno = int(e.colno)
    msg = e.msg or ""
    ctx = 3
    lo = max(1, lineno - ctx)
    hi = min(len(lines), lineno + ctx)
    width = len(str(hi)) if hi else 1
    print(f"mcp-doctor: failed to parse {path}", file=sys.stderr)
    print(f"  line {lineno}, col {colno}: {msg}", file=sys.stderr)
    print("", file=sys.stderr)
    for i in range(lo, hi + 1):
        prefix = f"  {str(i).rjust(width)} | "
        line_content = lines[i - 1] if i - 1 < len(lines) else ""
        print(prefix + line_content, file=sys.stderr)
        if i == lineno:
            print(" " * (len(prefix) + max(0, colno - 1)) + "^", file=sys.stderr)
    print("", file=sys.stderr)
    # Classify by attempting a conservative repair (pure function, no side
    # effects).  If it would succeed, point at `mcp.py repair`.
    try:
        from mcp import try_repair_simple_json  # sibling script
        repaired, summary = try_repair_simple_json(text)
    except Exception:
        repaired, summary = None, ""
    if repaired is not None:
        print(f"  classification: looks repairable ({summary.split(';')[0]})", file=sys.stderr)
        print(f"  suggested fix: uv run mcp.py repair \"{path}\"", file=sys.stderr)
        print( "                 (dry-run first; add --apply to write in place)",
              file=sys.stderr)
    else:
        print( "  classification: no safe automated repair available", file=sys.stderr)
        print( "  next step: open the file in an editor and fix manually,", file=sys.stderr)
        print(f"             then re-run `uv run mcp_doctor.py --config {path}`",
              file=sys.stderr)


def load_servers(path: Path) -> dict[str, dict]:
    """Read a config file and return {server_name: server_def}.

    Honors both `mcpServers` (Windsurf/Cursor/Claude/Devin) and `servers` (VS Code).
    Strips `// line comments` from JSONC for resilience.
    """
    raw = path.read_text(encoding="utf-8")
    # Best-effort strip of // comments (devin/vscode/jsonc) — naive but safe enough
    # for our use: don't touch // inside strings.
    cleaned_lines = []
    for line in raw.splitlines():
        in_str = False
        esc = False
        cut = -1
        for i, ch in enumerate(line):
            if esc:
                esc = False; continue
            if ch == "\\":
                esc = True; continue
            if ch == '"':
                in_str = not in_str; continue
            if not in_str and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                cut = i; break
        cleaned_lines.append(line[:cut] if cut >= 0 else line)
    cleaned = "\n".join(cleaned_lines)
    data = json.loads(cleaned)
    servers = data.get("mcpServers") or data.get("servers") or {}
    if not isinstance(servers, dict):
        return {}
    return servers


# ----- Per-server probe -----

@dataclass
class Result:
    name: str
    status: str   # OK | SLOW | FAIL | SKIP
    latency_ms: int
    n_tools: int | None
    detail: str   # for FAIL: reason; for OK: empty


_FAIL_HINTS: list[tuple[str, str]] = [
    # (substring, hint)
    ("Cannot connect to the Docker daemon", "Docker daemon not running. Start Docker Desktop (and verify WSL integration if on WSL)."),
    ("docker: command not found", "Docker CLI not on PATH. Install Docker or fix PATH for the launching shell."),
    ("permission denied while trying to connect to the Docker daemon", "User not in docker group / Docker Desktop not sharing socket."),
    ("npx: command not found", "Node.js / npx not on PATH. Install Node or add nvm bin to PATH for this shell."),
    ("uvx: command not found", "uv not on PATH. Install with: pip install uv (or brew/curl)."),
    ("python3: command not found", "python3 not on PATH."),
    ("command not found", "A required binary is not on the launcher's PATH (Windsurf often launches with a minimal env)."),
    ("ENOTFOUND", "Network/DNS error reaching the server endpoint."),
    ("ECONNREFUSED", "Service refused connection — likely the upstream service (DB / tunnel) is down."),
    ("could not connect to server", "Database/tunnel down."),
    ("authentication failed", "Token or password is wrong/expired."),
    ("401", "Auth failed (HTTP 401). Check token in secrets file."),
    ("403", "Forbidden (HTTP 403). Token lacks required scopes."),
    ("Unauthorized", "Auth failed. Rotate the token in secrets file."),
    ("ENOENT", "A required file/directory is missing — check paths in the wrapper."),
    ("EACCES", "Permission denied. chmod the wrapper or fix file ACLs."),
    ("MODULE_NOT_FOUND", "npx package missing — try a one-time install: npx -y <pkg> --help"),
]


def _hint(stderr_text: str, exit_code: int | None) -> str:
    for needle, hint in _FAIL_HINTS:
        if needle.lower() in stderr_text.lower():
            return hint
    if exit_code == 127:
        return "exit 127 = command not found. Check PATH and binary names."
    if exit_code == 126:
        return "exit 126 = not executable. chmod +x the wrapper."
    return ""


def probe_server(name: str, server: dict, timeout_s: float, slow_ms: int) -> Result:
    """Spawn the MCP server, send initialize + tools/list, return a Result."""
    if server.get("disabled"):
        return Result(name=name, status="SKIP", latency_ms=0, n_tools=None, detail="disabled")

    cmd_str = server.get("command")
    args = list(server.get("args") or [])
    if not cmd_str:
        return Result(name=name, status="FAIL", latency_ms=0, n_tools=None,
                      detail="no `command` field in config")

    # Resolve the command on PATH (Windows shims, .cmd, etc.)
    resolved = shutil.which(cmd_str) or cmd_str

    cmd = [resolved, *args]
    extra_env = server.get("env") or {}
    env = os.environ.copy()
    for k, v in extra_env.items():
        env[k] = str(v) if v is not None else ""

    init = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-doctor", "version": "1.0"},
        },
    }) + "\n"
    inited = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n"
    list_tools = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"

    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, bufsize=1,
        )
    except FileNotFoundError as e:
        return Result(name=name, status="FAIL", latency_ms=0, n_tools=None,
                      detail=f"command not found: {e.filename or cmd_str}")
    except Exception as e:
        return Result(name=name, status="FAIL", latency_ms=0, n_tools=None,
                      detail=f"spawn error: {e}")

    init_resp = None
    tools_resp = None
    sent_inited = False
    sent_list = False
    err_lines: list[str] = []

    deadline = t0 + timeout_s
    try:
        proc.stdin.write(init)
        proc.stdin.flush()
    except Exception as e:
        proc.kill()
        return Result(name=name, status="FAIL", latency_ms=int((time.monotonic() - t0) * 1000),
                      n_tools=None, detail=f"failed to write initialize: {e}")

    if sys.platform == "win32":
        # Windows lacks select() on file objects; use a thread-based pump.
        return _probe_windows(proc, name, t0, deadline, init, inited, list_tools, slow_ms)

    while time.monotonic() < deadline:
        remaining = max(0.05, deadline - time.monotonic())
        rlist, _, _ = select.select([proc.stdout, proc.stderr], [], [], remaining)
        if proc.stdout in rlist:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == 1 and "result" in msg:
                init_resp = msg
                if not sent_inited:
                    proc.stdin.write(inited); proc.stdin.flush(); sent_inited = True
                if not sent_list:
                    proc.stdin.write(list_tools); proc.stdin.flush(); sent_list = True
            elif msg.get("id") == 2 and "result" in msg:
                tools_resp = msg
                break
            elif "error" in msg:
                _terminate(proc)
                return Result(name=name, status="FAIL",
                              latency_ms=int((time.monotonic() - t0) * 1000),
                              n_tools=None, detail=f"server error: {msg['error']}")
        if proc.stderr in rlist:
            err_line = proc.stderr.readline()
            if err_line:
                err_lines.append(err_line.rstrip())
        if proc.poll() is not None and not tools_resp:
            break

    return _finalize(proc, name, t0, init_resp, tools_resp, err_lines, slow_ms)


def _probe_windows(proc, name, t0, deadline, init, inited, list_tools, slow_ms):
    """Windows-friendly probe using threads (no select on pipes)."""
    import queue, threading

    out_q: queue.Queue[str] = queue.Queue()
    err_q: queue.Queue[str] = queue.Queue()

    def pump(stream, q):
        try:
            for line in stream:
                q.put(line)
        except Exception:
            pass

    t_out = threading.Thread(target=pump, args=(proc.stdout, out_q), daemon=True)
    t_err = threading.Thread(target=pump, args=(proc.stderr, err_q), daemon=True)
    t_out.start(); t_err.start()

    init_resp = None
    tools_resp = None
    sent_inited = False
    sent_list = False
    err_lines: list[str] = []

    while time.monotonic() < deadline:
        try:
            line = out_q.get(timeout=0.1)
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == 1 and "result" in msg:
                init_resp = msg
                if not sent_inited:
                    try:
                        proc.stdin.write(inited); proc.stdin.flush()
                    except Exception:
                        pass
                    sent_inited = True
                if not sent_list:
                    try:
                        proc.stdin.write(list_tools); proc.stdin.flush()
                    except Exception:
                        pass
                    sent_list = True
            elif msg.get("id") == 2 and "result" in msg:
                tools_resp = msg
                break
            elif "error" in msg:
                _terminate(proc)
                return Result(name=name, status="FAIL",
                              latency_ms=int((time.monotonic() - t0) * 1000),
                              n_tools=None, detail=f"server error: {msg['error']}")
        except queue.Empty:
            pass
        # Drain stderr non-blockingly
        try:
            while True:
                err_lines.append(err_q.get_nowait().rstrip())
        except queue.Empty:
            pass
        if proc.poll() is not None and not tools_resp:
            break

    # One last drain
    try:
        while True:
            err_lines.append(err_q.get_nowait().rstrip())
    except queue.Empty:
        pass

    return _finalize(proc, name, t0, init_resp, tools_resp, err_lines, slow_ms)


def _finalize(proc, name, t0, init_resp, tools_resp, err_lines, slow_ms) -> Result:
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    exit_code = proc.poll()
    _terminate(proc)

    if init_resp is None:
        first_err = next((l for l in err_lines if l.strip()), "")
        hint = _hint("\n".join(err_lines), exit_code)
        detail = first_err or f"no response within timeout (exit={exit_code})"
        if hint:
            detail = f"{detail} | hint: {hint}"
        return Result(name=name, status="FAIL", latency_ms=elapsed_ms,
                      n_tools=None, detail=detail)

    n_tools = None
    if tools_resp:
        tools = tools_resp.get("result", {}).get("tools") or []
        n_tools = len(tools)

    status = "SLOW" if elapsed_ms > slow_ms else "OK"
    return Result(name=name, status=status, latency_ms=elapsed_ms,
                  n_tools=n_tools, detail="")


def _terminate(proc) -> None:
    try:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
    except Exception:
        pass


# ----- Preflight -----

def preflight() -> list[tuple[str, str]]:
    """Return list of (label, status) for common dependencies."""
    out = []
    # docker
    if shutil.which("docker"):
        try:
            r = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
            out.append(("docker", "OK" if r.returncode == 0 else "DOWN (daemon not reachable)"))
        except Exception as e:
            out.append(("docker", f"ERR ({e})"))
    else:
        out.append(("docker", "MISSING (not on PATH)"))
    # node tools
    for tool in ("npx", "uvx", "python3", "node"):
        out.append((tool, "OK" if shutil.which(tool) else "MISSING"))
    return out


# ----- CLI / output -----

def render_human(config_label: str, config_path: Path, results: list[Result],
                 preflight_rows: list[tuple[str, str]]) -> str:
    lines = []
    lines.append(f"mcp-doctor: config = {config_label} ({config_path})")
    lines.append(f"mcp-doctor: platform = {platform.system()} {platform.release()}")
    lines.append("")
    lines.append("Preflight:")
    for k, v in preflight_rows:
        lines.append(f"  {k:<10} {v}")
    lines.append("")
    lines.append("Servers:")
    name_w = max((len(r.name) for r in results), default=20)
    for r in results:
        if r.status == "SKIP":
            lines.append(f"  SKIP  {r.name:<{name_w}}  (disabled)")
            continue
        if r.status == "FAIL":
            lines.append(f"  FAIL  {r.name:<{name_w}}  {r.latency_ms}ms  {r.detail}")
            continue
        n = r.n_tools if r.n_tools is not None else "?"
        lines.append(f"  {r.status:<5} {r.name:<{name_w}}  {r.latency_ms}ms  {n} tools")
    n_ok = sum(1 for r in results if r.status == "OK")
    n_slow = sum(1 for r in results if r.status == "SLOW")
    n_fail = sum(1 for r in results if r.status == "FAIL")
    n_skip = sum(1 for r in results if r.status == "SKIP")
    lines.append("")
    lines.append(f"Summary: {n_ok} OK, {n_slow} SLOW, {n_fail} FAIL, {n_skip} skipped (disabled)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Diagnose MCP server availability across IDE configs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", help="Path to a specific MCP config file (mcpServers / servers).")
    p.add_argument("--server", help="Test only this server name.")
    p.add_argument("--list", action="store_true", help="List detected configs and exit.")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="Per-server timeout (s).")
    p.add_argument("--slow-ms", type=int, default=DEFAULT_SLOW_MS, help="Latency above which to tag SLOW.")
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output.")
    args = p.parse_args(argv)

    if args.list:
        rows = detect_configs()
        if args.json:
            print(json.dumps([{"label": l, "path": str(p)} for l, p in rows], indent=2))
        else:
            if not rows:
                print("No MCP configs detected on this system.")
                return 2
            for l, path in rows:
                print(f"{l:<16} {path}")
        return 0

    # Pick config
    if args.config:
        cfg_path = Path(os.path.expanduser(args.config)).resolve()
        cfg_label = "custom"
    else:
        detected = detect_configs()
        if not detected:
            print("mcp-doctor: no MCP config detected. Pass --config <path>.", file=sys.stderr)
            return 2
        cfg_label, cfg_path = detected[0]

    if not cfg_path.is_file():
        print(f"mcp-doctor: config not found: {cfg_path}", file=sys.stderr)
        return 2

    try:
        servers = load_servers(cfg_path)
    except json.JSONDecodeError as e:
        _print_parse_error(cfg_path, e)
        return 2
    except Exception as e:
        print(f"mcp-doctor: failed to parse {cfg_path}: {e}", file=sys.stderr)
        return 2

    if args.server:
        if args.server not in servers:
            print(f"mcp-doctor: server '{args.server}' not in {cfg_path}", file=sys.stderr)
            print(f"           available: {', '.join(servers.keys())}", file=sys.stderr)
            return 2
        servers = {args.server: servers[args.server]}

    pre = preflight()
    results = [probe_server(name, defn, args.timeout, args.slow_ms)
               for name, defn in servers.items()]

    if args.json:
        out = {
            "config_label": cfg_label,
            "config_path": str(cfg_path),
            "platform": f"{platform.system()} {platform.release()}",
            "preflight": [{"name": k, "status": v} for k, v in pre],
            "servers": [asdict(r) for r in results],
        }
        print(json.dumps(out, indent=2))
    else:
        print(render_human(cfg_label, cfg_path, results, pre))

    return 0 if not any(r.status == "FAIL" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
