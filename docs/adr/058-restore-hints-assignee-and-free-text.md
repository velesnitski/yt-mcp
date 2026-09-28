# 058 — Say what happened: restore hints, assignee names, free-text matches

## Context

Three places where the server's reply misled its caller without any
underlying error.

**A restore hint for a write that never happened.** `update_issue`
appended `To restore: update_issue(…, assignee="…")` whenever the
parameter was *supplied*, not when the field *changed*. A rejected
assignment therefore produced "No field changes detected" alongside a
restore hint whose value matched the current one — which reads as though
the tool were about to write something. The value was never wrong; it was
the genuine previous value, identical only because nothing had changed.
The defect was the gate.

**A display name rejected with its own echo.** The command grammar needs a
login, so `Assignee Firstname Lastname` fails with "Assignee expected:
Firstname Lastname". A model calling the tool has the display name, not
the login, and the error does not say which is wanted.

**A broad free-text match presented as an answer.** A bare phrase matches
any issue containing any of its words. A query that meant `project: KEY`
returned a full page of unrelated issues with nothing to distinguish it
from a scoped result, and the docstring did not mention that operators
exist.

## Decision

- Each restore hint is gated on the field's before/after comparison, which
  `update_issue` already performs to build its change summary. Description
  is compared against the snapshot taken before the write.
- `_assignee_login` resolves a value containing a space against the user
  directory. It substitutes only a **unique, exact, case-insensitive**
  full-name match. A login, `me`, no match, several matches, or a
  directory the token cannot read all pass through unchanged, so behaviour
  is never worse than before — two people sharing a name must not have
  one of them silently assigned.
- `search_issues` names the common operators in its docstring, and appends
  one line when a query with no operator returns 20 or more results.
  Suggesting a specific project key was considered and dropped: it would
  cost an extra request on every search.

## Consequences

- 1,628 tests (18 new). Each fix is mutation-verified: re-gating the
  hint on the parameter, never resolving, guessing on ambiguity, and
  dropping the search hint each turn a test red.
- One open item remains in this area: `get_project_fields` still reports
  conditionally-applicable fields as unconditionally required, because
  the condition is not fetched. That needs the API's field-dependency
  data, not a formatting change.
