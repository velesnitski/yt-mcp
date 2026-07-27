# 036 — UserInputError: Sentry filters caller-input errors by type

## Context

A `month=13` validation rejection from `monthly_time_report_by_user` (an
intentional guard test) landed in Sentry as a High-priority production
error. The rejection itself was correct behavior — the problem is that
expected input-validation errors reach Sentry as `level: error` at all.

The codebase already had the right philosophy: `_scrub_event` drops
user-input ValueErrors via `_USER_INPUT_VALUE_ERROR_PATTERNS`. But the
allowlist knew only three client-level message prefixes; every tool-side
guard added since ("month must be 1-12", "since must be YYYY-MM-DD",
"user is required", "group_by must be …") was invisible to it. String
allowlists rot: each new guard is a new chance to forget the update, and
this event is the proof.

## Decision

- **`UserInputError(ValueError)`** in `errors.py`. Subclasses ValueError so
  every existing `except ValueError` catch site handles it unchanged (the
  `YouTrackPermissionError` precedent).
- **`_is_user_input_error` matches by `isinstance` first**; the message
  patterns remain only as a fallback for pre-existing bare ValueErrors and
  the log-entry (no exc_info) path.
- **Raise sites converted:** all `time_report.py` guards, the
  `get_work_items` date guard, and the client's 400/404 mapping (bad query /
  not found = caller trouble by definition). `YouTrackPermissionError` now
  subclasses `UserInputError` — 401/403 are config/caller trouble too and
  stop paging as errors (they were never in the pattern list either).
- Convention going forward: **a tool-side validation guard raises
  `UserInputError`**, never bare ValueError — filtering is then automatic.

## Consequences

- Bad LLM/tool input in production stays visible as breadcrumbs and
  analytics (`status: error` in the tool log) but no longer creates Sentry
  error events — the error stream regains signal.
- Genuine ValueErrors (no type, no pattern match) still report — the
  regression test pins that.
- 820 tests pass. Patch release 1.19.4.
