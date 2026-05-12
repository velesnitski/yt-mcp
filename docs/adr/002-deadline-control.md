# 002 — Deadline control: audit, scorecard, manager suggester

## Context

A new internal policy ties individual quarterly performance reviews to deadline
compliance recorded in YouTrack. Two things must be measurable:

1. Whether a Due-Date shift was approved by the task owner or line manager.
2. Whether a deadline was missed without an approved extension.

The tool runs across teams with many operators (line managers self-serve their
own scorecards). Access control is delegated to YouTrack tokens — the tool adds
no fence beyond that.

## Decision

Three read-only tools in a new `tools/deadlines.py` module:

- `audit_deadline_changes` — forensic view of every Due-Date shift in a period
- `deadline_scorecard` — per-assignee rollup for a calendar quarter
- `suggest_managers` — bootstrap helper, derives a candidate
  `managers.suggested.json` from recent YouTrack activity

### Approver mapping

Stored in `~/.yt-mcp/managers.json` (canonical) with fallback to
`managers.suggested.json`. JSON, not YAML — zero dependency surface, clear
schema, easy to hand-edit. Schema is a candidate-set, not a single value:

```json
{
  "__default__": "fallback.user",
  "alice.user": {
    "primary": "bob.manager",
    "also_accept": ["carol.lead"],
    "manual_review": false
  }
}
```

`also_accept` exists because real orgs have dotted-line reporting and multiple
leads — a comment from *any* listed approver counts. `manual_review: true`
forces the audit tool to bucket shifts as `approver_unknown` instead of
`unauthorized` — protects assignees from being penalized because of the
operator's stale mapping.

### Classification buckets

Each shift goes into one of:

- `compliant_strict` — author of shift is an approver, OR approver comment in
  window contains both a keyword and the new date string
- `compliant_loose` — approver comment in window, but without keyword + date.
  Coaching opportunity, not a penalty
- `unauthorized` — no approval signal at all. Counts as a penalty
- `approver_unknown` — no mapping exists for this assignee. Doesn't count;
  surfaced in a "coverage gaps" section
- `pre_policy` — shift timestamp predates `policy_effective_date` from
  `~/.yt-mcp/policy.json`. Surfaced but not counted
- `informational` — first-time set (no previous value) or earlier date (pulled
  in, not postponed)

Strict mode (`strict=True`) demotes `compliant_loose` to `unauthorized`.
Default is non-strict — gives the team time to learn the comment format
before triggering penalties.

### PM-bias mitigation in the suggester

Raw reporter co-occurrence is unreliable when PMs file tasks on behalf of
devs. The suggester:

1. Computes `fanout(U) = count(distinct assignees of issues U reported)` over
   the lookback window.
2. Marks the top decile of fan-out (with a minimum count of 6) as PMs, excludes
   them from being LM candidates.
3. Scores remaining candidates with `0.55 * field_edits + 0.35 * resolves +
   0.10 * reporter_count`. `field_edits` (priority/assignee/deadline) and
   `resolves` (State → terminal) are the dominant signals — they are what
   managers *do*, not what PMs do.
4. Refuses to commit a `primary` when the top score isn't ≥25% ahead of the
   runner-up — emits `primary: null, manual_review: true` with the top-3
   candidates and their raw counts as evidence.

The suggester always writes to `managers.suggested.json` — never overwrites
`managers.json`. Operator reviews, hand-corrects flagged entries, copies
manually.

### Standup exclusion

Recurring daily/standup tickets dominate the deadline-shift signal and would
flood the audit output. Default patterns match `DevOps Daily`, `daily`,
`standup`, `дейли`, `стендап`, `решение текущих проблем`. Configurable via
`~/.yt-mcp/policy.json:standup_patterns` (regex list).

### Audit log

Every invocation appends a line to `~/.yt-mcp/deadline-audit.log` with
`{ts, operator, tool, scope, result_size}`. Operator is derived from
`/api/users/me` — the YouTrack token holder. Provides traceability for
performance-review tooling that touches sensitive data.

## Alternatives considered

- **YAML config** — adds PyYAML as a hard dependency. JSON is universally
  parseable, IDE-friendly, and our schema is structured (not a free-form
  document). Rejected YAML.
- **Single-value `manager` mapping** — score-with-explanation looked clean but
  invited false confidence ("0.82 must be right"). The candidate-set schema
  forces the operator to see the contest and decide; the tool refuses to
  commit when signal is mixed.
- **Real-time alerting tool** — feasible (reuse `poll_changes` pattern) but
  premature. Wait for the team to internalize the workflow; revisit in Q3.
- **GitLab cross-validation in `suggest_managers`** — designed but deferred.
  Activity-only signal is good enough for v1; GL adds value when activity is
  ambiguous, which is exactly the manual-review case anyway.
- **YouTrack workflow command** (e.g. `extend-deadline {date} {reason}`) —
  cleanest audit trail but requires admin-level YouTrack workflow setup and
  team behavioral change. Mark as a future option behind the strict-mode flag.

## Consequences

- Tool count: 69 → 72. `test_registration.py` and `test_server.py` updated.
- Test count: 239 → 282 (+43 new). All passing.
- New config files (operator-supplied, not committed): `managers.json`,
  `managers.suggested.json` (tool-written), `policy.json`,
  `deadline-audit.log`.
- All three tools read-only; no `WRITE_TOOLS` membership. Read-only mode
  preserves access.
- First fully-measurable quarter is Q3 2026 (policy effective May 12, 2026
  → Q2 2026 is half-spent and protected by the `pre_policy` bucket).
