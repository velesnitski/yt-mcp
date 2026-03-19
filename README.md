# yt-mcp

YouTrack MCP server for [Claude Code](https://claude.com/claude-code), [n8n](https://n8n.io), and any MCP-compatible client. Talk to your YouTrack instance in natural language.

## Quick start

### 1. Get a YouTrack permanent token

1. Open YouTrack → **Profile** → **Account Security** → **Tokens** → **New token**
2. Set scope: `YouTrack`
3. Grant permissions: **Read Issue**, **Write Issue**, **Read Project** (or just use admin token for full access)
4. Copy the token — it starts with `perm:`

### 2. Install in Claude Code

**Option A — via CLI** (recommended):

> **Prerequisite:** [uv](https://docs.astral.sh/uv/) is required. Install with: `curl -LsSf https://astral.sh/uv/install.sh | sh`

```bash
claude mcp add youtrack \
  -e YOUTRACK_URL=https://your-instance.youtrack.cloud \
  -e YOUTRACK_TOKEN=perm:your-token-here \
  -- uvx --from git+https://github.com/velesnitski/yt-mcp yt-mcp
```

**Option B — manually edit settings:**

Edit `~/.claude/settings.json` (global) or `.claude/settings.json` in your project root (per-project):

```json
{
  "mcpServers": {
    "youtrack": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/velesnitski/yt-mcp", "yt-mcp"],
      "env": {
        "YOUTRACK_URL": "https://your-instance.youtrack.cloud",
        "YOUTRACK_TOKEN": "perm:your-token-here"
      }
    }
  }
}
```

> **Troubleshooting: MCP server not found**
>
> If Claude Code can't find `uvx` at startup (e.g., `/mcp` doesn't show the server), use the full path instead:
>
> ```json
> "command": "/full/path/to/uvx"
> ```
>
> Find the full path with `which uvx` (typically `~/.local/bin/uvx` or `/opt/homebrew/bin/uvx`).

### 3. Restart Claude Code

```bash
claude
```

You should see `youtrack` listed when Claude starts. Try asking: *"List my YouTrack projects"*

## Multi-instance setup

Connect multiple YouTrack instances to a single MCP server. Each tool gets an optional `instance` parameter — the LLM picks the right instance from context, or auto-detects it when you paste a YouTrack URL.

### Configuration

Set `YOUTRACK_INSTANCES` to a comma-separated list of instance names, then provide `YOUTRACK_{NAME}_URL` and `YOUTRACK_{NAME}_TOKEN` for each:

```bash
YOUTRACK_INSTANCES=main,second
YOUTRACK_MAIN_URL=https://main.youtrack.cloud
YOUTRACK_MAIN_TOKEN=perm:xxx
YOUTRACK_SECOND_URL=https://second.youtrack.cloud
YOUTRACK_SECOND_TOKEN=perm:yyy
```

Adding more instances follows the same pattern:

```bash
YOUTRACK_INSTANCES=main,second,third,fourth
YOUTRACK_MAIN_URL=https://main.youtrack.cloud
YOUTRACK_MAIN_TOKEN=perm:xxx
YOUTRACK_SECOND_URL=https://second.youtrack.cloud
YOUTRACK_SECOND_TOKEN=perm:yyy
YOUTRACK_THIRD_URL=https://third.youtrack.cloud
YOUTRACK_THIRD_TOKEN=perm:zzz
YOUTRACK_FOURTH_URL=https://fourth.youtrack.cloud
YOUTRACK_FOURTH_TOKEN=perm:www
```

Instance names are arbitrary — use whatever makes sense: `prod,staging,dev`, `team1,team2`, etc. The name is uppercased to form the env var prefix (`dev` → `YOUTRACK_DEV_URL`).

### How it works

- **No `YOUTRACK_INSTANCES`** — single-instance mode, fully backward compatible. Uses `YOUTRACK_URL` / `YOUTRACK_TOKEN` as before.
- **First instance** falls back to unprefixed `YOUTRACK_URL` / `YOUTRACK_TOKEN` if its prefixed vars are not set.
- **URL auto-detection** — when you paste a YouTrack URL (e.g., `https://second.youtrack.cloud/issue/PROJ-123`), the server matches the domain to the right instance automatically.
- **Explicit `instance` parameter** — every tool accepts an optional `instance` param to target a specific instance.
- **Default** — if no instance is specified and no URL is provided, the first configured instance is used.
- **Global settings** — `YOUTRACK_READ_ONLY`, `DISABLED_TOOLS`, and `YOUTRACK_MAX_BULK_RESULTS` apply to all instances.

### Claude Code example

```bash
claude mcp add youtrack \
  -e YOUTRACK_INSTANCES=main,second \
  -e YOUTRACK_MAIN_URL=https://main.youtrack.cloud \
  -e YOUTRACK_MAIN_TOKEN=perm:xxx \
  -e YOUTRACK_SECOND_URL=https://second.youtrack.cloud \
  -e YOUTRACK_SECOND_TOKEN=perm:yyy \
  -- uvx --from git+https://github.com/velesnitski/yt-mcp yt-mcp
```

## What it does

Gives MCP clients live access to your YouTrack instance. Instead of opening the YouTrack UI, just ask:

- *"What open issues does the Android team have?"*
- *"Show me DEVOPS-423"*
- *"Create a bug in the WordPress project: homepage returns 500"*
- *"Set priority to Critical and assign to John"*
- *"List all agile boards"*
- *"What did the DevOps team close this week?"*
- *"Log 2 hours of development on BAC-1828"*
- *"Find articles about deployment"*

### Available tools (44)

#### Issues (18)

| Tool | Description |
|---|---|
| `search_issues` | Search issues using [YouTrack query syntax](https://www.jetbrains.com/help/youtrack/server/Search-and-Command-Attributes.html) |
| `get_issue` | Get full details of a specific issue (description, comments, fields, links) |
| `create_issue` | Create a new issue in a project (freeform) |
| `create_issue_from_template` | Create an issue using a template (bug, feature, task, daily, spike, release, devops, incident, epic) |
| `update_issue` | Update any issue field — summary, state, priority, type, deadline, assignee, custom fields via [command syntax](https://www.jetbrains.com/help/youtrack/server/Command-Reference.html) |
| `delete_issue` | Soft-delete (state → Obsolete) or permanently delete an issue |
| `get_issue_links` | Get all linked issues (parent, subtask, depends on, relates to, duplicates) |
| `add_issue_link` | Link two issues together (relates, depends on, parent/subtask, duplicates) |
| `remove_issue_link` | Remove a link between two issues |
| `add_comment` | Add a comment to an issue (markdown supported) |
| `update_comment` | Edit an existing comment on an issue |
| `delete_comment` | Delete a comment from an issue |
| `get_issue_history` | View change history of an issue (who changed what, when) |
| `get_issue_changes_summary` | Get a compact summary of issue changes (state transitions, comments, time logged) |
| `rollback_issue` | Revert a specific change using its activity ID |
| `poll_changes` | Poll for recently changed issues (for automation triggers — Make.com, n8n, cron) |
| `list_templates` | List available issue templates |

#### Time tracking (4)

| Tool | Description |
|---|---|
| `get_work_items` | Get time tracking work items for an issue |
| `add_work_item` | Log time to an issue (duration, date, type, description) |
| `update_work_item` | Update an existing work item (duration, date, description) |
| `delete_work_item` | Delete a work item from an issue |

#### Agile boards (5)

| Tool | Description |
|---|---|
| `get_agiles` | List all agile boards |
| `get_agile_board` | Search for an agile board by name (partial match) |
| `get_sprint_board` | Get issues on an agile board grouped by column/state for a sprint |
| `create_agile_board` | Create a new agile board for one or more projects |
| `delete_agile_board` | Delete an agile board (issues are not affected) |

#### Projects & users (3)

| Tool | Description |
|---|---|
| `list_projects` | List all accessible projects |
| `get_current_user` | Get the authenticated user's profile |
| `search_users` | Search users by name, login, or email |

#### Knowledge Base (8)

| Tool | Description |
|---|---|
| `search_articles` | Search Knowledge Base articles |
| `get_article` | Get a KB article with full content and comments |
| `create_article` | Create a new KB article (with optional nesting) |
| `update_article` | Update a KB article (title, content) |
| `delete_article` | Delete a KB article |
| `add_article_comment` | Add a comment to a KB article |
| `update_article_comment` | Edit a comment on a KB article |
| `delete_article_comment` | Delete a comment from a KB article |

#### Bulk operations (3)

| Tool | Description |
|---|---|
| `bulk_update_preview` | Preview which issues a bulk command would affect (dry run) |
| `bulk_update_execute` | Apply a command to all issues matching a query (auto-tags for rollback) |
| `bulk_rollback` | Undo all changes from a bulk update batch using its tag |

#### Translation (2)

| Tool | Description |
|---|---|
| `get_issues_for_translation` | Fetch issues with non-English text for LLM-assisted translation |
| `apply_translations` | Apply translated text to issues with batch tagging for rollback |

#### Priority dashboard & monitoring (7)

| Tool | Description |
|---|---|
| `get_top_active_issues` | Get top N active issues ranked by weighted scoring (priority, type, staleness, blockers) |
| `get_top_blocked_issues` | Get top N blocked issues ranked by scoring (priority, duration blocked, blockers) |
| `get_team_dashboard` | Combined project brief — top active + top blocked + summary stats |
| `get_issues_digest` | Digest of recent changes for any issues — state changes, comments, field updates |
| `get_at_risk_issues` | Find at-risk issues: stalled (active but silent), forgotten (filed but idle 30d+), overdue, over estimate |
| `check_task_creation` | Verify a requested task was created with proper fields (priority, assignee, description) + quality score |
| `get_creation_activity` | Report of recently created issues with quality indicators and PM follow-through stats |

#### Impact analysis (2)

| Tool | Description |
|---|---|
| `get_impact_map` | Build cross-product dependency graph from an issue (links, product overlap, mentions) |
| `get_deadline_impact` | Analyze what breaks if an issue slips (blocked, at risk, done) |

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `YOUTRACK_URL` | Yes | Your YouTrack instance URL (e.g., `https://company.youtrack.cloud`) |
| `YOUTRACK_TOKEN` | Yes | Permanent token (starts with `perm:`) |
| `YOUTRACK_INSTANCES` | No | Comma-separated instance names for multi-instance setup (e.g., `main,second`) |
| `YOUTRACK_READ_ONLY` | No | Set to `true` to disable all write operations |
| `DISABLED_TOOLS` | No | Comma-separated list of tools to disable (e.g., `delete_issue,bulk_update_execute`) |
| `YOUTRACK_MAX_BULK_RESULTS` | No | Maximum issues per bulk operation (default: `100`) |
| `YOUTRACK_ALLOW_HTTP` | No | Set to `1` to allow non-HTTPS URLs (not recommended) |

## Verify the server works

**Check that `uv` is installed** (required for the `uvx` command):

```bash
uv --version
```

If not installed, get it with:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Test the server starts and responds** by sending a JSON-RPC `initialize` request via stdio:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{},"clientInfo":{"name":"test","version":"1.0.0"},"protocolVersion":"2024-11-05"}}' \
  | YOUTRACK_URL="https://your-instance.youtrack.cloud" \
    YOUTRACK_TOKEN="perm:your-token-here" \
    uvx --from git+https://github.com/velesnitski/yt-mcp yt-mcp
```

A successful response looks like:

```json
{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"youtrack"},"capabilities":{"tools":{}},...}}
```

If you see `command not found: uvx`, install `uv` first (see above).

**Test from a local clone:**

```bash
cd yt-mcp
pip install -e .
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{},"clientInfo":{"name":"test","version":"1.0.0"},"protocolVersion":"2024-11-05"}}' \
  | YOUTRACK_URL="https://your-instance.youtrack.cloud" \
    YOUTRACK_TOKEN="perm:your-token-here" \
    yt-mcp
