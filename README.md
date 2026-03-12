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

### 3. Restart Claude Code

```bash
claude
```

You should see `youtrack` listed when Claude starts. Try asking: *"List my YouTrack projects"*

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

- Python 3.10+
- `uv` (recommended) or `pip`

## License

MIT
