# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""mcp_memory_sync - Mirror the Memory MCP knowledge-graph file across OSes.

The four sibling scripts (`mcp_sync.py` et al.) only sync MCP *configs*; they
deliberately do NOT touch the data files that MCP servers write to.  For most
MCPs that's correct.  For `@modelcontextprotocol/server-memory`, though, the
data file IS the knowledge graph -- the value lives entirely in it -- and
having two un-synced graphs (one on Windows, one on WSL) is the same kind of
silent-data-loss trap that `mcp_sync.py` exists to prevent for configs.

This script closes that gap.  Default behaviour is a **bidirectional union
merge**:

  * Read each side's memory-graph file (paths discovered from the
    `MEMORY_FILE_PATH` env var inside each side's Windsurf MCP config).
  * Parse as JSONL (the upstream format: one entity / relation per line).
  * Build a union keyed by entity `name` (observations merged, dedup-
    preserving order) and by relation triple `(from, to, relationType)`.
  * Write the union back to both sides.  Both originals are backed up to
    `.bak.<timestamp>` before any overwrite.

USAGE
    # Dry-run bidirectional merge (default, no writes)
    uv run mcp_memory_sync.py

    # Apply the merge
    uv run mcp_memory_sync.py --apply

    # One-way overwrite (WSL is master, Windows is overwritten)
    uv run mcp_memory_sync.py --direction wsl-to-windows --apply

    # One-way the other way
    uv run mcp_memory_sync.py --direction windows-to-wsl --apply

    # Custom file paths (bypass config discovery)
    uv run mcp_memory_sync.py --windows-file C:\\Users\\me\\mem.json \\
                              --wsl-file /home/me/mem.json

EXIT CODES
    0   no changes needed (graphs already in sync, or --apply succeeded)
    1   changes pending (dry-run with diffs)
    2   error
    3   one side missing MEMORY_FILE_PATH (refused; see README)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# --- UTF-8 stdout fix for Windows ---
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# ============================================================================
# Path discovery
# ============================================================================

# Windsurf MCP config locations (where MEMORY_FILE_PATH is declared).
WINDSURF_CFG_TEMPLATE = {
    "windows": "${USERPROFILE}\\.codeium\\windsurf\\mcp_config.json",
    "wsl":     "${HOME}/.codeium/windsurf/mcp_config.json",
}


def _detect_wsl_distro() -> str:
    """Return the WSL distro name (env-var first, then probe \\\\wsl.localhost)."""
    env = os.environ.get("MCP_WSL_DISTRO") or os.environ.get("WSL_DISTRO_NAME")
    if env:
        return env
    if sys.platform == "win32":
        root = Path("\\\\wsl.localhost")
        if root.is_dir():
            kids = [c.name for c in root.iterdir()
                    if c.is_dir() and not c.name.startswith("$")]
            if kids:
                return kids[0]
    return "Ubuntu"


def _detect_wsl_user() -> str:
    env = os.environ.get("MCP_WSL_USER")
    if env:
        return env
    distro = _detect_wsl_distro()
    if sys.platform == "win32":
        unc = Path(f"\\\\wsl.localhost\\{distro}\\home")
        if unc.is_dir():
            kids = [c.name for c in unc.iterdir() if c.is_dir()]
            if kids:
                return kids[0]
    return os.environ.get("USER") or os.environ.get("USERNAME") or "user"


def _detect_windows_user() -> str:
    env = os.environ.get("MCP_WINDOWS_USER")
    if env:
        return env
    if sys.platform != "win32":
        mnt = Path("/mnt/c/Users")
        if mnt.is_dir():
            for c in sorted(mnt.iterdir()):
                if c.is_dir() and c.name not in ("Public", "Default", "All Users", "Default User"):
                    return c.name
    return os.environ.get("USERNAME") or os.environ.get("USER") or "user"


