# 024 — Lazy server construction, boundary hygiene, size ratchet

## Context — architecture review, round 2 (runtime core)

ADR-022 reviewed the tools layer; this pass audited the runtime core
(`server`, `resolver`, `config`, `logging`) plus framework coupling, from a
pattern-realist stance: fix what demonstrably costs something, name what is
deliberately left alone.

### Findings

1. **`server.py` did all construction at import time.** Module import ran
   `setup_logging()`, `setup_sentry()`, `load_all_configs()`, built one
   httpx AsyncClient (HTTP/2 pool) per instance, and registered all 79
   tools — before `main()` even parsed arguments. Measurable symptom:
   `yt-mcp --version` took ~1.6 s, emitted a "Starting yt-mcp" JSON log
   line, and initialized Sentry, just to print a version string (this
   polluted every `sync-mcp-label`/health-check flow). Nothing imports the
   module-level `mcp` object — the eager globals bought nothing.
2. **Framework private-API reach-ins were scattered.** FastMCP has no public
   API to enumerate/mutate registered tools, so `register_all` touched
   `mcp._tool_manager._tools` in three places (wrap-with-logging, read-only
   removal, disabled removal). A pinned-version hack is acceptable; an
   *unlocalized* one is how SDK bumps break in three places at once.
3. **`resolver` poked `client._config.url`** while `client.base_url` — a
   public property added for exactly this — already existed.
4. **File-size question** ("prevent very long files, or leave if fine for
   AI"): the largest modules (`monitoring.py` ~1.2k, `pulse.py` ~1k lines)
   are linear report builders with cohesive functions and dedicated test
   files. For both humans and AI tooling, navigability is a function of
   function-level cohesion and greppable names, not file count — splitting
   working files retroactively churns history for no reader benefit.
   The real risk is *unbounded growth*.

## Decision

1. **`build_server() -> ServerBundle`.** All construction moved into a
   factory called from `main()` AFTER argparse (so `--version` exits before
   any work). The module docstring states the invariant: importing
   `yt_mcp.server` performs no side effects. The `_oauth_provider` module
   global became a `ServerBundle` field. Contract tests: `--version` prints
   only the version with empty-of-log stderr; importing the module leaves
   the `yt_mcp` logger handler-free and defines no `mcp` global;
   `build_server()` wires 79 tools in-process.
2. **`_registered_tools(mcp)`** — the ONE accessor allowed to touch
   FastMCP's private registry, with hasattr guards degrading to `{}`.
   `register_all` uses it for wrapping and removal.
3. **`resolver` uses `client.base_url`.** Test mocks updated to model the
   public property.
4. **Size ratchet, not a rewrite:** `test_hygiene.py` fails any module over
   1400 lines (current max 1223 + headroom), with instructions to split
   along tool-family lines (the `deadlines/` package precedent) rather than
   raise the cap. Long files are left as-is until growth forces the
   conversation — at which point the test forces it.

## Reviewed and deliberately unchanged

- **httpx clients never closed** — process-lifetime objects in a stdio
  server; an aclose ceremony adds shutdown paths for zero benefit.
- **`config._validate_url` printing to stderr** — runs before logging setup
  in some entry orders; stderr is the honest channel there.
- **WRITE_TOOLS as a hand-maintained set** — guarded by a completeness
  test; a decorator-based registry would be indirection without new safety.
- **`logged()` wrapping `tool.fn` post-registration** — the wrap must not
  disturb the schema FastMCP derives from the original signature;
  `functools.wraps` + post-hoc assignment is the simplest correct order.

## Consequences

- `yt-mcp --version`: ~1.6 s → ~0.4 s, silent, no Sentry init, no network
  pools created. Import of `yt_mcp.server` is side-effect-free and safely
  testable.
- SDK-coupling now breaks in one named function instead of three sites.
- Growth of oversized modules is mechanically gated.
- Tests 752 → 756. Patch bump 1.17.2 → 1.17.3.
