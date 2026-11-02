# 064 — Latency and reply size

## Context

The local analytics log (every call since March) showed that YouTrack itself
is not what is slow: the median call takes about 100 ms and the 95th
percentile 300–500 ms. The waiting came from six other places.

1. **Sequential writes.** `apply_translations` (median 5.9 s, worst 18 s),
   `bulk_update_execute` and `bulk_rollback` (about 2.6 s) processed one
   issue at a time, with 2–6 round trips each.
2. **No retry on refusal.** The client raised on the first 429 or 503.
   This made parallel writes unsafe to add.
3. **Tag-pinned launches.** A `git+…@vX.Y.Z` spec makes uv resolve the tag
   against GitHub on every spawn: 1.7 s on each start, against 0.3 s for a
   full commit SHA (measured). The server had started over 2,600 times.
4. **Very large replies.** `get_issues` returned up to 87K characters and
   `get_article` up to 78K. The model reads all of it, which costs more time
   than the call, and clients reject replies over their own output limit.
5. **Opaque query errors.** YouTrack gives the same "Can't parse search
   query" for bad syntax, an unknown value, and a project that lives on
   another configured instance. One session retried the wrong-instance case
   a dozen times, because nothing in the error pointed there.
6. **Uncached project list.** Every create and command path downloaded the
   full project list again to resolve a project's short name.

## Decision

- **Bounded fan-out** (`fanout.bounded_map`):
  - Per-issue write loops run up to 4 issues at once (below the 5-connection
    pool) and report in input order.
  - An exception surfaces only after every item has finished, so no write
    keeps running unobserved.
  - Bulk keeps its two phases: every issue is tagged before any is changed,
    because the tag is what rollback finds.
  - `apply_translations` falls back to one at a time when the same issue
    appears twice, since parallel read-modify-writes of one issue would race.
- **Retry 429 and 503 only:**
  - Up to 3 attempts, honoring `Retry-After` (capped at 10 s), with a
    0.5 s / 1.5 s backoff otherwise.
  - Both statuses mean the request was not applied, so the retry is safe for
    POST.
  - Other 5xx responses may have been applied and are never retried.
- **Pin the commit SHA:**
  - `release.sh ship` pins `--from …@<sha of the tag>`.
  - Its `--version` run also warms uv's cache for the first real spawn.
- **Reply budgets:**
  - `get_issues` JSON caps each description and comment at
    `description_chars` (default 2000; 0 = full), with a marker giving the
    dropped length.
  - `get_article` returns long content in parts (`offset`, `max_chars`,
    default 30000). Each part names the offset that continues it, and
    comments follow the last part.
  - A last-resort cap (`YOUTRACK_MAX_RESPONSE_CHARS`, default 60000; 0
    disables) cuts any other text reply at a line break, with a marker.
    It never cuts JSON, since a cut document is invalid, and never cuts
    `get_issues_for_translation`, whose text is written back.
- **Query-parse hints** (`query_hints`):
  - On that one error, check which configured instance has each `project:`
    in the query, using the cached list, and add the common syntax causes.
  - The lookup is time-boxed. Any failure in it re-raises the original
    error unchanged.
- **Project list cache** (`list_projects_cached`, 5 min):
  - A miss against a cached list refreshes once, so a project created a
    minute ago still resolves.
  - A failed fetch is not cached.

## Consequences

- Translation and bulk batches are expected to finish about 4× faster.
  Session start is about 1.4 s faster per server spawn.
- A long article or batch now takes an extra call to read in full. That is
  the intent: the reply says exactly which call.
- `~/.claude.json` holds a SHA rather than a tag. The `/mcp` label still
  carries the version (ADR-011).
- Tests in `tests/test_performance.py` pin each guarantee. Every one was
  checked against a mutant of the code it guards: removing the retry, the
  bound, the tag-first order, the duplicate-issue fallback, the JSON and
  translation exemptions, or the refresh-on-miss each fails a test.
