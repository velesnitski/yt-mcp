# 038 — Sprint board issues come from the sprint entity, not board columns

## Context

`get_sprint_board` rendered every column as `(0)` on a board whose current
sprint held 43 issues — a silent zero, caught only because the empty result
was cross-checked with a direct `Board <name>: {<sprint>}` search query.

Root cause, proven against the live API: the tool requested
`board(columns(presentation, issues(...)))` on the sprint endpoint, but
YouTrack **silently ignores** an `issues` selector nested under board
columns — the columns come back with no `issues` key at all. The real issue
list lives directly on the sprint entity (`issues(...)` at top level
returned all 43). No error, no warning: a wrong selector shape simply
yields an empty-looking board. Same failure family as ADR-034 (fictional
top-level `state` on links) and ADR-035 (`resolved:` alias): plausible
selectors that YT declines silently.

The tool had no behavior tests, so nothing pinned the contract.

## Decision

- Fetch `issues(idReadable,summary,assignee,customFields(State))` at the
  **sprint level**, plus `board(columns(presentation))` for column order.
- Group client-side by issue State into the board's columns
  (case-insensitive match on the column presentation). States the board
  does not render as a column go to an explicit "(states without a
  column)" bucket — visible, never silently dropped (the ADR-037/pulse
  anti-silent-zero convention).
- Behavior tests mock the REAL response shape, including the
  no-issues-key-under-columns property, an unmapped-state bucket case, and
  case-insensitive column matching.

## Consequences

- Sprint boards render their actual contents; consumers can trust `(0)`
  again.
- 834 tests pass (4 new). Patch release 1.20.1.
