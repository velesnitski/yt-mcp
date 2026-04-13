import asyncio

from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import (
    _resolve_state, _resolve_assignee, _get_custom_field,
    ISSUE_FIELDS, ACTIVE_STATES, COMPACT,
    compile_exclude_patterns, should_exclude, compact_lines,
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
    """Format a single scored issue."""
    issue_id = issue.get("idReadable", "?")
    summary = issue.get("summary", "?")
    state = _resolve_state(issue)
    assignee = _resolve_assignee(issue)
    priority = _get_priority_name(issue)
    days = _days_since_update(issue)
    blockers = _count_blockers(issue)
    blocking_others = _count_blocking_others(issue)
    products = _count_products(issue)

    if COMPACT:
        extras = []
        if blockers:
            extras.append(f"{blockers}sub")
        if blocking_others:
            extras.append(f"blk{blocking_others}")
        if products > 1:
            extras.append(f"{products}prod")
        extra_str = "|" + "|".join(extras) if extras else ""
        return f"{issue_id}|s:{score}|{state}|{summary}|{priority}|{days}d{extra_str}"

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


def _get_product_name(issue: dict) -> str:
    """Get product name for grouping. Returns 'No product' if unset."""
    return _get_custom_field(issue, "Product") or "No product"


def _format_grouped(
    scored: list[tuple[tuple[int, dict], dict]],
    limit: int,
) -> list[str]:
    """Group scored issues by product and format."""
    groups: dict[str, list[tuple[tuple[int, dict], dict]]] = {}
    for item in scored:
        product = _get_product_name(item[1])
        groups.setdefault(product, []).append(item)

    # Sort groups by highest score in each group
    sorted_groups = sorted(
        groups.items(),
        key=lambda g: g[1][0][0][0] if g[1] else 0,
        reverse=True,
    )

    lines: list[str] = []
    for product, items in sorted_groups:
        lines.append(f"### {product} ({len(items)})")
        lines.append("")
        for (score, breakdown), issue in items[:limit]:
            lines.append(_format_scored_issue(issue, score, breakdown))
            lines.append("")
    return lines


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def get_top_active_issues(
        project: str,
        limit: int = 3,
        states: str = "In Progress, Submitted, In Review, Ready for Test, Pause",
        group_by_product: bool = False,
        exclude_patterns: str = "",
        instance: str = "",
    ) -> str:
        """Get top active issues ranked by priority score.

        Args:
            project: Project short name
            limit: Top N results (default: 3), per product when grouped
            states: Comma-separated active states
            group_by_product: Group by Product field (default: false)
            exclude_patterns: Comma-separated regex to exclude
            instance: YouTrack instance (optional)
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

        lines = [
            f"## Top active issues in {project}",
            f"**Total active:** {total}",
            "",
        ]

        if group_by_product:
            lines.extend(_format_grouped(scored, limit))
        else:
            for (score, breakdown), issue in scored[:limit]:
                lines.append(_format_scored_issue(issue, score, breakdown))
                lines.append("")

        return compact_lines(lines)

    @mcp.tool()
    async def get_top_blocked_issues(
        project: str,
        limit: int = 3,
        group_by_product: bool = False,
        exclude_patterns: str = "",
        instance: str = "",
    ) -> str:
        """Get top blocked issues ranked by priority score.

        Args:
            project: Project short name
            limit: Top N results (default: 3), per product when grouped
            group_by_product: Group by Product field (default: false)
            exclude_patterns: Comma-separated regex to exclude
            instance: YouTrack instance (optional)
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

        lines = [
            f"## Top blocked issues in {project}",
            f"**Total blocked:** {total}",
            "",
        ]

        if group_by_product:
            lines.extend(_format_grouped(scored, limit))
        else:
            for (score, breakdown), issue in scored[:limit]:
                lines.append(_format_scored_issue(issue, score, breakdown))
                lines.append("")

        return compact_lines(lines)

    @mcp.tool()
    async def get_team_dashboard(
        project: str,
        active_limit: int = 3,
        blocked_limit: int = 3,
        group_by_product: bool = False,
        exclude_patterns: str = "",
        instance: str = "",
    ) -> str:
        """Combined dashboard: top active, top blocked, and summary stats for a project.

        Args:
            project: Project short name
            active_limit: Top N active (default: 3)
            blocked_limit: Top N blocked (default: 3)
            group_by_product: Group by Product field (default: false)
            exclude_patterns: Comma-separated regex to exclude
            instance: YouTrack instance (optional)
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

        lines.append(f"## Active issues ({len(active_issues)} total)")
        lines.append("")
        if group_by_product:
            lines.extend(_format_grouped(scored_active, active_limit))
        else:
            for (score, breakdown), issue in scored_active[:active_limit]:
                lines.append(_format_scored_issue(issue, score, breakdown))
                lines.append("")

        if not scored_active:
            lines.append("No active issues.\n")

        lines.append(f"## Blocked issues ({len(blocked_issues)} total)")
        lines.append("")
        if group_by_product:
            lines.extend(_format_grouped(scored_blocked, blocked_limit))
        else:
            for (score, breakdown), issue in scored_blocked[:blocked_limit]:
                lines.append(_format_scored_issue(issue, score, breakdown))
                lines.append("")

        if not scored_blocked:
            lines.append("No blocked issues.\n")

        return compact_lines(lines)

    @mcp.tool()
    async def get_multi_team_dashboard(
        projects: str,
        active_limit: int = 5,
        blocked_limit: int = 3,
        group_by_product: bool = False,
        exclude_patterns: str = "",
        instance: str = "",
    ) -> str:
        """Combined dashboard for multiple projects in one call.

        Args:
            projects: Comma-separated project short names
            active_limit: Top N active per project (default: 5)
            blocked_limit: Top N blocked per project (default: 3)
            group_by_product: Group by Product field (default: false)
            exclude_patterns: Comma-separated regex to exclude
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        patterns = compile_exclude_patterns(exclude_patterns)
        project_list = [p.strip() for p in projects.split(",") if p.strip()]

        if not project_list:
            return "No projects specified."

        # Fetch all projects in parallel
        async def _fetch_project(project: str) -> tuple[str, list]:
            try:
                issues = await client.get(
                    "/api/issues",
                    params={
                        "query": f"project: {project} #Unresolved",
                        "fields": ISSUE_FIELDS,
                        "$top": "500",
                    },
                )
                return project, issues
            except ValueError:
                return project, []

        results = await asyncio.gather(*[_fetch_project(p) for p in project_list])

        lines = [f"# Multi-Team Dashboard ({len(project_list)} projects)", ""]

        for project, all_issues in results:
            if patterns:
                all_issues = [i for i in all_issues if not should_exclude(i, patterns)]

            active_issues = []
            blocked_issues = []

            for issue in all_issues:
                state = _resolve_state(issue).lower()
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

            lines.append(f"## {project} — {len(active_issues)} active, {len(blocked_issues)} blocked")
            lines.append("")

            if scored_active:
                if group_by_product:
                    lines.extend(_format_grouped(scored_active, active_limit))
                else:
                    for (score, breakdown), issue in scored_active[:active_limit]:
                        lines.append(_format_scored_issue(issue, score, breakdown))
                        lines.append("")
            else:
                lines.append("No active issues.\n")

            if scored_blocked:
                lines.append(f"**Blocked ({len(blocked_issues)}):**")
                lines.append("")
                if group_by_product:
                    lines.extend(_format_grouped(scored_blocked, blocked_limit))
                else:
                    for (score, breakdown), issue in scored_blocked[:blocked_limit]:
                        lines.append(_format_scored_issue(issue, score, breakdown))
                        lines.append("")

            lines.append("---")
            lines.append("")

        return compact_lines(lines)
