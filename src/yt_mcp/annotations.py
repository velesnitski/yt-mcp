"""Tool annotation constructors, declared at each tool's definition site.

The protocol's four hints tell a client what a tool does before it calls
it: gate mutations behind confirmation, parallelize reads, retry safely.

They are passed to `@mcp.tool(annotations=...)` rather than attached in a
later pass so that the declaration lives next to the handler it describes
and is visible to anything reading the source — a registry or auditor need
not run the server to see them. Consistency with the server's write-tool
set is enforced by tests, so a hand-written value that contradicts the
tool's registered role fails CI rather than shipping (ADR-046).

`openWorldHint` is True everywhere: every tool reaches a YouTrack instance,
an external system whose contents change independently of this server.
"""

from mcp.types import ToolAnnotations


def read_only() -> ToolAnnotations:
    """A tool that only reads. Safe to call speculatively and in parallel."""
    return ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )


def mutates(*, destructive: bool = False, idempotent: bool = True) -> ToolAnnotations:
    """A tool that writes.

    Args:
        destructive: removes or reverts data (deletes, rollbacks, bulk
            overwrite) rather than adding or updating it.
        idempotent: repeating the call converges on the same state. False
            for creates and appends, where a second call adds a second
            entity.
    """
    return ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=True,
    )