```

**List available tools** by sending a `tools/list` request after initialization:

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{},"clientInfo":{"name":"test","version":"1.0.0"},"protocolVersion":"2024-11-05"}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n' \
  | YOUTRACK_URL="https://your-instance.youtrack.cloud" \
    YOUTRACK_TOKEN="perm:your-token-here" \
    uvx --from git+https://github.com/velesnitski/yt-mcp yt-mcp
```

You should see all 51 tools listed.

## Setup for Windows

**1. Install uv** (Python package runner):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Restart your terminal after installation.

**2. Get a YouTrack token:**

Open YouTrack → **Profile** → **Account Security** → **Tokens** → **New token** → scope: `YouTrack` → copy the `perm:...` token.

**3. Add the MCP server to Claude Code:**

```powershell
claude mcp add youtrack `
  -e YOUTRACK_URL=https://your-instance.youtrack.cloud `
  -e YOUTRACK_TOKEN=perm:your-token-here `
  -- uvx --from git+https://github.com/velesnitski/yt-mcp yt-mcp
```

> **Note:** If Claude Code can't find `uvx`, use the full path. Find it with `where uvx` (typically `%USERPROFILE%\.local\bin\uvx.exe`) and set `"command"` to that path in your settings.

**4. Restart Claude Code** and try: *"List my YouTrack projects"*

