from yt_mcp.client import YouTrackClient
from yt_mcp.formatters import format_issue_list, format_issue_detail


def register(mcp, client: YouTrackClient):

    @mcp.tool()
    async def search_issues(query: str, max_results: int = 50) -> str:
        """Search YouTrack issues using YouTrack query syntax.

        Examples:
            - "project: Android state: Open"
            - "project: DevOps updated: -1w"
            - "assignee: me tag: urgent"
            - "#Unresolved project: WordPress"
        """
        data = await client.get(
            "/api/issues",
            params={
                "query": query,
                "fields": "idReadable,summary,state(name),assignee(name),"
                "customFields(name,value(name)),created,updated",
                "$top": str(max_results),
            },
        )
        return format_issue_list(data)

    @mcp.tool()
    async def get_issue(issue_id: str) -> str:
        """Get full details of a specific YouTrack issue by its ID (e.g., 'DEVOPS-423')."""
        data = await client.get(
            f"/api/issues/{issue_id}",
            params={
                "fields": "idReadable,summary,description,state(name),priority(name),"
                "assignee(name),created,updated,resolved,"
                "comments(text,author(name),created),"
                "tags(name),customFields(name,value(name))",
            },
        )
        return format_issue_detail(data)

    @mcp.tool()
    async def create_issue(
        project: str, summary: str, description: str = "", product: str = ""
    ) -> str:
        """Create a new issue in a YouTrack project.

        Args:
            project: Project short name (e.g., 'DO', 'AP')
            summary: Issue title
            description: Issue description (markdown supported)
            product: Product name for the Product custom field (leave empty to skip)
        """
        project_id = await client.resolve_project_id(project)
        if not project_id:
            return f"Project '{project}' not found."

        data = await client.post(
            "/api/issues",
            json={
                "project": {"id": project_id},
                "summary": summary,
                "description": description,
            },
        )
        issue_id = data.get("idReadable", "?")

        product_str = ""
        if product:
            await client.execute_command(issue_id, f"Product {product}")
            product_str = f" | **Product:** {product}"

        return f"Created: **{issue_id}** — {data.get('summary', '')}{product_str}"

    @mcp.tool()
    async def update_issue(
        issue_id: str,
        summary: str = "",
        description: str = "",
        state: str = "",
        assignee: str = "",
        product: str = "",
    ) -> str:
        """Update fields of an existing YouTrack issue.

        Args:
            issue_id: Issue ID (e.g., 'DEVOPS-423')
            summary: New title (leave empty to keep current)
            description: New description (leave empty to keep current)
            state: New state name (e.g., 'In Progress', 'Done', 'Open')
            assignee: New assignee login or full name (leave empty to keep current)
            product: Product name for the Product custom field (leave empty to keep current)
        """
        payload: dict = {}
        if summary:
            payload["summary"] = summary
        if description:
            payload["description"] = description

        if not payload and not state and not assignee and not product:
            return "Nothing to update — provide at least one field."

        if payload:
            await client.post(f"/api/issues/{issue_id}", json=payload)

        commands = []
        if state:
            commands.append(f"State {state}")
        if assignee:
            commands.append(f"Assignee {assignee}")
        if product:
            commands.append(f"Product {product}")

        if commands:
            await client.execute_command(issue_id, " ".join(commands))

        data = await client.get(
            f"/api/issues/{issue_id}",
            params={"fields": "idReadable,summary,state(name),assignee(name)"},
        )
        a = data.get("assignee")
        return (
            f"Updated: **{data.get('idReadable', '?')}** — {data.get('summary', '')}\n"
            f"**State:** {data.get('state', {}).get('name', '?')} | "
            f"**Assignee:** {a.get('name') if a else 'Unassigned'}"
        )

    @mcp.tool()
    async def delete_issue(issue_id: str, permanent: bool = False) -> str:
        """Delete a YouTrack issue. By default performs a soft delete (sets state to Obsolete).
        Use permanent=True only when you need to remove the issue entirely — this cannot be undone.

        Args:
            issue_id: Issue ID (e.g., 'DEVOPS-423')
            permanent: If True, permanently delete the issue. If False (default), set state to Obsolete.
        """
        data = await client.get(
            f"/api/issues/{issue_id}",
            params={"fields": "idReadable,summary,state(name)"},
        )
        summary = data.get("summary", "")
        old_state = data.get("state", {}).get("name", "?")

        if permanent:
            await client.delete(f"/api/issues/{issue_id}")
            return f"Permanently deleted: **{issue_id}** — {summary}"

        await client.execute_command(issue_id, "State Obsolete")
        return (
            f"Soft-deleted: **{issue_id}** — {summary}\n"
            f"**State:** {old_state} → Obsolete"
        )
