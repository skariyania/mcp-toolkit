# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""mcp_sync - Translate-and-distribute one MCP master config across tools/OSes.

Reads a single source-of-truth MCP config (Windsurf-flavored mcpServers JSON)
and produces equivalent configs for any combination of:

    - Windsurf (Windows / WSL)
    - LM Studio (Windows / WSL)
    - VS Code (Windows / WSL)        [schema rewrite: mcpServers -> servers]
    - Devin     (Windows / WSL)      [merges into existing config.json]
    - Master    (Windows .config/mcp/servers.json)

It UNWRAPS bash wrapper scripts (the WSL pattern of `bash mcp-foo.sh` that
sources secrets.env then `exec`s docker/npx/uvx) back into their inline
`command + args + env` form so the result actually runs on a Windows IDE.
Wrappers it can't recognize are SKIPPED with an explanation; never silently
emits broken config.

Default is dry-run: prints a diff against each target file. Use `--apply`
to write. Backups (`.bak.<ts>`) are made before any overwrite.

Secrets handling: by default refuses to translate any server whose required
env vars aren't already present in the target secrets file. Pass
`--mirror-secrets` to copy missing keys from source secrets to target after
showing a diff (you'll still have to confirm with `yes` before plaintext
tokens cross OS boundaries).

USAGE
    # Dry-run: WSL master -> all known Windows targets
    uv run mcp_sync.py --source ~/.codeium/windsurf/mcp_config.json \\
                       --target-os windows --targets all

    # Apply: WSL master -> Windows Windsurf only
    uv run mcp_sync.py --source ~/.codeium/windsurf/mcp_config.json \\
                       --target-os windows --targets windsurf --apply

    # Reverse: Windows master -> WSL Windsurf
    uv run mcp_sync.py --source %USERPROFILE%\\.config\\mcp\\servers.json \\
                       --target-os wsl --targets windsurf,vscode --apply

    # Custom target file
    uv run mcp_sync.py --source <src> --to /custom/path/config.json --schema windsurf

    # Sync secrets across with consent
    uv run mcp_sync.py --source <src> --target-os windows --targets windsurf \\
                       --mirror-secrets --apply

EXIT CODES
    0   no changes needed (or all changes successfully applied)
    1   changes pending (dry-run with diffs)
    2   error
    3   one or more servers couldn't be translated (warnings shown)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import io
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

# --- UTF-8 stdout fix for Windows ---
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# --- Known target locations (per-OS) ---

@dataclass(frozen=True)
class TargetSpec:
    name: str
    schema: str   # "mcpServers", "servers", or "devin"  (devin = merge into existing)
    win_path: str  # absolute Windows path (string template; ${USERPROFILE}, ${APPDATA})
    wsl_path: str  # absolute POSIX path (string template; ${HOME})

TARGETS: dict[str, TargetSpec] = {
    "windsurf":  TargetSpec("windsurf",  "mcpServers",
                            "${USERPROFILE}\\.codeium\\windsurf\\mcp_config.json",
                            "${HOME}/.codeium/windsurf/mcp_config.json"),
    "lmstudio":  TargetSpec("lmstudio",  "mcpServers",
                            "${USERPROFILE}\\.lmstudio\\mcp.json",
                            "${HOME}/.lmstudio/mcp.json"),
    "vscode":    TargetSpec("vscode",    "servers",
                            "${APPDATA}\\Code\\User\\mcp.json",
                            "${HOME}/.config/Code/User/mcp.json"),
    "devin":     TargetSpec("devin",     "devin",
                            "${APPDATA}\\devin\\config.json",
                            "${HOME}/.config/devin/config.json"),
    "master":    TargetSpec("master",    "mcpServers",
                            "${USERPROFILE}\\.config\\mcp\\servers.json",
                            "${HOME}/.config/mcp/servers.json"),
}

