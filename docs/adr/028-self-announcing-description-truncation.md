# 028 — Self-announcing, link-safe compact-mode description truncation

## Context

`format_issue_detail`'s compact branch truncated an issue description with a
bare `desc[:500]`. Two defects, surfaced when an agent reading a spec issue
saw the description "end" at a Figma link and had to re-fetch:

1. **Silent.** No indicator that content was cut — the output read as if the
   description simply ended. A human or LLM can't distinguish "ended" from
   "truncated", so it either misses content or wastes a round-trip
   re-fetching to find out there was more.
2. **Naive slice.** A raw character cut at 500 can split a markdown link
   mid-URL (`[design](https://www.figma.com/fi`), producing a dangling,
   broken link — a Figma URL landing near the boundary triggers exactly the
   reported symptom.

Only the compact (`YOUTRACK_COMPACT`) render is affected; the non-compact
report branch and `format="json"` (`normalize_issue`) both return the full,
untruncated description.

## Decision

`_truncate_desc(desc, limit=500)`:

- Returns the text unchanged when within the limit.
- Otherwise cuts on the **last whitespace** within the window — which keeps a
  straddling link whole (a link has no internal spaces, so the boundary lands
  before it) — falling back to a hard cut only for a long unbroken token
  (a bare URL filling the window), so the text is never gutted to nothing.
- Appends a self-announcing marker with the **true total length** and the
  `format=json` hint: `_… truncated (N chars total) — use format=json for
  full_`.

## Consequences

- Compact-mode descriptions no longer masquerade as complete, and never emit
  a severed markdown link; the marker tells the reader precisely how to get
  the rest.
- No change to report(non-compact) or JSON modes (already full).
- Tests 784 total (+5 `_truncate_desc`: unchanged-when-short, exact-limit,
  announced-with-true-length, straddling-link-not-split, unbroken-token-hard-
  cut). Patch bump 1.18.1 → 1.18.2.
