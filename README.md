# yt-mcp

YouTrack MCP server for [Claude Code](https://claude.com/claude-code), [n8n](https://n8n.io), and any MCP-compatible client. Talk to your YouTrack instance in natural language.

## What it does

Gives MCP clients live access to your YouTrack instance. Instead of opening the YouTrack UI, just ask:

- *"What open issues does the Android team have?"*
- *"Show me DEVOPS-423"*
- *"Create a bug in the WordPress project: homepage returns 500"*
- *"List all agile boards"*
- *"What did the DevOps team close this week?"*

### Available tools

| Tool | Description |
|---|---|
| `search_issues` | Search issues using [YouTrack query syntax](https://www.jetbrains.com/help/youtrack/server/Search-and-Command-Attributes.html) |
| `get_issue` | Get full details of a specific issue (description, comments, fields) |
| `list_projects` | List all accessible projects |
| `get_agiles` | List all agile boards |
| `list_templates` | List available issue templates |
| `create_issue_from_template` | Create an issue using a template (bug, feature, task, daily, spike, release) |
| `create_issue` | Create a new issue in a project (freeform) |
| `update_issue` | Update issue fields (summary, description, state, assignee) |
| `get_issue_history` | View change history of an issue (who changed what, when) |
| `rollback_issue` | Revert a specific change using its activity ID |
| `delete_issue` | Soft-delete (state → Obsolete) or permanently delete an issue |
| `bulk_update_preview` | Preview which issues a bulk command would affect (dry run) |
| `bulk_update_execute` | Apply a command to all issues matching a query (auto-tags for rollback) |
| `bulk_rollback` | Undo all changes from a bulk update batch using its tag |
| `create_agile_board` | Create a new agile board for one or more projects |

## Setup for Claude Code

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

### 4. Verify the server works

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

You should see all thirteen tools listed.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `YOUTRACK_URL` | Yes | Your YouTrack instance URL (e.g., `https://company.youtrack.cloud`) |
| `YOUTRACK_TOKEN` | Yes | Permanent token (starts with `perm:`) |

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
- YouTrack API calls use HTTPS
- Consider using a token with minimal required permissions (read-only if you don't need `create_issue`)

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
