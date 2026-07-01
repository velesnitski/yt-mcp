# 015 — Fix a regex-transform mangle in the null-object sweep

## Context

The ADR-014 null-object hardening (`24c38c5`, v1.16.4) applied a
regex transform:

```
obj.get("X", {}).get(...)  ->  (obj.get("X") or {}).get(...)
```

via `re.sub(r'\b(\w+)\.get\((".*?"),\s*\{\}\)\.get\(', r'(\1.get(\2) or {}).get(')`.

That transform mangled exactly one line — `monitoring.py:852`, in
`check_task_creation`'s subtask-count loop. The original:

```python
if link.get("direction") == "OUTWARD" and "subtask" in link.get("linkType", {}).get("name", "").lower():
```

became:

```python
if (link.get("direction") == "OUTWARD" and "subtask" in link.get("linkType") or {}).get("name", "").lower():
```

Two things went wrong at once:

1. **Over-spanning `.*?`.** Python's `re` takes the *leftmost* match. The
   leftmost `\w+\.get("` on the line is `link.get("direction"`. From there
   the non-greedy `(".*?")` extended past `"direction") == "OUTWARD" and
   "subtask" in link.get("linkType"` to reach the *next* `", {})` — the one
   belonging to `linkType`. So the captured "receiver" and the wrap
   boundaries were both wrong.
2. **Precedence.** The resulting `(A == B and "subtask" in C or {})` parses
   (via `in`/`and`/`or` precedence) as `(A == B and ("subtask" in C)) or
   {}`, then `.get("name", "").lower()` is called on a *bool or dict* — so
   it both fails to guard the null (`"subtask" in None` → TypeError when
   `linkType` is null) and adds an AttributeError on the truthy branch.

It slipped through because: (a) it's syntactically valid Python (707 tests
+ AST parse passed), and (b) `check_task_creation`'s inline subtask loop
had no test exercising a null/`Subtask` link. The other 28 transform sites
were single-`.get` lines with no intervening operators, so none over-spanned.

Found by a cross-repo audit against `youtrack-reports`, whose equivalent
line was hardened by hand and stayed correct.

## Decision

- Re-parenthesize to the intended form:

  ```python
  if link.get("direction") == "OUTWARD" and "subtask" in (link.get("linkType") or {}).get("name", "").lower():
  ```

- Re-audit the **entire** `24c38c5` diff for the same failure mode with two
  discriminators: (a) any `( … == / and / in … or {})` wrap, and (b) any
  wrap spanning two `.get(` calls. Result: **only line 852** was affected.

- Add a driving regression test for `check_task_creation` with an OUTWARD
  link whose `linkType` is null (would TypeError on the mangled line), plus
  a happy-path test that a real `Subtask` link still counts — coverage the
  original sweep lacked for this inline occurrence.

## Consequences

- `check_task_creation` (and any digest path through that loop) no longer
  crashes on an issue with an OUTWARD link that has a null `linkType`.
- Tests: 707 → 709 (+2 in `test_at_risk.py`).
- Patch bump 1.16.4 → 1.16.5.

### Lesson

A bulk regex code-transform is not safe just because the result parses and
the suite is green. Non-greedy `.*?` will happily span across a whole
expression to satisfy the pattern at the *leftmost* anchor. When a transform
edits N sites, audit the diff for the transform's own failure signature —
here, a wrap containing operators or a second `.get(`. Prefer transforms
anchored so the receiver can't be a compound expression (e.g. forbid `(`,
`=`, keywords inside the captured group), or do the risky ones by hand.
