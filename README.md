# yt-mcp

YouTrack MCP server for [Claude Code](https://claude.com/claude-code). Talk to your YouTrack instance in natural language.

## What it does

Gives Claude Code live access to your YouTrack instance. Instead of opening the YouTrack UI, just ask:

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
| `create_issue` | Create a new issue in a project |

## Setup

### 1. Get a YouTrack permanent token

1. Open YouTrack → **Profile** → **Account Security** → **Tokens** → **New token**
2. Set scope: `YouTrack`
3. Grant permissions: **Read Issue**, **Write Issue**, **Read Project** (or just use admin token for full access)
4. Copy the token — it starts with `perm:`

### 2. Install in Claude Code

Add to your Claude Code settings file:

**Per-project** — create/edit `.claude/settings.json` in your project root:

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

**Global** (all projects) — edit `~/.claude/settings.json`:

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
> {
>   "mcpServers": {
>     "youtrack": {
>       "command": "/full/path/to/uvx",
>       "args": ["--from", "git+https://github.com/velesnitski/yt-mcp", "yt-mcp"],
>       "env": {
>         "YOUTRACK_URL": "https://your-instance.youtrack.cloud",
>         "YOUTRACK_TOKEN": "perm:your-token-here"
>       }
>     }
>   }
> }
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
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{},"clientInfo":{"name":"test"},"protocolVersion":"2024-11-05"}}' \
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
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{},"clientInfo":{"name":"test"},"protocolVersion":"2024-11-05"}}' \
  | YOUTRACK_URL="https://your-instance.youtrack.cloud" \
    YOUTRACK_TOKEN="perm:your-token-here" \
    yt-mcp
```

**List available tools** by sending a `tools/list` request after initialization:

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{},"clientInfo":{"name":"test"},"protocolVersion":"2024-11-05"}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n' \
  | YOUTRACK_URL="https://your-instance.youtrack.cloud" \
    YOUTRACK_TOKEN="perm:your-token-here" \
    uvx --from git+https://github.com/velesnitski/yt-mcp yt-mcp
```

You should see all five tools listed: `search_issues`, `get_issue`, `list_projects`, `get_agiles`, `create_issue`.

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
pip install -r requirements.txt  # or: pip install mcp httpx
python src/yt_mcp/server.py
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

## Security

- Tokens are passed via environment variables — never hardcoded
- The server runs locally on your machine via stdio (no network exposure)
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
