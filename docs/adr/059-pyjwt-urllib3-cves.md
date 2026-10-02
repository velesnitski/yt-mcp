# 059 — PyJWT and urllib3 advisories

## Context

Sixteen open advisories appeared within a week: thirteen against PyJWT
(resolved 2.13.0) and three against urllib3 (resolved 2.7.0).

The PyJWT set includes one critical — asymmetric-PEM detection bypassed
by whitespace and line-ending variants — and several high-severity key
confusions: DER and JWK public keys accepted as HMAC secrets, empty HMAC
keys accepted, and the JWKS client following redirects. Most are patched
in 2.14.0; one denial-of-service fix needs 2.15.0. One advisory lists no
patched release, but its affected range ends at 2.13.0, so any later
version is outside it.

urllib3 arrives only through sentry-sdk. Its advisories cover an HTTPS
proxy whose TLS settings could be ignored, unbounded buffering in chunked
reads, and an infinite loop in chunked deflate streaming.

## Decision

- Raise the declared floor to `PyJWT>=2.15.0`, which clears all thirteen
  in one step rather than leaving the 2.15-only fix for a second pass.
- Declare `urllib3>=2.8.0` directly despite it being transitive. As with
  anyio (ADR-057), the server is normally launched through
  `uvx --from git+…`, which resolves fresh from `pyproject.toml` and never
  reads our lock, so a lock-only bump would protect nobody who runs it.

Resolution moves to PyJWT 2.15.1 and urllib3 2.8.0. This ADR does not
claim the vulnerable paths are reachable from this server's own code;
the upgrades cost nothing and remove the question.

## Consequences

- 1,628 tests pass on the new resolution, including the 24 OAuth
  provider tests; no code change was needed.
- Patch release 1.34.1.
