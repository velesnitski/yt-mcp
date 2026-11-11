# 067 — Conditional fields in get_project_fields

## Context

`get_project_fields` is the call a caller makes before `create_issue` to
learn which fields to set. It labelled every field that cannot be empty as
**(required)**. A conditional field is one that a project shows only for
some values of another field, for example a defect-only field shown when
`Type = Bug`. Such a field was labelled required too.

So callers set it on issues of other types, YouTrack rejected the field,
and the command it was part of failed. The tool that exists to make the
first `create_issue` succeed was causing it to fail.

YouTrack exposes the condition on the project field: `condition` is a
`FieldBasedCondition` with the controlling `field`, the `values` that show
the field, and `showForNullValue`.

## Decision

- Request `condition(field(field(name)),values(name),showForNullValue)` and
  label conditional fields with their condition:
  `(required when Type = Bug)`, or `(only when …)` if they may be empty.
- A new optional `when` argument (`"Type=Tech task"`, pairs separated by
  `;`) leaves out fields whose condition the given values do not meet.
  They are listed in one closing line as not applicable.
  - A controlling field that `when` does not mention keeps the field, since
    its value is unknown.
  - An empty value meets `showForNullValue`.
- A server that rejects the condition selector gets the plain request
  again. It must not fall through to the sample-from-issues fallback,
  which loses the required flags.

## Consequences

- The discovery call now says which fields an issue of a given type can
  carry, so a `create_issue` built from it no longer includes unsettable
  fields.
- One extra request only on servers that predate conditional fields.
- `tests/test_project_fields.py` pins the labels, the filter and the
  fallback. Each was checked against a mutant of the code it guards.
