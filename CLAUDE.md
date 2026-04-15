# yt-mcp

YouTrack MCP server. 63 tools across 15 modules.

## Build & test

```bash
uv run python -m pytest tests/ -q     # run all tests (178)
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

When `command` param is provided:
1. Try full command as-is (handles emoji/multi-word fields)
2. On failure: split into individual field-value pairs via regex
3. On split failures: rejoin failed parts and retry as single command
4. Product is always a separate command call

If creation fails due to required fields: creates draft, applies commands, publishes.

## Sensitive data rules

**NEVER** use in code, tests, docs, or commits:
- Real company/product names (use "Alpha", "PROJ-123", "OPS-423")
- Real YouTrack instance URLs
- Real API tokens

Before every push: `grep -rn "planetvpn\|maxprotocol\|vpnly\|VPN" --include="*.py" --include="*.md"`

## Test conventions

- Test files: `tests/test_*.py`
- Mock client: `MagicMock()` with `AsyncMock` for async methods
- Tool count assertions in `test_registration.py` and `test_server.py`
- Update `WRITE_TOOLS` set when adding write tools
- Generic names only: "PROJ-123", "Alpha", "OPS-423", "DEMO-42"
