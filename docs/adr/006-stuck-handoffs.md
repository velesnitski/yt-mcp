# 006 — Stuck-handoff detection (`get_stuck_handoffs`)

## Context

The pulse and dashboard tools answer "what's the team doing right now,"
but they miss a specific failure mode: **a task crossed a team boundary
days ago and nobody on the receiving team has touched it since.**

Existing tools came close but each had a gap:

- **`get_handoff_snapshot`** lists items currently in handoff-receiving
  states and sorts by the `updated` field. Problem: `updated` advances
  on *any* field edit — a comment, a tag, a watcher change. An item
  parked in `Ready for test` for 60 days that got one comment yesterday
  looks fresh. False negatives.
- **`track_cross_dept_journey`** reconstructs the full path of a single
  issue (forensic deep-dive). Excellent for after-the-fact analysis,
  not for the operational "who needs nagging right now?" question.

The missing signal is **state-change-only timing**: an item is "stuck
after handoff" iff the last *state* change happened ≥ N days ago AND
that transition crossed a team-ownership role boundary AND no later
state change has occurred.

## Decision

New tool: `get_stuck_handoffs(board_name, stuck_days=4, lookback_days=30, limit=10, format="report")`.

### Algorithm

```
1. Resolve the board (reuse _resolve_board_for_pulse from pulse.py).
2. Classify the board's states into HANDOFF roles
   (dev/qa/release/rework/triage/intake/done/paused/unknown).
3. Identify "handoff-receiving" states (those in roles where stalls can
   occur: dev, qa, release, rework). Fetch all open issues currently in
   those states (single fast query).
4. For each issue, fetch state-change activities (bounded semaphore,
   reusing fetch_activities_only_bounded from the deadline package).
5. Extract the latest state-change event. If
      (now - event.ts) ≥ stuck_days
      AND (event.from_role, event.to_role) is a cross-team transition
   then the issue is stuck.
6. Group results by transition pattern, sort by days_stuck desc.
```

### Why a finer-grained role classifier than pulse

Pulse's `_COLUMN_PATTERNS` lumps `In Progress`, `For review`,
`Ready for test`, `On testing`, and `Ready for release` all into one
`in_progress` role — because pulse cares about pipeline flow, not team
ownership. For handoff detection we need the boundaries between those
sub-stages because that's exactly where the stalls happen:

| Role     | States                                                  |
|----------|---------------------------------------------------------|
| `dev`    | In Progress, For review, In Review, Code Review         |
| `qa`     | Ready for test, On testing, In testing, Dev/Staging/Prod QA |
| `release`| Ready for release, Ready for stage, Ready to prod       |
| `rework` | For revision, ReOpen, Rejected, "На доработку"          |
| `triage` | To Do, Backlog, Ready for Dev, Selected for Dev         |
| `intake` | Submitted, New, Open, Reported                          |
| `done`   | Closed, Done, Resolved, Released, Verified              |
| `paused` | Blocked, Pause, On hold, Waiting                        |

The two classifiers live in different modules because they answer
different questions. Trying to unify them would compromise both.

### Cross-team transitions

Explicit allowlist of transitions that count as team-ownership swaps:

```
dev → qa            # the canonical stuck case
dev → release       # rare skip-QA path
qa  → release
qa  → rework        # QA bounced back
qa  → dev           # QA reassigned for fixes
release → dev       # release failed
triage  → dev       # PM kicks off
intake  → dev / triage
rework  → dev       # rework resumed
```

Within-role transitions (`dev → dev` for `In Progress → For review`)
are NOT handoffs — they're internal workflow progress. Transitions to
`done` or `paused` aren't operational handoffs we can act on. Transitions
involving `unknown` are surfaced separately to avoid false counts.

### Why `stuck_days = 4`

Defaults exist on a spectrum:

- **Too eager (≤2d)**: catches weekend gaps, normal handoff latency.
- **Too lazy (≥7d)**: the test queue is already cold by the time you see it.
- **`get_handoff_snapshot` defaults to 5d** but its signal (`updated`)
  is noisier, so a higher threshold compensates. With state-change-only
  timing we can drop to 4 confidently.

User picked 3–5 as the acceptable range; 4 sits at the midpoint.

### Output shape

