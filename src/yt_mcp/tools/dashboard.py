import asyncio
import re
from datetime import datetime, timezone

from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import _resolve_state, _resolve_assignee, _get_custom_field
from yt_mcp.scoring import (
    compute_active_score,
    compute_blocked_score,
    format_score_breakdown,
    _get_priority_name,
    _count_blockers,
    _count_blocking_others,
    _count_products,
    _days_since_update,
)

_ISSUE_FIELDS = (
    "idReadable,summary,updated,created,state(name),priority(name),"
    "assignee(name),tags(name),"
    "customFields(name,value(name)),"
    "links(direction,linkType(name),issues(idReadable))"
)

_DEFAULT_ACTIVE_STATES = "In Progress, Submitted, In Review, Ready for Test, Pause"


def _should_exclude(issue: dict, patterns: list[re.Pattern]) -> bool:
    """Check if issue summary matches any exclusion pattern."""
    summary = issue.get("summary", "")
    return any(p.search(summary) for p in patterns)


def _compile_patterns(exclude_patterns: str) -> list[re.Pattern]:
    """Compile comma-separated regex patterns."""
    if not exclude_patterns:
        return []
    return [
        re.compile(p.strip(), re.IGNORECASE)
        for p in exclude_patterns.split(",")
        if p.strip()
    ]


def _format_scored_issue(issue: dict, score: int, breakdown: dict[str, int]) -> str:
    """Format a single scored issue as a markdown line."""
    issue_id = issue.get("idReadable", "?")
    summary = issue.get("summary", "?")
    state = _resolve_state(issue)
    assignee = _resolve_assignee(issue)
    priority = _get_priority_name(issue)
    days = _days_since_update(issue)
    blockers = _count_blockers(issue)
    blocking_others = _count_blocking_others(issue)
    products = _count_products(issue)

    parts = [f"- **{issue_id}** (score: **{score}**) [{state}] {summary}"]
    detail = f"  {assignee} | {priority} | {days}d idle"
    if blockers:
        detail += f" | {blockers} subtasks"
    if blocking_others:
        detail += f" | blocking {blocking_others}"
    if products > 1:
        detail += f" | {products} products"
    parts.append(detail)
    parts.append(f"  _{format_score_breakdown(breakdown)}_")
    return "\n".join(parts)


