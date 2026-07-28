# 039 — get_release_calendar: convention-driven release visibility

## Context

"What ships when" was answerable only by hand: query open release tickets,
query recently resolved ones, filter out tickets that merely *mention*
releases, infer cadence per team, notice parked store queues. Doing it
manually surfaced real signal (weekly cadences, a store-submission jam,
one team being the only one that sets deadlines on releases) — worth one
call instead of a research session.

Two live-validated parser facts feed the design:
- Release tickets are named by convention — `Release X.Y.Z`,
  `release-X.Y.Z`, `Release_lite`, `[Tag] Release …`, `RC-X.Y.Z` — while
  false positives ("Validate … before release", CI-failure noise) share the
  keyword but not the shape. A summary-shape regex separates them.
- A `resolved date:` range clause must be the FIRST clause in the query —
  the reversed order is rejected by the parser (same family as ADR-035's
  `resolved:` alias breakage). A test pins the clause order.

## Decision

New module `releases.py` (module #21, tool #83): `get_release_calendar
(projects, lookback_days)`:

- Four validated queries (open/shipped × Release/RC), deduped, shape-filtered.
- Per project: in-flight releases ordered closest-to-the-door first
  (store review / prod verification / ready → testing → in progress →
  queue), each with state, assignee, deadline when present; recent ships
  with dates.
- **Cadence** = median gap between ships in the lookback; when a project's
  open releases carry no deadlines, a cadence ETA is printed for the next
  ship (explicitly labeled "no deadlines set" — inference, not commitment).
- **Flags**: 3+ releases parked in ready/review states → submission
  bottleneck or dead tickets.
- Empty result explains what was searched and the naming convention it
  expects (anti-silent-zero).

## Consequences

- Release visibility across every team in one call; deadline-free teams
  still get an ETA, clearly marked as inferred.
- 843 tests pass (9 new). Minor release 1.21.0.
