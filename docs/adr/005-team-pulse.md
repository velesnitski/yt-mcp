# 005 — Team Pulse: lookback velocity + forward queue + insight flags

## Context

Existing tools answer "what's blocked", "what's active", "what's at risk", but
none answer the most common operational question: **"what shipped last week and
what's coming next?"** Operators were synthesising this themselves by stitching
together `get_sprint_board`, `get_top_active_issues`, and `count_issues` calls
and visually grepping for patterns.

Board reconnaissance on active workflows surfaced a hard constraint that
shapes the design: **sprints exist but are empty**. Boards have "current
sprint" objects, but issues are never placed in them. Teams use the **state
column as the kanban** — `Submitted → Backlog/To Do → In Progress → For
review → Ready for test → On testing → Ready for release`. A sprint-anchored
design would return zero issues for every board.

Additional findings from real boards:

- **State conventions vary per project.** One project uses `To Do`, another
  skips it entirely and uses `Backlog`. Both projects share the workflow
  shape but disagree on naming.
- **`Priority` is shallow** (`Low/Medium/High` only) and largely unset.
  `Severity` (`Blocker → Trivial`) is the richer ranking signal on a
  separate field.
- **`Submitted` is noisy.** One project has 90 items in `Submitted`, of
  which ~55 are recurring "Team Report DD.MM.YYYY" meta-tasks — pure
  housekeeping, not real work.
- **Synthetic team-pool assignees** (e.g. "Backend Team", "Frontend Team")
  appear alongside real users. They represent "anyone-on-the-team can
  claim" rather than a specific dev.

## Decision

One new tool: `get_team_pulse(board_name, horizon_days=14, lookback_days=30, limit=10)`.

### Asymmetric defaults: 30d lookback / 14d horizon

- **Lookback 30d** smooths weekly variance — closed-rate from a 7-day
  window is too noisy for a "velocity" signal.
- **Horizon 14d** matches what teams can realistically commit to. A
  longer forward window invites speculation.

### Auto-classified columns (not config-driven)

A pure-function `classify_column(name) → role` maps each column/state to one
of `{triaged, incoming, re_entry, paused, in_progress, done}` via regex
patterns. Unknown columns fall through to `triaged` with a diagnostic emitted
in the rendered output (safer to over-include than silently drop).

Per-state classification handles workflow disagreement between projects
without per-team YAML. The patterns cover:

| Role | Examples |
|---|---|
| `triaged` | To Do, Backlog, Ready for Dev, Selected for Dev |
| `incoming` | Submitted, New, Open, Reported |
| `re_entry` | For revision, ReOpen, Rejected, Needs Rework, "На доработку" |
| `paused` | Blocked, Pause, On hold, Waiting |
| `in_progress` | In Progress, For review, Ready for test, On testing, Ready for release |
| `done` | Closed, Done, Resolved, Released, Verified, Fixed |

### Ranking: Severity > Type > Deadline > Stale > Priority

```
score = severity_weight (Blocker=5..Trivial=0)
      + type_bonus (Bug=+2, Tech task=+1)
      + deadline_bonus (≤7d=+4, ≤14d=+2, ≤30d=+1; overdue clamped to +4)
      + stale_in_state (age_days * 0.05, capped at +3)
      + priority_tiebreak (High=+1, Medium=+0.5)
```

`Severity` is the dominant signal because `Priority` is sparse in practice
(most issues leave it unset). The stale-bonus is small and capped so urgent
fresh work isn't buried by ancient zombies.

### Velocity-aware insight flags

The "full picture" framing means lookback and forward halves *should* talk to
each other. A list of items is just data; a flag is an answer to "should I
act?":

| Flag | Trigger |
|---|---|
| 📈 Backlog growing | incoming > closed × 1.3 |
| 🐛 Quality concern | reopened / closed > 0.2 |
| 💤 Team underloaded | in_flight < closed / 3 |
| 🚧 WIP overload | pipeline_total > closed × 2 |
| 🔻 Bottleneck (column) | any pipeline column has ≥3 items and +3 over the next |
| ⏰ Deadline cliff | ≥3 ready items with deadline ≤7d |
| 🕸️ Stale triaged | ≥2 items >30d in queue |
| ⚠️ Velocity unknown | closed == 0 in lookback (ratios undefined) |

