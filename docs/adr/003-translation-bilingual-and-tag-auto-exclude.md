# 003 — Translation: auto-exclude prior batch tags & detect already-bilingual

## Context

Live use of the translation tooling surfaced two repeating frictions:

1. **Tag exclusion list grew unbounded.** Each `apply_translations` call
   stamps a tag like `yt-translate-1778601757`. To avoid re-fetching the
   same cards on subsequent runs, operators had to maintain a manually
   curated, comma-separated exclusion clause that grew with every batch
   (24+ entries across a long session). Error-prone, bloats prompts, and
   one missing tag silently triggered re-translation.

2. **Already-bilingual cards got triple content.** Once a card was
   translated, its description was `EN + ---- + RU`. Future runs still
   matched `_has_non_ascii(desc)` because of the Russian portion below
   the delimiter. Re-running `apply_translations` with
   `preserve_original=true` prepended a fresh English translation and
   appended the entire current description — producing
   `EN-new + ---- + EN-old + ---- + RU`. Operators worked around this by
   eyeballing each fetched batch and manually skipping the bilingual ones.

## Decision

### Auto-exclude prior translation tags

`get_issues_for_translation` gains `exclude_translated: bool = True`.

When true (default) and the caller did not include their own `tag:`
clause, the tool appends `tag: -yt-translate-*` to the query. YouTrack's
tag-query syntax supports wildcards, so this single token excludes every
prior batch regardless of how many ran.

If the caller has their own tag filter (`tag: critical`, etc.), the
auto-append is skipped — respects operator intent.

Set `exclude_translated=False` to force re-translation of previously
tagged cards.

### Detect already-bilingual descriptions

Two pure helpers added:

- `_split_bilingual(desc, delimiter)` — split on a standalone delimiter
  line, returns `(top, bottom)`. Inline `----` mid-text is ignored.
- `_is_bilingual(desc, delimiter)` — true when both `top` and `bottom`
  are non-empty and `bottom` contains non-ASCII characters. False
  positives only cause an unnecessary skip — never data loss.

In `get_issues_for_translation`: when an issue's summary is already
English but its description is bilingual, the issue is skipped and
counted under `already bilingual` (separately from `already English`)
in the output banner. If the summary still needs translation, the
issue is still included even when the description is bilingual.

### Smart-merge in `apply_translations`

When `preserve_original=true` and the current description is already
bilingual, the tool extracts the original-language portion via
`_split_bilingual` and constructs:

```
{new translation}
\n\n----\n\n
{original-language part of current desc}
```

This replaces the English section in-place rather than appending the
whole bilingual blob — eliminating the triple-content trap.

## Alternatives considered

- **Comma-separated tag fetching at call time.** Fetch all
  `yt-translate-*` tags from `/api/issueTags` and build an explicit
  exclusion list per call. Reliable but adds one API round-trip and
  doesn't scale well if 100+ batches accumulate. Wildcard is simpler.
- **Re-translating bilingual cards rather than skipping.** Would waste
  tokens, churn timestamps, and risk overwriting hand-curated
  translations. Skipping is the right default.
- **Stricter heuristic for `_is_bilingual` (ratio of ASCII chars in
  top section).** Adds complexity for marginal accuracy gain. The
  current rule "non-empty top + non-empty bottom + bottom has
  non-ASCII" handles the practical cases without false negatives.

## Consequences

- Test count: 300 → 319 (+19 new tests across pure helpers and
  end-to-end via mocked client). All passing.
- Operators no longer maintain tag-exclusion lists for the translation
  flow. The wildcard handles it automatically.
- Re-running `apply_translations` against an already-bilingual issue
  is now idempotent in shape — produces the same `EN + ---- + RU`
  format, not triple-content.
- The summary-still-Russian + bilingual-description case is correctly
  included for translation (only the summary needs work). This is a
  real case from production data.
- Backward compatible: passing `exclude_translated=False` restores the
  pre-change behavior for any caller that depended on it.
