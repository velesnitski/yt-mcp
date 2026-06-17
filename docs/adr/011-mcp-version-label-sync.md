# 011 — `/mcp` version label sync (`--version` + sync-mcp-label.py)

## Context

Claude Code's `/mcp` dialog labels each connected server by its **config
key** in `~/.claude.json`, *not* by the `serverInfo.name` the server
reports during `initialize`. yt-mcp already self-reports
`youtrack v<version>` as its server name (the `_SERVER_NAME` change), but
that string only surfaces in the instructions header — the dialog keeps
showing whatever the config key says, which is a hand-typed `"youtrack"`
that never reflects the running version.

Result: after every release the operator had no in-`/mcp` signal of which
build was actually connected, and `serverInfo.name` was a dead end for the
dialog.

Other servers in the fleet (zbbx-mcp ADR 061/062, slk-mcp ADR 024) already
solved this with a small release-time script that re-keys the config entry.
This ports that proven pattern to yt-mcp rather than inventing a new one.

## Decision

Two pieces.

### 1. `--version` CLI flag

`yt-mcp --version` prints the bare version and exits (argparse
`action="version"`, sourced from `__version__`). The server's startup logs
go to stderr, so stdout is just the version — clean for a script to read.

This is the lever the sync script pulls: it asks the *exact wired
invocation* its version rather than guessing from a path. Asking the wired
invocation (uvx cache and all) is deliberate — it reports the version that
actually **runs**, which is what the label should reflect, not a
just-pushed source the cached build hasn't picked up yet.

### 2. `scripts/sync-mcp-label.py`

Stdlib-only, no venv. It:

- Finds yt-mcp entries by a **path fragment** (`yt-mcp`) in
  `command`/`args` — not by the key, which may already carry a stale
  version (`youtrack v1.14.1`).
- Scans **all** containers: the root `mcpServers` plus every
  per-project one (the config carries one entry per project). The
  `any([...])`-over-a-list (not a generator) is load-bearing — a
  short-circuit would leave the entry stale in every container after the
  first (the bug zbbx fixed in its ADR 062).
- Asks `<command> <args...> --version`; falls back to the wired
  `--directory`'s `pyproject.toml` when present (helps `uv run
  --directory` style registrations; the uvx-from-git registration has no
  `--directory`, so the `--version` path is primary there).
- Re-keys to `youtrack v<version>`, preserving insertion order. Atomic
  write via a temp file + `os.replace`, keeps a `.bak`. Idempotent.

Run after a release bump: `python3 scripts/sync-mcp-label.py`, then
reconnect `/mcp`.

## Alternatives considered

- **Hand-edit the config key per release.** Goes stale the moment
  pyproject is bumped; the whole point is to remove that manual step.
- **Make `/mcp` read `serverInfo.name`.** Not ours to change — it's a
  Claude Code behavior. The config key is the only lever we control.
- **Read the version from local `pyproject.toml` only.** Would mislabel
  when the running uvx build lags the just-pushed source. Asking the wired
  invocation is more truthful.
- **A bespoke yt-mcp implementation.** Rejected — porting the fleet
  script keeps every server's release flow identical ("portable to
  fleet"), so the muscle memory and the tests transfer.

## Consequences

- New `--version` flag (additive CLI surface).
- New `scripts/sync-mcp-label.py` (release tooling, not shipped in the tool
  surface — tool count unchanged at 77).
- Tests: 657 → 680 (+23 in `tests/test_sync_label.py`, version lookup
  dependency-injected so they never spawn a subprocess or touch
  `~/.claude.json`). Covers the all-containers fix and the
  find-by-fragment / re-key-stale-version behavior.
- CLAUDE.md documents the workflow.
- Minor bump 1.14.1 → 1.15.0 (new user-facing CLI flag + release tooling).
- The script is **not** run by CI or on commit — it mutates the operator's
  `~/.claude.json`, so it's an explicit post-release step the human runs.
