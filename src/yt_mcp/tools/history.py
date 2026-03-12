from datetime import datetime, timezone

from yt_mcp.client import YouTrackClient
from yt_mcp.formatters import format_value


def register(mcp, client: YouTrackClient):

    @mcp.tool()
    async def get_issue_history(issue_id: str, max_results: int = 20) -> str:
        """Get the change history of a YouTrack issue from the activity log.

        Shows who changed what field, when, and the old/new values.
        Useful for auditing changes or finding values to rollback.

        Args:
            issue_id: Issue ID (e.g., 'DEVOPS-423')
            max_results: Maximum number of activities to return (default: 20)
        """
        activities = await client.get(
            f"/api/issues/{issue_id}/activities",
            params={
                "fields": "id,timestamp,author(name),field(name),"
                "added(name,text),removed(name,text)",
                "categories": "CustomFieldCategory,SummaryCategory,DescriptionCategory",
                "$top": str(max_results),
            },
        )

        if not activities:
            return f"No change history found for **{issue_id}**."

        lines = [f"## Change history for {issue_id}", ""]
        for a in activities:
            field = a.get("field", {}).get("name", "?")
            added = format_value(a.get("added"))
            removed = format_value(a.get("removed"))
            author = a.get("author", {}).get("name", "?")
            ts = datetime.fromtimestamp(
                a["timestamp"] / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
            activity_id = a.get("id", "?")
            lines.append(
                f"- `{activity_id}` **{field}**: {removed} → {added} "
                f"(by {author}, {ts})"
            )

        return "\n".join(lines)

    @mcp.tool()
    async def rollback_issue(issue_id: str, activity_id: str) -> str:
        """Rollback a specific change on a YouTrack issue by restoring the previous value.

        Use get_issue_history first to find the activity_id of the change to revert.

        Args:
            issue_id: Issue ID (e.g., 'DEVOPS-423')
            activity_id: Activity ID from get_issue_history (e.g., '0-0.88-598477')
        """
        activities = await client.get(
            f"/api/issues/{issue_id}/activities",
            params={
                "fields": "id,field(name),added(name,text),removed(name,text)",
                "categories": "CustomFieldCategory,SummaryCategory,DescriptionCategory",
                "$top": 100,
            },
        )

        target = None
        for a in activities:
            if a.get("id") == activity_id:
                target = a
                break

        if not target:
            return f"Activity `{activity_id}` not found for **{issue_id}**."

        field_name = target.get("field", {}).get("name", "")
        removed = target.get("removed")

        if field_name.lower() == "summary":
            if isinstance(removed, str):
                await client.post(
                    f"/api/issues/{issue_id}", json={"summary": removed}
                )
                return (
                    f"Rolled back **{issue_id}** summary:\n"
                    f"**Restored:** {removed}"
                )
            return "Cannot determine old summary value."

        if field_name.lower() == "description":
            old_desc = removed if isinstance(removed, str) else ""
            await client.post(
                f"/api/issues/{issue_id}", json={"description": old_desc}
            )
            return f"Rolled back **{issue_id}** description to previous version."

        if isinstance(removed, list) and removed:
            old_value = removed[0].get("name", "")
        elif isinstance(removed, list) and not removed:
            old_value = ""
        else:
            old_value = str(removed) if removed else ""

        if not old_value:
            return (
                f"Cannot rollback **{field_name}** — previous value was empty. "
                f"Use `update_issue` to manually set the desired value."
            )

        await client.execute_command(issue_id, f"{field_name} {old_value}")
        return (
            f"Rolled back **{issue_id}**:\n"
            f"**{field_name}:** restored to **{old_value}**"
        )
