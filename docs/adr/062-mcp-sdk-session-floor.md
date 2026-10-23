# 062 — MCP SDK session floor

## Context

The MCP Python SDK before 1.30.0 keeps every StreamableHTTP session it
creates and never evicts them, so a client that opens sessions in a loop
exhausts the server's memory. This server offers `--transport
streamable-http`, so the defect is reachable whenever that transport is used;
stdio is unaffected.

## Decision

Declare `mcp>=1.30.0` in `pyproject.toml`, for the reason ADR-061 gives: the
`uvx --from git+…` launch resolves from `pyproject.toml`, not the lock. The
lock is re-resolved as well.

No code change; the suite is the evidence the upgrade is inert.
