# 047 — Spell the four hints out at every decorator

## Context

ADR-046 moved the annotations from a registration pass onto each
decorator, using two helpers:

    @mcp.tool(annotations=read_only())
    @mcp.tool(annotations=mutates(destructive=True))

That fixed the visibility problem in principle — the declaration sits at
the definition site — but a re-scan of the released version still reported
"84/84 tools missing one or more hints", naming tools whose live
`tools/list` response carries all four as booleans.

The re-scan is the useful data point. It counted 84 tools, so it was
reading the current version; and it could not see hints that a `tools/list`
call returns unambiguously. Both are only true of a reader that parses
source and does not execute it. Such a reader cannot follow
`read_only()` to its return value — the call is opaque without running it,
so a tool annotated that way is indistinguishable from one annotated with
nothing.

The lesson generalizes past this one vendor: the audience for the source
form is not only the maintainer. A registry, a directory review, or a
reviewer skimming one handler all read text. An indirection that is
obvious to someone who knows the codebase is invisible to all of them.

## Decision

Write the four hints out at every decorator, with the SDK's own type:

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True))

The helper module is removed: with the values inline there is nothing left
for it to define, and keeping it would leave two ways to express the same
thing. The verbosity is the point — each handler states its own contract in
terms any reader can see, without resolving a name.

Correctness no longer rests on a shared constructor, so the tests carry
more weight and were strengthened accordingly:

- the runtime result must still match the derivation from `WRITE_TOOLS`, so
  a hand-edited value that contradicts a tool's registered role fails CI;
- the source test now requires all four identifiers **literally present**
  in every decorator — the exact property a static reader depends on — and
  asserts it scanned at least 84 of them, so a drifting regex cannot make
  the check pass by matching nothing.

## Consequences

- Hints are legible to every consumer: a client calling `tools/list`, a
  scanner parsing source, and a human reading one function.
- `tools/list` output is unchanged for the third release running — this
  moves where the values are written, never what is advertised.
- 911 tests pass. Minor release 1.26.0.
