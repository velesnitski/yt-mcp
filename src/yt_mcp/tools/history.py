from datetime import datetime, timezone

from yt_mcp.client import YouTrackClient
from yt_mcp.formatters import format_value, parse_issue_id


def register(mcp, client: YouTrackClient):

    @mcp.tool()
    async def get_issue_history(issue_id: str, max_results: int = 20) -> str:
        """Get the change history of a YouTrack issue from the activity log.

        Shows who changed what field, when, and the old/new values.
        Useful for auditing changes or finding values to rollback.

        Args:
            issue_id: Issue ID (e.g., 'DEVOPS-423') or YouTrack issue URL
            max_results: Maximum number of activities to return (default: 20)
        """
        issue_id = parse_issue_id(issue_id)
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
            issue_id: Issue ID (e.g., 'DEVOPS-423') or YouTrack issue URL
            activity_id: Activity ID from get_issue_history (e.g., '0-0.88-598477')
        """
        issue_id = parse_issue_id(issue_id)
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

    @mcp.tool()
    async def get_work_items(issue_id: str) -> str:
        """Get time tracking work items for an issue.

        Args:
            issue_id: Issue ID (e.g., 'BAC-1828') or YouTrack issue URL
        """
        issue_id = parse_issue_id(issue_id)
        items = await client.get(
            f"/api/issues/{issue_id}/timeTracking/workItems",
            params={
                "fields": "id,date,duration(minutes),author(name),text,type(name)",
            },
        )

        if not items:
            return f"No work items found for **{issue_id}**."

        lines = [f"## Work items for {issue_id}", ""]
        total_minutes = 0
        for item in items:
            date_ms = item.get("date")
            if date_ms:
                date_str = datetime.fromtimestamp(
                    date_ms / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d")
            else:
                date_str = "?"

            duration = item.get("duration", {})
            minutes = duration.get("minutes", 0) if duration else 0
            total_minutes += minutes

            author = item.get("author", {}).get("name", "?")
            text = item.get("text", "")
            text_str = f" — {text}" if text else ""
            item_id = item.get("id", "?")
            work_type = item.get("type", {})
            type_str = f" [{work_type.get('name', '')}]" if work_type and work_type.get("name") else ""

            if minutes >= 60:
                duration_str = f"{minutes // 60}h {minutes % 60}m" if minutes % 60 else f"{minutes // 60}h"
            else:
                duration_str = f"{minutes}m"

            lines.append(f"- `{item_id}` {date_str}: **{duration_str}** by {author}{type_str}{text_str}")

        # Total
        if total_minutes >= 60:
            total_str = f"{total_minutes // 60}h {total_minutes % 60}m" if total_minutes % 60 else f"{total_minutes // 60}h"
        else:
            total_str = f"{total_minutes}m"
        lines.append(f"\n**Total:** {total_str}")

        return "\n".join(lines)

    @mcp.tool()
    async def add_work_item(
        issue_id: str,
        duration_minutes: int,
        date: str = "",
        description: str = "",
        work_type: str = "",
    ) -> str:
        """Log time (add a work item) to a YouTrack issue.

        Args:
            issue_id: Issue ID (e.g., 'BAC-1828') or YouTrack issue URL
            duration_minutes: Time spent in minutes (e.g., 90 for 1h 30m)
            date: Date of work in YYYY-MM-DD format (default: today)
            description: Optional description of work done
            work_type: Optional work type (e.g., 'Development', 'Testing', 'Documentation')
        """
        issue_id = parse_issue_id(issue_id)
        if duration_minutes <= 0:
            return "Duration must be positive."

        payload: dict = {
            "duration": {"minutes": duration_minutes},
        }

        if date:
            try:
                dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                payload["date"] = int(dt.timestamp() * 1000)
            except ValueError:
                return f"Invalid date format: `{date}`. Use YYYY-MM-DD."

        if description:
            payload["text"] = description

        if work_type:
            payload["type"] = {"name": work_type}

        data = await client.post(
            f"/api/issues/{issue_id}/timeTracking/workItems",
            json=payload,
        )

        if duration_minutes >= 60:
            dur_str = f"{duration_minutes // 60}h {duration_minutes % 60}m" if duration_minutes % 60 else f"{duration_minutes // 60}h"
        else:
            dur_str = f"{duration_minutes}m"

        item_id = data.get("id", "?") if data else "?"
        date_str = date or "today"
        desc_str = f" — {description}" if description else ""
        type_str = f" [{work_type}]" if work_type else ""
        return f"Logged **{dur_str}** on **{issue_id}** ({date_str}){type_str}{desc_str}\n**Work item ID:** `{item_id}`"

    @mcp.tool()
    async def update_work_item(
        issue_id: str,
        work_item_id: str,
        duration_minutes: int = 0,
        date: str = "",
        description: str = "",
    ) -> str:
        """Update an existing work item (time log entry) on a YouTrack issue.

        Returns previous values so the change can be reverted if needed.
        Use get_work_items to find work item IDs.

        Args:
            issue_id: Issue ID (e.g., 'BAC-1828') or YouTrack issue URL
            work_item_id: Work item ID (from get_work_items)
            duration_minutes: New duration in minutes (0 = keep current)
            date: New date in YYYY-MM-DD format (empty = keep current)
            description: New description (empty = keep current)
        """
        issue_id = parse_issue_id(issue_id)
        payload: dict = {}

        if duration_minutes > 0:
            payload["duration"] = {"minutes": duration_minutes}
        if date:
            try:
                dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                payload["date"] = int(dt.timestamp() * 1000)
            except ValueError:
                return f"Invalid date format: `{date}`. Use YYYY-MM-DD."
        if description:
            payload["text"] = description

        if not payload:
            return "Nothing to update — provide at least one field."

        # Fetch old values before updating
        old = await client.get(
            f"/api/issues/{issue_id}/timeTracking/workItems/{work_item_id}",
            params={"fields": "duration(minutes),date,text"},
        )
        old_duration = old.get("duration", {})
        old_minutes = old_duration.get("minutes", 0) if old_duration else 0
        old_date_ms = old.get("date")
        old_date_str = ""
        if old_date_ms:
            old_date_str = datetime.fromtimestamp(
                old_date_ms / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d")
        old_text = old.get("text", "")

        await client.post(
            f"/api/issues/{issue_id}/timeTracking/workItems/{work_item_id}",
            json=payload,
        )

        parts = [f"Updated work item `{work_item_id}` on **{issue_id}**:"]
        if duration_minutes > 0:
            if duration_minutes >= 60:
                dur_str = f"{duration_minutes // 60}h {duration_minutes % 60}m" if duration_minutes % 60 else f"{duration_minutes // 60}h"
            else:
                dur_str = f"{duration_minutes}m"
            if old_minutes >= 60:
                old_dur_str = f"{old_minutes // 60}h {old_minutes % 60}m" if old_minutes % 60 else f"{old_minutes // 60}h"
            else:
                old_dur_str = f"{old_minutes}m"
            parts.append(f"**Duration:** {old_dur_str} → {dur_str}")
        if date:
            parts.append(f"**Date:** {old_date_str} → {date}")
        if description:
            parts.append(f"**Description:** {old_text or '(empty)'} → {description}")
        parts.append("")
        parts.append(
            f"To restore, call `update_work_item` with: "
            f"duration_minutes={old_minutes}, date=\"{old_date_str}\""
        )
        return "\n".join(parts)

    @mcp.tool()
    async def delete_work_item(issue_id: str, work_item_id: str) -> str:
        """Delete a work item (time log entry) from a YouTrack issue.

        Returns the deleted work item details so it can be re-added with add_work_item if needed.
        Use get_work_items to find work item IDs.

        Args:
            issue_id: Issue ID (e.g., 'BAC-1828') or YouTrack issue URL
            work_item_id: Work item ID (from get_work_items)
        """
        issue_id = parse_issue_id(issue_id)
        # Fetch details before deleting
        old = await client.get(
            f"/api/issues/{issue_id}/timeTracking/workItems/{work_item_id}",
            params={"fields": "duration(minutes),date,text,author(name),type(name)"},
        )
        old_duration = old.get("duration", {})
        old_minutes = old_duration.get("minutes", 0) if old_duration else 0
        old_date_ms = old.get("date")
        old_date_str = ""
        if old_date_ms:
            old_date_str = datetime.fromtimestamp(
                old_date_ms / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d")
        old_text = old.get("text", "")
        old_author = old.get("author", {}).get("name", "?") if old else "?"
        old_type = old.get("type", {})
        old_type_str = old_type.get("name", "") if old_type else ""

        await client.delete(
            f"/api/issues/{issue_id}/timeTracking/workItems/{work_item_id}"
        )

        if old_minutes >= 60:
            dur_str = f"{old_minutes // 60}h {old_minutes % 60}m" if old_minutes % 60 else f"{old_minutes // 60}h"
        else:
            dur_str = f"{old_minutes}m"

        parts = [f"Work item `{work_item_id}` deleted from **{issue_id}**."]
        parts.append(f"**Duration:** {dur_str}")
        parts.append(f"**Date:** {old_date_str}")
        parts.append(f"**Author:** {old_author}")
        if old_type_str:
            parts.append(f"**Type:** {old_type_str}")
        if old_text:
            parts.append(f"**Description:** {old_text[:300]}")
        parts.append("")
        parts.append(
            f"To restore, call `add_work_item` with: "
            f"duration_minutes={old_minutes}, date=\"{old_date_str}\""
        )
        return "\n".join(parts)

    @mcp.tool()
    async def get_issue_changes_summary(
        issue_id: str,
        since: str = "",
    ) -> str:
        """Get a compact summary of issue changes: state transitions, assignee changes,
        and comment count. Filters noise (description edits, spent time).

        Args:
            issue_id: Issue ID (e.g., 'MAN-118') or YouTrack issue URL
            since: Optional ISO date to filter from (e.g., '2026-03-01'). Empty = all history.
        """
        issue_id = parse_issue_id(issue_id)
        # Fetch all activities (state, assignee, comments, work items)
        activities = await client.get(
            f"/api/issues/{issue_id}/activities",
            params={
                "fields": "id,timestamp,author(name),field(name),"
                "added(name,text),removed(name,text)",
                "categories": "CustomFieldCategory,SummaryCategory,"
                "DescriptionCategory,CommentsCategory,SpentTimeCategory",
                "$top": 500,
            },
        )

        # Also get basic issue info
        issue_data = await client.get(
            f"/api/issues/{issue_id}",
            params={
                "fields": "idReadable,summary,created,reporter(name),"
                "customFields(name,value(name))",
            },
        )

        # Parse since filter
        since_ts = 0
        if since:
            try:
                since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                since_ts = int(since_dt.timestamp() * 1000)
            except ValueError:
                pass

        # Filter by since
        if since_ts:
            activities = [a for a in activities if a.get("timestamp", 0) >= since_ts]

        # Extract state transitions
        state_transitions = []
        for a in activities:
            field_name = a.get("field", {}).get("name", "")
            if field_name == "State":
                added = a.get("added")
                ts = a.get("timestamp", 0)
                if isinstance(added, list) and added:
                    new_state = added[0].get("name", "?")
                elif isinstance(added, dict):
                    new_state = added.get("name", "?")
                else:
                    continue
                date_str = datetime.fromtimestamp(
                    ts / 1000, tz=timezone.utc
                ).strftime("%b %d")
                state_transitions.append((ts, new_state, date_str))

        # Extract comment info
        comment_authors: dict[str, int] = {}
        comment_count = 0
        for a in activities:
            field_name = a.get("field", {}).get("name", "")
            if field_name == "comments":
                added = a.get("added")
                if added:
                    comment_count += 1 if not isinstance(added, list) else len(added)
                    author = a.get("author", {}).get("name", "?")
                    comment_authors[author] = comment_authors.get(author, 0) + (
                        1 if not isinstance(added, list) else len(added)
                    )

        # Extract time logged
        total_minutes = 0
        time_authors: dict[str, int] = {}
        for a in activities:
            field_name = a.get("field", {}).get("name", "")
            if field_name == "Spent time":
                added = a.get("added")
                author = a.get("author", {}).get("name", "?")
                # Spent time added is typically a duration value
                if isinstance(added, list) and added:
                    for item in added:
                        mins = item.get("minutes", 0)
                        total_minutes += mins
                        time_authors[author] = time_authors.get(author, 0) + mins

        # Last activity
        last_activity_ts = 0
        last_activity_desc = ""
        for a in activities:
            ts = a.get("timestamp", 0)
            if ts > last_activity_ts:
                last_activity_ts = ts
                field_name = a.get("field", {}).get("name", "?")
                last_activity_desc = field_name

        # Build output
        created_ms = issue_data.get("created")
        created_str = ""
        if created_ms:
            created_str = datetime.fromtimestamp(
                created_ms / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d")
        reporter = issue_data.get("reporter", {})
        reporter_name = reporter.get("name", "?") if reporter else "?"

        from yt_mcp.formatters import _resolve_state
        current_state = _resolve_state(issue_data)

        parts = [f"## {issue_data.get('idReadable', issue_id)} — Change Summary"]

        if created_str:
            parts.append(f"**Created:** {created_str} by {reporter_name}")

        # State transitions line
        if state_transitions:
            transitions_str = " → ".join(
                f"{s} ({d})" for _, s, d in state_transitions
            )
            parts.append(f"**State transitions:** {transitions_str}")

        # Time in current state
        if state_transitions:
            last_state_ts = state_transitions[-1][0]
            days_in_state = (
                datetime.now(tz=timezone.utc)
                - datetime.fromtimestamp(last_state_ts / 1000, tz=timezone.utc)
            ).days
            parts.append(f"**Current state:** {current_state} ({days_in_state} days)")
        else:
            parts.append(f"**Current state:** {current_state}")

        # Comments
        if comment_count:
            authors_str = ", ".join(
                f"{name}" for name in comment_authors.keys()
            )
            parts.append(f"**Comments:** {comment_count} (by {authors_str})")

        # Time logged
        if total_minutes:
            if total_minutes >= 60:
                time_str = f"{total_minutes // 60}h {total_minutes % 60}m" if total_minutes % 60 else f"{total_minutes // 60}h"
            else:
                time_str = f"{total_minutes} min"
            authors_str = ", ".join(time_authors.keys())
            parts.append(f"**Time logged:** {time_str} (by {authors_str})")

        # Last activity
        if last_activity_ts:
            last_date = datetime.fromtimestamp(
                last_activity_ts / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d")
            parts.append(f"**Last activity:** {last_date} ({last_activity_desc})")

        return "\n".join(parts)
