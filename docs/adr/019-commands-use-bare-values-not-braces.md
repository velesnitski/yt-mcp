# 019 — create_issue commands use BARE values (reverses ADR-016)

## Context

ADR-016 (v1.16.6) changed the `create_issue` split fallback to **re-wrap**
multi-word values in braces (`Assignee {Jane Doe}`), on the stated premise
that a bare `Assignee Jane Doe` makes YouTrack "read only the first token."

Live testing against the production instance proves that premise **false** and
the change a **regression**. YouTrack's *command* parser (`/api/commands`):

- **Rejects braces in values**, for multi-word *and* single-word, every field
  and project:
  - `Status {New Employee}` → 400 `Status expected: {New Employee}`
  - `Subsystem {Client Panel}` → 400
  - `Priority {High}` → 400
- **Accepts bare values and matches enum values greedily**:
  - `Status New Employee` → ✅ sets "New Employee"
  - `Subsystem Client Panel` → ✅
  - `Priority High` → ✅

Braces are **search-query** syntax (`State: {In Progress}` in a *query*), not
command syntax. ADR-016 conflated the two. The original code stripped braces
in the split (emitting bare, which works); ADR-016 made it emit braces (which
400). The bug hid because the unit tests mocked `/api/commands` to accept any
string — they never saw YT reject braces — and PROJ-110 "verified" only because
"New Employee" is the HR project's default Status.

Discovered while setting a Period estimation (`Evaluation time 🕙 3d`), where
every braced form failed.

## Decision

- **Split fallback emits bare** `f"{name} {value}"` (revert the ADR-016
  re-wrap). `_CMD_FIELD_RE` group 2 already excludes the braces, so a
  brace-delimited multi-word input is captured whole and sent bare.
- **Strip braces from the whole-command attempt too** (`command.replace("{",
  "").replace("}", "")`) so braces never reach YT on any path — the as-is
  attempt can now actually succeed for simple/single-field commands instead of
  guaranteed-400ing on the braces.
- **Braces stay an INPUT convention.** Callers still wrap a multi-word value in
  `{…}` so `_CMD_FIELD_RE` captures it as one group (otherwise the regex would
  split `Client Panel` and drop `Panel`); the tool strips them before sending.
  The docstring example (`Type Task Subsystem {Client Panel}`) is therefore
  still correct.

`update_issue`, `add_issue_link`, and bulk already used bare commands; the
brace-wrapping in pulse/discovery/dashboard/impact is all in *search queries*
(valid there). So this is the only command path that needed the fix.

## Consequences

- `create_issue` sets multi-word State/Product/Assignee/etc. reliably again;
  braces never reach `/api/commands`.
- **Supersedes ADR-016.** The "brace preservation" it introduced is removed.
- Tests: the former `TestCreateIssueBracePreservation` is replaced by
  `TestCreateIssueBareCommandValues`, whose mock **rejects any braced command
  (mirrors real YT)** — the test that would have caught this. It asserts the
  split clauses reach YT bare and no braced command is ever sent. 720 pass.
- Live-verified independently: bare commands set values, braced commands 400.
- Patch bump 1.16.8 → 1.16.9.

### Still open (not a regression, documented)

The split cannot parse a multi-word/emoji field **name** (`Evaluation time 🕙`
splits into `Evaluation time` + `🕙 3d`). Such fields only set via the
whole-command path; if that path fails and the split runs, they are dropped.
A boundary-aware split would need the project's field list. Deferred.