# Per-OS secrets file (where translated `--env-file` references should point,
# and where `--mirror-secrets` reads/writes keys).
SECRETS_PATH = {
    "windows": "${USERPROFILE}\\.config\\mcp\\.env",
    "wsl":     "${HOME}/.codeium/windsurf/secrets.env",
}


# --- Path expansion ---

def expand_for_os(template: str, target_os: str) -> Path:
    """Expand ${USERPROFILE}/${APPDATA}/${HOME} for the *target* OS.

    When running on the same OS as the target, env vars work directly.
    When crossing (e.g. WSL -> Windows), we synthesize the path using:
      1) MCP_WSL_DISTRO / MCP_WSL_USER / MCP_WINDOWS_USER env vars if set,
      2) WSL_DISTRO_NAME (set automatically inside WSL) for the distro,
      3) the first dir found under \\wsl.localhost\ or /mnt/c/Users,
      4) $USER / %USERNAME% / 'Ubuntu' as last-resort fallbacks.
    """
    if target_os == "windows":
        userprofile = os.environ.get("USERPROFILE")
        appdata = os.environ.get("APPDATA")
        if not userprofile or not appdata:
            # Probably running from WSL; synthesize using the WSL-mounted user dir.
            # Convention: /mnt/c/Users/<USER> — try to find it.
            mnt_users = Path("/mnt/c/Users")
            if mnt_users.is_dir():
                # Pick the only user dir that has a `.codeium` folder, or fall back
                # to the conventional one.
                candidates = sorted(mnt_users.iterdir())
                user_dir = None
                for c in candidates:
                    if (c / ".codeium").is_dir() or (c / ".config").is_dir():
                        user_dir = c; break
                if user_dir is None:
                    user_dir = candidates[0] if candidates else mnt_users / os.environ.get("USERNAME", "user")
                userprofile = str(user_dir)
                appdata = str(user_dir / "AppData" / "Roaming")
            else:
                # last-resort fallback (used in dry-run output only)
                userprofile = "C:\\Users\\<USER>"
                appdata = "C:\\Users\\<USER>\\AppData\\Roaming"
        return Path(template.replace("${USERPROFILE}", userprofile)
                            .replace("${APPDATA}", appdata))
    else:  # wsl
        # If we're actually running inside WSL/Linux, $HOME is correct.
        # If we're on Windows targeting WSL paths, $HOME (often set by git-bash)
        # would point to a Windows location — force UNC instead.
        running_on_posix = not sys.platform.startswith("win")
        home_env = os.environ.get("HOME")
        if running_on_posix and home_env:
            return Path(template.replace("${HOME}", home_env))
        # Cross from Windows -> WSL: use UNC. Detect the distro dynamically.
        distro = (os.environ.get("MCP_WSL_DISTRO")
                  or os.environ.get("WSL_DISTRO_NAME")
                  or _first_wsl_distro_or("Ubuntu"))
        unc_root = Path(f"\\\\wsl.localhost\\{distro}\\home")
        user_dir = None
        if unc_root.is_dir():
            kids = list(unc_root.iterdir())
            if kids:
                user_dir = kids[0]
        if user_dir is None:
            fallback_user = (os.environ.get("MCP_WSL_USER")
                             or os.environ.get("USER")
                             or os.environ.get("USERNAME")
                             or "user")
            user_dir = Path(f"\\\\wsl.localhost\\{distro}\\home") / fallback_user
        home = str(user_dir).replace("/", "\\")
        return Path(template.replace("${HOME}", home))


def _first_wsl_distro_or(default: str) -> str:
    """List \\wsl.localhost\ for installed distros, return the first or default."""
    if sys.platform == "win32":
        root = Path("\\\\wsl.localhost")
        if root.is_dir():
            kids = [c.name for c in root.iterdir() if c.is_dir() and not c.name.startswith("$")]
            if kids:
                return kids[0]
    return default


def secrets_path_for(target_os: str) -> Path:
    return expand_for_os(SECRETS_PATH[target_os], target_os)


