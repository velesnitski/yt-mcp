# 007 — Normalized issue JSON across single-issue tools

## Context

The pulse → handoffs → reports integration thread established a
consistent `format="report" | "json"` pattern on board-level tools:

- `get_team_pulse` (v1.8.1) — added `format="json"`, returned a
  hand-built payload dict.
- `get_multi_team_pulse` (v1.10.0) — same pattern, with an `aggregate`
  + `boards` wrapper.
- `get_stuck_handoffs` (v1.11.0) — same pattern, hand-built payload.

`get_issue` was the conspicuous outlier. It returned **only markdown**
via `format_issue_detail`. The downstream reports project needed
`tags(name)` and `created` for filtering and age calculation. Both were
already in the YT response (and fetched correctly), but the markdown
renderer didn't expose them: `created` wasn't shown at all, and tags
collapsed into a single comma-separated string. Consumers were
falling back to direct REST calls — bypassing yt-mcp's instance
routing, analytics breadcrumbs, and Sentry filtering.

## Decision

Two additions to `get_issue`, mirroring the established pattern:

### 1. `format: str = "report"`

Default `"report"` preserves current markdown for chat use. `"json"`
returns a normalized dict via a new shared helper:

```python
{
  "id", "summary", "description",
  "state", "priority",
  "assignee", "assignee_login",        # login for live YT filter URLs
  "created", "updated", "resolved",
  "tags": ["release-blocker", ...],     # flat list of strings
  "custom_fields": {                    # dict, not the awkward list of {name,value} pairs
    "Severity": "Major",
    "Type": "Bug",
    "Deadline ☠️": "2026-05-30",
    "Subsystems": ["API", "Auth"],      # list-valued fields collapsed to list-of-strings
  },
  "links": [
    {"id": "PROJ-99", "summary": "...", "state": "Open",
     "link_type": "Depend", "direction": "outward"}
  ],
  "comments": [                         # only when include_comments=True
    {"id", "text", "author", "author_login", "created"}
  ]
}
```

The normalization helper lives in `formatters.py:normalize_issue` so
other single-issue tools (or future helpers) can adopt it consistently.

### 2. `fields: str = ""`

Power-user override for the YT field selector. When empty, uses an
expanded default that includes `assignee(login,name)` (not just `name`)
and `customFields(name,value(name,login,presentation,text))` (richer
value extraction). When set, passes through verbatim.

`fields="<custom>"` + `format="json"` returns the **raw YT response**
unaltered — no normalization. Rationale: power callers asking for a
specific subset want exactly what they asked for; normalization could
drop keys they explicitly requested.

### Default field-set expansion

The historical default was:

```
"...assignee(name)...customFields(name,value(name))..."
```

New default:

```
"...assignee(login,name)...customFields(name,value(name,login,presentation,text))..."
```

Why this is safe:

- `_resolve_assignee` and `_get_custom_field` (the existing markdown
  formatters) only read `name`. Extra keys are silently ignored.
- The wire payload is marginally larger but still trivial.
- Consumers benefit immediately when they switch to `format="json"`.

## Alternatives considered

- **Return YT's raw response directly, no normalization.** Pulse and
  handoffs chose the normalized form because the YT-native
  `customFields: [{name, value}, ...]` is awkward to walk in every
  consumer. Staying consistent across tools is more valuable than
  preserving native shape.
- **Make `format="json"` the default.** Would break every chat-mode
  caller (markdown view is by far the most-used). Patch bumps should
  not flip defaults.
- **Add a separate `get_issue_json` tool.** Doubles the surface area
  for a single output toggle. The `format` param is the established
  pattern.

## Consequences

- Tool count unchanged (still 75 — same `get_issue` tool).
- Test count: 530 → 543 (+13 in `test_issues.py`).
- `normalize_issue` helper is now available in `formatters.py` —
  candidate for adoption by other single-issue / list-of-issues tools
  if downstream consumers ask. Defer until concrete need (avoid
  speculative API changes).
- The historical contract (markdown out, `include_comments` toggle) is
  unchanged. Bump is patch (1.11.1 → 1.11.2).

### Pattern summary across all tools that emit JSON

| Tool                       | Version | Shape                                          |
|----------------------------|---------|------------------------------------------------|
| `get_team_pulse`           | 1.8.1   | board + metrics + sections + insights          |
| `get_multi_team_pulse`     | 1.10.0  | aggregate + boards[] + errors[]                |
| `get_stuck_handoffs`       | 1.11.0  | stuck[] + by_transition + by_receiving_assignee |
| `get_issue`                | 1.11.2  | normalized issue (id, state, custom_fields, …) |

All four use `format="report"` (default) / `format="json"` — operators
get one API surface to remember.

## v1.11.3 — Batch `get_issues(ids, ...)`

Pulse-style enrichment passes need ~20 issues per run (e.g. 3 picks ×
7 teams). With single `get_issue` calls that's 20 round-trips per
report — slow, and the existing OR-query trick was being open-coded by
every consumer.

New tool: `get_issues(ids, fields="", format="report", include_comments=False)`.

- Accepts a comma-separated ID list; URLs are stripped to bare IDs.
- Composes one `#A or #B or #C` query — single round-trip.
- Reuses `get_issue`'s expanded default field set + `normalize_issue`
  for consistent JSON shape (array of normalized dicts).
- `fields="..."` override returns raw YT response array (no
  normalization — same contract as `get_issue`).
- `format="report"` returns a compact list with a count header and a
  "missing" line for IDs that didn't come back.
- `include_comments=False` by default (batch mode rarely needs them
  and they bloat the response). Pass `True` to include.
- Caps at 100 IDs per call — YT URL length limits batch fetches.
  Larger lists should be split across multiple calls. Returns a
  friendly error instead of silently truncating.

Tool count: 75 → 76. Tests: 543 → 554 (+11 covering OR-query
composition, URL stripping, ID-count cap, JSON normalization vs raw
override, report-mode missing-ID listing, comments toggle).

The fifth member of the JSON pattern family:

| Tool                       | Version | Shape                                          |
|----------------------------|---------|------------------------------------------------|
| `get_team_pulse`           | 1.8.1   | board + metrics + sections + insights          |
| `get_multi_team_pulse`     | 1.10.0  | aggregate + boards[] + errors[]                |
| `get_stuck_handoffs`       | 1.11.0  | stuck[] + by_transition + by_receiving_assignee |
| `get_issue`                | 1.11.2  | normalized issue (id, state, custom_fields, …) |
| `get_issues`               | 1.11.3  | array of normalized issues                     |