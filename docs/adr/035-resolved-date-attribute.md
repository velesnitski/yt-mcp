# 035 — Query `resolved date:`, not the `resolved:` alias

## Context

Team pulse broke overnight on every board of both instances — every query
400ing with "Can't parse search query". Live bisection isolated a single
construct: `resolved: <ISO> .. <ISO>`. A 2026-07 YouTrack Cloud upgrade
changed the parser (both instances upgraded together, hence the simultaneous
fleet-wide failure with zero code changes on our side):

- `resolved: A .. B` (spaced range) → 400
- `resolved: A..B` (unspaced) → parses but **silently matches nothing** —
  the trap fix, worse than the error
- `resolved date: A .. B` → correct (canonical attribute)
- `created:` / `updated:` spaced ranges, `State: {…}` braces, `has:` → all
  still fine

## Decision

Use `resolved date:` at both pulse query sites (closed-count,
released-count). A source-pinning test forbids reintroducing the bare
alias in any form, spaced or not.

## Consequences

- Pulse works again on post-upgrade YouTrack Cloud; unchanged behavior on
  the data itself.
- 814 tests pass. Patch release 1.19.3.
- Reminder this class exists: cloud YT upgrades can invalidate
  previously-valid query syntax with no notice — when a formerly-stable
  tool 400s across ALL boards/instances at once, bisect the query live
  before suspecting the code.
