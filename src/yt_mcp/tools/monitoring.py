import asyncio
import re
from datetime import datetime, timezone

from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import (
    _resolve_state, _resolve_assignee, _get_custom_field,
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
        ancient_days: int = 200,
        limit_per_category: int = 10,
        deadline_warning_days: int = 7,
        exclude_patterns: str = "",
        instance: str = "",
    ) -> str:
        """Find issues at risk: stalled, forgotten, unestimated, ancient, overdue, or over estimate.

        Categories (ordered by urgency):
        - Overdue: past their Deadline date (if Deadline field is used)
        - Approaching deadline: within N days of Deadline (if field is used)
        - Stalled: In Progress / In Review / Ready for Test with no updates in N days
          (actively worked on but went silent — high urgency)
        - Over estimate: spent time exceeds estimate (if time tracking is used)
        - Unestimated: active issues without an Estimation field set
        - Ancient: issues open for more than N days (default: 200)
        - Forgotten: Submitted / Pause / To Do with no updates in 30+ days
          (filed but never started or intentionally paused — lower urgency)

        Args:
            project: Project short name (e.g., 'DO', 'AP', 'BAC')
            stale_days: Days without updates to flag In Progress issues (default: 7)
            forgotten_days: Days without updates to flag Submitted/Pause issues (default: 30)
            ancient_days: Days since creation to flag as ancient (default: 200)
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
        unestimated: list[tuple[int, str]] = []
        ancient: list[tuple[int, str]] = []

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

            # Scan custom fields once for deadline and estimate data
            custom_fields = issue.get("customFields", [])
            estimate_minutes = 0
            spent_minutes = 0

            for cf in custom_fields:
                cf_name = cf.get("name", "").lower()
                val = cf.get("value")
                if val is None:
                    continue

                # Deadline check
                if cf_name in ("deadline", "due date", "due"):
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
                    if deadline_ts:
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

                # Estimate check
                elif cf_name in ("estimation", "estimate", "dev estimate", "dev estimation"):
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

            # Unestimated: active issues without estimation
            if estimate_minutes == 0 and state_lower in (working_states | waiting_states):
                unestimated.append((days_idle, _format_at_risk_line(
                    issue_id, state, summary, assignee, priority,
                    "**no estimation**",
                )))

            # Ancient: open for too long
            created_ms = issue.get("created", 0)
            if created_ms:
                days_open = (now - datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)).days
                if days_open >= ancient_days:
                    ancient.append((days_open, _format_at_risk_line(
                        issue_id, state, summary, assignee, priority,
                        f"**{days_open}d old**",
                    )))

        overdue.sort(key=lambda x: x[0], reverse=True)
        approaching.sort(key=lambda x: x[0])
        stalled.sort(key=lambda x: x[0], reverse=True)
        forgotten.sort(key=lambda x: x[0], reverse=True)
        over_estimate.sort(key=lambda x: x[0], reverse=True)
        unestimated.sort(key=lambda x: x[0], reverse=True)
        ancient.sort(key=lambda x: x[0], reverse=True)

        total_risks = len(overdue) + len(approaching) + len(stalled) + len(forgotten) + len(over_estimate) + len(unestimated) + len(ancient)

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
        _append_category("Unestimated", unestimated)
        _append_category("Ancient — open too long", ancient)
        _append_category("Forgotten — filed/paused but idle", forgotten)

        return "\n".join(lines)

    @mcp.tool()
    async def check_task_creation(
        keywords: str,
        project: str = "",
        created_since: str = "7d",
        expected_priority: str = "",
        instance: str = "",
    ) -> str:
        """Check if a task matching keywords was created, and assess its quality.

        Useful for verifying that a requested task was actually created with
        proper fields (priority, assignee, description, subtasks).

        Args:
            keywords: Search keywords for the task (e.g., 'SOCKS proxy', 'server audit')
            project: Project short name to narrow search (optional)
            created_since: How far back to look — duration ('7d', '24h') or date ('2026-03-18'). Default: 7d.
            expected_priority: Expected priority level to verify (e.g., 'Critical'). Empty = don't check.
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
        since_ts = _parse_since(created_since)

        query = keywords
        if project:
            query = f"project: {project} {keywords}"

        issues = await client.get(
            "/api/issues",
            params={
                "query": query,
                "fields": "idReadable,summary,created,updated,state(name),"
                "priority(name),assignee(name),description,"
                "reporter(name),tags(name),"
                "customFields(name,value(name)),"
                "links(direction,linkType(name),issues(idReadable))",
                "$top": "20",
            },
        )

        if not issues:
            return (
                f"## Task creation check: \"{keywords}\"\n\n"
                f"**No matching issues found** in the last {created_since}.\n"
                f"The task may not have been created yet."
            )

        # Filter by creation date
        matches = [
            i for i in issues
            if i.get("created", 0) >= since_ts
        ]

        if not matches:
            # Issues exist but were created before the time window
            older = issues[:3]
            lines = [
                f"## Task creation check: \"{keywords}\"",
                "",
                f"No issues created in the last {created_since}, but found older matches:",
                "",
            ]
            for issue in older:
                issue_id = issue.get("idReadable", "?")
                summary = issue.get("summary", "?")
                created_ms = issue.get("created", 0)
                created_str = datetime.fromtimestamp(
                    created_ms / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d") if created_ms else "?"
                lines.append(f"- **{issue_id}** — {summary} (created {created_str})")
            return "\n".join(lines)

        lines = [
            f"## Task creation check: \"{keywords}\"",
            f"**Found: {len(matches)} matching issues**",
            "",
        ]

        for issue in matches:
            issue_id = issue.get("idReadable", "?")
            summary = issue.get("summary", "?")
            state = _resolve_state(issue)
            assignee = _resolve_assignee(issue)
            priority = _get_priority_name(issue)
            description = issue.get("description", "") or ""
            reporter = issue.get("reporter", {})
            reporter_name = reporter.get("name", "?") if reporter else "?"
            created_ms = issue.get("created", 0)
            created_str = datetime.fromtimestamp(
                created_ms / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d") if created_ms else "?"

            # Count subtasks
            subtask_count = 0
            for link in issue.get("links", []):
                if link.get("direction") == "OUTWARD" and "subtask" in link.get("linkType", {}).get("name", "").lower():
                    subtask_count += len(link.get("issues", []))

            # Quality checks
            quality_score = 0
            quality_max = 10
            checks: list[str] = []

            # Has assignee? (+2)
            if assignee != "Unassigned":
                quality_score += 2
                checks.append("Assignee: **assigned**")
            else:
                checks.append("Assignee: **missing**")

            # Has description? (+2)
            if len(description) > 20:
                quality_score += 2
                checks.append("Description: **present**")
            elif description:
                quality_score += 1
                checks.append("Description: **short**")
            else:
                checks.append("Description: **missing**")

            # Has priority? (+2)
            if priority and priority != "?":
                quality_score += 2
                if expected_priority and priority.lower() != expected_priority.lower():
                    checks.append(f"Priority: **{priority}** (expected: {expected_priority})")
                else:
                    checks.append(f"Priority: **{priority}**")
            else:
                checks.append("Priority: **not set**")

            # State moved beyond Submitted? (+2)
            if state.lower() not in ("submitted", "open", ""):
                quality_score += 2
                checks.append(f"State: **{state}** (progressing)")
            else:
                checks.append(f"State: **{state}**")

            # Has subtasks? (+2)
            if subtask_count > 0:
                quality_score += 2
                checks.append(f"Subtasks: **{subtask_count}**")
            else:
                checks.append("Subtasks: **none**")

            lines.append(f"### {issue_id} — {summary}")
            lines.append(f"**Created:** {created_str} by {reporter_name}")
            lines.append(f"**Quality: {quality_score}/{quality_max}**")
            lines.append("")
            for check in checks:
                lines.append(f"- {check}")
            lines.append("")

        return "\n".join(lines)

    @mcp.tool()
    async def get_creation_activity(
        project: str,
        since: str = "7d",
        creator: str = "",
        limit: int = 20,
        instance: str = "",
    ) -> str:
        """Get a report of recently created issues with quality indicators.

        Shows all issues created in a project within a time window, with checks for
        whether each has an assignee, description, priority, and has progressed beyond Submitted.
        Useful for tracking PM/lead follow-through on task creation.

        Args:
            project: Project short name (e.g., 'DO', 'AP', 'BAC')
            since: How far back to look — duration ('7d', '24h', '30d') or date ('2026-03-18'). Default: 7d.
            creator: Filter by creator name (optional, partial match)
            limit: Maximum number of issues (default: 20)
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
        since_ts = _parse_since(since)

        issues = await client.get(
            "/api/issues",
            params={
                "query": f"project: {project}",
                "fields": "idReadable,summary,created,state(name),"
                "priority(name),assignee(name),description,"
                "reporter(name),"
                "customFields(name,value(name)),"
                "links(direction,linkType(name),issues(idReadable))",
                "$top": "200",
            },
        )

        if not issues:
            return f"No issues found in **{project}**."

        # Filter by creation date
        recent = [i for i in issues if i.get("created", 0) >= since_ts]

        # Filter by creator if specified
        if creator:
            creator_lower = creator.lower()
            recent = [
                i for i in recent
                if creator_lower in (i.get("reporter", {}) or {}).get("name", "").lower()
            ]

        if not recent:
            creator_str = f" by {creator}" if creator else ""
            return f"No issues created in **{project}**{creator_str} since {since}."

        # Sort by created date descending
        recent.sort(key=lambda x: x.get("created", 0), reverse=True)
        recent = recent[:limit]

        # Compute stats
        total = len(recent)
        has_assignee = sum(1 for i in recent if _resolve_assignee(i) != "Unassigned")
        has_description = sum(1 for i in recent if len(i.get("description", "") or "") > 20)
        has_priority = sum(1 for i in recent if _get_priority_name(i) not in ("", "?"))
        progressed = sum(
            1 for i in recent
            if _resolve_state(i).lower() not in ("submitted", "open", "")
        )

        creator_str = f" by {creator}" if creator else ""
        lines = [
            f"# Creation activity — {project}{creator_str}",
            f"**Period:** since {since}",
            f"**Issues created:** {total}",
            "",
            "## Quality summary",
            f"- Assignee set: **{has_assignee}/{total}** ({has_assignee * 100 // total}%)" if total else "",
            f"- Description present: **{has_description}/{total}** ({has_description * 100 // total}%)" if total else "",
            f"- Priority set: **{has_priority}/{total}** ({has_priority * 100 // total}%)" if total else "",
            f"- Progressed beyond Submitted: **{progressed}/{total}** ({progressed * 100 // total}%)" if total else "",
            "",
            "## Issues",
            "",
        ]
        # Remove empty lines from conditional stats
        lines = [l for l in lines if l is not None]

        for issue in recent:
            issue_id = issue.get("idReadable", "?")
            summary = issue.get("summary", "?")
            state = _resolve_state(issue)
            assignee = _resolve_assignee(issue)
            priority = _get_priority_name(issue)
            description = issue.get("description", "") or ""
            reporter = issue.get("reporter", {})
            reporter_name = reporter.get("name", "?") if reporter else "?"
            created_ms = issue.get("created", 0)
            created_str = datetime.fromtimestamp(
                created_ms / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d") if created_ms else "?"

            # Quality indicators
            flags: list[str] = []
            if assignee == "Unassigned":
                flags.append("no assignee")
            if not description:
                flags.append("no description")
            if priority in ("", "?"):
                flags.append("no priority")
            if state.lower() in ("submitted", "open"):
                flags.append("not started")

            flag_str = f" — {', '.join(flags)}" if flags else ""

            lines.append(
                f"- **{issue_id}** [{state}] {summary}\n"
                f"  {reporter_name} → {assignee} | {priority} | {created_str}{flag_str}"
            )

        return "\n".join(lines)

    @mcp.tool()
    async def get_project_health(
        project: str,
        since: str = "24h",
        exclude_patterns: str = "",
        instance: str = "",
    ) -> str:
        """Get a project health report: state/product distribution, health metrics with percentages, and recently resolved issues.

        Designed for daily briefs and status reports. Shows unestimated, stuck,
        stale, ancient, blocked, and unassigned counts as % of total.

        Args:
            project: Project short name (e.g., 'DO', 'AP', 'BAC')
            since: Period for "recently resolved" — duration ('24h', '7d') or date ('2026-03-18'). Default: 24h.
            exclude_patterns: Comma-separated regex patterns to exclude (e.g., 'DevOps Daily,Report')
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
        patterns = compile_exclude_patterns(exclude_patterns)
        since_ts = _parse_since(since)

        # Fetch unresolved and recently resolved in parallel
        all_unresolved, all_resolved = await asyncio.gather(
            client.get(
                "/api/issues",
                params={
                    "query": f"project: {project} #Unresolved",
                    "fields": "idReadable,summary,created,updated,state(name),priority(name),"
                    "assignee(name),customFields(name,value(name,minutes))",
                    "$top": "500",
                },
            ),
            client.get(
                "/api/issues",
                params={
                    "query": f"project: {project} #Resolved",
                    "fields": "idReadable,summary,resolved,state(name),assignee(name),"
                    "customFields(name,value(name))",
                    "$top": "100",
                },
            ),
        )

        if patterns:
            all_unresolved = [i for i in all_unresolved if not should_exclude(i, patterns)]

        recently_resolved = [
            i for i in all_resolved
            if i.get("resolved", 0) >= since_ts
        ]

        total = len(all_unresolved) or 1
        now = datetime.now(tz=timezone.utc)

        state_counts: dict[str, int] = {}
        product_counts: dict[str, int] = {}
        unestimated = 0
        stuck = 0
        stale = 0
        ancient = 0
        blocked = 0
        unassigned_count = 0

        for issue in all_unresolved:
            state = _resolve_state(issue).lower()
            state_counts[state] = state_counts.get(state, 0) + 1

            product = _get_custom_field(issue, "Product") or "No product"
            product_counts[product] = product_counts.get(product, 0) + 1

            if _resolve_assignee(issue) == "Unassigned":
                unassigned_count += 1
            if state == "blocked":
                blocked += 1

            days_idle = _days_since_update(issue)
            created_ms = issue.get("created", 0)
            days_open = (now - datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)).days if created_ms else 0

            if state in ("in progress", "in review", "ready for test") and days_idle > 7:
                stuck += 1
            if days_idle > 30:
                stale += 1
            if days_open > 200:
                ancient += 1

            has_estimate = False
            for cf in issue.get("customFields", []):
                if cf.get("name", "").lower() in ("estimation", "estimate", "dev estimate", "dev estimation"):
                    if cf.get("value") is not None:
                        has_estimate = True
                    break
            if not has_estimate:
                unestimated += 1

        def pct(n: int) -> str:
            return f"{n * 100 // total}%"

        lines = [f"# {project} — Project Health", ""]

        lines.append("## Health metrics")
        lines.append("| Metric | Count | % | Severity |")
        lines.append("|---|---|---|---|")
        lines.append(f"| Total unresolved | {len(all_unresolved)} | 100% | — |")
        lines.append(f"| Unestimated | {unestimated} | {pct(unestimated)} | {'CRITICAL' if unestimated > total // 4 else 'HIGH'} |")
        lines.append(f"| Stuck (>7d in progress) | {stuck} | {pct(stuck)} | CRITICAL |")
        lines.append(f"| Stale (>30d no update) | {stale} | {pct(stale)} | HIGH |")
        lines.append(f"| Ancient (>200d open) | {ancient} | {pct(ancient)} | CRITICAL |")
        lines.append(f"| Blocked | {blocked} | {pct(blocked)} | MEDIUM |")
        lines.append(f"| Unassigned | {unassigned_count} | {pct(unassigned_count)} | MEDIUM |")
        lines.append("")

        lines.append("## By state")
        for state_name, count in sorted(state_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- **{state_name.title()}:** {count} ({pct(count)})")
        lines.append("")

        if len(product_counts) > 1:
            lines.append("## By product")
            for prod, count in sorted(product_counts.items(), key=lambda x: -x[1]):
                lines.append(f"- **{prod}:** {count}")
            lines.append("")

        if recently_resolved:
            lines.append(f"## Recently resolved ({len(recently_resolved)}) — since {since}")
            for issue in recently_resolved:
                issue_id = issue.get("idReadable", "?")
                summary = issue.get("summary", "?")
                state = _resolve_state(issue)
                assignee = _resolve_assignee(issue)
                lines.append(f"- **{issue_id}** [{state}] {summary} → {assignee}")
        else:
            lines.append(f"_No issues resolved since {since}._")

        return "\n".join(lines)
