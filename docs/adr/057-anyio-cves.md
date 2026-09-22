# 057 — Pin anyio past CVE-2026-63374

## Context

Two advisories against `anyio`, both fixed in 4.14.2; the resolved
version was 4.12.1.

- **CVE-2026-63374 (critical)** — `TLSStream` encodes host names with
  IDNA 2003, whose mapping differs from IDNA 2008 for some labels. A name
  that validates as one host can connect as another, which undermines
  certificate verification for those labels.
- **CVE-2026-64847 (medium)** — process-pool workers can block
  indefinitely on an undrained pipe.

`anyio` is transitive here, arriving through the MCP SDK and httpx. That
is what makes a lockfile bump insufficient on its own: this server is
normally launched with `uvx --from git+…`, which resolves dependencies
fresh rather than from our lock, so a floor recorded only in `uv.lock`
would not reach the people running it.

## Decision

Declare `anyio>=4.14.2` as a direct dependency, with the reason next to
it, and relock — the same pattern used for the MCP SDK transport CVEs in
ADR-030. Resolution moves to 4.15.1.

Whether our own code reaches the vulnerable `TLSStream` path is not
established, and this ADR does not claim it does. The bump costs nothing
and removes the question.

## Consequences

- 1,610 tests pass on the new resolution; no code change was required.
- The floor is visible in `pyproject.toml`, so a fresh `uvx` launch gets
  the patched version rather than whatever resolves first.
- Patch release 1.33.1.
