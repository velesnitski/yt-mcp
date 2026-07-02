# 020 — Regression guard: no braces in /api/commands queries

## Context

The ADR-016 → ADR-019 round-trip (braces wrongly added to commands, then
removed) was possible because the `create_issue` unit tests mocked
`/api/commands` to accept *any* string. They never modelled the one fact that
matters: **real YouTrack 400s on braces in a command.** So a mock-green suite
shipped a broken command format twice.

## Decision

Encode the invariant directly, so the class of bug can't return silently:

1. **The mock rejects braces like real YT.** `TestCreateIssueBareCommandValues`'
   `/api/commands` stub raises on any query containing `{`/`}`. Any code path
   that sends a braced command now fails the command in-test, exactly as it
   would in production.
2. **A parametrized invariant guard.** `test_no_command_query_ever_contains_
   braces` runs several braced input shapes (single/multi multi-word, braced
   single-word, multi-field) through `create_issue` and asserts that **no**
   query reaching `/api/commands` contains a brace — across both the
   whole-command and per-field-split paths — and that the field still sets
   (no false "Could not set").

The invariant is deliberately about the *wire* (`{`/`}` in the sent query),
not internal shapes, so it holds regardless of how the command pipeline is
refactored.

## Verification

The guard was mutation-checked (not vacuous): reverting either fix element
makes it fail —
- whole-command sent un-stripped (`whole = command`) → all 5 cases fail;
- split clauses re-braced (the literal ADR-016 form) → the split-path case
  fails.
Reverted, all pass.

## Consequences

- Test-only change; the shipped tool is unchanged (stays v1.16.9), so no
  version bump / uvx refresh.
- Suite 720 → 725.
- Any future command-building path that lets a brace reach `/api/commands` is
  now caught by a fast unit test instead of a live 400.
