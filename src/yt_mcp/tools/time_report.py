"""Time tracking reports grouped by user.

Monthly time reports aggregating work items by assignee/author.
"""

from datetime import datetime
from yt_mcp.formatters import compact_lines, escape_query_value
from yt_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver):
    """Register time reporting tools."""

    @mcp.tool()
    async def monthly_time_report_by_user(
        instance: str = "",
        projects: str = "",
        year: int = 2026,
        month: int = 7,
    ) -> str:
        """Generate a monthly time report aggregated by user.

        Groups work items by user (assignee) within a date range and sums:
        - Time spent
        - Time estimated
        - Number of issues touched

        Args:
            instance: YouTrack instance name/URL (auto-detected if blank)
            projects: Comma-separated project keys (all projects if blank)
            year: Report year (default: current)
            month: Report month 1–12 (default: current)

        Returns:
            Formatted time report table (user | time_spent | time_estimated | issues)
        """
        client = resolver.resolve(instance)

        # Validate month
        if not 1 <= month <= 12:
            raise ValueError(f"Month must be 1–12, got {month}")

        # Compute date range: first to last day of month
        import calendar
        _, days_in_month = calendar.monthrange(year, month)
        start_date = datetime(year, month, 1).isoformat()
        end_date = datetime(year, month, days_in_month).isoformat()

        # Build query: all work items in date range with time spent
        project_filter = ""
        if projects:
            project_keys = [p.strip() for p in projects.split(",")]
            escaped = " OR ".join(f"project: {escape_query_value(k)}" for k in project_keys)
            project_filter = f"({escaped}) AND "

        # Work items with non-zero time spent
        query = (
            f"{project_filter}"
            f"updated: {start_date}..{end_date} AND "
            f"work items: (duration > 0)"
        )

        # Fetch work items with assignee and time tracking fields
        result = await client.get(
            "/api/issues",
            params={
                "query": query,
                "fields": "idReadable(id),assignee(id,name,email),customFields(name,value(id,name))",
                "$top": 1000,  # Reasonable batch size
            },
        )

        issues = result.get("issue", [])
        if not issues:
            return f"No time entries for {year}-{month:02d}"

        # Aggregate by user
        user_stats: dict[str, dict] = {}

        for issue in issues:
            assignee = issue.get("assignee")
            if not assignee:
                continue

            user_id = assignee.get("id") or "unknown"
            user_name = assignee.get("name") or assignee.get("email") or user_id

            if user_name not in user_stats:
                user_stats[user_name] = {
                    "time_spent": 0,
                    "time_estimated": 0,
                    "issue_count": 0,
                }

            # Parse time spent from custom fields (YouTrack stores as "Spent time")
            custom_fields = issue.get("customFields", [])
            for field in custom_fields:
                field_name = field.get("name", "").lower()
                if "spent" in field_name:
                    val = field.get("value")
                    if val and isinstance(val, dict):
                        # Value may be an object with duration/minutes
                        if "id" in val:
                            # Parse duration (typically in minutes or seconds)
                            try:
                                duration = int(val.get("id", 0))
                                user_stats[user_name]["time_spent"] += duration
                            except (ValueError, TypeError):
                                pass

            user_stats[user_name]["issue_count"] += 1

        # Sort by time spent descending
        sorted_users = sorted(
            user_stats.items(),
            key=lambda x: x[1]["time_spent"],
            reverse=True,
        )

        # Format as table
        lines = [
            f"Time Report: {year}-{month:02d}",
            "",
            "User | Time Spent (min) | Time Est. (min) | Issues",
            "-" * 55,
        ]

        for user_name, stats in sorted_users:
            lines.append(
                f"{user_name:20} | {stats['time_spent']:15} | "
                f"{stats['time_estimated']:15} | {stats['issue_count']}"
            )

        if not sorted_users:
            lines.append("(no time entries)")

        lines.append("")
        lines.append(
            f"Total: {sum(s['time_spent'] for s in user_stats.values())} min "
            f"across {len(user_stats)} user(s)"
        )

        return compact_lines(lines)

    @mcp.tool()
    async def user_time_summary(
        instance: str = "",
        user_name: str = "",
        since: str = "",
    ) -> str:
        """Get detailed time summary for a specific user.

        Args:
            instance: YouTrack instance name/URL
            user_name: User name or email to query
            since: Start date (ISO format, e.g., '2026-07-01')

        Returns:
            User's time summary: total spent, estimated, issue count
        """
        if not user_name:
            raise ValueError("user_name is required")

        client = resolver.resolve(instance)

        # Query work items assigned to user
        query = f"assignee: {escape_query_value(user_name)} AND work items: (duration > 0)"
        if since:
            query += f" AND updated: {since}.."

        result = await client.get(
            "/api/issues",
            params={
                "query": query,
                "fields": "idReadable,assignee(name,email),customFields(name,value(id,name))",
                "$top": 500,
            },
        )

        issues = result.get("issue", [])
        total_spent = 0
        total_estimated = 0

        for issue in issues:
            custom_fields = issue.get("customFields", [])
            for field in custom_fields:
                field_name = field.get("name", "").lower()
                if "spent" in field_name:
                    val = field.get("value", {})
                    if isinstance(val, dict) and "id" in val:
                        try:
                            total_spent += int(val["id"])
                        except (ValueError, TypeError):
                            pass
                elif "estimate" in field_name:
                    val = field.get("value", {})
                    if isinstance(val, dict) and "id" in val:
                        try:
                            total_estimated += int(val["id"])
                        except (ValueError, TypeError):
                            pass

        lines = [
            f"Time Summary for: {user_name}",
            f"Total spent: {total_spent} min",
            f"Total estimated: {total_estimated} min",
            f"Issues: {len(issues)}",
        ]

        return compact_lines(lines)

