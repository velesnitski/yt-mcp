from yt_mcp.client import YouTrackClient
from yt_mcp.formatters import format_issue_list, format_issue_detail, _resolve_state, _get_custom_field


def register(mcp, client: YouTrackClient):

    @mcp.tool()
    async def search_issues(query: str, max_results: int = 50) -> str:
        """Search YouTrack issues using YouTrack query syntax.

        Date syntax notes:
            Working:     "updated: {Last week}", "updated: {Last month}", "created: 2026-03-12"
            NOT working: "updated: -7d", "updated: -180d .. -60d"
            Use named periods in curly braces for relative dates.

        Examples:
            - "project: Android state: Open"
            - "project: DevOps updated: {Last week}"
            - "assignee: me tag: urgent"
            - "#Unresolved project: WordPress"

        Args:
            query: YouTrack search query string
            max_results: Maximum number of results to return (default: 50)
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
        result = format_issue_list(data)

        count = len(data)
        if count == 0:
            return result
        header = f"**Found: {count} issues**"
        if count >= max_results:
            header += f" (showing first {max_results}, more may exist)"
        return f"{header}\n\n{result}"

    @mcp.tool()
    async def get_issue(issue_id: str, include_comments: bool = True) -> str:
        """Get full details of a specific YouTrack issue by its ID (e.g., 'DEVOPS-423').

        Args:
            issue_id: Issue ID (e.g., 'DEVOPS-423')
            include_comments: Whether to include comments in output (default: True).
                             Set to False for long issues where comments aren't needed.
        """
        fields = (
            "idReadable,summary,description,state(name),priority(name),"
            "assignee(name),created,updated,resolved,"
            "tags(name),customFields(name,value(name)),"
            "links(direction,linkType(name),issues(idReadable,summary,state(name),"
            "customFields(name,value(name))))"
        )
        if include_comments:
            fields += ",comments(id,text,author(name),created)"

        data = await client.get(
            f"/api/issues/{issue_id}",
            params={"fields": fields},
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
        add_tag: str = "",
        remove_tag: str = "",
    ) -> str:
        """Update fields of an existing YouTrack issue.

        Args:
            issue_id: Issue ID (e.g., 'DEVOPS-423')
            summary: New title (leave empty to keep current)
            description: New description (leave empty to keep current)
            state: New state name (e.g., 'In Progress', 'Done', 'Open')
            assignee: New assignee login or full name (leave empty to keep current)
            product: Product name for the Product custom field (leave empty to keep current)
            add_tag: Tag name to add to the issue (leave empty to skip)
            remove_tag: Tag name to remove from the issue (leave empty to skip)
        """
        payload: dict = {}
        if summary:
            payload["summary"] = summary
        if description:
            payload["description"] = description

        if not payload and not state and not assignee and not product and not add_tag and not remove_tag:
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
        if add_tag:
            commands.append(f"tag {add_tag}")
        if remove_tag:
            commands.append(f"untag {remove_tag}")

        if commands:
            await client.execute_command(issue_id, " ".join(commands))

        data = await client.get(
            f"/api/issues/{issue_id}",
            params={
                "fields": "idReadable,summary,state(name),assignee(name),"
                "customFields(name,value(name)),tags(name)",
            },
        )
        from yt_mcp.formatters import _resolve_state, _resolve_assignee
        a_name = _resolve_assignee(data)
        s_name = _resolve_state(data)
        tags = data.get("tags", [])
        tag_str = f" | **Tags:** {', '.join(t.get('name', '') for t in tags)}" if tags else ""
        return (
            f"Updated: **{data.get('idReadable', '?')}** — {data.get('summary', '')}\n"
            f"**State:** {s_name} | "
            f"**Assignee:** {a_name}{tag_str}"
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
            params={
                "fields": "idReadable,summary,state(name),"
                "customFields(name,value(name))",
            },
        )
        summary = data.get("summary", "")
        old_state = _resolve_state(data)

        if permanent:
            await client.delete(f"/api/issues/{issue_id}")
            return f"Permanently deleted: **{issue_id}** — {summary}"

        await client.execute_command(issue_id, "State Obsolete")
        return (
            f"Soft-deleted: **{issue_id}** — {summary}\n"
            f"**State:** {old_state} → Obsolete"
        )

    @mcp.tool()
    async def get_issue_links(issue_id: str) -> str:
        """Get all linked issues (parent, subtask, depends on, relates to, duplicates).

        Args:
            issue_id: Issue ID (e.g., 'BAC-1828')
        """
        data = await client.get(
            f"/api/issues/{issue_id}",
            params={
                "fields": "idReadable,summary,"
                "links(direction,linkType(name),"
                "issues(idReadable,summary,state(name),"
                "customFields(name,value(name))))",
            },
        )

        links = data.get("links", [])
        if not links:
            return f"No links found for **{issue_id}**."

        # Group by link type + direction
        groups: dict[str, list[str]] = {}
        for link in links:
            link_type = link.get("linkType", {}).get("name", "?")
            direction = link.get("direction", "BOTH")
            # Build a label from type + direction
            if direction == "OUTWARD":
                label = link_type
            elif direction == "INWARD":
                label = f"{link_type} (inward)"
            else:
                label = link_type

            for linked in link.get("issues", []):
                ls = linked.get("state")
                if ls and isinstance(ls, dict) and ls.get("name"):
                    linked_state = ls["name"]
                else:
                    linked_state = _get_custom_field(linked, "State") or "?"
                line = (
                    f"- {linked.get('idReadable', '?')} [{linked_state}] "
                    f"{linked.get('summary', '')}"
                )
                groups.setdefault(label, []).append(line)

        parts = [f"## Links for {data.get('idReadable', issue_id)}"]
        for label, items in groups.items():
            parts.append(f"\n### {label}")
            parts.extend(items)

        return "\n".join(parts)

    @mcp.tool()
    async def add_issue_link(
        issue_id: str,
        target_id: str,
        link_type: str = "Relates",
    ) -> str:
        """Link two issues together.

        Args:
            issue_id: Source issue ID (e.g., 'BAC-1828')
            target_id: Target issue ID to link to (e.g., 'BAC-1234')
            link_type: Relationship type — 'Relates', 'Depends on', 'Is required for',
                       'Duplicates', 'Is duplicated by', 'Parent for', 'Subtask of'
        """
        # Use YouTrack command API to create the link
        command = f"{link_type} {target_id}"
        await client.execute_command(issue_id, command)
        return f"Linked **{issue_id}** → **{target_id}** ({link_type})"

    @mcp.tool()
    async def remove_issue_link(
        issue_id: str,
        target_id: str,
        link_type: str = "Relates",
    ) -> str:
        """Remove a link between two issues.

        Use get_issue_links to find existing links. The link_type must match
        the original link type used when creating the link.

        Args:
            issue_id: Source issue ID (e.g., 'BAC-1828')
            target_id: Target issue ID to unlink (e.g., 'BAC-1234')
            link_type: Relationship type to remove — 'Relates', 'Depends on', 'Is required for',
                       'Duplicates', 'Is duplicated by', 'Parent for', 'Subtask of'
        """
        # YouTrack command: prefix with "remove" to delete a link
        command = f"remove {link_type} {target_id}"
        await client.execute_command(issue_id, command)
        return f"Unlinked **{issue_id}** → **{target_id}** ({link_type})"

    @mcp.tool()
    async def add_comment(issue_id: str, text: str) -> str:
        """Add a comment to a YouTrack issue.

        Args:
            issue_id: Issue ID (e.g., 'BAC-1828')
            text: Comment text (markdown supported)
        """
        data = await client.post(
            f"/api/issues/{issue_id}/comments",
            json={"text": text},
        )
        author = data.get("author", {}).get("name", "?") if data else "?"
        return f"Comment added to **{issue_id}** by {author}:\n> {text[:200]}"

    @mcp.tool()
    async def update_comment(issue_id: str, comment_id: str, text: str) -> str:
        """Update an existing comment on a YouTrack issue.

        Returns the previous text so it can be restored if needed.
        Use get_issue with include_comments=True to find comment IDs.

        Args:
            issue_id: Issue ID (e.g., 'BAC-1828')
            comment_id: Comment ID (e.g., '4-15.91-12345')
            text: New comment text (markdown supported)
        """
        old = await client.get(
            f"/api/issues/{issue_id}/comments/{comment_id}",
            params={"fields": "text"},
        )
        old_text = old.get("text", "") if old else ""

        await client.update_comment(issue_id, comment_id, text)
        return (
            f"Comment `{comment_id}` updated on **{issue_id}**:\n"
            f"**Previous text:** {old_text[:300]}\n"
            f"**New text:** {text[:300]}\n\n"
            f"To restore, call `update_comment` with the previous text."
        )

    @mcp.tool()
    async def delete_comment(issue_id: str, comment_id: str) -> str:
        """Delete a comment from a YouTrack issue.

        Returns the comment text so it can be re-added with add_comment if needed.
        Use get_issue with include_comments=True to find comment IDs.

        Args:
            issue_id: Issue ID (e.g., 'BAC-1828')
            comment_id: Comment ID (e.g., '4-15.91-12345')
        """
        old = await client.get(
            f"/api/issues/{issue_id}/comments/{comment_id}",
            params={"fields": "text,author(name)"},
        )
        old_text = old.get("text", "") if old else ""
        old_author = old.get("author", {}).get("name", "?") if old else "?"

        await client.delete(f"/api/issues/{issue_id}/comments/{comment_id}")
        return (
            f"Comment `{comment_id}` deleted from **{issue_id}**.\n"
            f"**Author:** {old_author}\n"
            f"**Deleted text:** {old_text[:500]}\n\n"
            f"To restore, call `add_comment` with the text above."
        )
