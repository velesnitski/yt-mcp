# 076 — Error reporting never stops the server

## Context

The server is installed with `uvx --from git+…@<sha>`. That resolves
dependencies afresh from `pyproject.toml` and ignores `uv.lock`. The
dependency range was `sentry-sdk>=2.0`, so a fresh install of v1.42.0 picked
up sentry-sdk 3.0.1. That release rejects the `send_default_pii` option
passed to `sentry_sdk.init`. With `SENTRY_DSN` set, every start ended in
`TypeError: Unknown option 'send_default_pii'`, and the client reported the
server as closed.

Error reporting is optional. A failure in it took the whole server down.

## Decision

- **Cap the range:** `sentry-sdk>=2.0,<3`. A major release of the SDK is
  adopted on purpose, not by a fresh install.
- **Never fail on telemetry.**
  - When `sentry_sdk.init` rejects an option, the server retries without
    it. The event scrubber stays, so nothing personal is sent.
  - Any other failure while setting up Sentry leaves the server running
    without it, and says so on stderr.

## Consequences

- The server starts with any installed sentry-sdk, with or without
  `SENTRY_DSN`.
- A broken Sentry setup costs error reports, not the server.
- Tests in `tests/test_sentry_init.py` cover four cases: no DSN, an option
  rejected and retried, an init that fails outright, and a normal init.
