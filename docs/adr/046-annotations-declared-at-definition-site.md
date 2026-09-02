# 046 — Declare tool annotations at the definition site

## Context

ADR-045 added the four protocol hints to every tool in a single pass at
registration, deriving read-only from the write-tool set. That is correct
and it is what clients receive: a `tools/list` over stdio returns 84 tools,
zero with missing or non-boolean hints.

An external audit nonetheless reported "no tools have annotations" after
the release. Two causes, and the second is the durable one. Its snapshot
predated the change (81 tools — a count last true six releases ago). But
the same report describes its method as static analysis of source, and a
runtime pass is invisible to that no matter how current the snapshot is.
The same limitation would apply to any registry that inspects a repository
rather than connecting to the server, and at least one directory is said
to reject tools whose hints are absent.

The protocol's answer is `tools/list`, and that answer was already right.
The problem is that a reader of the source could not see it — including a
human reading one handler and asking "is this destructive?".

## Decision

Each tool declares its own annotations on its decorator:

    @mcp.tool(annotations=read_only())
    @mcp.tool(annotations=mutates(destructive=True))
    @mcp.tool(annotations=mutates(idempotent=False))

The hint values still come from one place — `annotations.py` — so the
meaning of "read-only" is defined once, and each call site states which
kind of tool it is rather than restating four booleans. The declaration now
sits next to the handler it describes.

Three layers keep this honest:

1. **Decorators** are the source of truth, and are visible statically.
2. **The registration pass** remains as a backstop, filling only a tool
   that declared nothing, so a future omission degrades to a correct
   default rather than shipping hintless.
3. **Tests** assert the runtime result still matches the derivation from
   `WRITE_TOOLS` — a hand-written value contradicting a tool's registered
   role fails CI — plus a source-level test that no bare `@mcp.tool()`
   remains, which is the property a static reader depends on.

## Consequences

- Hints are now discoverable both by connecting to the server and by
  reading the source; neither method can report them missing.
- 911 tests pass. `tools/list` output is byte-identical to ADR-045's —
  this changes where the values are written, not what is advertised.
- Minor release 1.25.0.
