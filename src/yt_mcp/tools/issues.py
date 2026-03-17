from datetime import datetime, timezone

from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import format_issue_list, format_issue_detail, _resolve_state, _resolve_assignee, _get_custom_field, parse_issue_id


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def search_issues(query: str, max_results: int = 50, instance: str = "") -> str:
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
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
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
    async def get_issue(issue_id: str, include_comments: bool = True, instance: str = "") -> str:
        """Get full details of a specific YouTrack issue.

        Args:
            issue_id: Issue ID (e.g., 'DEVOPS-423') or YouTrack issue URL
            include_comments: Whether to include comments in output (default: True).
                             Set to False for long issues where comments aren't needed.
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
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
        project: str, summary: str, description: str = "", product: str = "",
        instance: str = "",
    ) -> str:
        """Create a new issue in a YouTrack project.

        Args:
            project: Project short name (e.g., 'DO', 'AP')
            summary: Issue title
            description: Issue description (markdown supported)
            product: Product name for the Product custom field (leave empty to skip)
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
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
        command: str = "",
        instance: str = "",
    ) -> str:
        """Update fields of an existing YouTrack issue.

        Returns previous field values so changes can be rolled back.

        The `command` parameter accepts any YouTrack command string and can set
        ANY field, including custom fields. Multiple commands can be combined in
        one string. Use this for fields not covered by the explicit parameters.

        Command examples:
            "Priority High"
            "Type Bug Priority Critical"
            "Deadline 2026-04-01"
            "Version Main"
            "Dev Estimate 12"
            "Assignee John, Jane"  (multiple assignees)
            "project MOBILE"  (move issue to another project)

        Args:
            issue_id: Issue ID (e.g., 'DEVOPS-423') or YouTrack issue URL
            summary: New title (leave empty to keep current)
            description: New description (leave empty to keep current)
            state: New state name (e.g., 'In Progress', 'Done', 'Open')
            assignee: New assignee login or full name (leave empty to keep current)
            product: Product name for the Product custom field (leave empty to keep current)
            add_tag: Tag name to add to the issue (leave empty to skip)
            remove_tag: Tag name to remove from the issue (leave empty to skip)
            command: YouTrack command string for any field (e.g., 'Priority High Type Bug')
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
        has_changes = (
            summary or description or state or assignee
            or product or add_tag or remove_tag or command
        )
        if not has_changes:
            return "Nothing to update — provide at least one field or command."

        # Snapshot before changes for rollback
        before = await client.get(
            f"/api/issues/{issue_id}",
            params={
                "fields": "idReadable,summary,description,"
                "state(name),assignee(name),tags(name),"
                "customFields(name,value(name,login))",
            },
        )

        old_summary = before.get("summary", "?")
        old_state = _resolve_state(before)
        old_assignee = _resolve_assignee(before)
        old_tags = [t.get("name", "") for t in before.get("tags", [])]

        # Collect old custom field values for rollback info
        old_fields: dict[str, str] = {}
        for cf in before.get("customFields", []):
            cf_name = cf.get("name", "")
            cf_value = cf.get("value")
            if cf_value is None:
                old_fields[cf_name] = "(empty)"
            elif isinstance(cf_value, list):
                old_fields[cf_name] = ", ".join(
                    v.get("name", v.get("login", "?")) for v in cf_value
                )
            elif isinstance(cf_value, dict):
                old_fields[cf_name] = cf_value.get("name", cf_value.get("login", "?"))
            else:
                old_fields[cf_name] = str(cf_value)

        # Apply REST API changes (summary, description)
        payload: dict = {}
        if summary:
            payload["summary"] = summary
        if description:
            payload["description"] = description

        if payload:
            await client.post(f"/api/issues/{issue_id}", json=payload)

        # Build command string from explicit params + raw command
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
        if command:
            commands.append(command)

        if commands:
            await client.execute_command(issue_id, " ".join(commands))

        # Fetch updated state
        after = await client.get(
            f"/api/issues/{issue_id}",
            params={
                "fields": "idReadable,summary,state(name),assignee(name),"
                "customFields(name,value(name,login)),tags(name)",
            },
        )
        new_state = _resolve_state(after)
        new_assignee = _resolve_assignee(after)
        new_tags = [t.get("name", "") for t in after.get("tags", [])]

        # Build response with changes + rollback info
        parts = [
            f"Updated: **{after.get('idReadable', '?')}** — {after.get('summary', '')}"
        ]

        # Show what changed
        changes = []
        if summary and summary != old_summary:
            changes.append(f"**Summary:** {old_summary} → {summary}")
        if new_state != old_state:
            changes.append(f"**State:** {old_state} → {new_state}")
        if new_assignee != old_assignee:
            changes.append(f"**Assignee:** {old_assignee} → {new_assignee}")
        if new_tags != old_tags:
            changes.append(f"**Tags:** {', '.join(old_tags) or '(none)'} → {', '.join(new_tags) or '(none)'}")

        # Check custom fields for changes
        for cf in after.get("customFields", []):
            cf_name = cf.get("name", "")
            cf_value = cf.get("value")
            if cf_value is None:
                new_val = "(empty)"
            elif isinstance(cf_value, list):
                new_val = ", ".join(
                    v.get("name", v.get("login", "?")) for v in cf_value
                )
            elif isinstance(cf_value, dict):
                new_val = cf_value.get("name", cf_value.get("login", "?"))
            else:
                new_val = str(cf_value)
            old_val = old_fields.get(cf_name, "(empty)")
            if new_val != old_val:
                changes.append(f"**{cf_name}:** {old_val} → {new_val}")

        if changes:
            parts.append("")
            parts.extend(changes)
        else:
            parts.append("No field changes detected.")

        # Rollback instructions
        rollback_parts = []
        if summary:
            rollback_parts.append(f"summary=\"{old_summary}\"")
        if state:
            rollback_parts.append(f"state=\"{old_state}\"")
        if assignee:
            rollback_parts.append(f"assignee=\"{old_assignee}\"")
        if command:
            rollback_parts.append(f"(use `rollback_issue` with activity ID for command fields)")

        if rollback_parts:
            parts.append("")
            parts.append(f"To restore: `update_issue({issue_id}, {', '.join(rollback_parts)})`")

        return "\n".join(parts)

    @mcp.tool()
    async def delete_issue(issue_id: str, permanent: bool = False, instance: str = "") -> str:
        """Delete a YouTrack issue. By default performs a soft delete (sets state to Obsolete).
        Use permanent=True only when you need to remove the issue entirely — this cannot be undone.

        Args:
            issue_id: Issue ID (e.g., 'DEVOPS-423') or YouTrack issue URL
            permanent: If True, permanently delete the issue. If False (default), set state to Obsolete.
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
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
    async def get_issue_links(issue_id: str, instance: str = "") -> str:
        """Get all linked issues (parent, subtask, depends on, relates to, duplicates).

        Args:
            issue_id: Issue ID (e.g., 'BAC-1828') or YouTrack issue URL
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
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
        instance: str = "",
    ) -> str:
        """Link two issues together.

        Args:
            issue_id: Source issue ID (e.g., 'BAC-1828') or YouTrack issue URL
            target_id: Target issue ID to link to (e.g., 'BAC-1234') or YouTrack issue URL
            link_type: Relationship type — 'Relates', 'Depends on', 'Is required for',
                       'Duplicates', 'Is duplicated by', 'Parent for', 'Subtask of'
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
        target_id = parse_issue_id(target_id)
        command = f"{link_type} {target_id}"
        await client.execute_command(issue_id, command)
        return f"Linked **{issue_id}** → **{target_id}** ({link_type})"

    @mcp.tool()
    async def remove_issue_link(
        issue_id: str,
        target_id: str,
        link_type: str = "Relates",
        instance: str = "",
    ) -> str:
        """Remove a link between two issues.

        Use get_issue_links to find existing links. The link_type must match
        the original link type used when creating the link.

        Args:
            issue_id: Source issue ID (e.g., 'BAC-1828') or YouTrack issue URL
            target_id: Target issue ID to unlink (e.g., 'BAC-1234') or YouTrack issue URL
            link_type: Relationship type to remove — 'Relates', 'Depends on', 'Is required for',
                       'Duplicates', 'Is duplicated by', 'Parent for', 'Subtask of'
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
        target_id = parse_issue_id(target_id)
        command = f"remove {link_type} {target_id}"
        await client.execute_command(issue_id, command)
        return f"Unlinked **{issue_id}** → **{target_id}** ({link_type})"

    @mcp.tool()
    async def add_comment(issue_id: str, text: str, instance: str = "") -> str:
        """Add a comment to a YouTrack issue.

        Args:
            issue_id: Issue ID (e.g., 'BAC-1828') or YouTrack issue URL
            text: Comment text (markdown supported)
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
        data = await client.post(
            f"/api/issues/{issue_id}/comments",
            json={"text": text},
        )
        author = data.get("author", {}).get("name", "?") if data else "?"
        return f"Comment added to **{issue_id}** by {author}:\n> {text[:200]}"

    @mcp.tool()
    async def update_comment(issue_id: str, comment_id: str, text: str, instance: str = "") -> str:
        """Update an existing comment on a YouTrack issue.

        Returns the previous text so it can be restored if needed.
        Use get_issue with include_comments=True to find comment IDs.

        Args:
            issue_id: Issue ID (e.g., 'BAC-1828') or YouTrack issue URL
            comment_id: Comment ID (e.g., '4-15.91-12345')
            text: New comment text (markdown supported)
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
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
    async def delete_comment(issue_id: str, comment_id: str, instance: str = "") -> str:
        """Delete a comment from a YouTrack issue.

        Returns the comment text so it can be re-added with add_comment if needed.
        Use get_issue with include_comments=True to find comment IDs.

        Args:
            issue_id: Issue ID (e.g., 'BAC-1828') or YouTrack issue URL
            comment_id: Comment ID (e.g., '4-15.91-12345')
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance, issue_id)
        issue_id = parse_issue_id(issue_id)
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

    @mcp.tool()
    async def poll_changes(
        query: str = "",
        since_minutes: int = 5,
        max_results: int = 50,
        instance: str = "",
    ) -> str:
        """Poll for recently changed issues. Useful for automation triggers (Make.com, n8n, cron).

        Returns issues updated within the last N minutes, with a summary of what changed.
        Call on a schedule (e.g., every 5 minutes) to detect new activity.

        Args:
            query: Optional YouTrack query to filter issues (e.g., 'project: DO'). Combined with the time filter.
            since_minutes: How far back to look for changes (default: 5 minutes)
            max_results: Maximum number of changed issues to return (default: 50)
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)

        now = datetime.now(tz=timezone.utc)
        since_ts = int((now.timestamp() - since_minutes * 60) * 1000)

        # Build query: combine user query with time filter
        time_filter = f"updated: {{{since_minutes}m ago}} .. {{Now}}"
        full_query = f"{query} {time_filter}".strip() if query else time_filter

        data = await client.get(
            "/api/issues",
            params={
                "query": full_query,
                "fields": "idReadable,summary,state(name),assignee(name),"
                "customFields(name,value(name)),updated",
                "$top": str(max_results),
            },
        )

        if not data:
            return f"No changes in the last {since_minutes} minutes."

        lines = [
            f"## Changes in the last {since_minutes} minutes",
            f"**Query:** `{full_query}`",
            f"**Issues changed:** {len(data)}",
            "",
        ]

        for issue in data:
            issue_id = issue.get("idReadable", "?")
            summary = issue.get("summary", "?")
            state = _resolve_state(issue)
            assignee = _resolve_assignee(issue)
            updated_ms = issue.get("updated")
            updated_str = ""
            if updated_ms:
                updated_str = datetime.fromtimestamp(
                    updated_ms / 1000, tz=timezone.utc
                ).strftime("%H:%M UTC")

            lines.append(
                f"- **{issue_id}** [{state}] {summary} → {assignee} ({updated_str})"
            )

        if len(data) >= max_results:
            lines.append(f"\n*Showing first {max_results}, more may exist.*")

        return "\n".join(lines)