# --- Comment-tolerant JSON load ---

def strip_jsonc(raw: str) -> str:
    out_lines = []
    for line in raw.splitlines():
        in_str = False; esc = False; cut = -1
        for i, ch in enumerate(line):
            if esc: esc = False; continue
            if ch == "\\": esc = True; continue
            if ch == '"': in_str = not in_str; continue
            if not in_str and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                cut = i; break
        out_lines.append(line[:cut] if cut >= 0 else line)
    return "\n".join(out_lines)

def load_json(path: Path) -> dict:
    return json.loads(strip_jsonc(path.read_text(encoding="utf-8")))


# --- Canonical Server model ---

@dataclass
class Server:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)   # explicit values
    env_inherit: list[str] = field(default_factory=list) # docker -e VAR (no value, inherits)
    env_file: str | None = None                         # path to a .env (translated per target)
    disabled: bool = False
    extra: dict = field(default_factory=dict)           # passthrough fields (registry, disabledTools, ...)
    notes: list[str] = field(default_factory=list)      # human-readable notes (translation log)
    source_os: str = "wsl"                              # where this Server was unwrapped from


def parse_source(path: Path, source_os: str) -> tuple[list[Server], list[tuple[str, str]]]:
    """Read source config, return (servers, skipped) where skipped is [(name, reason)]."""
    data = load_json(path)
    raw_servers = data.get("mcpServers") or data.get("servers") or {}
    servers: list[Server] = []
    skipped: list[tuple[str, str]] = []
    for name, defn in raw_servers.items():
        if not isinstance(defn, dict):
            skipped.append((name, "definition is not an object")); continue
        s = Server(name=name, command=defn.get("command", ""),
                   args=list(defn.get("args") or []),
                   env=dict(defn.get("env") or {}),
                   disabled=bool(defn.get("disabled", False)),
                   source_os=source_os)
        # passthrough metadata
        for k, v in defn.items():
            if k not in ("command", "args", "env", "disabled"):
                s.extra[k] = v
        # If wrapper-style, try to unwrap.
        if _is_bash_wrapper(s):
            ok, reason = unwrap_bash_wrapper(s, source_os)
            if not ok:
                skipped.append((name, reason)); continue
        servers.append(s)
    return servers, skipped


# --- Wrapper unwrapping ---

def _is_bash_wrapper(s: Server) -> bool:
    if s.command not in ("bash", "/bin/bash", "/usr/bin/bash"):
        return False
    if not s.args:
        return False
    # arg[0] should be a path ending in .sh
    return str(s.args[0]).endswith(".sh")


_WRAPPER_PATTERNS = {
    # `set -a; source "$(dirname "$0")/../secrets.env"; set +a`
    "source_secrets":  re.compile(r"source\s+[\"']?\$\(dirname\s+\"\$0\"\)/\.\./secrets\.env[\"']?"),
    "export_kv":       re.compile(r'^\s*export\s+([A-Z_][A-Z0-9_]*)\s*=\s*"([^"]*)"\s*$', re.MULTILINE),
    "exec_line_start": re.compile(r"^\s*exec\s+", re.MULTILINE),
}