## Alternative installation methods

### From local clone

```bash
git clone https://github.com/velesnitski/yt-mcp.git
cd yt-mcp
pip install -e .
```

Then use in settings:

```json
{
  "mcpServers": {
    "youtrack": {
      "command": "yt-mcp",
      "env": {
        "YOUTRACK_URL": "https://your-instance.youtrack.cloud",
        "YOUTRACK_TOKEN": "perm:your-token-here"
      }
    }
  }
}
```

### Run directly with Python

```bash
git clone https://github.com/velesnitski/yt-mcp.git
cd yt-mcp
pip install -e .  # installs mcp and httpx dependencies
yt-mcp
```

## Updating any field

The `update_issue` tool can update **any** YouTrack field — not just summary and state. Use the `command` parameter with [YouTrack command syntax](https://www.jetbrains.com/help/youtrack/server/Command-Reference.html):

```
Priority Critical                          # set priority
Type Bug                                   # set issue type
Deadline 2026-04-01                        # set deadline
Assignee John, Jane                        # multiple assignees
Version 2.5.0                              # set version
Dev Estimate 12                            # set custom numeric field
State In Progress Priority High Type Task  # multiple fields at once
project MOBILE                             # move issue to another project
```

All changes show a before/after diff and return rollback instructions.

> *"Set DEVOPS-423 priority to Critical and assign to John"*
> *"Move BAC-100 to the MOBILE project and set deadline to April 1st"*
> *"Set dev estimate to 8 and QA estimate to 3 on iOS-55"*

## Query syntax examples

The `search_issues` tool accepts standard [YouTrack search queries](https://www.jetbrains.com/help/youtrack/server/Search-and-Command-Attributes.html):

```
project: Android state: Open                    # open Android issues
project: DevOps updated: -1w                    # DevOps issues updated in last week
assignee: John priority: Critical               # John's critical issues
#Unresolved project: WordPress                  # unresolved WordPress issues
created: -3d .. today                           # issues created in last 3 days
project: Backend tag: ZTNA                      # Backend issues tagged ZTNA
```

## Issue templates

The server includes built-in templates for creating structured issues. Ask Claude to list them or use them directly:

- *"List available templates"*
- *"Create a bug report in the Android project"*

### Available templates

| Template | Sections |
|---|---|
| `bug` | Summary, Steps to Reproduce, Expected/Actual Result, Environment, Severity |
| `feature` | Problem, Proposed Solution, Alternatives, Acceptance Criteria, Priority |
| `task` | Objective, Requirements, Technical Notes, Definition of Done |
| `daily` | Done Yesterday, Planned Today, Blockers |
| `spike` | Goal, Context, Scope, Timebox, Findings, Recommendation |
| `release` | Version, Changes, Pre/Post-release Checklists, Rollback Plan |
| `devops` | Description, Requirement, Expected Result, Affected Services, Rollback Plan |
| `incident` | Impact, Symptoms, Environment, Steps to Reproduce, Root Cause, Fix Applied, Prevention |
| `epic` | Goal, Background, Scope, Success Criteria, Dependencies, Risks, Subtasks |

### Examples

**Bug report** — just describe the problem naturally:

> *"Create a bug in MOBILE: app crashes when switching between servers. Steps: connect to server A, tap server B, app freezes and restarts. Happens on Android 14. Critical severity."*

**Feature request:**

> *"Create a feature request in WEB: add dark mode toggle to the settings page. Should follow system theme by default. Must have priority."*

**DevOps task:**

> *"Create a devops task in OPS: Deploy new nodes in two additional regions. Need 2 servers per region with monitoring configured. Affected services: free-tier, premium-tier."*

**Daily standup:**

> *"Create a daily in OPS: Yesterday — fixed monitoring alerts, deployed config update to staging. Today — production rollout, server audit. No blockers."*

**Research spike:**

> *"Create a spike in MOBILE: Research crash causes on Android 13+. Timebox: 2 days. Need to determine if the issue is in the native layer or Java bridge."*

**Release checklist:**

> *"Create a release task in MOBILE: version 2.5.0. Changes: new auth flow, config updates, purchase module. Rollback: revert to 2.4.9 via app store."*

Claude will automatically use the matching template and fill in the sections from your description.

## Using with n8n, Langchain, and other HTTP clients

By default the server uses **stdio** transport (for Claude Code). For integration with **n8n**, **Langchain**, **OpenAI Agents SDK**, or any HTTP-based MCP client, start the server in **SSE** or **streamable-http** mode:

### Start the server with SSE transport

```bash
YOUTRACK_URL="https://your-instance.youtrack.cloud" \
YOUTRACK_TOKEN="perm:your-token-here" \
uvx --from git+https://github.com/velesnitski/yt-mcp yt-mcp --transport sse --port 8000
```

The server will be available at `http://localhost:8000/sse`.

### Start with streamable HTTP transport

```bash
YOUTRACK_URL="https://your-instance.youtrack.cloud" \
YOUTRACK_TOKEN="perm:your-token-here" \
uvx --from git+https://github.com/velesnitski/yt-mcp yt-mcp --transport streamable-http --port 8000
```

The server will be available at `http://localhost:8000/mcp`.

### CLI options

| Flag | Default | Description |
|---|---|---|
| `--transport` | `stdio` | Transport protocol: `stdio`, `sse`, or `streamable-http` |
| `--host` | `0.0.0.0` | Host to bind to |
| `--port` | `8000` | Port to bind to |

### n8n setup

1. Start the server in SSE mode (see above)
2. In n8n, add an **MCP Client** node (or use the HTTP Request node)
3. Set the MCP server URL to `http://localhost:8000/sse`
4. The YouTrack tools will be available as actions in your n8n workflows

### Make.com setup

1. Start the server in SSE mode (see above)
2. In Make.com, use an **HTTP** or **MCP** module to connect to `http://localhost:8000/sse`
3. Use the `poll_changes` tool on a schedule to detect new activity:
   - Set `query` to filter issues (e.g., `project: DO`)
   - Set `since_minutes` to match your polling interval (e.g., `5`)
4. Route changed issues to other Make.com modules (Slack, email, Jira, etc.)

### Docker (for remote / always-on deployments)

```bash
docker run -d --name youtrack-mcp \
  -e YOUTRACK_URL="https://your-instance.youtrack.cloud" \
  -e YOUTRACK_TOKEN="perm:your-token-here" \
  -p 8000:8000 \
  ghcr.io/velesnitski/yt-mcp --transport sse
```

> **Note:** Docker image is not yet published. For now, use the local install method with `uvx` or `pip`.

## Security

- Tokens are passed via environment variables — never hardcoded
- In **stdio** mode, the server has no network exposure (local pipes only)
- In **SSE/HTTP** mode, the server listens on a network port — bind to `127.0.0.1` if you don't need external access, or use a reverse proxy with authentication for production
- YouTrack API calls use HTTPS (non-HTTPS URLs are blocked unless `YOUTRACK_ALLOW_HTTP=1`)
- Consider using a token with minimal required permissions (read-only if you don't need `create_issue`)
- Bulk operations are capped at 100 issues per batch
- Error messages are truncated to prevent leaking internal API details

### Read-only mode

To disable all write operations (create, update, delete issues/articles, bulk execute, time tracking):

```bash
YOUTRACK_READ_ONLY=true
```

### Disable specific tools

Block individual tools by name (comma-separated, case-insensitive):

```bash
DISABLED_TOOLS=delete_issue,bulk_update_execute,apply_translations
```

This removes the specified tools from the MCP server entirely — clients won't see them.

## Requirements

### Python 3.10+

Check your version:

```bash
python3 --version
```

If not installed or below 3.10:

- **macOS** (via [Homebrew](https://brew.sh)):
  ```bash
  brew install python@3.12
  ```
- **Ubuntu / Debian**:
  ```bash
  sudo apt update && sudo apt install python3 python3-pip
  ```
- **Windows**: download from [python.org](https://www.python.org/downloads/) or use `winget install Python.Python.3.12`

### uv (recommended)

[uv](https://docs.astral.sh/uv/) is a fast Python package manager. The `uvx` command (included with `uv`) is used to run the MCP server without a manual install.

```bash
uv --version   # check if already installed
```

If not installed:

- **macOS / Linux**:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
  Then restart your shell or run `source $HOME/.local/bin/env`.

- **macOS** (Homebrew alternative):
  ```bash
  brew install uv
  ```

- **Windows**:
  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```

If you prefer not to use `uv`, see [Alternative installation methods](#alternative-installation-methods) for `pip`-based setup.

## License

MIT
