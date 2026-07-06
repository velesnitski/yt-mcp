# yt-mcp

YouTrack MCP server. 79 tools across 19 modules.

## Build & test

```bash
uv run python -m pytest tests/ -q     # run all tests (657+)
uv run python -m pytest tests/ -v      # verbose
uv pip install -e .                     # editable install
```

## Architecture

```
src/yt_mcp/
  server.py          # entry point, FastMCP setup, OAuth lazy loading
  client.py          # YouTrackClient (httpx async, HTTP/2)
  config.py          # env var parsing, multi-instance config
  resolver.py        # InstanceResolver (instance name / URL auto-detection)
  formatters.py      # shared helpers (parse_issue_id, format_*, escape_query_value)
  scoring.py         # weighted scoring models for active/blocked issues
  logging.py         # structured JSON logging, Sentry (lazy), analytics
  auth.py            # OAuth 2.0 provider (lazy, only when YOUTRACK_OAUTH_URL set)
  tools/
    issues.py        # CRUD: search, get, create, update, delete, poll_changes, count
    comments.py      # add, update, delete comments
    attachments.py   # list attachments, get download URL
    projects.py      # list projects, get_project_fields, agile boards
    sprints.py       # create, update sprint, add issues to sprint
    discovery.py     # list_tags, list_saved_searches, run_saved_search
    history.py       # issue history, rollback, work items, changes summary
    bulk.py          # bulk preview, execute, rollback with batch tags
    translate.py     # translation workflow with batch rollback
    deadlines/       # deadline audit/scorecard + manager-mapping suggester (package)
    pulse.py         # team pulse (single + multi-board parallel) + insight flags
    handoffs.py      # stuck-handoff detection: cross-team transition stalls
    impact.py        # dependency graph, deadline impact analysis
    dashboard.py     # scoring-based dashboards (active, blocked, team, multi-team)
    monitoring.py    # digest, at-risk, task creation checks, project health
    templates.py     # built-in issue templates
    articles.py      # Knowledge Base CRUD
    users.py         # current user, search users
    __init__.py      # register_all, WRITE_TOOLS set
```

## Key patterns

- Every tool has `instance: str = ""` for multi-instance support
- `resolver.resolve(instance, identifier)` picks the right YouTrackClient
- `parse_issue_id()` extracts ID from URLs
- `compact_lines()` joins output (respects YOUTRACK_COMPACT mode)
- `escape_query_value()` sanitizes user input for YT queries
- Write tools are listed in `WRITE_TOOLS` (blocked in read-only mode)
- `create_issue` uses draft-based creation for projects with required fields

## create_issue command strategy

When `command` param is provided (values always sent BARE — YT's command
parser rejects `{braces}`; braces are input-only grouping, ADR-019):
1. Try full command as one call (braces stripped)
2. On failure: split per-field using the project's REAL field names as
   boundaries (`_split_command_with_field_names`, ADR-021) — handles
   multi-word/emoji names like `Evaluation time 🕙`; regex split is the
   fallback when the field list is unavailable
3. On split failures: rejoin failed parts and retry as single command
4. Product is always a separate command call

If creation fails due to required fields: creates draft, applies commands, publishes.

For gated state changes use `transition_issue` (ADR-021): sets fields
first, then transitions, and surfaces the blocking workflow rule's own
text instead of a raw 400.

## Sensitive data rules

**NEVER** use in code, tests, docs, or commits:
- Real company/product names (use "Alpha", "PROJ-123", "OPS-423")
- Real YouTrack instance URLs
- Real API tokens

Before every push: `grep -rn "acme\|acme\|acme\|VPN" --include="*.py" --include="*.md"`

## Test conventions

- Test files: `tests/test_*.py`
- Mock client: `MagicMock()` with `AsyncMock` for async methods
- Tool count assertions in `test_registration.py` and `test_server.py`
- Update `WRITE_TOOLS` set when adding write tools
- Generic names only: "PROJ-123", "Alpha", "OPS-423", "DEMO-42"

## `/mcp` version label

The `/mcp` dialog labels servers by their **config key** in
`~/.claude.json`, not by `serverInfo.name`. The server already self-reports
`youtrack v<version>` (via `_SERVER_NAME` in `server.py`), but that only
shows in the instructions header — never in the dialog. After a version
bump, run `python3 scripts/sync-mcp-label.py` to re-key the entry to
`youtrack v<version>` across all config containers, then reconnect `/mcp`.
Check the running build any time with `uv run yt-mcp --version`. See
ADR-011. (Fleet pattern shared with zbbx-mcp / slk-mcp.)
