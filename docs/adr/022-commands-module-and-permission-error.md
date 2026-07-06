# 022 — Architecture: `commands.py` extraction + client-level permission error

## Context — architecture review findings

A design review after the ADR-016→021 arc found the two structural causes
behind this month's bug class, both measurable:

1. **Command-application logic had no home.** `issues.py` alone had 10
   `/api/commands` post sites; the hard-won invariants — bare values
   (ADR-019), field-aware split (ADR-021), URL-free error text — lived in
   closures inside `create_issue`, invisible to `update_issue`,
   `transition_issue`, and any future tool. The ADR-016 regression happened
   exactly because this logic had no single owner with its own tests. The
   query-vs-command grammar distinction (braces required in queries,
   rejected in commands) was documented only in comments.

2. **The 401/403 asymmetry was fleet-wide.** `client._handle_error` produced
   clean `ValueError`s only for 400/404; everything else escaped as
   `httpx.HTTPStatusError`, whose `str()` embeds the request URL. ADR-017
   fixed `issues.py` piecemeal (bulk/translate had opted in earlier), but
   ~12 other tool modules catch only `ValueError` — in all of them a
   permission failure still surfaced as a raw, host-leaking error. ADR-017
   had rejected a client-level fix as "changes global error semantics"; that
   objection dissolves with subclassing.

## Decision

### `yt_mcp/commands.py` — single home for command invariants

Moved from `tools/issues.py` closures/helpers, unchanged in behavior:
`CMD_FIELD_RE`, `CMD_KEYWORDS`, `strip_braces`, `cmd_error_text`,
`get_project_field_names`, `split_command` (field-aware), `regex_split`,
plus two new composites:

- `split_field_clauses(command, names)` — field-aware split with the regex
  fallback baked in.
- `apply_field_commands(client, issue_ref, command, get_field_names)` — the
  whole→split→rejoin strategy as ONE function returning human-readable
  failures instead of raising. `make_field_names_getter` memoizes the field
  list per flow.

The module docstring states the query-vs-command grammar rule explicitly —
the distinction that caused ADR-016 is now architecture, not tribal
knowledge. `tools/issues.py` re-exports the old underscore names so existing
imports/tests keep working.

**Consumers rewired:** `create_issue` (`_apply_commands` is now 15 lines),
`transition_issue` (uses `split_field_clauses`), and — new robustness —
`update_issue`: when its joined multi-field command fails to parse (a real
failure mode with multi-word values), it now retries each part individually
and reports per-part failures as "Could not apply: …" instead of aborting
the whole update with a raw error. `bulk.py`/`translate.py` keep their own
one-command→many-issues shape (different problem; no forced reuse).

### `yt_mcp/errors.py` — `YouTrackPermissionError(ValueError)`

`client._handle_error` now maps **401/403 → YouTrackPermissionError**, a
`ValueError` subclass carrying `status_code` and clean, URL-free text.
Because it subclasses `ValueError`, every existing catch site — plain
`except ValueError` and `except (httpx.HTTPStatusError, ValueError)` alike —
handles it with zero changes, which is what makes the global fix safe where
ADR-017 judged it unsafe. Effects:

- All 79 tools now degrade cleanly on permission failures; no tool can leak
  the instance URL through a permission error.
- 5xx and transport errors still raise `httpx` exceptions (retryable server
  trouble stays loud).
- `create_issue` catches the subclass explicitly for its friendly
  "ask an admin" message; the `httpx.HTTPStatusError` branch remains as
  defense-in-depth for transports that bypass `_handle_error`.

## What was reviewed and deliberately NOT changed

- **19 `register(mcp, resolver)` closures** — FastMCP's idiom; converting to
  classes adds indirection without testability gains (pure logic is
  extracted to module level instead, as here).
- **`monitoring.py`/`pulse.py` size (1.2k/1k lines)** — long but linear
  report builders with dedicated test files; splitting would shuffle, not
  simplify.
- **`formatters.py` grab-bag** — cohesive enough at 431 lines.
- **Per-call field-list fetch** (no global cache) — projects change rarely
  but the server is long-lived; a stale-cache bug costs more than one GET
  per failing create.

## Consequences

- The two recurring bug classes of the past week each now have exactly one
  owner: command grammar in `commands.py`, permission mapping in
  `client.py`+`errors.py`.
- Tests 739 → 746: `test_errors.py` (401/403 mapping, 5xx passthrough,
  400 unchanged, fleet-wide no-URL-leak via a plain read tool) and
  `update_issue` fallback coverage (joined failure → per-part retry; one bad
  part reported, not raised). One integration assertion updated to the new
  canonical permission text. Brace-guard and mutation-guard tests all still
  green; live smoke on an ephemeral draft reproduced pre-refactor behavior
  exactly.
- Patch bump 1.17.0 → 1.17.1 (no tool-surface change).
