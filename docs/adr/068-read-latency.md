# 068 — Read latency: count endpoint, parallel lookups, cached user

## Context

Measured against YouTrack Cloud, HTTP/2 multiplexes well: 16 concurrent
requests completed in 0.16 s, while 4 sequential ones took 0.39 s. The
slow read tools were slow because they waited on one request at a time, or
fetched more than they needed:

- `count_issues`, the most-called tool, downloaded up to 500 issue IDs only
  to count them. It stopped at "500+".
- `deadline_scorecard` made two requests per issue, eight at a time. It
  first ran three independent lookups one after another.
- `get_my_mentions` ran two independent searches in sequence.
- `get_impact_map` walked the link graph one issue at a time.
- `add_issues_to_sprint` applied its command to one issue at a time.
- The current user was fetched again by every tool that needed it.

## Decision

- **Exact counts.** `count_issues` uses `POST /api/issuesGetter/count`.
  - The endpoint answers `-1` while YouTrack is still counting. The tool
    polls briefly (0.1–1 s), then falls back to the old listing.
  - A 404 means a server without the endpoint and also falls back.
  - A query error is raised as before, so the query hints (ADR-064) still
    apply.
  - The OR-clause rewrite that GET queries get is applied to the POST body.
- **Independent lookups run together** (`asyncio.gather`):
  - The scorecard's operator, issue search and deadline-field probe.
  - The two mention searches, merged in the old order so a duplicate keeps
    its first copy.
- **Bounded fan-out for reads.** `READ_CONCURRENCY = 16` sits beside the
  write bound of 4. The deadline fetcher's default concurrency rises from
  8 to 16. The client already retries 429 and 503.
- **Level-parallel impact walk.** The graph is walked breadth-first, with
  each level fetched in parallel and recorded in queue order. It visits the
  same issues, in the same order and to the same depth, as the one-by-one
  walk.
- **Parallel sprint assignment.** Issues are added in parallel (bounded) and
  reported in input order.
- **The current user is cached per client** (`lookups.current_user`). A
  token cannot change owner. A failed lookup is not cached.
- The helpers use only the client's public `get` and `post`, so every test
  double that fakes those keeps working.

## Consequences

- `count_issues` is exact beyond 500 and transfers one number, not 500 IDs.
- Scorecard, mentions, impact and sprint assignment are bounded by their
  slowest level, not by the sum of their requests.
- `tests/test_perf_reads.py` pins each behavior, and each was checked
  against a mutant of the code it guards. `tests/conftest.py` now isolates
  every test from `~/.yt-mcp`.
