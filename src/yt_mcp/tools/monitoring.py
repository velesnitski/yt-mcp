import asyncio
import re
from datetime import datetime, timezone

from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import (
    _resolve_state, _resolve_assignee,
    ISSUE_FIELDS,
    compile_exclude_patterns, should_exclude,
)
from yt_mcp.scoring import _get_priority_name, _days_since_update

_SINCE_RE = re.compile(r"^(\d+)\s*(h|d|m)$", re.IGNORECASE)

_SINCE_MULTIPLIERS = {"m": 60, "h": 3600, "d": 86400}


def _parse_since(since: str) -> int:
    """Parse since string to epoch milliseconds.

    Accepts:
        - Duration: '24h', '7d', '30m'
        - ISO date: '2026-03-18'
    Returns timestamp in milliseconds.
    """
    since = since.strip()
    match = _SINCE_RE.match(since)
    if match:
        value = int(match.group(1))
        unit = match.group(2).lower()
        seconds_ago = value * _SINCE_MULTIPLIERS[unit]
        return int((datetime.now(tz=timezone.utc).timestamp() - seconds_ago) * 1000)

    try:
        dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        pass

    # Default: 24h ago
    return int((datetime.now(tz=timezone.utc).timestamp() - 86400) * 1000)


def _format_at_risk_line(
    issue_id: str, state: str, summary: str, assignee: str, priority: str, extra: str,
) -> str:
    """Format a single at-risk issue line."""
    return (
        f"- **{issue_id}** [{state}] {summary}\n"
        f"  {assignee} | {priority} | {extra}"
    )


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def get_issues_digest(
        query: str,
        since: str = "24h",
        limit: int = 10,
        instance: str = "",
    ) -> str:
        """Get a digest of recent changes for issues matching a query.

        For each issue, shows state changes, new comments, and link updates
        since the specified time. Useful for daily standups, status checks,
        and automated reports.

        Args:
            query: YouTrack search query (e.g., 'project: DO State: {In Progress}', 'issue id: BAC-1828')
            since: How far back to look — duration ('24h', '7d', '30m') or date ('2026-03-18'). Default: 24h.
            limit: Maximum number of issues (default: 10)
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
        since_ts = _parse_since(since)

        issues = await client.get(
            "/api/issues",
            params={
                "query": query,
                "fields": "idReadable,summary,state(name),assignee(name),"
                "customFields(name,value(name)),updated",
                "$top": str(limit),
            },
        )

        if not issues:
            return f"No issues match query: `{query}`"

        # Fetch activities for all issues in parallel
        async def _fetch_activities(issue_id: str) -> list:
            try:
                return await client.get(
                    f"/api/issues/{issue_id}/activities",
                    params={
                        "fields": "id,timestamp,author(name),field(name),"
                        "added(name,text,idReadable),removed(name,text,idReadable)",
                        "categories": "CustomFieldCategory,SummaryCategory,"
                        "CommentsCategory,LinksCategory",
                        "$top": 100,
                    },
                )
            except (ValueError, Exception):
                return []

        all_activities = await asyncio.gather(
            *[_fetch_activities(i.get("idReadable", "?")) for i in issues]
        )

        lines = [
            f"## Issues digest (since {since})",
            f"**Query:** `{query}`",
            f"**Issues:** {len(issues)}",
            "",
        ]

        has_any_changes = False

        for issue, activities in zip(issues, all_activities):
            issue_id = issue.get("idReadable", "?")
            summary = issue.get("summary", "?")
            state = _resolve_state(issue)
            assignee = _resolve_assignee(issue)

            recent = [a for a in activities if a.get("timestamp", 0) >= since_ts]

            state_changes: list[str] = []
            comments_added: list[str] = []
            field_changes: list[str] = []
            link_changes: list[str] = []

            for a in recent:
                field_name = a.get("field", {}).get("name", "")
                author = a.get("author", {}).get("name", "?")
                added = a.get("added")
                removed = a.get("removed")
                ts = a.get("timestamp", 0)
                time_str = datetime.fromtimestamp(
                    ts / 1000, tz=timezone.utc
                ).strftime("%H:%M") if ts else ""

                if field_name == "State":
                    old_state = removed[0].get("name", "?") if isinstance(removed, list) and removed else ""
                    new_state = added[0].get("name", "?") if isinstance(added, list) and added else ""
                    if old_state and new_state:
                        state_changes.append(f"{old_state} → {new_state} (by {author}, {time_str})")
                elif field_name == "comments":
                    if added:
                        comments_added.append(author)
                elif field_name == "links":
                    if isinstance(added, list):
                        link_changes.extend(f"+ {lnk.get('idReadable', '?')}" for lnk in added)
                    if isinstance(removed, list):
                        link_changes.extend(f"- {lnk.get('idReadable', '?')}" for lnk in removed)
                elif field_name not in ("", "description"):
                    old_val = removed[0].get("name", str(removed)) if isinstance(removed, list) and removed else (removed if isinstance(removed, str) else "")
                    new_val = added[0].get("name", str(added)) if isinstance(added, list) and added else (added if isinstance(added, str) else "")
                    if old_val or new_val:
                        field_changes.append(f"{field_name}: {old_val or '(empty)'} → {new_val or '(empty)'}")

            lines.append(f"### {issue_id} [{state}] — {summary}")
            lines.append(f"**Assignee:** {assignee}")

            if not recent:
                lines.append("_No changes in this period._")
            else:
                has_any_changes = True
                for sc in state_changes:
                    lines.append(f"- **State:** {sc}")
                if comments_added:
                    author_counts: dict[str, int] = {}
                    for a in comments_added:
                        author_counts[a] = author_counts.get(a, 0) + 1
                    authors_str = ", ".join(
                        f"{name} ({c})" if c > 1 else name
                        for name, c in author_counts.items()
                    )
                    lines.append(f"- **Comments:** {sum(author_counts.values())} added (by {authors_str})")
                for fc in field_changes:
                    lines.append(f"- **{fc}**")
                if link_changes:
                    lines.append(f"- **Links:** {', '.join(link_changes)}")

                last_ts = max(a.get("timestamp", 0) for a in recent)
                if last_ts:
                    hours_ago = int((datetime.now(tz=timezone.utc) - datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)).total_seconds() / 3600)
                    if hours_ago < 1:
                        lines.append("- _Last active: <1h ago_")
                    elif hours_ago < 24:
                        lines.append(f"- _Last active: {hours_ago}h ago_")
                    else:
                        lines.append(f"- _Last active: {hours_ago // 24}d ago_")

            lines.append("")

        if not has_any_changes:
            lines.append("_No changes found for any issue in this period._")

        return "\n".join(lines)

    @mcp.tool()
    async def get_at_risk_issues(
        project: str,
        stale_days: int = 7,
        forgotten_days: int = 30,
        limit_per_category: int = 10,
        deadline_warning_days: int = 7,
        exclude_patterns: str = "",
        instance: str = "",
    ) -> str:
        """Find issues at risk: stalled, forgotten, overdue deadlines, or over estimate.

        Categories (ordered by urgency):
        - Overdue: past their Deadline date (if Deadline field is used)
        - Approaching deadline: within N days of Deadline (if field is used)
        - Stalled: In Progress / In Review / Ready for Test with no updates in N days
          (actively worked on but went silent — high urgency)
        - Over estimate: spent time exceeds estimate (if time tracking is used)
        - Forgotten: Submitted / Pause / To Do with no updates in 30+ days
          (filed but never started or intentionally paused — lower urgency)

        Args:
            project: Project short name (e.g., 'DO', 'AP', 'BAC')
            stale_days: Days without updates to flag In Progress issues (default: 7)
            forgotten_days: Days without updates to flag Submitted/Pause issues (default: 30)
            limit_per_category: Max issues shown per category (default: 10)
            deadline_warning_days: Days before deadline to flag as approaching (default: 7)
            exclude_patterns: Comma-separated regex patterns to exclude (e.g., 'DevOps Daily,Report')
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
        patterns = compile_exclude_patterns(exclude_patterns)

        at_risk_fields = (
            "idReadable,summary,updated,created,state(name),priority(name),"
            "assignee(name),tags(name),"
            "customFields(name,value(name,presentation,minutes)),"
            "links(direction,linkType(name),issues(idReadable))"
        )

        all_issues = await client.get(
            "/api/issues",
            params={
                "query": f"project: {project} #Unresolved",
                "fields": at_risk_fields,
                "$top": "500",
            },
        )

        if not all_issues:
            return f"No unresolved issues found in **{project}**."

        if patterns:
            all_issues = [i for i in all_issues if not should_exclude(i, patterns)]

        now = datetime.now(tz=timezone.utc)

        working_states = frozenset({"in progress", "in review", "ready for test"})
        waiting_states = frozenset({"submitted", "pause", "to do", "reopen"})

        overdue: list[tuple[int, str]] = []
        approaching: list[tuple[int, str]] = []
        stalled: list[tuple[int, str]] = []
        forgotten: list[tuple[int, str]] = []
        over_estimate: list[tuple[float, str]] = []

        for issue in all_issues:
            issue_id = issue.get("idReadable", "?")
            summary = issue.get("summary", "?")
            state = _resolve_state(issue)
            state_lower = state.lower()
            assignee = _resolve_assignee(issue)
            priority = _get_priority_name(issue)
            days_idle = _days_since_update(issue)

            if state_lower in working_states and days_idle >= stale_days:
                stalled.append((days_idle, _format_at_risk_line(
                    issue_id, state, summary, assignee, priority,
                    f"**{days_idle}d without updates**",
                )))
            elif state_lower in waiting_states and days_idle >= forgotten_days:
                forgotten.append((days_idle, _format_at_risk_line(
                    issue_id, state, summary, assignee, priority,
                    f"**{days_idle}d without updates**",
                )))

            # Deadline checks
            for cf in issue.get("customFields", []):
                cf_name = cf.get("name", "").lower()
                if cf_name not in ("deadline", "due date", "due"):
                    continue
                val = cf.get("value")
                if val is None:
                    continue
                deadline_ts = None
                if isinstance(val, (int, float)):
                    deadline_ts = val
                elif isinstance(val, dict):
                    pres = val.get("presentation", "")
                    if pres:
                        try:
                            deadline_ts = int(
                                datetime.strptime(pres, "%Y-%m-%d")
                                .replace(tzinfo=timezone.utc)
                                .timestamp() * 1000
                            )
                        except ValueError:
                            pass
                if not deadline_ts:
                    continue
                deadline_dt = datetime.fromtimestamp(deadline_ts / 1000, tz=timezone.utc)
                days_left = (deadline_dt - now).days
                deadline_str = deadline_dt.strftime("%Y-%m-%d")

                if days_left < 0:
                    overdue.append((abs(days_left), _format_at_risk_line(
                        issue_id, state, summary, assignee, priority,
                        f"Deadline: {deadline_str} (**{abs(days_left)}d overdue**)",
                    )))
                elif days_left <= deadline_warning_days:
                    approaching.append((days_left, _format_at_risk_line(
                        issue_id, state, summary, assignee, priority,
                        f"Deadline: {deadline_str} (**{days_left}d left**)",
                    )))

            # Over estimate check
            estimate_minutes = 0
            spent_minutes = 0
            for cf in issue.get("customFields", []):
                cf_name = cf.get("name", "").lower()
                val = cf.get("value")
                if val is None:
                    continue
                if cf_name in ("estimation", "estimate", "dev estimate", "dev estimation"):
                    estimate_minutes = val.get("minutes", 0) or 0 if isinstance(val, dict) else int(val) if isinstance(val, (int, float)) else 0
                elif cf_name in ("spent time", "spent"):
                    spent_minutes = val.get("minutes", 0) or 0 if isinstance(val, dict) else int(val) if isinstance(val, (int, float)) else 0

            if estimate_minutes > 0 and spent_minutes > estimate_minutes:
                ratio = spent_minutes / estimate_minutes
                est_str = f"{estimate_minutes // 60}h" if estimate_minutes >= 60 else f"{estimate_minutes}m"
                spent_str = f"{spent_minutes // 60}h" if spent_minutes >= 60 else f"{spent_minutes}m"
                over_estimate.append((ratio, _format_at_risk_line(
                    issue_id, state, summary, assignee, priority,
                    f"Estimate: {est_str}, Spent: {spent_str} (**{ratio:.0%}**)",
                )))

        overdue.sort(key=lambda x: x[0], reverse=True)
        approaching.sort(key=lambda x: x[0])
        stalled.sort(key=lambda x: x[0], reverse=True)
        forgotten.sort(key=lambda x: x[0], reverse=True)
        over_estimate.sort(key=lambda x: x[0], reverse=True)

        total_risks = len(overdue) + len(approaching) + len(stalled) + len(forgotten) + len(over_estimate)

        if total_risks == 0:
            return f"No at-risk issues found in **{project}** (stale: {stale_days}d, forgotten: {forgotten_days}d)."

        lines = [
            f"# At Risk Issues — {project}",
            f"**Total at risk:** {total_risks}",
            "",
        ]

        def _append_category(title: str, items: list[tuple[int | float, str]]) -> None:
            if not items:
                return
            lines.append(f"## {title} ({len(items)})")
            for _, line in items[:limit_per_category]:
                lines.append(line)
            if len(items) > limit_per_category:
                lines.append(f"_...and {len(items) - limit_per_category} more_")
            lines.append("")

        _append_category("Overdue", overdue)
        _append_category("Deadline approaching", approaching)
        _append_category("Stalled — actively worked on but went silent", stalled)
        _append_category("Over estimate", over_estimate)
        _append_category("Forgotten — filed/paused but idle", forgotten)

        return "\n".join(lines)
