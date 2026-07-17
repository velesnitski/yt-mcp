# 032 — Time reports built on the top-level workItems API

## Context

PR #3 (external contribution) proposed two genuinely useful tools —
`monthly_time_report_by_user` and `user_time_summary` — but the
implementation aggregated time from `/api/issues` custom fields and could not
work: the search query it built is rejected by YouTrack (400), the response
was consumed as `{"issue": [...]}` when the endpoint returns a bare list, and
"spent time" was read as `int(value.id)` where `value.id` is an entity id —
so totals were always 0. Its unit tests mocked the fictional shapes and
passed anyway. Full review with reproductions is on the PR.

The feature is worth having, so this ADR re-implements it on contracts
validated against a live instance **before** writing the code:

- **Top-level `GET /api/workItems`** exists and returns a bare JSON list of
  `IssueWorkItem`: `duration.minutes` (real integer minutes),
  `author(login,name)`, `issue(idReadable)`, epoch-ms `date`. One paged
  request chain covers any date range — no per-issue N+1.
- `startDate` / `endDate` params accept `YYYY-MM-DD` and filter by
  **work-item date**.
- `author=<login|id>` filters server-side and 404s on unknown users — which
  `client._handle_error` already maps to a clean `ValueError` message.
- Multi-project filters must use the comma-list idiom (`project: A, B`);
  OR-joined same-prefix clauses 400 (the same trap ADR-020 documents).

## Decision

New module `tools/time_report.py` (module #20, tools #80–81):

- `monthly_time_report_by_user(instance, projects, year, month)` — sums
  `duration.minutes` for the calendar month **grouped by work-item author**
  (whoever logged the time), not issue assignee: assignee attribution
  misreports any issue worked by several people, and logged time semantically
  belongs to its logger. Filtered by work-item date, not issue `updated` (an
  issue updated in July can carry June work). `year`/`month` default to the
  current UTC month (0 = current).
- `user_time_summary(user, instance, since, until, top_issues)` — one user's
  total plus a per-issue breakdown, same attribution and date semantics;
  `since`/`until` validated as `YYYY-MM-DD` up front.
- Shared `_fetch_work_items`: `$top`/`$skip` pagination (page 500) with a
  5000-item cap — and when the cap is hit the report **says so** instead of
  silently truncating.
- Duration formatting matches `get_work_items` in `history.py` (`2h 30m`).

Both tools are read-only (no `WRITE_TOOLS` change) and full-toolset only —
not added to `CORE_TOOLS`, consistent with ADR-026's analytics/reporting
split.

## Consequences

- 79 → **81 tools**, 19 → 20 modules; registration/count tests and CLAUDE.md
  updated. 797 tests pass.
- Test mocks mirror the **validated** endpoint shapes (bare list,
  `duration.minutes`), including pagination and cap-truncation paths — the
  contract-vs-mock gap that let PR #3's bugs through cannot recur here.
- Supersedes PR #3's implementation; the tool names and intent are kept so
  the original idea lands (credited in the PR).
- Also fixes the stale SDK-pin comment in `tools/__init__.py`
  (`mcp>=1.0,<1.26` → `>=1.28.1,<2.0`, stale since ADR-030).
