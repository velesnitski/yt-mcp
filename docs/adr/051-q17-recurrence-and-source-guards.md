# 051 — The same rejected clause, shipped twice

## Context

ADR-050 fixed an OR-joined project clause in the deadline tools. A scan
for the same shape found a second copy, in the pulse pipeline, built the
same way and broken the same way:

    "(" + " or ".join(f"project: {p}" for p in projects) + ")"

The API rejects it. Any board bound to more than one project made
`get_team_pulse` and `get_multi_team_pulse` return a parse error —
reproduced live before the fix. Boards bound to a single project took a
different branch, which is why every existing test passed and why the
defect sat unnoticed: the common case never builds the broken string.

The same function emits `State: {A}, {B}` correctly two lines later. The
comma-list idiom was known; it just wasn't applied here. That is the
useful detail — this was not ignorance of the rule but a second
hand-rolled implementation of something that already had a canonical one.

## Decision

Both sites now call `contract.project_clause`. Two hand-rolled builders
become zero.

The tests changed shape as a result. Per-site assertions could not have
caught this: each site tested the string it produced, and both produced
what their author expected. What was missing is a check on the shape the
API refuses, wherever it appears. So `test_contract.py` now scans the
source and fails on any module that joins repeated same-prefix clauses
with `or`, including a copy written later by someone who never reads this
file.

That scanner carries two controls of its own: one asserting it still
matches the known-bad form, and one asserting it does **not** match
`" or ".join(f"#{iid}" ...)`, the valid batch-by-id syntax. A guard that
silences itself is worse than no guard, and a guard that fires on correct
code gets deleted by the next author.

Behaviour tests cover the multi-project board path directly, and assert
that queries were actually captured before asserting anything about them —
an empty capture would otherwise pass every claim vacuously.

## Consequences

- Pulse works for boards bound to several projects. It never had.
- 944 tests. Restoring the OR form turns both the behaviour test and the
  source scan red, verified by mutation.
- Generalizable, and the reason this ADR exists rather than a one-line
  fix: a rule recorded in the registry is not a guard. Q17 was written
  down, cited, and violated twice. The guard has to look at the code.
