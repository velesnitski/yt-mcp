from yt_mcp.client import YouTrackClient


def register(mcp, client: YouTrackClient):

    @mcp.tool()
    async def list_projects() -> str:
        """List all accessible YouTrack projects."""
        projects = await client.get(
            "/api/admin/projects",
            params={"fields": "shortName,name,archived,leader(name)"},
        )
        lines = []
        for p in projects:
            status = " (archived)" if p.get("archived") else ""
            leader = p.get("leader", {})
            leader_name = leader.get("name", "?") if leader else "?"
            lines.append(
                f"- **{p.get('shortName', '?')}** — {p.get('name', '?')}{status} (lead: {leader_name})"
            )
        return "\n".join(lines) if lines else "No projects found."

    @mcp.tool()
    async def get_agiles() -> str:
        """List all agile boards in YouTrack."""
        boards = await client.get(
            "/api/agiles",
            params={"fields": "id,name,projects(shortName,name),owner(name)"},
        )
        lines = []
        for b in boards:
            proj_names = ", ".join(p.get("shortName", "?") for p in b.get("projects", []))
            owner = b.get("owner", {})
            owner_name = owner.get("name", "?") if owner else "?"
            lines.append(
                f"- **{b.get('name', '?')}** (projects: {proj_names}, owner: {owner_name})"
            )
        return "\n".join(lines) if lines else "No agile boards found."

    @mcp.tool()
    async def create_agile_board(
        name: str,
        projects: str,
        column_field: str = "State",
    ) -> str:
        """Create a new agile board in YouTrack.

        Args:
            name: Board name (e.g., 'My Sprint Board')
            projects: Comma-separated project short names (e.g., 'DO,BAC')
            column_field: Field to use for columns (default: 'State')
        """
        project_list = [p.strip() for p in projects.split(",")]

        project_ids = []
        for short_name in project_list:
            pid = await client.resolve_project_id(short_name)
            if not pid:
                return f"Project '{short_name}' not found."
            project_ids.append({"id": pid})

        data = await client.post(
            "/api/agiles",
            json={
                "name": name,
                "projects": project_ids,
                "columnSettings": {
                    "field": {"name": column_field},
                    "$type": "ColumnSettings",
                },
            },
        )
        return (
            f"Created board: **{data.get('name', name)}**\n"
            f"**ID:** {data.get('id', '?')}\n"
            f"**Projects:** {', '.join(project_list)}"
        )
