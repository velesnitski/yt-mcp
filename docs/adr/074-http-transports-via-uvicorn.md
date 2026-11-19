# 074 — Serve every HTTP transport through one uvicorn path

## Context

With mcp 1.30, `--transport streamable-http` and `--transport sse` crashed on
start without OAuth: `FastMCP.run()` no longer takes `host` or `port`. Every
HTTP deployment without OAuth was down, while stdio worked, so no test
noticed.

Fixing only the call would not have been enough:

- **Remote clients were refused.** The server object is built before the
  host is known, with the SDK default `127.0.0.1`. The SDK then turns on
  DNS-rebinding protection that accepts only localhost `Host` headers. A
  container bound to `0.0.0.0` and reached by its service name would answer
  every request with `421 Misdirected Request`.
- **The OAuth branch did not start streamable HTTP sessions.** It mounts the
  MCP app inside its own Starlette app, and a mounted app's lifespan does
  not run. Streamable HTTP starts its session manager in that lifespan.
- **The image healthcheck asked for `/sse`.** Streamable HTTP has no such
  path, so the container reported itself unhealthy.

## Decision

- `server.http_app()` builds the ASGI app for both branches. `main()` always
  serves it with `uvicorn.run(app, host, port)`.
- The bound host and port go into the server settings. DNS-rebinding
  protection stays on only when bound to localhost, which is the SDK's own
  rule for a server built with that host.
- The OAuth app carries the mounted MCP app's lifespan.
- The healthcheck probes the socket, so it holds for any transport. A
  non-default port needs its own `--health-cmd` (README).

## Consequences

- HTTP deployments start again, with and without OAuth.
- `tests/test_http_transports.py` starts each transport in a subprocess on
  a free port and sends an MCP `initialize`. It also checks that a
  localhost bind still refuses a foreign `Host`, and that the OAuth app
  starts the session manager. The old code fails these tests, and so does
  each fix taken out on its own.
