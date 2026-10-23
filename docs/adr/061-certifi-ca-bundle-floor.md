# 061 — certifi CA bundle floor

## Context

A certificate authority generated weak keys for eight of its intermediate
certificates; they were revoked and browsers blocked them. `certifi` before
2026.10.19 still trusts the root they chain to (GHSA-6q48-6c7w-9gw2,
critical). This server talks to the tracker API over HTTPS through httpx,
which trusts the certifi bundle, so a party on the network path could
present a certificate chained to that root and read or alter the traffic,
the API token included.

## Decision

Declare `certifi>=2026.10.19` directly, for the reason ADR-059 gives for
urllib3: the server is normally launched through `uvx --from git+…`, which
resolves from `pyproject.toml` and never reads the lock, so a lock-only bump
would protect nobody who runs it. The lock is re-resolved as well.

No code change; the suite is the evidence the upgrade is inert.
