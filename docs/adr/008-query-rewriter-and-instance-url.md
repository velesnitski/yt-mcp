# 008 — Query auto-rewriter + instance-URL exposure

## Context

Two papercuts surfaced in real downstream use:

**E. OR-syntax footgun.** The natural way to express "any of these"
in a YT query is `summary: X OR summary: Y OR summary: Z`. YouTrack
rejects this with a generic 400 "Can't parse search query" — no hint
that the correct idiom is the comma-list `summary: X, Y, Z`. A
downstream generation flow burned ~10 wasted tool calls before
landing on the right form by guesswork. The same footgun was found
earlier in handoffs.py (v1.11.1 fix) and resolved with
`build_state_clause` — but that was internal-only. External callers
typing queries by hand still hit the wall.

**F. Hidden instance URL.** Consumers building issue hyperlinks
(`<base>/issue/<ID>`) need the YouTrack host. The MCP knows it
(it's in the client config), but never surfaces it. A downstream
report-rendering flow ended up probing candidate hostnames externally
to find the right one — wasteful and brittle.

## Decision

### E. Auto-rewrite OR clauses in `client.py`

New helper `formatters.rewrite_or_clauses(query: str) -> (str, [changes])`:

- Detects `<prefix>: x OR <prefix>: y [OR <prefix>: z]` (same-prefix,
  case-insensitive `OR`).
- Rewrites to `<prefix>: x, y[, z]`.
- Preserves the original prefix casing from the first clause.
- Bails on braces/quotes/parens — those indicate structure
  (wrapped values, quoted literals, nested groups) where auto-merge
  could change semantics.
- Bails when prefixes differ (`summary: x OR state: y` is a real
  cross-field disjunction, not a footgun).
- Returns the rewrites as a list of strings for logging — never
  user-visible.

Wired into `client.py.get` via a `_preprocess_query_params` helper.
Applied to **every GET that carries a `query` param**, so every tool
benefits automatically — search_issues, count_issues, get_issues_digest,
and any future query consumer. POSTs are not preprocessed (their
`query` field is a command syntax, not a search filter).

Rewrites are logged at INFO level (successful intervention, not error).
No user-facing surfacing — silent fix, the operator just sees the
right result. If they care, the log shows what changed.

### F. Expose instance URL

Two pieces, ship both:

1. **New tool `get_instance_url(format="report", instance="")`** —
   returns the base URL. Cheap, no API call (just reads from the
   resolved client config). `format="json"` returns
   `{"base_url": "..."}` for programmatic consumers.

2. **`get_current_user`** gains `format="report"|"json"`. JSON shape
   includes `instance_url` alongside login/name/email so the
   who-am-I caller doesn't need a second probe. Default `format="report"`
   adds an `Instance:` line to the markdown render — visible without
   breaking existing scrapes (it's appended, not interpolated).

The `YouTrackClient.base_url` property is the canonical surface
(replaces the prior `client._config.url` private-poking) — also fixes
a layering issue for any other tool that wants it.

## Alternatives considered

- **Error-only hint (no auto-rewrite).** Catch the 400 and surface a
  helpful "try comma-list" message. Saves the round-trip cost but
  still costs the round-trip itself and forces the caller to retry
  manually. Auto-rewrite is strictly better when the rewrite is
  safe — which the conservative bail-out rules ensure.
- **Tool-level rewrite vs client-level.** Wiring in `client.py` means
  every present and future tool benefits without remembering. The
  alternative was applying it in each of `search_issues`,
  `count_issues`, etc. — that's where the original footgun lives,
  but new tools would have to remember to wrap too.
- **Aggressive parsing of complex queries.** Could in theory handle
  `(A OR B) and C` by recursing into the paren group. Skipped:
  the failure mode is silent semantic change, which is much worse
  than the current "leave it alone, let the caller see the YT
  error." Conservative bail keeps the rewrite trustworthy.
- **Augment only `get_current_user` (skip the standalone
  `get_instance_url`).** Forces an auth-bearing call for what's
  fundamentally a config read. The standalone tool is ~5 LOC and
  removes a meaningful friction for cron-style renderers.

## Consequences

- Tool count: 76 → 77 (`get_instance_url` is new).
- Test count: 554 → 577 (+23 covering rewrite edge cases —
  same-prefix merge, different-prefix bail, braces/quotes/parens bail,
  case preservation, empty input — plus `_preprocess_query_params`
  end-to-end, `get_instance_url` report+json, `get_current_user`
  JSON includes `instance_url`).
- All existing query callers gain the rewrite for free; no per-tool
  changes needed.
- `YouTrackClient.base_url` is a new public property — supersedes
  `client._config.url` poking in any tool that needs the host.
- Minor version bump (1.11.3 → 1.12.0) — new tool + new public
  property + new behavior under existing tools (rewrite). All
  additive, no breaking changes.

### Pattern note

The `format="report"|"json"` convention has now reached two more
tools (`get_current_user`, `get_instance_url`). Six tools now use
the shared shape — see ADR-007's table.