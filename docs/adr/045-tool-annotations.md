# 045 — MCP tool annotations, derived from the write-tool set

> **Superseded by [ADR-046](046-annotations-declared-at-definition-site.md) and [ADR-047](047-hints-literal-in-source.md).** The classification here still
> holds and is enforced by tests; only the *place* the values are written has
> changed — from a registration pass to each decorator.


## Context

An external MCP audit scored this server and flagged that ~80 of its tools
advertise no annotations. The finding is correct: the protocol defines four
hints (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`)
that let a client reason about a tool *before* calling it — gate mutations
behind confirmation, parallelize reads, retry safely after a timeout — and
this server sent none of them.

Two other findings from the same audit did not survive verification. "No
test files detected" is false for the repository (31 files, 900+ tests); the
scanner almost certainly read the built wheel, which by default packages the
importable module only. And a try/except-density metric penalizes this
codebase for a deliberate design: errors are raised as typed exceptions that
the framework converts into protocol errors, so wrapping each handler would
add noise and swallow structure, not safety. One finding worth fixing, one
worth correcting, one worth declining.

## Decision

Annotate every tool in a single pass in `register_all` rather than passing
84 decorator arguments. The classification is a property of a tool's role,
not of its call site, so it belongs where the role sets already live:

- **`readOnlyHint` is derived from `WRITE_TOOLS`**, not restated. Every
  mutating tool must already be registered there for read-only mode, so a
  new write tool cannot become read-only through omission.
- **`DESTRUCTIVE_TOOLS`** — deletes, rollbacks, link removal, and bulk field
  overwrite. Read-only tools are always `destructiveHint=False`.
- **`NON_IDEMPOTENT_TOOLS`** — creates and appends, where calling twice is
  not the same as calling once. Everything else converges on one state.
- **`openWorldHint=True` everywhere**: every tool reaches an external
  instance whose contents change independently of this server.

Packaging: the source distribution now ships `tests/`, `docs/` and
`scripts/`, so an auditor reading the published artifact sees what a reader
of the repository sees.

## Consequences

- Clients can distinguish a search from a delete without calling either.
- Guard tests assert the derivation itself: every tool annotated, read-only
  exactly matching the complement of `WRITE_TOOLS`, no read-only tool marked
  destructive, and both role sets subsets of `WRITE_TOOLS` (a typo in either
  would otherwise annotate nothing silently). 910 tests pass.
- Minor release 1.24.0. No behavior change — annotations are advisory
  metadata.
