from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import (
    _resolve_state, _resolve_assignee,
    ISSUE_FIELDS, ACTIVE_STATES,
    compile_exclude_patterns, should_exclude,
)
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


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def get_top_active_issues(
        project: str,
        limit: int = 3,
        states: str = "In Progress, Submitted, In Review, Ready for Test, Pause",
        exclude_patterns: str = "",
        instance: str = "",
    ) -> str:
        """Get top active issues for a project, ranked by weighted scoring model.

        Scores issues by priority, type, state, tags, staleness, and blocker count.
        Useful for daily standups, team briefs, and priority dashboards.

        Args:
            project: Project short name (e.g., 'AP', 'DO', 'BAC')
            limit: Number of top issues to return (default: 3)
            states: Comma-separated active states (default: 'In Progress, Submitted, In Review, Ready for Test, Pause')
            exclude_patterns: Comma-separated regex patterns to exclude (e.g., 'DevOps Daily,Report')
            instance: YouTrack instance name (optional, for multi-instance setups)
        """
        client = resolver.resolve(instance)
        patterns = compile_exclude_patterns(exclude_patterns)
        state_set = {s.strip().lower() for s in states.split(",")}

        all_issues = await client.get(
            "/api/issues",
            params={
                "query": f"project: {project} #Unresolved",
                "fields": ISSUE_FIELDS,
                "$top": "500",
            },
        )

        if not all_issues:
            return f"No active issues found in **{project}**."

        issues = [
            i for i in all_issues
            if _resolve_state(i).lower() in state_set
            and not (patterns and should_exclude(i, patterns))
        ]

        scored = sorted(
            ((compute_active_score(issue), issue) for issue in issues),
            key=lambda x: x[0][0],
            reverse=True,
        )

        total = len(scored)
        top = scored[:limit]

        lines = [
            f"## Top {len(top)} active issues in {project}",
            f"**Total active:** {total}",
            "",
        ]
        for (score, breakdown), issue in top:
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
        patterns = compile_exclude_patterns(exclude_patterns)

        issues = await client.get(
            "/api/issues",
            params={
                "query": f"project: {project} State: {{Blocked}}",
                "fields": ISSUE_FIELDS,
                "$top": "200",
            },
        )

        if not issues:
            return f"No blocked issues found in **{project}**."

        if patterns:
            issues = [i for i in issues if not should_exclude(i, patterns)]

        scored = sorted(
            ((compute_blocked_score(issue), issue) for issue in issues),
            key=lambda x: x[0][0],
            reverse=True,
        )

        total = len(scored)
        top = scored[:limit]

        lines = [
            f"## Top {len(top)} blocked issues in {project}",
            f"**Total blocked:** {total}",
            "",
        ]
        for (score, breakdown), issue in top:
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
        patterns = compile_exclude_patterns(exclude_patterns)

        all_issues = await client.get(
            "/api/issues",
            params={
                "query": f"project: {project} #Unresolved",
                "fields": ISSUE_FIELDS,
                "$top": "500",
            },
        )

        if not all_issues:
            return f"No unresolved issues found in **{project}**."

        if patterns:
            all_issues = [i for i in all_issues if not should_exclude(i, patterns)]

        active_issues = []
        blocked_issues = []
        state_counts: dict[str, int] = {}

        for issue in all_issues:
            state = _resolve_state(issue).lower()
            state_counts[state] = state_counts.get(state, 0) + 1
            if state in ACTIVE_STATES:
                active_issues.append(issue)
            elif state == "blocked":
                blocked_issues.append(issue)

        scored_active = sorted(
            ((compute_active_score(issue), issue) for issue in active_issues),
            key=lambda x: x[0][0],
            reverse=True,
        )

        scored_blocked = sorted(
            ((compute_blocked_score(issue), issue) for issue in blocked_issues),
            key=lambda x: x[0][0],
            reverse=True,
        )

        lines = [f"# {project} — Team Dashboard", ""]

        lines.append("## Summary")
        lines.append(f"**Total unresolved:** {len(all_issues)}")
        for state_name in ["in progress", "submitted", "in review", "ready for test", "pause", "blocked", "open"]:
            count = state_counts.get(state_name, 0)
            if count:
                lines.append(f"**{state_name.title()}:** {count}")

        if scored_active:
            top = scored_active[0]
            lines.append(
                f"**Highest active score:** {top[1].get('idReadable', '?')} "
                f"(score: {top[0][0]})"
            )
        if scored_blocked:
            top = scored_blocked[0]
            lines.append(
                f"**Highest blocked score:** {top[1].get('idReadable', '?')} "
                f"(score: {top[0][0]})"
            )
        lines.append("")

        lines.append(f"## Top {min(active_limit, len(scored_active))} active issues ({len(active_issues)} total)")
        lines.append("")
        for (score, breakdown), issue in scored_active[:active_limit]:
            lines.append(_format_scored_issue(issue, score, breakdown))
            lines.append("")

        if not scored_active:
            lines.append("No active issues.\n")

        lines.append(f"## Top {min(blocked_limit, len(scored_blocked))} blocked issues ({len(blocked_issues)} total)")
        lines.append("")
        for (score, breakdown), issue in scored_blocked[:blocked_limit]:
            lines.append(_format_scored_issue(issue, score, breakdown))
            lines.append("")

        if not scored_blocked:
            lines.append("No blocked issues.\n")

        return "\n".join(lines)
