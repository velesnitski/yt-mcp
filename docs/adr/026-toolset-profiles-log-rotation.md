# 026 — Toolset profiles + log rotation (memory/token audit)

## Context — a data-driven audit, and what it did NOT find

A lead-level review of memory usage, architecture, and token costs, measured
against our own telemetry (the analytics log records `response_size` per
call) and compared with the official YouTrack MCP server.

**Memory: not a problem.** The running server holds ~12 MB RSS — httpx
pools and 79 registered tools are cheap. The real "memory" incident class
was orphaned *processes* (fixed at ship time, ADR-025). Verdict: no code
change warranted; do not add caching/pooling machinery to a 12 MB process.

**Per-call token costs: one intrinsic, one structural.** Telemetry over 673
calls (~0.4M output tokens): `get_issues_for_translation` alone produced
57% of all output — but its size is *intrinsic* (you cannot translate text
you did not read); truncating it would break the feature, so it stays.
`get_issue` bloat from duplicate bot comments was already fixed (ADR-023).

**The structural cost is the tool schemas, not the responses.** 79 tool
schemas serialize to ~84K chars ≈ **21K context tokens injected into every
session** on clients without deferred tool loading (Cursor, n8n, JetBrains —
all advertised in the README). That is ~50× the average tool response.
Shrinking docstrings was rejected: they are the LLM's usage manual, and the
ADR-016 era showed what under-documented tools cost in mis-use.

**Unbounded growth: the logs.** `~/.yt-mcp/{yt-mcp,analytics}.log` used
plain `FileHandler` — no rotation, growing forever on a long-lived install.

**Official YouTrack MCP (2025.3+) for comparison:** 23 predefined tools,
issue/article/user CRUD + search, OAuth, server-embedded, permission-scoped;
its docs emphasize a deliberately small surface. Our differentiation is the
other 59 tools (analytics, pulse, deadlines, bulk, translation, gate-aware
transitions) and multi-instance support — but a user who only needs the
CRUD surface should not pay 21K tokens/session for the rest.

## Decision

1. **`YOUTRACK_TOOLSET=core|full` (default `full`).** `core` registers only
   `CORE_TOOLS` — a 20-tool everyday issue-CRUD surface deliberately sized
   like the official server's 23 (search/get/create/update/transition,
   comments, links, projects/fields, tags/saved searches, users/instance,
   KB read). Measured: **20 tools / ~4.7K tokens vs 79 / ~21K — ~4.5×
   cheaper per session.** Composes with the existing `read_only` and
   `DISABLED_TOOLS` filters in `register_all`. Tests pin: core == exactly
   `CORE_TOOLS` (equality also catches typos in the set), every core name
   exists in full, core+read_only strips writes, full still registers 79.
2. **Log rotation.** Both file handlers become `RotatingFileHandler`
   (2 MB × 2 backups) — bounded disk forever, no behavior change otherwise.

## Rejected

- Truncating `get_issues_for_translation` output (intrinsic workload).
- Trimming tool docstrings (degrades tool-use quality; profiles solve the
  session cost better).
- Any memory work (12 MB RSS — nothing to win).

## Consequences

- Token-sensitive registrations (Cursor, n8n, secondary instances) can opt
  into `core` and pay ~4.7K tokens/session; Claude Code with deferred
  loading is unaffected either way.
- Logs are bounded at ~6 MB total per install.
- Tests 761 → 769. Minor bump 1.17.4 → 1.18.0 (new env surface).