_SINCE_RE = re.compile(r"^(\d+)\s*(h|d|m)$", re.IGNORECASE)


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
        multipliers = {"m": 60, "h": 3600, "d": 86400}
        seconds_ago = value * multipliers[unit]
        return int((datetime.now(tz=timezone.utc).timestamp() - seconds_ago) * 1000)

    # Try ISO date
    try:
        dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        pass

    # Default: 24h ago
    return int((datetime.now(tz=timezone.utc).timestamp() - 86400) * 1000)


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def get_top_active_issues(
        project: str,
        limit: int = 3,
        states: str = _DEFAULT_ACTIVE_STATES,
        exclude_patterns: str = "",
        instance: str = "",
    ) -> str:
        """Get top active issues for a project, ranked by weighted scoring model.

        Scores issues by priority, type, state, tags, staleness, and blocker count.
        Useful for daily standups, team briefs, and priority dashboards.

        Args:
            project: Project short name (e.g., 'AP', 'DO', 'BAC')
            limit: Number of top issues to return (default: 3)
            states: Comma-separated active states (default: 'In Progress, Submitted, In Review, Ready for Test')
            exclude_patterns: Comma-separated regex patterns to exclude (e.g., 'DevOps Daily,Report')
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
        patterns = _compile_patterns(exclude_patterns)
        state_set = {s.strip().lower() for s in states.split(",")}

        # Fetch all unresolved issues and filter by state client-side
        # (avoids YouTrack query syntax compatibility issues across versions)
        all_issues = await client.get(
            "/api/issues",
            params={
                "query": f"project: {project} #Unresolved",
                "fields": _ISSUE_FIELDS,
                "$top": "500",
            },
        )

        if not all_issues:
            return f"No active issues found in **{project}**."

        # Filter by state and exclusion patterns
        issues = [
            i for i in all_issues
            if _resolve_state(i).lower() in state_set
            and not (patterns and _should_exclude(i, patterns))
        ]

        # Score and sort
        scored = []
        for issue in issues:
            score, breakdown = compute_active_score(issue)
            scored.append((score, breakdown, issue))
        scored.sort(key=lambda x: x[0], reverse=True)

        total = len(scored)
        top = scored[:limit]

        lines = [
            f"## Top {len(top)} active issues in {project}",
            f"**Total active:** {total}",
            "",
        ]
        for score, breakdown, issue in top:
            lines.append(_format_scored_issue(issue, score, breakdown))
            lines.append("")

        return "\n".join(lines)

    @mcp.tool()
    async def get_top_blocked_issues(
        project: str,
        limit: int = 3,
        exclude_patterns: str = "",
        instance: str = "",
    ) -> str:
        """Get top blocked issues for a project, ranked by weighted scoring model.

        Scores blocked issues by priority, type, tags, how long they've been blocked, and blocker count.
        Useful for identifying long-standing blockers that need escalation.

        Args:
            project: Project short name (e.g., 'AP', 'DO', 'BAC')
            limit: Number of top issues to return (default: 3)
            exclude_patterns: Comma-separated regex patterns to exclude (e.g., 'DevOps Daily,Report')
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
        patterns = _compile_patterns(exclude_patterns)

        query = f"project: {project} State: {{Blocked}}"

        issues = await client.get(
            "/api/issues",
            params={
                "query": query,
                "fields": _ISSUE_FIELDS,
                "$top": "200",
            },
        )

        if not issues:
            return f"No blocked issues found in **{project}**."

        # Filter exclusions
        if patterns:
            issues = [i for i in issues if not _should_exclude(i, patterns)]

        # Score and sort
        scored = []
        for issue in issues:
            score, breakdown = compute_blocked_score(issue)
            scored.append((score, breakdown, issue))
        scored.sort(key=lambda x: x[0], reverse=True)

        total = len(scored)
        top = scored[:limit]

        lines = [
            f"## Top {len(top)} blocked issues in {project}",
            f"**Total blocked:** {total}",
            "",
        ]
        for score, breakdown, issue in top:
            lines.append(_format_scored_issue(issue, score, breakdown))
            lines.append("")

        return "\n".join(lines)

    @mcp.tool()
    async def get_team_dashboard(
        project: str,
        active_limit: int = 3,
        blocked_limit: int = 3,
        exclude_patterns: str = "",
        instance: str = "",
    ) -> str:
        """Get a combined priority dashboard for a project — top active, top blocked, and summary stats.

        Provides a quick overview for standups, manager briefs, or automated reports.

        Args:
            project: Project short name (e.g., 'AP', 'DO', 'BAC')
            active_limit: Number of top active issues (default: 3)
            blocked_limit: Number of top blocked issues (default: 3)
            exclude_patterns: Comma-separated regex patterns to exclude (e.g., 'DevOps Daily,Report')
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
        patterns = _compile_patterns(exclude_patterns)

        # Fetch all unresolved issues
        all_issues = await client.get(
            "/api/issues",
            params={
                "query": f"project: {project} #Unresolved",
                "fields": _ISSUE_FIELDS,
                "$top": "500",
            },
        )

        if not all_issues:
            return f"No unresolved issues found in **{project}**."

        # Filter exclusions
        if patterns:
            all_issues = [i for i in all_issues if not _should_exclude(i, patterns)]

        # Categorize by state
        active_states = {"in progress", "submitted", "in review", "ready for test", "pause"}
        active_issues = []
        blocked_issues = []
        state_counts: dict[str, int] = {}

        for issue in all_issues:
            state = _resolve_state(issue).lower()
            state_counts[state] = state_counts.get(state, 0) + 1
            if state in active_states:
                active_issues.append(issue)
            elif state == "blocked":
                blocked_issues.append(issue)

        # Score active
        scored_active = []
        for issue in active_issues:
            score, breakdown = compute_active_score(issue)
            scored_active.append((score, breakdown, issue))
        scored_active.sort(key=lambda x: x[0], reverse=True)

        # Score blocked
        scored_blocked = []
        for issue in blocked_issues:
            score, breakdown = compute_blocked_score(issue)
            scored_blocked.append((score, breakdown, issue))
        scored_blocked.sort(key=lambda x: x[0], reverse=True)

        # Build output
        lines = [f"# {project} — Team Dashboard", ""]

        # Summary stats
        lines.append("## Summary")
        lines.append(f"**Total unresolved:** {len(all_issues)}")
        for state_name in ["in progress", "submitted", "in review", "ready for test", "pause", "blocked", "open"]:
            count = state_counts.get(state_name, 0)
            if count:
                lines.append(f"**{state_name.title()}:** {count}")

        if scored_active:
            top_active = scored_active[0]
            lines.append(
                f"**Highest active score:** {top_active[2].get('idReadable', '?')} "
                f"(score: {top_active[0]})"
            )
        if scored_blocked:
            top_blocked = scored_blocked[0]
            lines.append(
                f"**Highest blocked score:** {top_blocked[2].get('idReadable', '?')} "
                f"(score: {top_blocked[0]})"
            )
        lines.append("")

        # Top active
        lines.append(f"## Top {min(active_limit, len(scored_active))} active issues ({len(active_issues)} total)")
        lines.append("")
        for score, breakdown, issue in scored_active[:active_limit]:
            lines.append(_format_scored_issue(issue, score, breakdown))
            lines.append("")

        if not scored_active:
            lines.append("No active issues.\n")

        # Top blocked
        lines.append(f"## Top {min(blocked_limit, len(scored_blocked))} blocked issues ({len(blocked_issues)} total)")
        lines.append("")
        for score, breakdown, issue in scored_blocked[:blocked_limit]:
            lines.append(_format_scored_issue(issue, score, breakdown))
            lines.append("")

        if not scored_blocked:
            lines.append("No blocked issues.\n")

        return "\n".join(lines)

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
        async def _fetch_activities(issue_id: str):
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

        issue_ids = [i.get("idReadable", "?") for i in issues]
        all_activities = await asyncio.gather(
            *[_fetch_activities(iid) for iid in issue_ids]
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

            # Filter activities to since window
            recent = [a for a in activities if a.get("timestamp", 0) >= since_ts]

            # Categorize changes
            state_changes = []
            comments_added = []
            field_changes = []
            link_changes = []

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
                    old_state = ""
                    new_state = ""
                    if isinstance(removed, list) and removed:
                        old_state = removed[0].get("name", "?")
                    if isinstance(added, list) and added:
                        new_state = added[0].get("name", "?")
                    if old_state and new_state:
                        state_changes.append(f"{old_state} → {new_state} (by {author}, {time_str})")
                elif field_name == "comments":
                    if added:
                        count = 1 if not isinstance(added, list) else len(added)
                        comments_added.append(author)
                elif field_name == "links":
                    if isinstance(added, list):
                        for lnk in added:
                            lid = lnk.get("idReadable", "?")
                            link_changes.append(f"+ {lid}")
                    if isinstance(removed, list):
                        for lnk in removed:
                            lid = lnk.get("idReadable", "?")
                            link_changes.append(f"- {lid}")
                elif field_name not in ("", "description"):
                    # Other field changes (priority, assignee, etc.)
                    old_val = ""
                    new_val = ""
                    if isinstance(removed, list) and removed:
                        old_val = removed[0].get("name", str(removed))
                    elif isinstance(removed, str):
                        old_val = removed
                    if isinstance(added, list) and added:
                        new_val = added[0].get("name", str(added))
                    elif isinstance(added, str):
                        new_val = added
                    if old_val or new_val:
                        field_changes.append(f"{field_name}: {old_val or '(empty)'} → {new_val or '(empty)'}")

            # Build issue section
            lines.append(f"### {issue_id} [{state}] — {summary}")
            lines.append(f"**Assignee:** {assignee}")

            if not recent:
                lines.append("_No changes in this period._")
            else:
                has_any_changes = True
                if state_changes:
                    for sc in state_changes:
                        lines.append(f"- **State:** {sc}")
                if comments_added:
                    # Deduplicate and count
                    author_counts: dict[str, int] = {}
                    for a in comments_added:
                        author_counts[a] = author_counts.get(a, 0) + 1
                    authors_str = ", ".join(
                        f"{name} ({c})" if c > 1 else name
                        for name, c in author_counts.items()
                    )
                    total_comments = sum(author_counts.values())
                    lines.append(f"- **Comments:** {total_comments} added (by {authors_str})")
                if field_changes:
                    for fc in field_changes:
                        lines.append(f"- **{fc}**")
                if link_changes:
                    lines.append(f"- **Links:** {', '.join(link_changes)}")

                # Last activity timestamp
                last_ts = max(a.get("timestamp", 0) for a in recent)
                if last_ts:
                    last_dt = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)
                    now = datetime.now(tz=timezone.utc)
                    hours_ago = int((now - last_dt).total_seconds() / 3600)
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
        patterns = _compile_patterns(exclude_patterns)

        # Fetch all unresolved issues with extended fields
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
            all_issues = [i for i in all_issues if not _should_exclude(i, patterns)]

        now = datetime.now(tz=timezone.utc)

        # Active = being worked on; stall here is high urgency
        working_states = {"in progress", "in review", "ready for test"}
        # Waiting = filed but not started; stall here is lower urgency
        waiting_states = {"submitted", "pause", "to do", "reopen"}

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

            def _fmt_line(extra: str) -> str:
                return (
                    f"- **{issue_id}** [{state}] {summary}\n"
                    f"  {assignee} | {priority} | {extra}"
                )

            # --- Stalled: actively worked on but went silent ---
            if state_lower in working_states and days_idle >= stale_days:
                stalled.append((days_idle, _fmt_line(f"**{days_idle}d without updates**")))

            # --- Forgotten: filed/paused but idle for a long time ---
            elif state_lower in waiting_states and days_idle >= forgotten_days:
                forgotten.append((days_idle, _fmt_line(f"**{days_idle}d without updates**")))

            # --- Deadline checks ---
            for cf in issue.get("customFields", []):
                cf_name = cf.get("name", "").lower()
                if cf_name in ("deadline", "due date", "due"):
                    val = cf.get("value")
                    if val is not None:
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
                            deadline_dt = datetime.fromtimestamp(
                                deadline_ts / 1000, tz=timezone.utc
                            )
                            days_left = (deadline_dt - now).days
                            deadline_str = deadline_dt.strftime("%Y-%m-%d")

                            if days_left < 0:
                                overdue.append((
                                    abs(days_left),
                                    _fmt_line(f"Deadline: {deadline_str} (**{abs(days_left)}d overdue**)"),
                                ))
                            elif days_left <= deadline_warning_days:
                                approaching.append((
                                    days_left,
                                    _fmt_line(f"Deadline: {deadline_str} (**{days_left}d left**)"),
                                ))

            # --- Over estimate check ---
            estimate_minutes = 0
            spent_minutes = 0
            for cf in issue.get("customFields", []):
                cf_name = cf.get("name", "").lower()
                val = cf.get("value")
                if val is None:
                    continue
                if cf_name in ("estimation", "estimate", "dev estimate", "dev estimation"):
                    if isinstance(val, dict):
                        estimate_minutes = val.get("minutes", 0) or 0
                    elif isinstance(val, (int, float)):
                        estimate_minutes = int(val)
                elif cf_name in ("spent time", "spent"):
                    if isinstance(val, dict):
                        spent_minutes = val.get("minutes", 0) or 0
                    elif isinstance(val, (int, float)):
                        spent_minutes = int(val)

            if estimate_minutes > 0 and spent_minutes > estimate_minutes:
                ratio = spent_minutes / estimate_minutes
                est_str = f"{estimate_minutes // 60}h" if estimate_minutes >= 60 else f"{estimate_minutes}m"
                spent_str = f"{spent_minutes // 60}h" if spent_minutes >= 60 else f"{spent_minutes}m"
                over_estimate.append((
                    ratio,
                    _fmt_line(f"Estimate: {est_str}, Spent: {spent_str} (**{ratio:.0%}**)"),
                ))

        # Sort each category by severity (worst first)
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

        def _append_category(title: str, items: list[tuple], limit: int) -> None:
            if not items:
                return
            showing = min(limit, len(items))
            lines.append(f"## {title} ({len(items)})")
            for _, line in items[:limit]:
                lines.append(line)
            if len(items) > limit:
                lines.append(f"_...and {len(items) - limit} more_")
            lines.append("")

        _append_category("Overdue", overdue, limit_per_category)
        _append_category("Deadline approaching", approaching, limit_per_category)
        _append_category("Stalled — actively worked on but went silent", stalled, limit_per_category)
        _append_category("Over estimate", over_estimate, limit_per_category)
        _append_category("Forgotten — filed/paused but idle", forgotten, limit_per_category)

        return "\n".join(lines)
