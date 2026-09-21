# 050 — Deadline tools: two broken queries, one silent

## Context

A scorecard run over one team's quarter returned a single issue for a
single person. The number was not obviously wrong — a quiet quarter looks
like that — but the tool printed its own warning: YouTrack had rejected
the `due date:` clause, so it had fallen back to `updated:` alone. Pulling
that thread found two separate defects.

**There is no `due date:` search attribute.** The deadline is a per-project
custom field whose name varies by project — decorated (`Deadline ☠️`),
plain (`Due Date`), or localized. Querying the literal attribute returns a
parse error, so the range half of the scope was never applied. Anything
whose deadline fell inside the quarter but which had not been touched
recently was invisible. The anti-silent-zero convention did its job here:
the tool said it had degraded rather than presenting a short list as fact.

**`build_project_clause` emitted OR-joined project clauses.** The form
`(project: A or project: B)` is rejected outright — registry Q17, violated
in our own code. Single-project calls built no such clause, so the bug only
appeared once a caller passed a second project, and then took down
`deadline_scorecard`, `audit_deadline_changes` and `suggest_managers`
alike. It had no test.

Composition was also tried and rejected: `(updated: R or <deadline>: R)`
does not parse either, so the two ranges cannot be expressed as one query.

## Decision

`build_project_clause` emits the comma-list `project: A, B`.

`resolve_deadline_field` learns the field's real name by reading it off
recent issues and matching with the existing `_is_deadline_field` patterns.
Issue reads return custom-field names to any account, so this needs no
admin rights — an admin-API lookup would have failed for exactly the
reporting tokens these tools are meant for.

The scorecard now runs **two queries and merges them** by `idReadable`,
rather than composing an OR the parser refuses. When no deadline field
exists in scope the scope is honestly reported as `updated:`-only, which is
a real answer rather than a degraded one.

## Consequences

- Multi-project calls work at all, across three tools.
- Deadline-in-quarter issues are counted whether or not they were touched.
- One extra request per run to learn the field name.
- 939 tests (10 new). The Q17 guard is mutation-verified: restoring the
  OR form turns three tests red. The old code had no test on this path,
  which is why a clause that parses nowhere shipped.
- Live-verified query shapes before writing the code: the comma-list and
  the braced custom-field range both return sane counts; the OR forms and
  the literal `due date:` all return 400.
