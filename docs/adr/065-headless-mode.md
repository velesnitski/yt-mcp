# 065 — Headless mode: run any tool once from the command line

## Context

yt-mcp answers only inside an MCP session. A scheduled job cannot use what
the tools already do, such as a morning digest, an at-risk sweep, a
deadline scorecard or a team pulse. Examples are a cron report, a CI step,
or another bot that reads the tracker every half hour. Re-implementing
those tools in each consumer would fork the logic and its fixes. The Slack
server solved the same problem with a headless mode; this server follows the
same command shape, so one scheduler can drive both.

## Decision

Two subcommands on the same entry point: `yt-mcp tools` lists the tools, and
`yt-mcp run <tool> [key=value …] [--args JSON|-] [--json] [--timeout 5m]`
calls one.

- **In-process, through FastMCP's public API.**
  - The command builds the server exactly as the transports do and calls
    `list_tools()` and `call_tool()`.
  - So every wrapper applies: analytics logging, the reply cap and the
    query-error hints (ADR-064). So does every gate:
    - `YOUTRACK_READ_ONLY`, `DISABLED_TOOLS` and `YOUTRACK_TOOLSET` remove
      tools before the command sees them.
    - A write tool under read-only does not exist here either.
  - Nothing is wired per tool, so a tool is reachable here the day it is
    registered.
- **Arguments typed by the schema:**
  - `key=value` pairs become integers, numbers, booleans or JSON according
    to the tool's input schema, looking through `Optional`.
  - A key the tool does not take is a usage error that names the valid
    keys. This applies to keys from pairs and from `--args`. So are a
    malformed value and a missing required argument.
  - `--args` takes a JSON object, or `-` for stdin. Pairs override it.
    Flags may sit anywhere.
- **Command line before configuration before server:**
  - A usage mistake is reported without building anything.
  - A missing URL or token is reported up front, naming the variable. The
    transports only discover it on the first call.
- **Output for machines:**
  - Plain text by default.
  - `--json` prints one object with stable fields: `tool`, `args`, `ok`
    and `text`.
  - Unlike the Slack server there is no `cursor`, because no tool here
    returns a delta token.
- **Exit codes:**
  - 0 success.
  - 1 the tool reported an error, or ran past `--timeout`.
  - 2 a usage or configuration error.
- **Logging and teardown:**
  - Logs go to stderr at WARNING by default (`YOUTRACK_LOG_LEVEL`
    overrides), so stdout holds only the result. The log file still
    records INFO.
  - The HTTP clients are closed on exit.

## Consequences

- A scheduler gets the tools without an MCP client and without a second
  implementation. For read-only jobs, `YOUTRACK_READ_ONLY=1` removes the
  write tools rather than trusting the caller.
- One call per process. Each run pays a server start, about 0.2 s once the
  package is installed, plus the call itself.
- The in-process call skips the transport, so a transport-level defect is
  not exercised here; the transport tests still cover that.
- `tests/test_headless.py` pins the exit codes, typing, gating and output
  shape. Each guarantee was checked against a mutant of the code it guards.
