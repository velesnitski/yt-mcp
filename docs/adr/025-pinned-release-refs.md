# 025 — Tag releases, pin the MCP config to the tag, kill stale servers

## Context

After shipping v1.17.3, a `/mcp` reconnect still reported **v1.16.9** — two
releases behind the config label. Diagnosis against the live process table
and uv archives found two compounding failures:

1. **Unpinned git spec = nondeterministic spawns.** The config launched
   `uvx --from git+https://…/yt-mcp yt-mcp` with no ref. Without
   `--refresh`, uv resolves the URL from its cached ref resolution — so
   which version a spawn gets depends on the cache's mood at spawn time.
   Observed concretely: a 4:26 PM spawn ran the 1.17.2 archive *after*
   1.17.3 had shipped and the refreshed 1.17.3 env already existed on disk
   (three different yt-mcp versions were simultaneously live from three uv
   archives). The ship-time `uvx --refresh … --version` check verified a
   fresh build existed — not that reconnects would use it.
2. **Stale server processes survive reconnects.** A yt-mcp process from
   days earlier (v1.16.9) was still serving this session — the known
   cross-session MCP orphan leak. Even a perfectly resolved spawn can't fix
   a session that never respawns.

Net effect: `sync-mcp-label.py` renamed the config key each release, and
the *label* was taken as proof of deployment while reconnects kept running
old builds. The label was cosmetic; the loop was never closed.

## Decision

1. **Tag every release**: `release.sh ship` now creates and pushes
   `v<version>` (force-updated if re-shipping the same version).
2. **Pin the config to the tag**: `sync-mcp-label.py --pin <ref>` rewrites
   the entry's `--from git+…/yt-mcp[@old]` to `git+…/yt-mcp@<ref>` before
   the label sync, so (a) every spawn is deterministic and instant from
   cache, and (b) the version query that names the label runs the *pinned*
   build — label and runtime can no longer diverge. `pin_args()` is a pure
   function with unit tests (unpinned → pinned, re-pin, idempotence,
   pin-before-query ordering).
3. **Kill stale servers at ship**: `ship` pkills lingering yt-mcp server
   processes (patterns live in the script file, so the pkill can't match
   its own command line). The next `/mcp` reconnect must spawn fresh — from
   the pinned tag. This operationalizes the "periodic pkill" workaround for
   the known orphan leak at the moment it matters.

## Consequences

- What `/mcp` runs after a reconnect is exactly what was shipped — by
  construction, not by cache luck. The uvx warm-up step also becomes
  meaningful: it pre-builds the tag the config now points at.
- Rollback story improves for free: pinning the previous tag in the config
  is now a one-line change.
- Repo gains release tags (`v1.17.4` onward).
- Tests 756 → 761. Patch bump 1.17.3 → 1.17.4.
