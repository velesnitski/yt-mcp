# 048 — Resolve work-item types by name, not by shape

## Context

Logging time with a work type has never worked. The payload sent the type
as `{"name": ...}`, and this endpoint will not look a WorkItemType up that
way — it answers "unable to locate a WorkItemType-type entity unless its ID
is also provided". An outside contributor hit it on their instance, traced
it precisely, and proposed sending `{"id": ...}` when the value *looks
like* an id: a leading digit plus a hyphen somewhere.

Two problems with deciding by shape.

The test is looser than the id format. `251-9` matches, but so do ordinary
type names — `1-on-1`, `24-7 oncall`, `2-factor rollout`. Six of ten
realistic names tried against it were misrouted, each becoming an id that
resolves to nothing or, worse, to a different type.

More fundamentally it fixes the rarer direction. The caller here is a
model. It knows `Development` because that is what the UI shows; it cannot
know `251-9`, and a docstring recommending an id invites it to invent one
that means something else on another instance. Accepting ids serves the
caller who already has the answer and leaves the common path broken.

## Decision

Resolve the name ourselves. `_work_type_payload` takes what the caller
supplied and returns the reference to send:

- a value matching `^\d+-\d+$` — the actual entity-id format — passes
  through as `{"id": ...}` with no lookup;
- anything else is matched case-insensitively against
  `/api/admin/timeTrackingSettings/workItemTypes`, and the matched id is
  sent;
- no match raises `UserInputError` naming the valid types, so the caller
  gets the answer rather than a 400.

Reading that catalogue needs admin rights, so a refusal falls back to
sending the name — no worse than before, and a low-privilege token keeps
working for every other field. The same fallback covers a response that
is not the documented list: a refusal can arrive shaped as a dict or a
string, and iterating that used to raise `AttributeError` mid-write.

The docstring leads with the name, since it is the model's prompt.

## Consequences

- `add_work_item(work_type="Development")` works, which it never did.
- One extra GET only when a name is supplied; ids and omissions skip it.
- 929 tests. The guards are mutation-verified: loosening the id pattern,
  dropping the list check, or removing the whitespace strip each turn tests
  red. A first mutation run passed vacuously because the edit silently
  failed to apply — mutations now assert their anchor exists first.
- Credit to the contributor for the diagnosis and the reproduction.
