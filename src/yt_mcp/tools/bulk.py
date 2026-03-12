import time

import httpx

from yt_mcp.client import YouTrackClient


def register(mcp, client: YouTrackClient):

    @mcp.tool()
    async def bulk_update_preview(query: str, command: str, max_results: int = 50) -> str:
        """Preview which issues would be affected by a bulk update (dry run).

        Always call this BEFORE bulk_update_execute to review the affected issues.

        Args:
            query: YouTrack search query to select issues (e.g., 'project: DO state: Open')
            command: YouTrack command to apply (e.g., 'State Done', 'Assignee John', 'tag Important')
            max_results: Maximum number of issues to preview (default: 50)
        """
        issues = await client.get(
            "/api/issues",
            params={
                "query": query,
                "fields": "idReadable,summary,state(name),assignee(name)",
                "$top": str(max_results),
            },
        )

        if not issues:
            return f"No issues match query: `{query}`"

        lines = [
            f"## Bulk update preview",
            f"**Query:** `{query}`",
            f"**Command:** `{command}`",
            f"**Issues affected:** {len(issues)}",
            "",
        ]
        for issue in issues:
            assignee = issue.get("assignee", {})
            assignee_name = assignee.get("name", "Unassigned") if assignee else "Unassigned"
            state = issue.get("state", {})
            state_name = state.get("name", "Unknown") if state else "Unknown"
            lines.append(
                f"- **{issue.get('idReadable', '?')}** [{state_name}] "
                f"{issue.get('summary', 'No summary')} → {assignee_name}"
            )

        lines.append("")
        lines.append("⚠ Call `bulk_update_execute` with the same query and command to apply.")
        return "\n".join(lines)

    @mcp.tool()
    async def bulk_update_execute(query: str, command: str, max_results: int = 50) -> str:
        """Execute a bulk update on issues matching a query. DESTRUCTIVE — call bulk_update_preview first.

        Each batch is tagged with a unique ID (e.g., 'yt-mcp-1741794000') for rollback.
        Use bulk_rollback with the batch tag to undo all changes from a batch.

        Args:
            query: YouTrack search query to select issues (e.g., 'project: DO state: Open')
            command: YouTrack command to apply (e.g., 'State Done', 'Assignee John', 'tag Important')
            max_results: Maximum number of issues to update (default: 50)
        """
        batch_tag = f"yt-mcp-{int(time.time())}"

        issues = await client.get(
            "/api/issues",
            params={
                "query": query,
                "fields": "idReadable,summary",
                "$top": str(max_results),
            },
        )

        if not issues:
            return f"No issues match query: `{query}`"

        tagged = []
        updated = []
        errors = []

        for issue in issues:
            issue_id = issue.get("idReadable", "?")
            try:
                await client.execute_command(issue_id, f"tag {batch_tag}")
                tagged.append(issue_id)
            except (httpx.HTTPStatusError, ValueError) as e:
                errors.append(f"{issue_id}: tag failed ({e})")

        for issue_id in tagged:
            try:
                await client.execute_command(issue_id, command)
                updated.append(issue_id)
            except (httpx.HTTPStatusError, ValueError) as e:
                errors.append(f"{issue_id}: command failed ({e})")

        lines = [f"## Bulk update complete"]
        lines.append(f"**Batch tag:** `{batch_tag}`")
        lines.append(f"**Command:** `{command}`")
        lines.append(f"**Updated:** {len(updated)} issues")
        if updated:
            lines.append(f"**IDs:** {', '.join(updated)}")
        if errors:
            lines.append(f"\n**Errors ({len(errors)}):**")
            for err in errors:
                lines.append(f"- {err}")
        lines.append("")
        lines.append(f"To undo: `bulk_rollback(batch_tag=\"{batch_tag}\")`")
        return "\n".join(lines)

    @mcp.tool()
    async def bulk_rollback(batch_tag: str) -> str:
        """Rollback all changes from a bulk update batch.

        Finds all issues tagged with the batch tag, looks up the changes made
        after the tag was applied, and reverts each change to its previous value.

        Args:
            batch_tag: The batch tag from bulk_update_execute (e.g., 'yt-mcp-1741794000')
        """
        issues = await client.get(
            "/api/issues",
            params={
                "query": f"tag: {{{batch_tag}}}",
                "fields": "idReadable,summary",
                "$top": 200,
            },
        )

        if not issues:
            return f"No issues found with tag `{batch_tag}`. The batch may have already been rolled back."

        try:
            batch_ts = int(batch_tag.split("-")[-1]) * 1000
        except (ValueError, IndexError):
            return f"Invalid batch tag format: `{batch_tag}`"

        rolled_back = []
        errors = []

        for issue in issues:
            issue_id = issue.get("idReadable", "?")
            try:
                activities = await client.get(
                    f"/api/issues/{issue_id}/activities",
                    params={
                        "fields": "id,timestamp,field(name),"
                        "added(name,text),removed(name,text)",
                        "categories": "CustomFieldCategory,SummaryCategory,"
                        "DescriptionCategory",
                        "$top": 50,
                    },
                )

                batch_changes = [
                    a for a in activities
                    if a.get("timestamp", 0) >= batch_ts
                    and a.get("timestamp", 0) <= batch_ts + 60000
                    and a.get("field", {}).get("name", "").lower() != "tag"
                ]

                for change in batch_changes:
                    field_name = change.get("field", {}).get("name", "")
                    removed = change.get("removed")

                    if field_name.lower() == "summary":
                        if isinstance(removed, str) and removed:
                            await client.post(
                                f"/api/issues/{issue_id}",
                                json={"summary": removed},
                            )
                            rolled_back.append(f"{issue_id}: summary restored")
                    elif field_name.lower() == "description":
                        old_desc = removed if isinstance(removed, str) else ""
                        await client.post(
                            f"/api/issues/{issue_id}",
                            json={"description": old_desc},
                        )
                        rolled_back.append(f"{issue_id}: description restored")
                    else:
                        if isinstance(removed, list) and removed:
                            old_value = removed[0].get("name", "")
                        else:
                            old_value = ""
                        if old_value:
                            await client.execute_command(
                                issue_id, f"{field_name} {old_value}"
                            )
                            rolled_back.append(f"{issue_id}: {field_name} → {old_value}")

                await client.execute_command(issue_id, f"untag {batch_tag}")

            except (httpx.HTTPStatusError, ValueError) as e:
                errors.append(f"{issue_id}: {e}")

        lines = [f"## Bulk rollback complete"]
        lines.append(f"**Batch tag:** `{batch_tag}`")
        lines.append(f"**Issues processed:** {len(issues)}")
        lines.append(f"**Changes reverted:** {len(rolled_back)}")
        if rolled_back:
            lines.append("")
            for r in rolled_back:
                lines.append(f"- {r}")
        if errors:
            lines.append(f"\n**Errors ({len(errors)}):**")
            for err in errors:
                lines.append(f"- {err}")
        return "\n".join(lines)