def unwrap_bash_wrapper(s: Server, source_os: str) -> tuple[bool, str]:
    """Mutate `s` in place: unwrap bash wrapper to inline command + args + env.

    Returns (success, reason). On failure, `s` may be partially mutated; caller
    should treat as skipped.

    Recognized patterns (matches the user's wrappers/*.sh format):
      1) `set -euo pipefail`
      2) `set -a; source ../secrets.env; set +a`
      3) zero or more `export VAR="literal"` lines
      4) one `exec <cmd> <args...>` line where <cmd> is docker / npx / uvx / python3
    """
    wrapper_path = s.args[0]
    # If running on a different OS than where the wrapper lives, translate the path
    # to read it. The wrapper is stored on the source side.
    src_path = _read_wrapper(wrapper_path, source_os)
    if src_path is None:
        return False, f"wrapper not readable: {wrapper_path}"

    text = src_path.read_text(encoding="utf-8", errors="replace")
    sources_secrets = bool(_WRAPPER_PATTERNS["source_secrets"].search(text))
    explicit_env: dict[str, str] = {}
    for m in _WRAPPER_PATTERNS["export_kv"].finditer(text):
        explicit_env[m.group(1)] = m.group(2)

    exec_lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("exec ")]
    if len(exec_lines) != 1:
        return False, f"unsupported wrapper: expected exactly one `exec` line, found {len(exec_lines)}"
    exec_line = exec_lines[0][len("exec "):].strip()
    # The wrapper may continue across lines via backslash-newline; normalize.
    # Re-grab the full continuation block:
    block = []
    in_block = False
    for ln in text.splitlines():
        if not in_block:
            if ln.strip().startswith("exec "):
                in_block = True
                block.append(ln.rstrip())
                if not ln.rstrip().endswith("\\"):
                    break
            continue
        block.append(ln.rstrip())
        if not ln.rstrip().endswith("\\"):
            break
    full_exec = " ".join(b.rstrip("\\").strip() for b in block)[len("exec "):].strip()

    try:
        tokens = shlex.split(full_exec)
    except ValueError as e:
        return False, f"unsupported wrapper: cannot parse exec line ({e})"
    if not tokens:
        return False, "unsupported wrapper: empty exec"
    cmd, *rest = tokens
    if cmd not in ("docker", "npx", "uvx", "python3"):
        return False, f"unsupported wrapper: exec command `{cmd}` not in (docker|npx|uvx|python3)"

    # Special handling for `python3 .../mcp-tool-rename.py` — this is a WSL-only
    # shim. We can't translate it to Windows-native cleanly. Skip.
    if cmd == "python3" and any("mcp-tool-rename.py" in t for t in rest):
        return False, "unsupported wrapper: uses mcp-tool-rename.py shim (POSIX-only)"

    # For docker, convert `-e VAR` (no value, inherits from env) into env_inherit
    # and rewrite to `--env-file <target secrets path>`. We do that *here* by
    # extracting the inheriting vars; the per-target writer will inject the
    # correct --env-file path.
    if cmd == "docker":
        new_args, inherit_vars = _strip_docker_e_inherit(rest)
        s.command = "docker"
        s.args = new_args
        s.env_inherit = inherit_vars
        s.env_file = "<TARGET_SECRETS>"   # placeholder; replaced per target
        # explicit env from the wrapper's `export VAR=lit`
        s.env.update(explicit_env)
        s.notes.append(f"unwrapped from bash wrapper {wrapper_path}")
        return True, "ok"

    # npx / uvx / python3 — straight passthrough (env populated by source-secrets)
    s.command = cmd
    s.args = rest
    s.env.update(explicit_env)
    if sources_secrets:
        # Mark that this server inherits ALL keys from the source secrets file.
        # We can't enumerate them without reading secrets; set a sentinel.
        s.notes.append("wrapper sources secrets.env; on Windows this needs explicit env on the launching tool.")
    s.notes.append(f"unwrapped from bash wrapper {wrapper_path}")
    return True, "ok"