def _expand_for_os(template: str, side: str) -> Path:
    """Expand ${HOME}/${USERPROFILE} for either side, including cross-OS UNC/mnt."""
    if side == "windows":
        userprofile = os.environ.get("USERPROFILE")
        if userprofile and sys.platform == "win32":
            return Path(template.replace("${USERPROFILE}", userprofile))
        # Running on WSL/Linux but targeting Windows -> /mnt/c/Users/<user>
        user = _detect_windows_user()
        userprofile = f"/mnt/c/Users/{user}"
        # Convert Windows-style backslashes to forward for the /mnt path
        t = template.replace("${USERPROFILE}", userprofile).replace("\\", "/")
        return Path(t)
    # side == "wsl"
    home = os.environ.get("HOME")
    if home and sys.platform != "win32":
        return Path(template.replace("${HOME}", home))
    # Running on Windows but targeting WSL -> UNC
    distro = _detect_wsl_distro()
    user = _detect_wsl_user()
    home = f"\\\\wsl.localhost\\{distro}\\home\\{user}"
    return Path(template.replace("${HOME}", home).replace("/", "\\"))


def _translate_native_path_to_local(native_path: str, side: str) -> Path:
    """Given a native path declared in side's config (e.g. /home/x/foo.json on
    WSL or C:\\Users\\x\\foo.json on Windows), return a Path we can actually
    open from THIS process."""
    p = native_path.strip()
    if not p:
        return Path(p)
    on_windows = sys.platform == "win32"
    if side == "windows":
        # The config path is Windows-native (e.g. C:\Users\x\foo.json).
        if on_windows:
            return Path(os.path.expandvars(os.path.expanduser(p)))
        # We're on WSL: rewrite C:\... -> /mnt/c/...
        if len(p) >= 2 and p[1] == ":":
            drive = p[0].lower()
            rest = p[2:].lstrip("\\/").replace("\\", "/")
            return Path(f"/mnt/{drive}/{rest}")
        return Path(p)
    # side == "wsl": config path is POSIX (e.g. /home/x/foo.json).
    if not on_windows:
        return Path(os.path.expandvars(os.path.expanduser(p)))
    # On Windows: rewrite /home/... -> \\wsl.localhost\<distro>\home\...
    distro = _detect_wsl_distro()
    rest = p.lstrip("/").replace("/", "\\")
    return Path(f"\\\\wsl.localhost\\{distro}\\{rest}")


def _strip_jsonc(raw: str) -> str:
    """Tiny JSONC stripper (line-comments only)."""
    out = []
    for line in raw.splitlines():
        in_str = False; esc = False; cut = -1
        for i, ch in enumerate(line):
            if esc: esc = False; continue
            if ch == "\\": esc = True; continue
            if ch == '"': in_str = not in_str; continue
            if not in_str and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                cut = i; break
        out.append(line[:cut] if cut >= 0 else line)
    return "\n".join(out)


def _read_memory_file_path_from_config(cfg_path: Path) -> str | None:
    """Open a Windsurf-format MCP config, return the memory server's
    MEMORY_FILE_PATH (or None if missing/empty/server absent)."""
    if not cfg_path.exists():
        return None
    try:
        cfg = json.loads(_strip_jsonc(cfg_path.read_text(encoding="utf-8")))
    except Exception as e:
        print(f"  WARN: could not parse {cfg_path}: {e}", file=sys.stderr)
        return None
    servers = cfg.get("mcpServers") or cfg.get("servers") or {}
    mem = servers.get("memory")
    if not mem:
        return None
    env = mem.get("env") or {}
    p = env.get("MEMORY_FILE_PATH", "").strip()
    return p or None


@dataclass
class Side:
    name: str                 # "windows" | "wsl"
    cfg_path: Path            # path we read MEMORY_FILE_PATH from
    declared_path: str        # raw path string from config (native to that OS)
    file: Path                # path we can actually open from this process