Markdown groups by transition (`Dev → QA stalls`, `QA → Release stalls`,
etc.) so the operator immediately sees *which* handoff most often gets
dropped. At-a-glance KPIs: worst stall, median, top-3 receiving
assignees, deadline-cliff cross-reference.

JSON exposes the same structure for programmatic consumption — same
pattern as pulse's `format="json"`:

```json
{
  "board": "...",
  "stuck_days": 4,
  "total_stuck": 12,
  "candidates_examined": 47,
  "stuck": [
    {
      "id": "PROJ-123",
      "summary": "...",
      "current_state": "Ready for test",
      "current_role": "qa",
      "previous_state": "For review",
      "previous_role": "dev",
      "transition": "dev→qa",
      "days_stuck": 14.0,
      "transitioned_at": "2026-05-04",
      "last_mover": "alice.smith",
      "current_assignee": "Bob B",
      "current_assignee_login": "bob.b",
      "severity": "Major",
      "type": "Bug",
      "deadline_days": 3
    }
  ],
  "by_transition": {"dev→qa": 8, "qa→release": 3, "qa→dev": 1},
  "by_receiving_assignee": {"QA-Team": 8, ...},
  "median_days_stuck": 11.0,
  "worst": {"id": "PROJ-123", "days_stuck": 14.0}
}
```

## Alternatives considered

- **Add as an insight flag in pulse.** Would clutter pulse's "should I
  act?" flag list and lose the per-transition grouping that makes this
  tool useful.
- **Extend `track_cross_dept_journey` with a `stuck=True` filter.** That
  tool's role-classification is *project-based* (PROJ=Mobile,
  PROJ=Backend) — wrong primitive for single-board cross-stage stalls.
- **Sort `get_handoff_snapshot` by activity-based age instead of
  `updated`.** Possible, but the existing tool's contract is
  snapshot-by-state. The state-change-history signal is heavy enough
  (per-issue activity fetch with semaphore) to deserve its own tool
  with the right query shape.

## Consequences

- Tool count: 74 → 75; modules 18 → 19.
- Test count: 454 → 519 (+65 in `tests/test_handoffs.py`).
- Operators can answer "who's sitting on something?" without manually
  walking activity logs.
- Per-transition grouping (`Dev → QA stalls (8)`) makes it obvious which
  handoff most often fails — actionable for process changes, not just
  per-item nags.
- Deadline cross-reference surfaces the worst combination: stuck +
  deadline within 7 days.
- The finer-grained role classifier is local to this module; pulse's
  classifier stays as-is. If future tools also need it, candidate for
  promotion to a shared module — defer until the second consumer.

## v1.11.1 — Query construction fix (BLOCKER)

The v1.11.0 ship fired 400 "Can't parse search query" on every call
because the state filter was built as
`(State: {A} or State: {B} or State: {C})` — YouTrack rejects this
OR-joined repeated-field form on many versions/projects. The same bug
also affected the older `get_handoff_snapshot` (which had been
unreliable for the same reason).

Pulse always worked because it builds the same filter differently:
`State: {A}, {B}, {C}` — YT's comma-list idiom for multi-value
filters. Once the difference was identified, the fix was mechanical.

**Resolution:**

- New shared helper `formatters.build_state_clause(states)` returns the
  comma-list form. Both `handoffs.py` and `journey.py:get_handoff_snapshot`
  use it.
- Same fix applied to the multi-project clause (`project: A, B, C`
  instead of `(project: A or project: B)`).
- Also fixed `get_handoff_snapshot`'s `updated: {minus Nd} .. Today`
  to use absolute ISO dates via a new `build_absolute_date_clause`
  helper (relative bounds are version-dependent — the same lesson from
  the v1.8.1 pulse fix, now generalized).

Tests: 519 → 530 (+11). New `TestBuildStateClause` and
`TestBuildAbsoluteDateClause` in `test_formatters.py`, plus a regression
test in `test_handoffs.py` that asserts no `" or "` keyword appears in
the rendered state clause.

### Lesson

When extracting a "shared helper" pattern across tools, default to
the form that already has battle-tested miles on it. Pulse's
comma-list approach was the right one — we just didn't recognize it
as the working idiom vs the broken OR-list pattern that was copied
from earlier code.
