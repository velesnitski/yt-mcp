# 013 — Null-vs-missing safety in format_issue_detail

## Context

Production Sentry (yt-mcp@1.16.0): `get_issue` crashed with

```
TypeError: 'NoneType' object is not subscriptable
  File "yt_mcp/formatters.py", line 333, in format_issue_detail
    text = c.get("text", "")[:200]
```

on an issue whose comment had `"text": null`. The bug is a classic
**null-vs-missing** confusion: `dict.get("text", "")` returns the default
`""` only when the **key is absent**. YouTrack serializes empty fields as
the key *present* with a `null` value (a comment with only an attachment,
or certain system-generated comments, carry `text: null`). So `.get`
returned `None`, and `None[:200]` raised.

The same idiom was used unsafely throughout `format_issue_detail` — the
one line that fired was just the first reachable instance. The whole
"key present, value null" class was latent:

- `c.get("text", "")[:200]` → subscript crash (the one that fired)
- `data.get("summary", "")` appended to a list then `"\n".join(...)` →
  join crash on a null summary (compact path)
- `c.get("author", {}).get("name", ...)` → `.get` on None → AttributeError
- `link.get("linkType", {}).get(...)` → same
- `data.get("links", [])` / `data.get("tags", [])` /
  `data.get("comments", [])` → `for x in None` → not-iterable crash

## Decision

Replace `.get(key, <default>)` with `.get(key) or <default>` everywhere a
null value would be unsafe in `format_issue_detail` and `format_issue_list`:

- string slice / join targets: `(c.get("text") or "")`, `data.get("summary") or ""`
- dict access: `(c.get("author") or {})`, `(link.get("linkType") or {})`
- iteration targets: `data.get("links") or []`, `... tags ... comments ...`,
  `link.get("issues") or []`

`or <default>` collapses both "key missing" and "key present but null" to
the safe default; the `.get` default only handles the former.

The JSON path (`normalize_issue`, added later) was already written this
way and needed no change — confirming the fix belongs to the older
markdown renderer, not the data layer.

## Alternatives considered

- **Guard each access with `if x is not None`.** Verbose and easy to miss
  one; the `or default` idiom is uniform and self-documenting.
- **Sanitize the YouTrack response once on ingest.** Heavier, and would
  have to anticipate every nullable field; defensive rendering at the
  point of use is simpler and local.
- **Pydantic-model the issue.** Out of proportion for a formatter.

## Consequences

- `get_issue` (report mode) no longer crashes on null comment text /
  author / summary / linkType, or on null tags/links/comments lists.
- Null fields render as empty, not the literal string "None".
- Tests: 699 → 703 (+4 in `tests/test_formatters.py`): a repro issue with
  null text/author/linkType/summary across both COMPACT and non-compact
  paths, plus a null comments/links/tags-list case. The compact repro is
  the exact production payload.
- Patch bump 1.16.1 → 1.16.2.

### Lesson

`dict.get(k, default)` and `dict.get(k) or default` are **not**
interchangeable against a JSON source that emits explicit nulls. For any
external payload where a field can be `null`, prefer `or default` at the
point of use.