def discover_sides(
    windows_file: str | None = None,
    wsl_file: str | None = None,
) -> tuple[Side | None, Side | None, list[str]]:
    """Locate both sides' memory files.  Overrides (--windows-file / --wsl-file)
    skip config discovery for that side."""
    notes: list[str] = []

    def _build(side_name: str, override: str | None) -> Side | None:
        cfg = _expand_for_os(WINDSURF_CFG_TEMPLATE[side_name], side_name)
        if override:
            declared = override
            notes.append(f"  {side_name}: overridden via --{side_name}-file")
        else:
            declared = _read_memory_file_path_from_config(cfg) or ""
            if not declared:
                notes.append(f"  {side_name}: NO MEMORY_FILE_PATH in {cfg}")
                return None
        local = _translate_native_path_to_local(declared, side_name)
        return Side(name=side_name, cfg_path=cfg, declared_path=declared, file=local)

    win = _build("windows", windows_file)
    wsl = _build("wsl", wsl_file)
    return win, wsl, notes


# ============================================================================
# Graph parse / merge
# ============================================================================

@dataclass
class Graph:
    # entity_name -> dict (raw upstream JSON), with observations as list
    entities: dict[str, dict] = field(default_factory=dict)
    # (from, to, relationType) -> raw dict
    relations: dict[tuple[str, str, str], dict] = field(default_factory=dict)
    # Lines we couldn't parse (kept verbatim for round-trip safety)
    extras: list[str] = field(default_factory=list)


def parse_graph(path: Path) -> Graph:
    g = Graph()
    if not path.exists():
        return g
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"  WARN: could not read {path}: {e}", file=sys.stderr)
        return g
    for ln in raw.splitlines():
        s = ln.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except Exception:
            g.extras.append(ln)
            continue
        t = obj.get("type")
        if t == "entity":
            name = obj.get("name")
            if not isinstance(name, str):
                g.extras.append(ln); continue
            # Normalize observations to a list of strings
            obs = obj.get("observations") or []
            if not isinstance(obs, list):
                obs = []
            obj["observations"] = [str(o) for o in obs]
            g.entities[name] = obj
        elif t == "relation":
            f = obj.get("from"); to = obj.get("to"); rt = obj.get("relationType")
            if not all(isinstance(x, str) for x in (f, to, rt)):
                g.extras.append(ln); continue
            g.relations[(f, to, rt)] = obj
        else:
            g.extras.append(ln)
    return g


def _merge_observations(left: list[str], right: list[str]) -> list[str]:
    """Union of observations, preserving order: left first, then any new from right."""
    seen = set(left)
    out = list(left)
    for o in right:
        if o not in seen:
            out.append(o)
            seen.add(o)
    return out


@dataclass
class MergeStats:
    entities_added_from_other: int = 0
    relations_added_from_other: int = 0
    entities_observations_grew: int = 0
    entity_type_conflicts: list[tuple[str, str, str]] = field(default_factory=list)  # (name, left_type, right_type)


def union_into(base: Graph, other: Graph) -> MergeStats:
    """Mutate `base` to be the union of base and other.  Returns counts of
    changes made to `base`."""
    st = MergeStats()
    for name, e in other.entities.items():
        if name not in base.entities:
            base.entities[name] = dict(e)
            base.entities[name]["observations"] = list(e.get("observations", []))
            st.entities_added_from_other += 1
            continue
        # Same name on both sides
        b = base.entities[name]
        b_type = b.get("entityType")
        e_type = e.get("entityType")
        if b_type and e_type and b_type != e_type:
            st.entity_type_conflicts.append((name, b_type, e_type))
            # Keep base's entityType.  Continue with observation merge below.
        before = len(b.get("observations", []))
        b["observations"] = _merge_observations(
            list(b.get("observations") or []),
            list(e.get("observations") or []),
        )
        if len(b["observations"]) > before:
            st.entities_observations_grew += 1
    for key, r in other.relations.items():
        if key not in base.relations:
            base.relations[key] = dict(r)
            st.relations_added_from_other += 1
    return st


