# 014 — Codebase-wide null-object hardening sweep

## Context

ADR-013 fixed a production crash in `format_issue_detail` where a comment
with `"text": null` hit `c.get("text", "")[:200]` → `None[:200]`. The root
cause — **`.get(key, default)` returns the default only when the key is
*absent*, not when it's present with a `null` value** — is a *class*, not a
one-off. YouTrack emits explicit nulls for empty objects (`author`,
`linkType`, `field`, `project`, `state`), so the same shape was latent
anywhere the code did `obj.get("X", {}).get(...)` or sliced a `.get()`
result.

A direct sweep found the siblings:

- **28 `obj.get("X", {}).get(...)` chains** — crash if `X` is null. The
  nullable objects are real: `author` (system/imported comments &
  activities — exactly PROJ-280), `linkType` (proven in the ADR-013 fix),
  `field` (activity entries), plus `project` / `columnSettings`.
- **2 subscript-on-`.get()` sites** — the literal PROJ-280 shape:
  `p.get("summary", "?")[:80]` (summary can be null) and
  `prev.get('ts', '?')[:10]`.

(Collection iteration — `for x in obj.get("Y", [])` — was assessed and
left alone: YouTrack returns empty arrays `[]`, not `null`, for collection
fields, and the hot paths already guard with `or []`.)

## Decision

Apply the same always-safe transform the ADR-013 fix used, uniformly:

```
obj.get("X", {}).get(...)   →   (obj.get("X") or {}).get(...)
obj.get("X", "?")[:N]       →   (obj.get("X") or "?")[:N]
```

`or default` collapses both "key missing" and "key present but null" to the
safe default; semantics are unchanged for the dict/missing cases. Done as a
reviewed regex transform across `tools/*.py` + `scoring.py` (29 chains,
two-pass to catch the one nested `columnSettings → field` case), plus two
hand edits for the subscript sites. Every changed file was AST-parsed and
the full suite re-run.

## Alternatives considered

- **Fix only the proven-nullable keys (`author`/`linkType`).** Leaves
  `field`/`project` latent; the uniform transform is the same cost and
  kills the whole class.
- **A shared `safe_get_name(obj, key)` helper + migration.** More churn
  for no extra safety; the `(x.get(k) or {})` idiom is already the
  house style (normalize_issue, the ADR-013 fix).
- **Sanitize the YouTrack response on ingest.** Heavier and must
  anticipate every nullable field; defensive access at the point of use
  is local and simpler.

## Consequences

- 31 latent crash sites across 11 modules (`history`, `monitoring`,
  `comments`, `translate`, `impact`, `scoring`, `issues`, `journey`,
  `projects`, `bulk`, `articles`) hardened against null `author` / `field`
  / `linkType` / `project` / `summary` / `ts`.
- The `get_issue` crash family (the one still showing in Sentry from old
  builds) is now closed at the source *and* across every sibling tool, so
  no new variant should surface.
- Tests: 703 → 707 (+4 in `test_scoring.py`): null `linkType` on the
  link-count helpers and `_gather_subtask_ids`, null `project`/`field` on
  `_build_journey`. The formatter path already had its ADR-013 repro.
- Patch bump 1.16.3 → 1.16.4.

### Lesson (reinforced)

Against any JSON source that emits explicit nulls, `dict.get(k, default)`
is **not** a null guard. Use `dict.get(k) or default` at the point of use.
A single production crash of this shape is a signal to sweep the whole
class, not patch the one line.
