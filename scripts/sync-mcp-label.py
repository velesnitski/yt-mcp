#!/usr/bin/env python3
"""Sync the yt-mcp entry's key in ~/.claude.json to "youtrack v<version>".

Why this exists: Claude Code's /mcp dialog labels each server by its
*config key* in ~/.claude.json, NOT by the serverInfo.name the server
reports during initialize. The server already names itself "youtrack
v<version>" (ADR 008-era server-name change), but that only shows in the
instructions header, never in the /mcp dialog. The one lever for the
dialog label is the config key — and a hand-typed version goes stale the
moment pyproject is bumped.

This keeps the key truthful automatically. It finds the yt-mcp entry by a
path fragment in its command/args (not by the key, which may already carry
a version), asks that exact wired invocation its version
(`<command> <args...> --version`), and renames the key to
"youtrack v<version>". If the subprocess can't answer (no uvx on PATH,
offline, etc.) it falls back to the version in the wired --directory's
pyproject.toml when present. Idempotent, atomic write, keeps a .bak.

Asking the *wired* invocation (cache and all) is deliberate: it reports
the version that actually RUNS, which is what the label should reflect —
not a just-pushed source that the cached uvx build hasn't picked up yet.

Run it after a release bump (`python3 scripts/sync-mcp-label.py`), then
reconnect `/mcp`. Stdlib only — no venv required. Ported from the fleet
pattern (zbbx-mcp ADR 061/062).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

CLAUDE = os.path.expanduser("~/.claude.json")
BINARY_MATCH = "yt-mcp"  # path fragment identifying the youtrack server entry
DISPLAY = "youtrack"  # friendly name; matches the server's serverInfo.name
_SEMVER = re.compile(r"\d+\.\d+\.\d+[^\s]*")


def mcp_containers(cfg):
    """Every mcpServers dict in the config: the root one plus per-project ones."""
    out = []
    if isinstance(cfg.get("mcpServers"), dict):
        out.append(cfg["mcpServers"])
    for proj in (cfg.get("projects") or {}).values():
        if isinstance(proj, dict) and isinstance(proj.get("mcpServers"), dict):
            out.append(proj["mcpServers"])
    return out


def entry_matches(entry):
    """True if this config entry invokes yt-mcp (fragment in command or args)."""
    if not isinstance(entry, dict):
        return False
    blob = entry.get("command", "") + " " + " ".join(entry.get("args", []) or [])
    return BINARY_MATCH in blob


def parse_semver(text):
    """Extract the first semver-looking token from text, or '' if none."""
    m = _SEMVER.search(text or "")
    return m.group(0) if m else ""


def version_from_pyproject(project_dir):
    """Read the [project] version from project_dir/pyproject.toml (regex, no tomllib)."""
    path = os.path.join(project_dir or "", "pyproject.toml")
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return ""
    # First `version = "..."` line — pyproject puts it under [project] near the top.
    m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else ""


def _wired_directory(args):
    """The path passed to `uv run --directory <dir>`, or '' if absent."""
    args = args or []
    if "--directory" in args:
        i = args.index("--directory")
        if i + 1 < len(args):
            return args[i + 1]
    return ""


def query_version(command, args):
    """Ask the wired invocation its version; fall back to its pyproject.toml.

    `<command> <args...> --version` prints the bare version and exits
    (argparse action="version"). The server's own startup logs go to
    stderr, so stdout is the version; we still semver-match to be
    defensive. The uvx-from-git invocation has no --directory, so the
    pyproject fallback only helps `uv run --directory <dir>` style
    registrations.
    """
    try:
        r = subprocess.run(
            [command, *(args or []), "--version"],
            capture_output=True, text=True, timeout=120,
        )
        ver = parse_semver(r.stdout)
        if ver:
            return ver
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  ! could not run '{command} --version': {e}")
    return version_from_pyproject(_wired_directory(args))


def rename_in(container, get_version=query_version):
    """Rename matching entries' keys to 'youtrack v<version>'. Returns True if changed."""
    changed = False
    for key in list(container.keys()):
        entry = container[key]
        if not entry_matches(entry):
            continue
        version = get_version(entry.get("command", ""), entry.get("args", []))
        if not version:
            print(f"  ! no version for '{key}' — skipped")
            continue
        new_key = f"{DISPLAY} v{version}"
        if key == new_key:
            print(f"  = already '{new_key}'")
            continue
        # Preserve insertion order: rebuild the dict with just this key renamed.
        rebuilt = {(new_key if k == key else k): v for k, v in container.items()}
        container.clear()
        container.update(rebuilt)
        print(f"  ✓ '{key}' → '{new_key}'")
        changed = True
    return changed


def sync_config(cfg, get_version=query_version):
    """Rename matching entries across ALL containers. Returns True if any changed.

    The list comprehension is load-bearing: `any(rename_in(c) for c in ...)`
    over a generator short-circuits after the first container that changes,
    leaving the youtrack entry stale in every other container (the config
    carries one per project). Build the full list first, then reduce.
    """
    return any([rename_in(c, get_version) for c in mcp_containers(cfg)])


def main():
    if not os.path.exists(CLAUDE):
        print(f"no config at {CLAUDE} — nothing to do")
        return 0
    with open(CLAUDE) as f:
        cfg = json.load(f)

    if not sync_config(cfg):
        print("nothing to update (yt-mcp entry not found or label already current)")
        return 0

    shutil.copy2(CLAUDE, CLAUDE + ".bak")
    # Atomic replace so a crash never leaves ~/.claude.json half-written.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CLAUDE), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CLAUDE)
    print(f"updated {CLAUDE} (backup: {CLAUDE}.bak)")
    print("→ run '/mcp' reconnect (or restart Claude Code) to see the new label")
    return 0


if __name__ == "__main__":
    sys.exit(main())
