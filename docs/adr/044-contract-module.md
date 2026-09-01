# 044 — contract.py: one home for the API-contract quirks

## Context

The quirk registry (`docs/YT_QUIRKS.md`, Q1-Q17) documents seventeen ways
this API is silently wrong under the obvious implementation — a query that
400s, or worse, one that parses and returns nothing. Each entry names a
guard, but the guarded code was scattered: date-range construction in the
pulse module, the comma-list idiom inline at two call sites, shape readers
in formatters, the release-shape regex in the releases module.

Two costs followed. Internally, a new tool author has no single place to
look, so quirks get rediscovered per module (the release calendar rebuilt
its own date-range handling; the sprint tool did not know about the
selector-silently-ignored class at all). Externally, any separate consumer
of this API must mirror the same rules by hand, and an upstream break has
already been fixed twice, independently.

## Decision

New module `src/yt_mcp/contract.py` holding the quirk encodings:

- **Query construction:** `build_date_range` (Q1), `resolved_window_query`
  — which puts the range clause first by construction (Q2),
  `project_clause` comma-list (Q17), `escape_query_value`.
- **Response shapes:** `linked_state` (Q3), `custom_field` with exact-name
  matching including emoji (Q9), `split_service_comments` (Q10),
  `is_release_ticket` shape filter (Q11), `with_fields`/`CREATE_FIELDS`
  for entity-creating POSTs (Q16).

Existing modules delegate to it rather than keeping copies, so there is
one implementation and every current call site keeps working unchanged.

The module is **stdlib-only and synchronous** — no httpx, no MCP SDK, no
async — and a test asserts that property. That is what makes it importable
by a consumer that cannot take the server's dependency tree, which is the
prerequisite for retiring hand-mirroring later. Whether any consumer
actually takes that dependency is a separate decision, made where its
supply-chain implications are visible; this change is worth making on its
own for the single-home property.

Scoring stays where it is: mirroring it has caught defects before they
shipped downstream, and a dependency would invert that into inheriting
them.

## Consequences

- One auditable place for "how do we talk to this API", directly indexed
  by the registry IDs.
- 902 tests pass (34 new): one guard per quirk, plus the
  no-heavy-dependency assertion.
- Minor release 1.23.0. No behavior change — pure consolidation.
