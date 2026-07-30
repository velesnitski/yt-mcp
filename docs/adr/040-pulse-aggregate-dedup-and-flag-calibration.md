# 040 — Multi-board aggregate dedup + underload flag calibration

## Context

Two pulse defects surfaced by consecutive live org runs:

1. **Board views multiplied org totals.** `get_multi_team_pulse` summed
   metrics across boards, but closed/incoming are PROJECT-scoped queries —
   five boards that are views over one project reported the same numbers
   five times, and the org header showed throughput ×5. Observed twice on
   a five-board single-project instance; both times the correction had to
   be made by hand in the analysis.
2. **The 💤 underload flag fired on every board of every instance**
   (13/13 across two orgs). It compared instantaneous WIP against the
   30-day closed TOTAL (`in_flight < closed/3`), a threshold almost any
   healthy team trips. A flag that is always on carries no signal —
   classic alert fatigue.

## Decision

- Payloads carry their board's **project set**; `_aggregate_payloads`
  groups boards by identical project set. Within a group, metrics and
  pipeline counts take the per-key MAX (boards differ only in column
  mapping — the fullest view wins) and flags come from one representative;
  distinct groups sum as before. Boards without project info fall back to
  per-board groups (legacy behavior). The render notes how many duplicate
  views were merged — dedup is visible, never silent. Partial overlaps
  (a cross-team board sharing SOME projects with team boards) are out of
  scope: only identical sets dedupe.
- Underload now compares WIP to **weekly velocity**: flag when
  `in_flight < (closed / (lookback/7)) / 2` — less than half a week's
  throughput in flight. On live data this keeps the flag for a genuinely
  thin pipeline (8 in flight at ~35/wk) and silences the 12 false alarms.

## Consequences

- Org headers are truthful for view-heavy instances; the merged-views note
  explains any board-count/metric mismatch.
- The underload flag becomes rare enough to read.
- 848 tests pass (5 new: ×5-view regression, distinct-set summing, legacy
  fallback, healthy-WIP silence, thin-pipeline fire). Patch release 1.21.1.