def graph_to_jsonl(g: Graph) -> str:
    """Serialize a Graph back to JSONL.  Entities first (alpha by name), then
    relations (alpha by from/to/type), then any preserved extras."""
    lines: list[str] = []
    for name in sorted(g.entities):
        lines.append(json.dumps(g.entities[name], ensure_ascii=False))
    for key in sorted(g.relations):
        lines.append(json.dumps(g.relations[key], ensure_ascii=False))
    for x in g.extras:
        lines.append(x)
    return "\n".join(lines) + ("\n" if lines else "")


# ============================================================================
# Backup + write
# ============================================================================

def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(path.name + f".bak.{ts}")
    bak.write_bytes(path.read_bytes())
    return bak


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


# ============================================================================
# Summary printing
# ============================================================================

def _print_side(side: Side, g: Graph, label: str) -> None:
    print(f"  {label} ({side.name}): {side.file}")
    print(f"    declared in config as: {side.declared_path}")
    print(f"    entities: {len(g.entities)}   "
          f"relations: {len(g.relations)}   "
          f"extras: {len(g.extras)}")


# ============================================================================
# Main
# ============================================================================

def run(
    direction: str = "bidirectional",
    apply: bool = False,
    windows_file: str | None = None,
    wsl_file: str | None = None,
) -> int:
    win, wsl, notes = discover_sides(windows_file=windows_file, wsl_file=wsl_file)
    if notes:
        for n in notes:
            print(n)
    if not win or not wsl:
        missing = []
        if not win: missing.append("Windows")
        if not wsl: missing.append("WSL")
        print()
        print(f"REFUSED: no MEMORY_FILE_PATH for: {', '.join(missing)}.")
        print("Set MEMORY_FILE_PATH in the affected side's Windsurf MCP config")
        print("(see README \"Memory MCP needs an explicit MEMORY_FILE_PATH\").")
        print("Or pass --windows-file / --wsl-file to override.")
        return 3

    print()
    print("Memory MCP graph sync")
    print(f"  direction: {direction}")
    print(f"  mode:      {'APPLY (will write)' if apply else 'DRY-RUN (no writes)'}")
    print()

    g_win = parse_graph(win.file)
    g_wsl = parse_graph(wsl.file)
    _print_side(win, g_win, "Windows side")
    _print_side(wsl, g_wsl, "WSL side    ")
    print()

    # Compute planned writes per direction.  We always work on copies so the
    # in-memory diff against original is unambiguous.
    if direction == "bidirectional":
        merged = Graph()
        # Seed merged with Windows side first so its entityType "wins" on
        # conflict; observations are preserved-then-extended either way.
        merged.entities = {k: {**v, "observations": list(v.get("observations") or [])}
                           for k, v in g_win.entities.items()}
        merged.relations = dict(g_win.relations)
        merged.extras = list(g_win.extras)
        st_from_wsl = union_into(merged, g_wsl)
        # Also gather what WSL would gain by overlaying merged onto it.
        # Easiest way: compare merged vs g_wsl in the same direction.
        gains_wsl = MergeStats()
        for name in merged.entities:
            if name not in g_wsl.entities:
                gains_wsl.entities_added_from_other += 1
            else:
                if len(merged.entities[name].get("observations") or []) > \
                   len(g_wsl.entities[name].get("observations") or []):
                    gains_wsl.entities_observations_grew += 1
        for key in merged.relations:
            if key not in g_wsl.relations:
                gains_wsl.relations_added_from_other += 1
        gains_win = st_from_wsl
        plan_for_win = merged
        plan_for_wsl = merged
    elif direction == "wsl-to-windows":
        plan_for_win = g_wsl
        plan_for_wsl = None  # WSL untouched
        gains_win = MergeStats(
            entities_added_from_other=len([k for k in g_wsl.entities if k not in g_win.entities]),
            relations_added_from_other=len([k for k in g_wsl.relations if k not in g_win.relations]),
        )
        gains_wsl = MergeStats()
    elif direction == "windows-to-wsl":
        plan_for_wsl = g_win
        plan_for_win = None
        gains_wsl = MergeStats(
            entities_added_from_other=len([k for k in g_win.entities if k not in g_wsl.entities]),
            relations_added_from_other=len([k for k in g_win.relations if k not in g_wsl.relations]),
        )
        gains_win = MergeStats()
    else:
        print(f"ERROR: unknown direction: {direction}", file=sys.stderr)
        return 2

    print("Plan:")
    if plan_for_win is not None:
        print(f"  Windows will gain  +{gains_win.entities_added_from_other} entities, "
              f"+{gains_win.relations_added_from_other} relations, "
              f"{gains_win.entities_observations_grew} entities with new observations")
    else:
        print("  Windows: unchanged")
    if plan_for_wsl is not None:
        print(f"  WSL     will gain  +{gains_wsl.entities_added_from_other} entities, "
              f"+{gains_wsl.relations_added_from_other} relations, "
              f"{gains_wsl.entities_observations_grew} entities with new observations")
    else:
        print("  WSL: unchanged")

    if direction == "bidirectional" and st_from_wsl.entity_type_conflicts:
        print()
        print("WARN: entity-type conflicts (Windows side's entityType kept):")
        for name, lt, rt in st_from_wsl.entity_type_conflicts:
            print(f"  - {name}: windows={lt!r} wsl={rt!r}")

    no_changes = True
    if plan_for_win is not None and (gains_win.entities_added_from_other
                                     or gains_win.relations_added_from_other
                                     or gains_win.entities_observations_grew):
        no_changes = False
    if plan_for_wsl is not None and (gains_wsl.entities_added_from_other
                                     or gains_wsl.relations_added_from_other
                                     or gains_wsl.entities_observations_grew):
        no_changes = False
    if no_changes:
        print()
        print("No changes needed -- graphs already in sync.")
        return 0

    if not apply:
        print()
        print("(dry-run -- pass --apply to write)")
        return 1

    # Apply
    print()
    print("Applying...")
    if plan_for_win is not None:
        text = graph_to_jsonl(plan_for_win)
        bak = _backup(win.file)
        if bak:
            print(f"  backed up Windows file -> {bak}")
        _write_atomic(win.file, text)
        print(f"  wrote {win.file} ({len(plan_for_win.entities)} entities, "
              f"{len(plan_for_win.relations)} relations)")
    if plan_for_wsl is not None:
        text = graph_to_jsonl(plan_for_wsl)
        bak = _backup(wsl.file)
        if bak:
            print(f"  backed up WSL file     -> {bak}")
        _write_atomic(wsl.file, text)
        print(f"  wrote {wsl.file} ({len(plan_for_wsl.entities)} entities, "
              f"{len(plan_for_wsl.relations)} relations)")
    print("Done.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Mirror the Memory MCP knowledge-graph file across Windows + WSL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--direction",
                   choices=["bidirectional", "wsl-to-windows", "windows-to-wsl"],
                   default="bidirectional",
                   help="Merge direction (default: bidirectional union merge).")
    p.add_argument("--apply", action="store_true",
                   help="Actually write changes (default is dry-run).")
    p.add_argument("--windows-file", default=None,
                   help="Override the Windows-side memory file path (skips config discovery).")
    p.add_argument("--wsl-file", default=None,
                   help="Override the WSL-side memory file path (skips config discovery).")
    a = p.parse_args(argv)
    return run(direction=a.direction, apply=a.apply,
               windows_file=a.windows_file, wsl_file=a.wsl_file)


if __name__ == "__main__":
    sys.exit(main())
