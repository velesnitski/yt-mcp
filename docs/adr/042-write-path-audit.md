# 042 — Write-path audit: create/transition/delete correctness

## Context

A focused audit of the write tools (issue creation, status change, delete,
bulk) found seven defects. The two serious ones sat exactly on the
most-used paths:

1. **Direct create returned "Created: ?" and misdirected follow-ups.**
   `POST /api/issues` was sent without a `fields` selector; YouTrack's
   default POST serialization is ONLY `{$type, id}` (registry Q16), so
   `idReadable` was absent, the tool reported `Created: **?**`, and the
   product/command follow-ups targeted `{"idReadable": "?"}`. The
   draft-publish response had been fixed for exactly this long ago
   (`fields=idReadable,summary`); the direct path was missed. It survived
   because (a) most projects here have required fields, routing real
   creations through the fixed draft path, and (b) the test mocks returned
   `idReadable` for the fieldless POST — a richer response than real YT
   ever sends.
2. **`transition_issue` ignored issue-URL instance routing** — the only
   CRUD tool calling `resolver.resolve(instance)` without the issue
   identifier, silently targeting the default instance for a
   pasted second-instance URL.

Medium: (3) a failed draft publish left the draft orphaned; (4) soft
delete hardcoded `State Obsolete`, 400ing on projects whose state field is
named `Status` (a case `transition_issue` already handles) or that lack an
`Obsolete` value; (5) braces could still reach `/api/commands` on
non-create paths (update's explicit params and joined command, bulk),
violating the ADR-019 invariant. Minor: (6) description changes were
invisible in update's diff and rollback hints; (7) tag-order differences
reported as false "Tags changed".

## Decision

- Direct create requests `?fields=idReadable,summary` (Q16 added to the
  shared quirk registry).
- `transition_issue` resolves with the raw issue id, like every other
  CRUD tool.
- Publish failure best-effort-deletes the draft before returning.
- Soft delete detects the State/Status field name from the already-fetched
  custom fields and returns a clean actionable message (suggesting
  `transition_issue` or `permanent=True`) instead of a raw 400.
- The ADR-019 invariant is enforced at the choke point:
  `client.execute_command` strips braces, covering update/bulk/delete in
  one place; update's per-part fallback strips too.
- Update shows `Description: updated (N → M chars)`, hints
  `rollback_issue` for restore, and compares tags as sets.

## Consequences

- Ten regression tests in `tests/test_write_paths.py`, built on an HONEST
  POST mock (`fields=` → rich response, otherwise `{$type, id}` — the real
  default), so the Q16 class cannot re-hide behind friendly mocks; three
  existing create mocks updated to tolerate the query string.
- 858 tests pass. Patch release 1.21.3.
