# 073 — `description_chars` on `get_issue`

## Context

In compact mode, `get_issue` cuts a description at 500 characters
(ADR-028). The truncation marker pointed to `format=json`. `comment_chars=0`
lifts only the comment cap. A caller reading a long tech task had no way to
get the full description as Markdown; it had to switch to JSON and render
the text itself.

## Decision

- `get_issue` takes `description_chars`, with the same meaning as in
  `get_issues`: a cap in characters, `0` for the full text.
  - The default (`-1`) keeps today's behaviour: compact mode caps at 500,
    the full report does not cap.
  - A positive value caps the description in both modes, with the same
    link-safe, self-announcing cut.
- The truncation marker names the parameter that lifts it:
  `description_chars=0 for the full text`.

## Consequences

- A long description can be read in full in one Markdown call.
- No default output changes, apart from the wording of the marker.
- Tests in `tests/test_formatters.py` pin the default cap, the full text at
  `0`, the uncut full report and a positive cap. Each was checked against a
  mutant of the branch it guards.