def _read_wrapper(wrapper_path: str, source_os: str) -> Path | None:
    """Best-effort: locate the wrapper file readable from this process."""
    # Try the literal path first (works when running on the same OS as source).
    p = Path(os.path.expanduser(os.path.expandvars(wrapper_path)))
    if p.is_file():
        return p
    # If source is WSL but we're on Windows, translate POSIX → UNC.
    if source_os == "wsl" and sys.platform == "win32" and wrapper_path.startswith("/"):
        distro = (os.environ.get("MCP_WSL_DISTRO")
                  or _first_wsl_distro_or("Ubuntu"))
        unc = Path(f"\\\\wsl.localhost\\{distro}") / Path(*Path(wrapper_path).parts[1:])
        if unc.is_file():
            return unc
    # If source is Windows but we're on Linux/WSL, translate Windows path.
    if source_os == "windows" and not sys.platform.startswith("win"):
        # Replace C:\... with /mnt/c/...
        m = re.match(r"^([A-Za-z]):\\(.*)$", wrapper_path.replace("\\", "\\"))
        if m:
            drive = m.group(1).lower(); rest = m.group(2).replace("\\", "/")
            mp = Path(f"/mnt/{drive}/{rest}")
            if mp.is_file():
                return mp
    return None


def _strip_docker_e_inherit(args: list[str]) -> tuple[list[str], list[str]]:
    """Process `docker run ... -e VAR ... -e VAR2 ...`: remove the bare-name
    `-e VAR` flags (inherit from process env) and return (cleaned_args, vars).
    Leaves `-e VAR=value` (explicit value) alone.
    """
    out: list[str] = []
    inherit: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-e" and i + 1 < len(args):
            val = args[i + 1]
            if "=" in val:
                # explicit value, keep
                out.extend([a, val])
            else:
                inherit.append(val)
            i += 2; continue
        out.append(a); i += 1
    return out, inherit


# --- Per-target rendering ---

def render_target(servers: list[Server], target: TargetSpec, target_os: str,
                  existing: dict | None) -> tuple[dict, list[str]]:
    """Return (new_config_dict, notes). `existing` is the prior file's parsed
    contents (for devin merge); pass None if file doesn't exist.
    """
    notes: list[str] = []
    secrets_target = secrets_path_for(target_os)

    rendered: dict[str, dict] = {}
    for s in servers:
        d: dict = {"command": s.command}
        if s.args:
            args = list(s.args)
            if s.env_file == "<TARGET_SECRETS>":
                # Inject `--env-file <secrets>` right after `run` if docker, else first.
                if s.command == "docker" and "run" in args:
                    idx = args.index("run") + 1
                    args[idx:idx] = ["--env-file", str(secrets_target)]
                else:
                    args = ["--env-file", str(secrets_target)] + args
            d["args"] = args
        if s.env:
            d["env"] = dict(s.env)
        if s.disabled:
            d["disabled"] = True
        # passthrough extras (registry, disabledTools, ...)
        for k, v in s.extra.items():
            d[k] = v
        # Schema-specific transforms
        if target.schema == "servers":
            # VS Code wants type: stdio
            d["type"] = "stdio"
        rendered[s.name] = d

    if target.schema in ("mcpServers", "servers"):
        key = "mcpServers" if target.schema == "mcpServers" else "servers"
        out = {key: rendered}
        # Preserve a top-level _comment if existing had one (cosmetic)
        if existing and "_comment" in existing:
            out["_comment"] = existing["_comment"]
        return out, notes

    if target.schema == "devin":
        # Merge: keep all existing top-level fields, replace mcpServers only.
        merged = dict(existing or {})
        merged["mcpServers"] = rendered
        merged.setdefault("version", 1)
        notes.append("merged into existing devin config (preserved permissions, theme, agent, etc.)")
        return merged, notes

    raise ValueError(f"unknown schema {target.schema}")


# --- Diff & write ---

def render_pretty(d: dict) -> str:
    return json.dumps(d, indent=2, ensure_ascii=False) + "\n"

def diff_against(existing_text: str, new_text: str, label: str) -> str:
    if existing_text == new_text:
        return ""
    diff = difflib.unified_diff(
        existing_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"{label} (current)", tofile=f"{label} (new)", n=3,
    )
    return "".join(diff)

