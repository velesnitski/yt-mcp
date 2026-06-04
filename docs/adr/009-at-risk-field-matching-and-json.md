# 009 — get_at_risk_issues: pattern field-matching + JSON/category output

## Context

While verifying a "Deadline Demon" daily Slack report against live data,
`get_at_risk_issues(project="DO")` returned **no Overdue category at all**
and an implausibly large **Unestimated (62)** — despite many DO issues
having both a deadline and a logged estimate.

Root cause was in the custom-field scan:

```python
if cf_name in ("deadline", "due date", "due"):        # deadline
elif cf_name in ("estimation", "estimate", "dev estimate", "dev estimation"):
elif cf_name in ("spent time", "spent"):
```

Exact-match on the lowercased field name. But real projects decorate
these fields:

- Deadline field is `Deadline ☠️` → lowercases to `deadline ☠️` ≠ `deadline`
- Estimate is `Evaluation time 🕙`
- Spent is `Spent time 🚴🏻‍♂️`

None matched. Consequences, all from one root cause:

1. **Overdue silently empty** — the deadline value was never read, so no
   issue was ever classified overdue or approaching.
2. **Unestimated wildly inflated** — `estimate_minutes` stayed 0 for every
   issue (the field never matched), so every active issue was flagged
   "no estimation."
3. **Over-estimate always empty** — same reason; estimate vs spent could
   never be compared.

A correct, battle-tested deadline matcher already existed in the codebase:
`deadlines.parser._is_deadline_field`, with `([\W_]|$)` boundaries that
match a trailing emoji/space while rejecting partial words.

## Decision

### 1. Match deadline/estimate/spent by pattern, not literal

- Reuse `_is_deadline_field` (handles `Deadline ☠️`, `Due Date`, camelCase,
  Russian variants).
- Add sibling matchers `_is_estimate_field` / `_is_spent_field` in
  `monitoring.py` with the same boundary discipline. Estimate covers
  `Estimate / Estimation / Dev Estimate / Evaluation time / Total
  Estimate` (+ Russian); spent covers `Spent time / Spent / Time Spent /
  Logged time`.
- `_period_to_minutes` extracts the authoritative YT `minutes` value
  (correct under the project's work schedule). It deliberately does **not**
  parse the `presentation` string ("1w 2d 1h") — day/week length is
  project-configurable, so parsing it ourselves would yield wrong
  over-estimate ratios. Absent `minutes` → 0, never a guess.
- Estimate/spent take the **first** matching non-zero field
  (deterministic when a project has several estimate-like fields).

### 2. `format="json"` + `category` filter

Matching the cross-tool convention (pulse / handoffs / get_issue /
get_issues), `get_at_risk_issues` gains:

- `format="report"` (default markdown) | `"json"`.
- `category=""` — restrict to one bucket (`overdue`, `approaching`,
  `stalled`, `over_estimate`, `unestimated`, `ancient`, `forgotten`),
  with friendly aliases (`over estimate`, `deadline`, `stale`, …). An
  unknown category fails fast with the valid list — before any API call.

JSON shape:

```json
{
  "project": "DO",
  "total_at_risk": 96,
  "thresholds": {"stale_days": 7, "forgotten_days": 30,
                 "ancient_days": 200, "deadline_warning_days": 7},
  "categories": {
    "overdue": {"count": N, "issues": [
      {"id","state","summary","assignee","priority","detail"}
    ]},
    ...
  },
  "filtered_category": "overdue"   // only when category= is set
}
```

JSON returns the **full** per-category list (not truncated to
`limit_per_category` — the source is already bounded by `$top=500`), so a
bot can act on every overdue item. Markdown keeps the per-category cap.

Refactor: each category now collects structured `_risk_record` dicts; the
markdown renderer and the JSON path read the same records, so they can't
drift.

## Why this matters

`get_at_risk_issues(project=..., category="overdue", format="json")` is now
exactly the backend a daily deadline bot should call — the very use case
that surfaced the bug. Previously such a bot would have had to bypass the
tool and hand-roll a `{Deadline ☠️}: … .. <today>` query per project,
guessing the field name (and eating 400s, as observed).

## Alternatives considered

- **Just add `Deadline ☠️` to the literal list.** Fixes one project, breaks
  on the next decoration. Pattern matching is the general fix and the
  helper already existed.
- **Parse `presentation` for period minutes.** Rejected — 1d is 8h or 24h
  depending on project config; guessing corrupts the over-estimate ratio.
- **A separate `get_overdue_issues` tool.** Redundant — overdue is already
  one of at-risk's categories. `category="overdue"` is the focused view
  without a new tool to maintain.

## Consequences

- Behavior change: Overdue/approaching/over-estimate now populate where
  they silently didn't; Unestimated shrinks to genuinely-unestimated
  issues. Outputs for affected projects change meaningfully — hence a
  minor bump (1.12.3 → 1.13.0), not a patch.
- Tool count unchanged (77). Test count: 579 → 627 (+48 in
  `tests/test_at_risk.py` — field matchers, period extraction, decorated-
  field detection end-to-end, json shape, category filter + aliases +
  invalid-category fast-fail, and a bare-field regression guard).
- No new public surface beyond two params; `format`/`category` default to
  the prior behavior, so existing callers are unaffected.
