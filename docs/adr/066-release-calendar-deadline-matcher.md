# 066 — Release calendar reads any deadline field

## Context

`get_release_calendar` estimates a release's date from its deadline field.
It found that field by comparing names against one literal, decorated name.
Every other tool that reads a deadline uses the shared matcher
`_is_deadline_field`, which accepts `Deadline`, `Due Date`, decorated
variants and qualifiers such as `(soft)`. ADR-009 and ADR-050 record why a
literal fails: the name varies by instance and by project.

So on any instance whose field had a different name, the calendar showed no
deadline-based dates, and nothing reported the gap.

## Decision

`_deadline_ms` uses `_is_deadline_field`, like the other tools. The test
fixtures and documentation now use a neutral decorated example
(`Deadline ⏰`) throughout, so no example implies that one specific name is
expected.

## Consequences

- The release calendar works with any deadline-style field name.
- Tests pin both directions: deadline-style names are read, and other date
  fields (`Start date`, `Created`) are not mistaken for the deadline.