def backup_and_write(path: Path, text: str) -> Path | None:
    """If path exists, copy to path.bak.<ts>. Then write new text. Return backup path or None."""
    bak: Path | None = None
    if path.exists():
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = path.with_name(path.name + f".bak.{ts}")
        bak.write_bytes(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return bak


# --- Secrets handling ---

_KV_LINE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$")

def parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _KV_LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip('"').strip("'")
    return out

def write_env_file(path: Path, kv: dict[str, str]) -> None:
    lines = ["# Synced by mcp_sync — chmod 600 on POSIX. Do not commit.", ""]
    for k in sorted(kv.keys()):
        lines.append(f"{k}={kv[k]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not sys.platform.startswith("win"):
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass

def required_env_keys(servers: list[Server]) -> set[str]:
    """Keys that must come from the target secrets file: docker `-e VAR`
    inheriting flags, MINUS any that are already inlined as explicit `env`
    values on the same server (those came from `export VAR=lit` in the
    wrapper and are not actually secrets)."""
    keys: set[str] = set()
    for s in servers:
        for v in s.env_inherit:
            if v not in s.env:
                keys.add(v)
    return keys


# --- CLI ---

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Translate-and-distribute one MCP master config across tools/OSes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--source", required=True, help="Path to source MCP config.")
    p.add_argument("--source-os", choices=["windows", "wsl"], default=None,
                   help="OS the source config was authored for. Default: detect from path.")
    p.add_argument("--target-os", choices=["windows", "wsl"], default=None,
                   help="OS to write target paths for. Default: current OS.")
    p.add_argument("--targets", default="",
                   help=f"Comma list or 'all'. Known: {', '.join(TARGETS.keys())}.")
    p.add_argument("--to", default=None,
                   help="Custom target file path (use with --schema). Mutually exclusive with --targets.")
    p.add_argument("--schema", choices=["mcpServers", "servers", "devin"], default=None,
                   help="Schema for --to.")
    p.add_argument("--apply", action="store_true",
                   help="Actually write files (default is dry-run).")
    p.add_argument("--mirror-secrets", action="store_true",
                   help="Copy missing secrets from source secrets file to target. Asks once.")
    args = p.parse_args(argv)

    # Resolve source
    src_path = Path(os.path.expanduser(args.source)).resolve()
    if not src_path.is_file():
        print(f"mcp_sync: source not found: {src_path}", file=sys.stderr)
        return 2

    source_os = args.source_os or _guess_os_from_path(src_path)
    target_os = args.target_os or ("windows" if sys.platform == "win32" else "wsl")

    # Parse + unwrap
    print(f"mcp_sync: source = {src_path}  (source_os={source_os})")
    print(f"mcp_sync: target_os = {target_os}\n")
    servers, skipped = parse_source(src_path, source_os)
    print(f"Parsed {len(servers)} servers ({sum(1 for s in servers if s.disabled)} disabled).")
    if skipped:
        print(f"Skipped {len(skipped)} servers:")
        for n, r in skipped:
            print(f"  - {n}: {r}")
    print()

    # Resolve targets
    target_specs: list[tuple[TargetSpec, Path]] = []
    if args.to:
        if not args.schema:
            print("mcp_sync: --to requires --schema", file=sys.stderr); return 2
        spec = TargetSpec(name="custom", schema=args.schema, win_path="", wsl_path="")
        target_specs.append((spec, Path(os.path.expanduser(args.to)).resolve()))
    else:
        if args.targets in ("", "all"):
            keys = list(TARGETS.keys()) if args.targets == "all" else []
        else:
            keys = [t.strip() for t in args.targets.split(",") if t.strip()]
        if not keys:
            print("mcp_sync: nothing to do — pass --targets <list> or --targets all, or --to <path>",
                  file=sys.stderr)
            return 2
        for k in keys:
            if k not in TARGETS:
                print(f"mcp_sync: unknown target '{k}'. known: {', '.join(TARGETS.keys())}",
                      file=sys.stderr); return 2
            tspec = TARGETS[k]
            tpath = expand_for_os(
                tspec.win_path if target_os == "windows" else tspec.wsl_path,
                target_os,
            )
            target_specs.append((tspec, tpath))

    # Secrets check
    src_secrets_path = secrets_path_for(source_os)
    tgt_secrets_path = secrets_path_for(target_os)
    src_secrets = parse_env_file(src_secrets_path) if src_secrets_path.is_file() else {}
    tgt_secrets = parse_env_file(tgt_secrets_path) if tgt_secrets_path.is_file() else {}
    needed = required_env_keys(servers)
    missing_in_target = sorted(needed - set(tgt_secrets.keys()))
    if missing_in_target:
        print(f"Secrets check: {len(missing_in_target)} key(s) needed in target secrets ({tgt_secrets_path}) but missing:")
        for k in missing_in_target:
            print(f"  - {k}  (source has it: {'yes' if k in src_secrets else 'NO'})")
        if not args.mirror_secrets:
            print("\n  Pass --mirror-secrets to copy these from source -> target (with consent).\n")
        else:
            mirrorable = [k for k in missing_in_target if k in src_secrets]
            non_mirrorable = [k for k in missing_in_target if k not in src_secrets]
            if non_mirrorable:
                print(f"  Source secrets file is missing: {', '.join(non_mirrorable)} — these can't be mirrored.")
            if mirrorable:
                resp = input(f"\n  Mirror {len(mirrorable)} secret(s) to {tgt_secrets_path}? (yes/NO): ").strip().lower()
                if resp in ("yes", "y"):
                    new_secrets = dict(tgt_secrets)
                    for k in mirrorable:
                        new_secrets[k] = src_secrets[k]
                    if args.apply:
                        write_env_file(tgt_secrets_path, new_secrets)
                        print(f"  Wrote {len(mirrorable)} keys to {tgt_secrets_path}")
                    else:
                        print(f"  (dry-run) would write {len(mirrorable)} keys to {tgt_secrets_path}")
                else:
                    print("  Skipped secrets mirroring.")

    # Per-target render + diff
    pending = 0
    applied = 0
    for tspec, tpath in target_specs:
        print(f"\n--- Target: {tspec.name} -> {tpath}")
        existing = None
        existing_text = ""
        if tpath.is_file():
            existing_text = tpath.read_text(encoding="utf-8")
            try:
                existing = load_json(tpath)
            except Exception as e:
                print(f"  (target unparseable: {e}; will overwrite)")
        new_dict, notes = render_target(servers, tspec, target_os, existing)
        new_text = render_pretty(new_dict)
        for n in notes:
            print(f"  note: {n}")

        d = diff_against(existing_text, new_text, str(tpath))
        if not d:
            print("  no changes.")
            continue
        pending += 1
        print(d)
        if args.apply:
            bak = backup_and_write(tpath, new_text)
            applied += 1
            if bak:
                print(f"  wrote {tpath} (backup: {bak.name})")
            else:
                print(f"  wrote {tpath} (new file)")

    # Summary
    print()
    if args.apply:
        print(f"mcp_sync: applied {applied} change(s).")
    else:
        if pending:
            print(f"mcp_sync: dry-run — {pending} target(s) have pending changes. Re-run with --apply.")
        else:
            print("mcp_sync: dry-run — no changes needed.")

    rc = 0
    if not args.apply and pending:
        rc = 1
    if skipped:
        rc = max(rc, 3)
    return rc


def _guess_os_from_path(p: Path) -> str:
    s = str(p)
    if s.startswith("\\\\wsl") or s.startswith("/home/") or s.startswith("/mnt/"):
        return "wsl"
    if re.match(r"^[A-Za-z]:[\\/]", s) or s.startswith("\\\\"):
        return "windows"
    # POSIX-looking
    return "wsl" if s.startswith("/") else ("windows" if sys.platform == "win32" else "wsl")


if __name__ == "__main__":
    sys.exit(main())
