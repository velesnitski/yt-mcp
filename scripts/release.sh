#!/usr/bin/env bash
# Release ceremony, collapsed (ADR-023). Two phases so the commit message —
# the part that needs a human/agent brain — stays manual, and the push still
# happens only after explicit confirmation:
#
#   scripts/release.sh prepare <version>   # bump + sync + tests + sweep
#   ... write ADR, git add, git commit ...
#   scripts/release.sh ship                # push dev, ff-merge main, push,
#                                          # verify remotes, uvx refresh,
#                                          # /mcp relabel
set -euo pipefail
cd "$(dirname "$0")/.."

phase="${1:-}"

case "$phase" in
prepare)
    ver="${2:?usage: release.sh prepare <version>}"
    [[ "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "bad version: $ver" >&2; exit 1; }
    cur="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/yt_mcp/__init__.py)"
    echo "bump: $cur -> $ver"
    sed -i '' "s/^version = \"$cur\"/version = \"$ver\"/" pyproject.toml
    sed -i '' "s/^__version__ = \"$cur\"/__version__ = \"$ver\"/" src/yt_mcp/__init__.py
    grep -q "\"$ver\"" pyproject.toml && grep -q "\"$ver\"" src/yt_mcp/__init__.py \
        || { echo "bump failed" >&2; exit 1; }
    uv sync --extra test --quiet
    uv run python -m pytest tests/ -q | tail -1
    ./scripts/sweep.sh
    echo "prepare OK — now: git add … && git commit, then release.sh ship"
    ;;
ship)
    branch="$(git rev-parse --abbrev-ref HEAD)"
    [[ "$branch" == "dev" ]] || { echo "ship must run from dev (on: $branch)" >&2; exit 1; }
    [[ -z "$(git status --porcelain)" ]] || { echo "working tree not clean" >&2; exit 1; }
    ver="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/yt_mcp/__init__.py)"
    tag="v$ver"
    git push origin dev
    git checkout main
    git merge --ff-only dev
    git push origin main
    git checkout dev
    git fetch origin -q
    d="$(git rev-parse origin/dev)"; m="$(git rev-parse origin/main)"; l="$(git rev-parse dev)"
    [[ "$d" == "$m" && "$m" == "$l" ]] || { echo "remote mismatch: dev=$d main=$m local=$l" >&2; exit 1; }
    echo "remotes in sync @ ${l:0:7}"
    # Tag the release and PIN the config to it (ADR-025): an unpinned
    # git+URL leaves the spawned version to uv's cached ref resolution —
    # observed two releases behind what the label claimed.
    git tag -f "$tag" && git push -f origin "$tag"
    "$HOME/.local/bin/uvx" --from "git+https://github.com/velesnitski/yt-mcp@$tag" yt-mcp --version | tail -1
    python3 scripts/sync-mcp-label.py --pin "$tag"
    # Kill stale yt-mcp server processes (known cross-session leak) so the
    # next /mcp reconnect MUST spawn the pinned build instead of reattaching
    # to a weeks-old process. Patterns live in this file, not the pkill
    # cmdline, so the script can't match itself.
    killed=0
    pkill -f "archive-v0/.*/bin/yt-mcp" 2>/dev/null && killed=1
    pkill -f "uvx --from git\+https://github.com/velesnitski/yt-mcp" 2>/dev/null && killed=1
    [[ "$killed" == 1 ]] && echo "stale yt-mcp server processes killed"
    echo "ship OK ($tag pinned) — reconnect /mcp to load the new build"
    ;;
*)
    echo "usage: release.sh prepare <version> | ship" >&2
    exit 1
    ;;
esac
