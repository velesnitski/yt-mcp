# 010 — QA-skip compliance signal in get_at_risk_issues

## Context

Some teams enforce QA with a required gating field — e.g. a project where
`QA Required: Yes/No` is mandatory on every issue. The field declares
whether work *must* pass QA before shipping, but no analytics tool used it.
A genuinely useful, otherwise-invisible failure mode: an issue marked
`QA Required: Yes` that reaches a release/done state **without ever having
passed through a QA state** — i.e. it shipped (or is about to) unverified.

`get_at_risk_issues` is the natural home: it already classifies unresolved
issues into risk buckets, and "about to ship without QA" is exactly an
at-risk condition.

## Decision

Add a `qa_skipped` category.

### Detection (accurate, not a heuristic)

A true skip can't be inferred from current state alone — an issue at
`Ready for release` might have passed QA legitimately. So detection is
**history-confirmed**, in two stages:

1. **Candidate gather (free, in the existing bulk loop):** an issue is a
   candidate iff its QA-gating field is affirmative AND its *current* state
   role is `release` or `done` (the window where QA should already be
   complete). Role classification reuses `handoffs.classify_handoff_role` —
   one source of pipeline-role truth across tools.
2. **History confirm (bounded, suspects only):** for each candidate, walk
   its state-change activities (reusing `fetch_activities_only_bounded`).
   Flag only those whose history **never touched a QA-role state** (checking
   both the `added` and `removed` sides, so "was in QA then moved on" still
   counts as passed).

Empty/unavailable history is **inconclusive → not flagged** (no false
alarms from an API hiccup or a directly-created issue).

### Cost discipline ("treat the MCP as a common tool")

- Field matched by **pattern** (`_is_qa_required_field`: `QA Required`,
  `QA Needed`, `Requires QA`, `Needs QA`, `QA Gate`, + Russian) — works
  across teams with zero config.
- A project **without** any QA-gating field yields **no candidates → no
  history walk → zero added cost**, and the category is simply absent from
  output.
- The history walk runs **only when `qa_skipped` is in scope** (no category
  filter, or `category="qa_skipped"`). Asking for `category="overdue"`
  pays nothing extra — verified by test (`client.get` called once).
- Suspects capped at `_QA_SKIP_CHECK_MAX = 80` (stalest first); any
  overflow is reported, never silently dropped.

### Distribution note

This signal's value scales with how often QA is required. On an ops/DevOps
project where QA Required is almost always `No`, it's near-silent and
high-signal (watch the rare `Yes`). On dev teams where `Yes` is common,
it's a real release-gate check. Either way it's correct and cheap.

## Alternatives considered

- **Current-state-only heuristic** (flag any QA-required issue at a release
  state). Rejected — it false-positives on every issue that legitimately
  passed QA before release; noisy exactly where the field is most used.
- **Scan recently-resolved issues** (the literal "shipped without QA").
  `get_at_risk_issues` is `#Unresolved`-scoped by contract, and the
  release/done-but-unresolved window is the actionable one (you can still
  route to QA before final close). A resolved-issue audit is a different
  tool's job.
- **Always walk history for all issues.** Would erase the tool's
  single-cheap-query identity. Suspect-only walking keeps it proportional.

## Consequences

- New `qa_skipped` category (placed second, after `overdue` — shipping
  unverified is high severity). Participates in markdown, `format="json"`,
  the `category` filter, and aliases (`qa`, `qa skipped`, `qa compliance`,
  …) for free via the shared category structure.
- `get_at_risk_issues(project=..., category="qa_skipped", format="json")`
  is the focused, programmatic QA-compliance check.
- Tool count unchanged (77). Test count: 627 → 657 (+30 — field/affirmative
  matchers, `_passed_qa_state` both-sides, fires/doesn't-fire matrix,
  no-field-zero-cost, inconclusive-not-flagged, category-filter-skips-walk,
  alias).
- Minor bump 1.13.0 → 1.14.0 (additive category + new behavior; existing
  callers unaffected — `category`/`format` default to prior behavior, and
  projects without the field see no change).
- New cross-module reuse: monitoring now (lazily) imports
  `handoffs.classify_handoff_role` and `deadlines.fetcher.
  fetch_activities_only_bounded`. No import cycle (handoffs/deadlines don't
  import monitoring); imports are function-level to keep module load order
  trivial.
