# 034 — Linked-issue state: read the custom field, not a fictional top-level field

## Context

Auditing a management-project board (parent tickets whose real work lives in
linked subtasks) exposed that `get_issue`/`get_issues` with `format="json"`
returned `"state": ""` for **every** link, always. The audit consequently
trusted parent states and got the picture wrong — subtasks that were Closed /
On testing / Ready for release all rendered as blank.

Root cause: YouTrack issues have **no top-level `state` field** — State is a
custom field. The link selector requests `state(name)` on linked issues
(silently ignored by YT) *and* `customFields(name,value(name))` (returned),
but only the markdown/report renderer fell back to the custom field;
`normalize_issue`'s link loop and the compact path read solely the
nonexistent top-level field. Same bug class as PR #3: code written against
an imagined response shape, tests mocking that same imagined shape
(`test_links_flattened` used `"state": {"name": ...}` — a shape real YT
never produces for links), so everything passed while production returned
empty strings.

## Decision

- New `_linked_state(linked)` helper: top-level `state` first (forward
  compatibility, keeps the old mocked shape working), then
  `_get_custom_field(linked, "State")` — used by **all three** link-render
  sites (normalize, compact, report), replacing two broken inline copies and
  one working one.
- Normalized links gain `"assignee"` (from the linked issue's Assignee
  custom field, multi-user comma-joined) — the management-project drill-down
  ("who actually owns the real task?") now works in one call instead of a
  second batch fetch. The selectors already fetched the data; it was simply
  dropped.
- Regression tests use the **real** YT link shape (no top-level state,
  State/Assignee in customFields) alongside the legacy shape.

## Consequences

- JSON link states now populate; report/compact paths unchanged in behavior
  but share one helper.
- `links` dicts gain a key (`assignee`) — additive, no consumer breakage.
- 813 tests pass. Patch release 1.19.2.
