# 021 — Field-aware command split + gate-aware `transition_issue`

## Context

Two gaps left open by ADR-019, both hit live:

1. **The split fallback garbles multi-word / emoji field NAMES.**
   `_CMD_FIELD_RE` assumes a field name is one token. Real projects have
   "Dev Estimation", "Phase detected", "Evaluation time 🕙" — the split
   turned `Evaluation time 🕙 3d` into `Evaluation time` + `🕙 3d`
   (→ `400 Unknown command`), silently dropping the estimation whenever the
   whole-command attempt failed. This also made creates with many required
   fields fragile: the combined command doesn't parse (verified live), so
   everything rides on the split being correct.

2. **No gate-aware way to change State.** Workflow scripts gate transitions
   ("set Dev Estimation before To Do", "укажите Assignee") and fail one
   opaque 400 at a time. Feedback ("двигать задачи на доске — отдельный вид
   пыток", PROJ release tasks) traced directly to this: no tool could set the
   gate's fields and transition in one legible step.

## Decision

### Field-aware split

`_split_command_with_field_names(command, field_names)` splits a multi-field
command using the **project's real field names** as clause boundaries:

- Names fetched once per create via `_get_project_field_names`
  (`/api/admin/projects/{id}/customFields`, permission-filtered per ADR-018;
  best-effort, `[]` on failure).
- Longest name (in words) matches first, so "QA Estimation" wins over any
  shorter overlap.
- `{braces}` remain the input-only grouping convention: a braced token is
  always a value, never a boundary, and braces never reach YT (ADR-019).
- A new clause only opens when the current one already has a value, so an
  unbraced value that starts with another field's name ("Type Product task",
  where "Product" is also a field) is not mis-split.
- Returns `[]` when nothing matches → caller falls back to the regex split.

`create_issue` computes the split lazily (only when the whole-command
attempt fails) and caches it across the direct and draft paths.

### `transition_issue` (new tool, WRITE_TOOLS, 78 → 79)

`transition_issue(issue_id, state, set_fields="")`:

1. Reads the issue; detects the project's real state field name — "State" on
   dev boards, "Status" on HR-style projects — commands need the real name.
2. Applies `set_fields` clause-by-clause via the field-aware split (regex
   fallback), collecting per-clause failures instead of aborting.
3. Attempts `<StateField> <state>` (braces stripped; bare per ADR-019).
4. On a gate: returns the workflow rule's **own text** + current state +
   fields set / not set + a `set_fields` retry hint. Values are never
   invented — the tool makes the gate legible, it does not defeat it.
5. On success: re-reads the state and reports `old → new`, flagging when a
   workflow redirected the transition to a different state than requested.
6. Accepts internal ids (digits-digits, e.g. drafts) as well as readable ids.

## Live validation (ephemeral PROJ draft, deleted after)

- Split with real PROJ field names: `Dev Estimation 2h · QA Estimation 1h ·
  Evaluation time 🕙 3h · Type Product task` — all four clauses intact.
- Blocked path: gate text surfaced verbatim ("Перед переводом в To Do укажи
  Dev Estimation…"), current state and hint included.
- `set_fields` path: all three estimations set (emoji name included); the
  *next* gate ("…Укажите Assignee") surfaced cleanly — gates peel one at a
  time with exact rule text.

## Consequences

- Emoji/multi-word field names survive the split; multi-required-field
  creates are no longer at the mercy of the regex.
- Gated state changes become one legible call instead of a 400-guessing
  loop. The YouTrack-side cure for release tasks (a Release type/tag
  exempted from gates/nags) remains a workflow-admin change, tracked in the
  feedback thread — this tool is the client-side half.
- Tests 725 → 739: pure-function split coverage (emoji names, brace
  atomicity, ambiguous-prefix guard, case-insensitivity, fallback), a
  create_issue integration test proving the emoji clause survives, and 6
  transition_issue tests (success, gate text, set_fields ordering,
  Status-field detection, partial set_fields failure, brace stripping).
  Older create_issue harnesses gained an async `client.get` (empty field
  list → regex fallback) to keep exercising the pre-existing path.
- Minor version bump 1.16.9 → 1.17.0 (new tool).
