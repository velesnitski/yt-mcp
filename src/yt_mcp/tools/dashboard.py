import re

from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import _resolve_state, _resolve_assignee, _get_custom_field
from yt_mcp.scoring import (
    compute_active_score,
    compute_blocked_score,
    format_score_breakdown,
    _get_priority_name,
    _count_blockers,
    _days_since_update,
)

_ISSUE_FIELDS = (
    "idReadable,summary,updated,created,state(name),priority(name),"
    "assignee(name),tags(name),"
    "customFields(name,value(name)),"
    "links(direction,linkType(name),issues(idReadable))"
)

_DEFAULT_ACTIVE_STATES = "In Progress, Submitted, In Review, Ready for Test"


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

    parts = [f"- **{issue_id}** (score: **{score}**) [{state}] {summary}"]
    parts.append(f"  {assignee} | {priority} | {days}d idle | {blockers} blocked")
    parts.append(f"  _{format_score_breakdown(breakdown)}_")
    return "\n".join(parts)


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

        # Build state filter
        state_list = [s.strip() for s in states.split(",")]
        state_query = " ".join(f"State: {{{s}}}" for s in state_list)
        query = f"project: {project} ({' or '.join(f'State: {{{s}}}' for s in state_list)})"

        issues = await client.get(
            "/api/issues",
            params={
                "query": query,
                "fields": _ISSUE_FIELDS,
                "$top": "200",
            },
        )

        if not issues:
            return f"No active issues found in **{project}**."

        # Filter exclusions
        if patterns:
            issues = [i for i in issues if not _should_exclude(i, patterns)]

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
        active_states = {"in progress", "submitted", "in review", "ready for test"}
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
        for state_name in ["in progress", "submitted", "in review", "ready for test", "blocked", "open"]:
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