Flags emit only when triggered. A healthy board produces zero flags.

### Section order: re_entry before triaged

The forward section sequence is intentional:

1. **Pipeline-unblockers (re_entry)** — items that came back from review/test;
   pulling these unblocks code that's already mid-flight.
2. **Ready to pull (triaged)** — fresh on-deck work.
3. **Incoming – needs PM triage (last 30d)** — preview queue.

Re-entry comes first because it has higher leverage than starting fresh work.

### Team-balanced view: round-robin + team-pool isolation

A separate output section groups the top forward items per assignee and
displays them via round-robin (each dev gets one item before any gets two).
**No WIP cap** — the view shows every dev's next item regardless of current
load, on the principle that the operator sees the truth and decides.

Synthetic team-pool assignees (regex `\b(team|команда)\b`) and unassigned
items go to a separate **"Available to claim"** bucket — they're not
specific to any one person.

### Hard filters before ranking

Drop from all forward sections:

- Items with an unresolved `Depend`/`Subtask` outward link (can't start).
- Items matching the standup/report regex (shared with the deadline tool's
  `_DEFAULT_STANDUP_PATTERNS`, extended to catch `\bteam[\.\s]+report\b`).
- Items in `incoming` not updated within `lookback_days` (zombies).

## Alternatives considered

- **Two separate tools** (`get_completed_tasks` + `get_upcoming_tasks`) +
  a composing meta-tool. Rejected — the cross-window insight flags only
  work when both halves run together, and splitting forces operators to
  merge data mentally.
- **Sprint-based queue rather than state-based.** Dead on arrival
  because boards have empty sprints. Could re-add if any team adopts
  sprints later, as a fallback path.
- **Per-team YAML for column mapping** (like `policy.json` for
  deadlines). Adds setup friction; the regex classifier covers the
  observed conventions and unknown-column diagnostics catch outliers.
- **WIP-cap-aware balanced view** (skip devs already at WIP limit).
  Rejected per operator preference — the view shows truth, not
  prescription; the human chooses who to skip.
- **Per-dev item caps in the balanced view.** Not needed — round-robin
  is a natural cap. A dev with 8 ready items still gets the top 1 first,
  then 2nd, etc.

## Consequences

- Tool count: 72 → 73; modules 17 → 18.
- Test count: 328 → 395 (+67 new tests in `tests/test_pulse.py`).
- Operators get a single command per board that answers "ship-vs-load,
  shipped-vs-incoming, and on-deck queue" in one render.
- The standup-pattern extension (`\bteam[\.\s]+report\b`) also benefits
  the deadline tool — both share `_DEFAULT_STANDUP_PATTERNS`.
- Unknown columns produce a diagnostic line rather than silent drop, so
  workflow drift is observable from the report.

## Post-ship fixes (v1.8.1)

Two integration blockers surfaced when wiring the tool into the
downstream reports pipeline.

### 1. YouTrack query parse error on lookback clause

The original implementation built `resolved: -30d .. *`. YouTrack rejects
this with `400 Can't parse search query`: the `*` upper-bound wildcard
isn't valid in date ranges, and bare relative-offset bounds (`-30d`) are
inconsistently supported across YT versions.

**Fix:** new helper `build_lookback_clause(lookback_days, now_ms)`
returns an absolute ISO range `YYYY-MM-DD .. YYYY-MM-DD`. All five
lookback queries (`closed`, `incoming`, `incoming-recency`, `released`,
`reopened`) consume it. Absolute dates are portable across YT versions.

### 2. `format` parameter for downstream programmatic consumption

The HTML-email reports project needs structured data, not markdown.
Today's markdown-only return forced consumers to re-parse the rendered
output.

**Fix:** added `format: str = "report"` param. Default preserves
markdown for chat use; `format="json"` returns
`json.dumps(payload, indent=2, ensure_ascii=False)` with the dict shape:

```
{
  "board": ..., "lookback_days": ..., "horizon_days": ...,
  "metrics":          {closed, released, incoming, reopened},
  "pipeline_counts":  {in_progress, for_review, ready_for_test, on_testing, ready_for_release},
  "triaged":          [{id, summary, severity, type, score, breakdown, ...}],
  "re_entry":         [...],
  "incoming":         [...],
  "team_balanced":    [{assignee, items: [...]}],
  "team_pool":        [...],
  "insights":         ["📈 ...", ...],
  "unknown_columns":  [...],
}
```

The payload is assembled once; both `format` branches read from the same
dict, so the markdown renderer and the JSON output stay in sync by
construction. Always returns `str` for MCP wire compatibility — callers
do `json.loads()`.

Tests: 395 → 413 (+18 covering `build_lookback_clause`, `_issue_to_dict`,
the payload-based renderer, and the JSON round-trip).

## v1.9.0 — Stale & overdue filtering

Downstream reporting (`velesnitski/youtrack-reports`) discovered the raw
pipeline buckets are full of "100–200d-idle ghosts" — items technically
in `In Progress`/`For review`/`On testing` but with no activity in
months. They inflate WIP counts and dilute the velocity-ratio insight
flags. Measured impact of adding a 60-day idle filter on real data:

| Board                       | Before → After |
|-----------------------------|----------------|
| Desktop (one product)       | 200 → 81       |
| Frontend                    | 88 → 48        |
| WordPress                   | 65 → 52        |
| QA (sub-product)            | 15 → 7         |

A second filter — drop items >30d past their deadline — addresses
deeply-overdue ghosts that aren't realistically next-up: they need a
different conversation than what pulse provides.

**Two new params, both opt-out via 0:**

- `max_idle_days: int = 60` — drop pipeline (`in_progress`, `for_review`,
  `ready_for_test`, `on_testing`, `ready_for_release`) and `re_entry`
  items not updated within this window. Triaged/incoming stay
  unfiltered (the existing `stale_triaged` insight surfaces them as a
  flag rather than dropping silently).
- `max_overdue_days: int = 30` — drop items from all forward sections
  whose `Deadline ☠️` is past by more than this window.

**Safety:** missing `updated` field is treated as active (surface
ambiguous data rather than silently drop); items without a deadline are
never filtered by the overdue rule.

**Side effect:** the post-filter pipeline counts feed `compute_insights`,
so velocity ratios (`incoming > closed × 1.3`, `WIP overload`,
`underloaded`) become honest signals instead of distorted by abandoned
tickets. This is the primary motivation cited by the downstream
consumer.

Tests: 413 → 431 (+18 covering boundary cases, opt-out, missing-field
safety, and the batch filter helpers).

### Why minor bump (1.9.0), not patch

The defaults change behavior of existing calls — operators relying on
the wider unfiltered output need to know. Both filters can be disabled
with `=0` for full backwards compatibility.

## v1.9.1 — Split "WIP overload" into WIP + Pipeline overload

The original flag was labelled "WIP overload" but the math summed the
entire pipeline (in_progress + for_review + ready_for_test + on_testing).
This conflated two distinct operational problems:

- **WIP overload** = devs juggling too many concurrent items at the
  dev-work stage. The fix is reduce in-progress count / focus.
- **Pipeline overload** = downstream-of-dev clog (test queue or review
  queue backed up). The fix is unblock the QA/review side, not change
  what devs are doing.

The downstream consumer caught this with a real-shape example: a team
running 5 in_progress + 6 for_review + 34 ready_for_test at ~28
closed/30d. Dev capacity (5) is fine; the test queue (34) is the
problem. The old flag would have said "WIP overload" with a number that
included the test queue — pointing operators at the wrong fix.

**Resolution:**

```
🚧 WIP overload     — in_progress count vs weekly_velocity × 2
🪣 Pipeline overload — pipeline_total vs closed × 2 (was the old flag)
```

`compute_insights` signature gains `lookback_days: int = 30` so it can
compute `weekly_velocity = closed / (lookback_days / 7)`. The "WIP cap"
is two weeks of velocity — a team closing 14/30d has cap ≈ 6.5
concurrent items.

Both flags can fire together when both conditions are met. The
bottleneck flag (separate) continues to identify *which* downstream
column is the culprit when pipeline overload fires.

Tests: 431 → 435 (+4 covering the downstream-clog false-positive case,
pipeline overload independent of WIP, both flags firing together, and
the `lookback_days` parameter affecting velocity).
