from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import _resolve_state, _resolve_assignee, _get_custom_field


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def list_projects(instance: str = "") -> str:
        """List all accessible YouTrack projects.

        Args:
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
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
    async def get_agiles(instance: str = "") -> str:
        """List all agile boards in YouTrack.

        Args:
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
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
    async def get_agile_board(name: str, instance: str = "") -> str:
        """Search for an agile board by name, ID, or URL.

        Accepts:
            - Board name (partial match): 'iOS', 'Sprint Board'
            - Board ID: '98-114'
            - YouTrack URL: 'https://company.youtrack.cloud/agiles/98-114/current'

        Args:
            name: Board name, ID, or YouTrack agile board URL
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        import re

        client = resolver.resolve(instance, name)

        # Extract board ID from URL if provided
        url_match = re.search(r"/agiles/([\d-]+)", name)
        board_id = url_match.group(1) if url_match else None

        # If it looks like a board ID (digits and hyphens), try direct fetch
        if not board_id and re.match(r"^\d+-\d+$", name.strip()):
            board_id = name.strip()

        fields = (
            "id,name,projects(shortName,name),"
            "owner(name),currentSprint(name),"
            "columnSettings(field(name)),"
            "sprints(name,start,finish,archived)"
        )

        if board_id:
            try:
                board = await client.get(
                    f"/api/agiles/{board_id}",
                    params={"fields": fields},
                )
                matches = [board]
            except (ValueError, Exception):
                return f"No agile board found with ID '{board_id}'."
        else:
            boards = await client.get(
                "/api/agiles",
                params={"fields": fields},
            )
            query_lower = name.lower()
            matches = [b for b in boards if query_lower in b.get("name", "").lower()]

        if not matches:
            return f"No agile board found matching '{name}'."

        lines = []
        for b in matches:
            proj_names = ", ".join(
                f"{p.get('shortName', '?')} ({p.get('name', '?')})"
                for p in b.get("projects", [])
            )
            owner = b.get("owner", {})
            owner_name = owner.get("name", "?") if owner else "?"
            col_field = b.get("columnSettings", {}).get("field", {}).get("name", "?")
            current_sprint = b.get("currentSprint", {})
            sprint_name = current_sprint.get("name", "None") if current_sprint else "None"

            lines.append(f"# {b.get('name', '?')}")
            lines.append(f"**ID:** {b.get('id', '?')}")
            lines.append(f"**Owner:** {owner_name}")
            lines.append(f"**Projects:** {proj_names}")
            lines.append(f"**Column field:** {col_field}")
            lines.append(f"**Current sprint:** {sprint_name}")

            sprints = [s for s in b.get("sprints", []) if not s.get("archived")]
            if sprints:
                lines.append(f"\n**Active sprints ({len(sprints)}):**")
                for s in sprints:
                    lines.append(f"- {s.get('name', '?')}")

            lines.append("")

        return "\n".join(lines)

    @mcp.tool()
    async def create_agile_board(
        name: str,
        projects: str,
        column_field: str = "State",
        instance: str = "",
    ) -> str:
        """Create a new agile board in YouTrack.

        Args:
            name: Board name (e.g., 'My Sprint Board')
            projects: Comma-separated project short names (e.g., 'DO,BAC')
            column_field: Field to use for columns (default: 'State')
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
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

    @mcp.tool()
    async def delete_agile_board(board_name: str, instance: str = "") -> str:
        """Delete an agile board from YouTrack. The board's issues are NOT deleted.

        Uses case-insensitive partial matching to find the board.

        Args:
            board_name: Full or partial board name to delete
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
        boards = await client.get(
            "/api/agiles",
            params={"fields": "id,name,projects(shortName)"},
        )

        query_lower = board_name.lower()
        matches = [b for b in boards if query_lower in b.get("name", "").lower()]

        if not matches:
            return f"No agile board found matching '{board_name}'."

        if len(matches) > 1:
            names = ", ".join(f"'{b.get('name', '?')}'" for b in matches)
            return f"Multiple boards match '{board_name}': {names}. Be more specific."

        board = matches[0]
        board_id = board["id"]
        board_display_name = board.get("name", board_name)
        proj_names = ", ".join(
            p.get("shortName", "?") for p in board.get("projects", [])
        )

        await client.delete(f"/api/agiles/{board_id}")
        return (
            f"Deleted agile board: **{board_display_name}**\n"
            f"**Projects:** {proj_names}\n\n"
            f"Issues are not affected. To restore, call `create_agile_board`."
        )

    @mcp.tool()
    async def get_sprint_board(board_name: str, sprint: str = "current", instance: str = "") -> str:
        """Get issues on an agile board grouped by column (state).

        Args:
            board_name: Board name, ID (e.g., '98-114'), or YouTrack URL
            sprint: Sprint name or 'current' for active sprint (default: 'current')
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        import re

        client = resolver.resolve(instance, board_name)

        # Extract board ID from URL if provided
        url_match = re.search(r"/agiles/([\d-]+)", board_name)
        resolved_id = url_match.group(1) if url_match else None

        if not resolved_id and re.match(r"^\d+-\d+$", board_name.strip()):
            resolved_id = board_name.strip()

        fields = "id,name,currentSprint(id,name),sprints(id,name,start,finish,archived)"

        if resolved_id:
            try:
                board = await client.get(
                    f"/api/agiles/{resolved_id}",
                    params={"fields": fields},
                )
            except (ValueError, Exception):
                return f"No agile board found with ID '{resolved_id}'."
        else:
            boards = await client.get(
                "/api/agiles",
                params={"fields": fields},
            )
            query_lower = board_name.lower()
            matches = [b for b in boards if query_lower in b.get("name", "").lower()]
            if not matches:
                return f"No agile board found matching '{board_name}'."
            board = matches[0]

        board_id = board["id"]
        board_display_name = board.get("name", board_name)

        # Find the sprint
        sprint_id = None
        sprint_name = None

        if sprint.lower() == "current":
            cs = board.get("currentSprint")
            if cs:
                sprint_id = cs.get("id")
                sprint_name = cs.get("name", "Current Sprint")
            else:
                # Try to find first non-archived sprint
                active = [s for s in board.get("sprints", []) if not s.get("archived")]
                if active:
                    sprint_id = active[-1].get("id")
                    sprint_name = active[-1].get("name", "?")
                else:
                    return f"No current sprint found for board '{board_display_name}'."
        else:
            sprint_lower = sprint.lower()
            for s in board.get("sprints", []):
                if sprint_lower in s.get("name", "").lower():
                    sprint_id = s.get("id")
                    sprint_name = s.get("name", sprint)
                    break
            if not sprint_id:
                return f"Sprint '{sprint}' not found on board '{board_display_name}'."

        # Fetch sprint issues via board cells
        sprint_data = await client.get(
            f"/api/agiles/{board_id}/sprints/{sprint_id}",
            params={
                "fields": "name,start,finish,"
                "board(columns(presentation,wipLimit,"
                "issues(idReadable,summary,assignee(name),"
                "customFields(name,value(name)))))",
            },
        )

        start = sprint_data.get("start")
        finish = sprint_data.get("finish")
        date_range = ""
        if start and finish:
            from datetime import datetime, timezone
            start_str = datetime.fromtimestamp(start / 1000, tz=timezone.utc).strftime("%b %d")
            finish_str = datetime.fromtimestamp(finish / 1000, tz=timezone.utc).strftime("%b %d")
            date_range = f" ({start_str} - {finish_str})"

        parts = [f"## {board_display_name} — {sprint_name}{date_range}"]

        board_data = sprint_data.get("board", {})
        columns = board_data.get("columns", [])

        if not columns:
            parts.append("\nNo columns/issues found in this sprint.")
            return "\n".join(parts)

        for col in columns:
            col_name = col.get("presentation", "?")
            issues = col.get("issues", [])
            parts.append(f"\n### {col_name} ({len(issues)})")
            for issue in issues:
                assignee_name = _resolve_assignee(issue)
                parts.append(
                    f"- {issue.get('idReadable', '?')} "
                    f"{issue.get('summary', 'No summary')} "
                    f"→ {assignee_name}"
                )

        return "\n".join(parts)
