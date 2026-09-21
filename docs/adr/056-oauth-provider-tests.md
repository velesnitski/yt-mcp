# 056 — The untested module was the security one

## Context

Coverage by module put `auth.py` at **0%** — 117 statements, none
executed by any test. The reason it stayed invisible is structural: the
OAuth provider is imported lazily, only when an OAuth URL is configured,
so no test ever pulled it in and no per-module suite thought to.

It is also the one module where a defect is a security bug rather than a
wrong report. Everything else here renders a summary; this decides
whether a caller gets a token.

## Decision

Tests for the properties that matter when an attacker is the caller,
rather than for line count:

- **Single use.** An authorization code is consumed by exchange, and a
  verification session is consumed by completion. A captured code or a
  replayed verification must not mint a second token.
- **Binding.** A code loaded by a different `client_id` is refused; so is
  a refresh token presented by another client.
- **Expiry.** Codes, sessions and access tokens all stop working once
  their window passes, and an expired session is discarded rather than
  left in memory.
- **Rotation.** Exchanging a refresh token invalidates the old one, and
  refreshing without explicit scopes keeps the original scopes rather
  than silently widening or dropping them.
- **Verification gate.** In access-code mode, `authorize` must redirect
  to verification and must **not** carry a code — checked explicitly,
  because issuing the code early would defeat the gate while looking
  correct from the client's side.
- **Revocation.** A revoked access or refresh token stops loading.

## Consequences

- `auth.py` 0% → 79%; overall 81% → 83%.
- 24 tests, 23 of which passed on their first run against code written
  months earlier — the provider is sound; it was simply unverified.
- The gap this closes is not a percentage: nothing in CI would have
  caught a regression in code-reuse or client-binding, and those are the
  failures that do not announce themselves.
- Remaining coverage sits in `pulse`, `handoffs`, `projects`, `issues`
  and `translate` — rendering variants, worth doing for the bugs reading
  them surfaces rather than for the number.
