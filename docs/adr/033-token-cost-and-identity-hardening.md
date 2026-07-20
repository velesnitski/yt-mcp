# 033 — Work-item token cost, identity keying, and report ergonomics

## Context

Live validation of the v1.19.0 time-report tools (ADR-032) surfaced a batch
of issues — none blocking, three consequential:

1. **`get_work_items` was a token bomb.** Work-item `text` carries
   multi-paragraph work journals; one documented issue cost ~4K tokens in a
   single call. Any agent flow touching a few such issues burns 10–20K
   context tokens on prose it rarely needs — against the ADR-026 priority.
2. **Time-report aggregation was keyed by display name.** Two users sharing
   a display name silently merge into one row; a mid-month rename splits one
   person into two. In old months, all deleted users collapsed into a single
   "Deleted User" row. Silent data corruption in a report that reads as
   payroll-adjacent.
3. **`serverInfo.version` reported the mcp SDK's version** (e.g. `1.28.1`),
   because FastMCP exposes no version kwarg and the low-level
   `Server.version` defaults to the SDK package. Any client reading
   `version` instead of parsing the `youtrack vX.Y.Z` name got the wrong
   answer.

Plus ergonomics: "1 issues, 1 entries" grammar in every report; unknown-user
404s that dead-ended instead of pointing at `search_users`; an unbounded
`user_time_summary` default that scanned a user's whole history; no
per-project view; CHANGELOG.md frozen at 1.7.0 with no pointer to where
notes actually live.

## Decision

- **`get_work_items`**: truncate `text` to 200 chars by default with an
  explicit `… (+N chars, include_text=True for full)` marker; add
  `include_text: bool = False` and optional `since`/`until` (`YYYY-MM-DD`)
  filters. The per-issue endpoint has no date params, so filtering is
  client-side on the epoch-ms work date, end bound inclusive.
- **`time_report` aggregation keys by `author.login`**, displaying
  `author.name`. Collisions, renames, and deleted-user merging all resolve;
  a regression test pins two same-name/different-login users to two rows.
- **`server.py` sets the low-level `Server.version` to `__version__`** via
  the same guarded reach-in pattern as `tools._registered_tools` (no-op if
  the SDK layout shifts). The stdio integration test now asserts
  `serverInfo.version == __version__` end-to-end.
- **Ergonomics**: `_plural()` fixes report grammar; unknown-user errors
  append "use search_users to find it by name"; `user_time_summary` with no
  bounds defaults to the current UTC month (all-time stays available via an
  explicit early `since`); `monthly_time_report_by_user` gains
  `group_by="project"` (issue-prefix grouping with per-project user counts);
  CHANGELOG.md points to GitHub Releases + ADRs for post-1.7.0 notes.

## Consequences

- `get_work_items` output shrinks ~10–20x on journal-heavy issues while
  staying complete on demand; date filters make it usable for period
  reconciliation against the time-report tools.
- Tool schemas grow slightly (new optional params); tool count unchanged
  at 81.
- 811 tests pass (14 new: truncation, date filters, identity keying,
  grammar, group_by, month default, 404 hint, serverInfo.version).
- Patch release 1.19.1.
