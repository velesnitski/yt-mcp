import os
import json
from mcp.server.fastmcp import FastMCP
import httpx

mcp = FastMCP("youtrack")

YOUTRACK_URL = os.environ.get("YOUTRACK_URL", "").rstrip("/")
YOUTRACK_TOKEN = os.environ.get("YOUTRACK_TOKEN", "")


def _headers():
    return {
        "Authorization": f"Bearer {YOUTRACK_TOKEN}",
        "Accept": "application/json",
    }


def _format_issues(issues: list) -> str:
    if not issues:
        return "No issues found."
    lines = []
    for issue in issues:
        assignee = issue.get("assignee", {})
        assignee_name = assignee.get("name", "Unassigned") if assignee else "Unassigned"
        state = issue.get("state", {})
        state_name = state.get("name", "Unknown") if state else "Unknown"
        lines.append(
            f"- **{issue.get('idReadable', '?')}** [{state_name}] "
            f"{issue.get('summary', 'No summary')} → {assignee_name}"
        )
    return "\n".join(lines)


@mcp.tool()
async def search_issues(query: str, max_results: int = 50) -> str:
    """Search YouTrack issues using YouTrack query syntax.

    Examples:
        - "project: Android state: Open"
        - "project: DevOps updated: -1w"
        - "assignee: me tag: urgent"
        - "#Unresolved project: WordPress"
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{YOUTRACK_URL}/api/issues",
            params={
                "query": query,
                "fields": "idReadable,summary,state(name),assignee(name),created,updated",
                "$top": str(max_results),
            },
            headers=_headers(),
        )
        resp.raise_for_status()
        return _format_issues(resp.json())


@mcp.tool()
async def get_issue(issue_id: str) -> str:
    """Get full details of a specific YouTrack issue by its ID (e.g., 'DEVOPS-423')."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{YOUTRACK_URL}/api/issues/{issue_id}",
            params={
                "fields": "idReadable,summary,description,state(name),priority(name),"
                "assignee(name),created,updated,resolved,"
                "comments(text,author(name),created),"
                "tags(name),customFields(name,value(name))",
            },
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

        parts = [
            f"# {data.get('idReadable', '?')}: {data.get('summary', '')}",
            "",
            f"**State:** {data.get('state', {}).get('name', '?')}",
            f"**Priority:** {data.get('priority', {}).get('name', '?')}",
        ]

        assignee = data.get("assignee")
        parts.append(f"**Assignee:** {assignee.get('name') if assignee else 'Unassigned'}")

        tags = data.get("tags", [])
        if tags:
            parts.append(f"**Tags:** {', '.join(t.get('name', '') for t in tags)}")

        desc = data.get("description")
        if desc:
            parts.extend(["", "## Description", desc])

        comments = data.get("comments", [])
        if comments:
            parts.extend(["", f"## Comments ({len(comments)})"])
            for c in comments:
                author = c.get("author", {}).get("name", "Unknown")
                parts.append(f"**{author}:** {c.get('text', '')}")
                parts.append("")

        return "\n".join(parts)


@mcp.tool()
async def list_projects() -> str:
    """List all accessible YouTrack projects."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{YOUTRACK_URL}/api/admin/projects",
            params={"fields": "shortName,name,archived,leader(name)"},
            headers=_headers(),
        )
        resp.raise_for_status()
        projects = resp.json()
        lines = []
        for p in projects:
            status = " (archived)" if p.get("archived") else ""
            leader = p.get("leader", {})
            leader_name = leader.get("name", "?") if leader else "?"
            lines.append(f"- **{p.get('shortName', '?')}** — {p.get('name', '?')}{status} (lead: {leader_name})")
        return "\n".join(lines) if lines else "No projects found."


@mcp.tool()
async def get_agiles() -> str:
    """List all agile boards in YouTrack."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{YOUTRACK_URL}/api/agiles",
            params={"fields": "id,name,projects(shortName,name),owner(name)"},
            headers=_headers(),
        )
        resp.raise_for_status()
        boards = resp.json()
        lines = []
        for b in boards:
            projects = ", ".join(p.get("shortName", "?") for p in b.get("projects", []))
            owner = b.get("owner", {})
            owner_name = owner.get("name", "?") if owner else "?"
            lines.append(f"- **{b.get('name', '?')}** (projects: {projects}, owner: {owner_name})")
        return "\n".join(lines) if lines else "No agile boards found."


@mcp.tool()
async def create_issue(project: str, summary: str, description: str = "") -> str:
    """Create a new issue in a YouTrack project.

    Args:
        project: Project short name (e.g., 'DEVOPS', 'Android')
        summary: Issue title
        description: Issue description (markdown supported)
    """
    payload = {
        "project": {"id": None},
        "summary": summary,
        "description": description,
    }

    # First resolve project ID
    async with httpx.AsyncClient(timeout=30) as client:
        proj_resp = await client.get(
            f"{YOUTRACK_URL}/api/admin/projects",
            params={"query": f"shortName: {project}", "fields": "id,shortName"},
            headers=_headers(),
        )
        proj_resp.raise_for_status()
        projects = proj_resp.json()
        if not projects:
            return f"Project '{project}' not found."
        payload["project"]["id"] = projects[0]["id"]

        resp = await client.post(
            f"{YOUTRACK_URL}/api/issues",
            json=payload,
            headers={**_headers(), "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        return f"Created: **{data.get('idReadable', '?')}** — {data.get('summary', '')}"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
