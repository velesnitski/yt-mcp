# 031 — Make the stdio integration test deterministic (SDK EOF-drain race)

## Context

After the `mcp` 1.25 → 1.28.1 bump (ADR-030), CI went **flaky**, not broken:
`test_server.py::TestServerStartup::test_tools_list` failed on Python 3.13 in
the main-branch run while the **same commit** (`35db13d`) passed every version
on the dev-branch run — and it passed locally and on 3.10/3.11/3.12. The
failure was "no `tools/list` (id 2) response", with only the `initialize`
reply present.

Root cause is the test harness, not the server. `_run_jsonrpc` wrote all three
messages (`initialize`, `notifications/initialized`, `tools/list`) with
`subprocess.run`, which closes stdin immediately and waits for exit. The server
then races: its stdio loop can hit stdin-EOF and shut down before the async
`tools/list` reply is flushed. The newer SDK's loop tightened that ordering, so
the pre-existing race started losing more often. A real MCP client keeps stdin
open for the whole session and never triggers this.

## Decision

Rewrite `_run_jsonrpc` to model a real client:

- Use `subprocess.Popen` and **keep stdin open**; read stdout on a background
  thread into a queue.
- Collect replies until **every request id has been answered** (or a timeout),
  then `terminate()` the process. No dependency on drain-before-EOF ordering.
- `stderr` → `DEVNULL` (these cases never assert on it; avoids any pipe-fill
  stall).

## Consequences

- Deterministic: 25/25 back-to-back runs of `TestServerStartup` pass; full
  suite 784 green.
- **Test-only change** — no `src/` change, runtime identical to 1.18.4, so no
  version bump or re-release; this lands on top of the v1.18.4 tag to green CI.
- The helper now returns exactly the replies for the ids it was asked to send
  (it stops once they arrive); all callers already match by `id`, so behavior
  is unchanged for them.
