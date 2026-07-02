# 016 — create_issue: preserve braces in the command split fallback

## Context

Users reported (repeatedly) that `create_issue` "can't set a required field
(e.g. Department) at creation" on a project whose required enum/state/user
fields have no defaults. A prior session concluded it was a hard tool
limitation and told users to create the issue manually, then `update_issue`.

An empirical live test disproved that: the issue **was** created and the
required fields **were** set. The direct `POST /api/issues` succeeds even
with required fields missing (YouTrack REST does not enforce required-ness
at create the way the UI does), and the follow-up command set the fields.

The real defect was in the **command split fallback**. `create_issue` tries
the whole `command` as one YT command first; on failure it splits into
per-field clauses via `_CMD_FIELD_RE`. That regex captures a `{braced}`
value **without** its braces (group 2), and the split rejoined it as
`f"{name} {value}"` — dropping the braces. So a multi-word value like a
two-word status or a two-word assignee became a bare `Assignee Two Words`,
which YT parses as value `Two` → `400 … expected`. The tool then surfaced a
misleading `Could not set: …` even though the field had already been set by
the earlier whole-command attempt — which is exactly what led the prior
session to declare it impossible.

Single-word values (most enum options) were unaffected, so the failure only
bit multi-word values — intermittent enough to look like a mysterious
"required field" problem.

## Decision

Re-wrap braced values when building the split clauses:

```python
if m.group(2) is not None:              # value came from the {braced} group
    split_commands.append(f"{name} {{{value}}}")
else:
    split_commands.append(f"{name} {value}")
```

Now the split fallback is faithful to the original command and idempotent:
multi-word values keep their braces, the per-field retries actually succeed,
and no false `Could not set:` is emitted. A multi-word value that the
whole-command attempt failed on (e.g. a second multi-word co-assignee) now
gets applied by the split pass instead of being silently dropped.

## Alternatives considered

- **Set custom fields in the create POST body (`customFields` with $type).**
  The atomically-correct YT-native approach, but it needs per-field `$type`
  strings, single/multi handling, and user-field login resolution — a large,
  fragile change. Unnecessary once we saw the direct create already succeeds
  and only the command formation was broken.
- **Always brace every value.** `Department {DevOps}` also works, but
  needlessly braces single tokens; wrapping only the values that were
  braced in the input keeps the output minimal and matches intent.

## Consequences

- `create_issue` reliably sets required (and multi-word) fields at creation
  on projects with no field defaults; the misleading `Could not set:` for
  multi-word values is gone.
- Tests: 709 → 711 (+2 in `test_issues.py`): the split fallback keeps braces
  on multi-word values, leaves single tokens bare, and emits no false
  failure. Driven through the real `create_issue` with a mock client that
  forces the split path.
- Patch bump 1.16.5 → 1.16.6.

### Note (separate, not fixed here)

`get_project_fields` under-reports the assignable users for a User-type
field (it showed a partial list that omitted a user who is in fact assigned
to live issues in that project). It reads the field's `bundle values`, which
isn't the authoritative assignable-user set for user fields. Filed for a
later fix; it doesn't block creation (the command resolves users by name).
