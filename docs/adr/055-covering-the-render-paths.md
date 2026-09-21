# 055 — Covering what only runs when there is something to show

## Context

ADR-054's registry-wide suite lifted coverage from 56% to 78% by calling
every tool against empty, null and populated payloads. What it could not
reach is the code that only executes when a result has *interesting*
structure: the branch that formats a state change, the one that lists a
reporter breakdown, the one that restores a summary rather than a custom
field. A uniform fixture reaches each tool once; it does not walk each
tool's own decision tree.

So this round is targeted, aimed at the largest remaining blocks:
single-issue rollback and work-item writes, the digest and
creation-activity renderers, and the two infrastructure modules that run
on every invocation.

## Decision

Three suites, written against the implementations rather than against an
assumed API — the first draft of the logging tests asserted key names
from the wrong formatter, and was rewritten after reading the module.

- **Rollback and work items.** `rollback_issue` restores summaries,
  descriptions and custom fields by different mechanisms; each is pinned,
  as is its refusal to "restore" a change that has no previous value,
  which would clear the field instead of undoing it. `delete_work_item`
  is pinned to read before it deletes, since the message it prints is the
  only record of what was removed.
- **Digest and creation activity.** Per-change-type rendering, quiet
  periods reported as unchanged rather than omitted, and `limit`
  honoured.
- **Logging and CLI.** What the formatters emit, what they omit, and that
  a zero duration survives — `is not None` rather than truthiness, since
  0 ms is a measurement. Losing the instance id degrades to `"unknown"`
  instead of stopping the server.

## Consequences

- Coverage 78% → 81%; `monitoring` 75→87, `history` 67→83, `logging`
  70→80, `server` 52→65, `projects` 57→69.
- 1,586 tests pass; no existing behaviour changed in this round.
- **The 85% target was not reached**, and the honest reason is that the
  remainder is concentrated in `pulse`, `issues` and `translate` — large
  modules whose uncovered lines are mostly rendering variants. They are
  worth covering for the bugs the reading finds, not for the number.
- A fixture bug worth remembering: the digest tests first asserted
  against a hard-coded timestamp while the tools compute their window
  from the real clock, so every fixture fell outside it and the suite
  exercised the "nothing found" path while appearing to test the
  renderer. The same trap appeared earlier this month in the mentions
  tests. Fixtures for anything time-windowed anchor to `now`.
