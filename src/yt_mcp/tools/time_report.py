"""Time reports aggregated from work items.

Both tools read the top-level `/api/workItems` endpoint — one paged request
chain for the whole date range, no per-issue N+1 — and attribute time to the
work-item AUTHOR (whoever logged it), filtered by work-item DATE (what the
work was logged for), not issue `updated`. Contracts validated against a
live instance (ADR-032): the endpoint returns a bare list of IssueWorkItem
(`duration.minutes`, `author(login,name)`, `issue(idReadable)`, epoch-ms
`date`); `startDate`/`endDate` accept `YYYY-MM-DD`; `author` accepts a login
or user id and 404s (clean ValueError via the client) on unknown users.
"""

import calendar
import re
from datetime import datetime, timezone

from yt_mcp.formatters import compact_lines, escape_query_value
from yt_mcp.resolver import InstanceResolver

_PAGE_SIZE = 500
_MAX_ITEMS = 5000  # hard cap; reports say so when they hit it
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WORK_ITEM_FIELDS = "id,date,duration(minutes),author(login,name),issue(idReadable)"


def _fmt_minutes(minutes: int) -> str:
    if minutes >= 60:
        return f"{minutes // 60}h {minutes % 60}m" if minutes % 60 else f"{minutes // 60}h"
    return f"{minutes}m"


async def _fetch_work_items(client, params: dict) -> tuple[list, bool]:
    """Page through /api/workItems; returns (items, truncated)."""
    items: list = []
    skip = 0
    while True:
        page = await client.get(
            "/api/workItems",
            params={**params, "$top": _PAGE_SIZE, "$skip": skip},
        )
        items.extend(page)
        if len(page) < _PAGE_SIZE:
            return items, False
        if len(items) >= _MAX_ITEMS:
            return items, True
        skip += _PAGE_SIZE


def register(mcp, resolver: InstanceResolver):
    """Register time reporting tools."""

    @mcp.tool()
    async def monthly_time_report_by_user(
        instance: str = "",
        projects: str = "",
        year: int = 0,
        month: int = 0,
    ) -> str:
        """Monthly time report aggregated by the user who logged the time.

        Sums work-item durations for the calendar month, grouped by work-item
        author — not issue assignee, so time on shared issues is credited to
        whoever actually logged it.

        Args:
            instance: YouTrack instance name/URL (auto-detected if blank)
            projects: Comma-separated project keys (all projects if blank)
            year: Report year (default: current UTC year)
            month: Report month 1-12 (default: current UTC month)
        """
        now = datetime.now(timezone.utc)
        year = year or now.year
        month = month or now.month
        if not 1 <= month <= 12:
            raise ValueError(f"month must be 1-12, got {month}")

        days = calendar.monthrange(year, month)[1]
        params = {
            "fields": _WORK_ITEM_FIELDS,
            "startDate": f"{year}-{month:02d}-01",
            "endDate": f"{year}-{month:02d}-{days:02d}",
        }
        if projects:
            keys = [escape_query_value(p.strip()) for p in projects.split(",") if p.strip()]
            # Comma-list, not OR-joined clauses: YT 400s on
            # `project: A OR project: B` (see rewrite_or_clauses, ADR-020).
            params["query"] = f"project: {', '.join(keys)}"

        client = resolver.resolve(instance)
        items, truncated = await _fetch_work_items(client, params)
        if not items:
            return f"No work items logged in {year}-{month:02d}."

        stats: dict[str, dict] = {}
        for item in items:
            author = item.get("author") or {}
            name = author.get("name") or author.get("login") or "?"
            entry = stats.setdefault(name, {"minutes": 0, "issues": set(), "entries": 0})
            entry["minutes"] += (item.get("duration") or {}).get("minutes") or 0
            entry["entries"] += 1
            issue_id = (item.get("issue") or {}).get("idReadable")
            if issue_id:
                entry["issues"].add(issue_id)

        scope = f" — projects: {projects}" if projects else ""
        lines = [f"## Time report {year}-{month:02d}{scope}", ""]
        total = 0
        for name, s in sorted(stats.items(), key=lambda kv: kv[1]["minutes"], reverse=True):
            total += s["minutes"]
            lines.append(
                f"- **{name}**: {_fmt_minutes(s['minutes'])} "
                f"({len(s['issues'])} issues, {s['entries']} entries)"
            )
        lines.append("")
        lines.append(
            f"**Total:** {_fmt_minutes(total)} by {len(stats)} user(s), "
            f"{len(items)} work item(s)"
        )
        if truncated:
            lines.append(
                f"⚠️ Truncated at {_MAX_ITEMS} work items — filter by `projects` for exact totals."
            )
        return compact_lines(lines)

    @mcp.tool()
    async def user_time_summary(
        user: str,
        instance: str = "",
        since: str = "",
        until: str = "",
        top_issues: int = 10,
    ) -> str:
        """Time summary for one user: total logged plus per-issue breakdown.

        Matches by work-item author and work-item date. Unknown users surface
        YouTrack's own error message.

        Args:
            user: YouTrack login (or user id) — required
            instance: YouTrack instance name/URL (auto-detected if blank)
            since: Start date YYYY-MM-DD (optional)
            until: End date YYYY-MM-DD (optional)
            top_issues: How many top issues to list (default: 10)
        """
        if not user:
            raise ValueError("user is required")
        for label, value in (("since", since), ("until", until)):
            if value and not _DATE_RE.match(value):
                raise ValueError(f"{label} must be YYYY-MM-DD, got {value!r}")

        params = {"fields": _WORK_ITEM_FIELDS, "author": user}
        if since:
            params["startDate"] = since
        if until:
            params["endDate"] = until

        client = resolver.resolve(instance)
        items, truncated = await _fetch_work_items(client, params)
        period = f"{since or '…'} → {until or 'now'}"
        if not items:
            scope = f" in {period}" if (since or until) else ""
            return f"No work items logged by {user}{scope}."

        per_issue: dict[str, int] = {}
        total = 0
        for item in items:
            minutes = (item.get("duration") or {}).get("minutes") or 0
            total += minutes
            issue_id = (item.get("issue") or {}).get("idReadable") or "?"
            per_issue[issue_id] = per_issue.get(issue_id, 0) + minutes

        display = (items[0].get("author") or {}).get("name") or user
        lines = [f"## Time summary — {display}"]
        if since or until:
            lines.append(f"Period: {period}")
        lines.append(
            f"**Total:** {_fmt_minutes(total)} across {len(per_issue)} issue(s), "
            f"{len(items)} work item(s)"
        )
        if top_issues > 0:
            lines.append("")
            lines.append("Top issues:")
            ranked = sorted(per_issue.items(), key=lambda kv: kv[1], reverse=True)
            for issue_id, minutes in ranked[:top_issues]:
                lines.append(f"- {issue_id}: {_fmt_minutes(minutes)}")
        if truncated:
            lines.append(
                f"⚠️ Truncated at {_MAX_ITEMS} work items — narrow the date range for exact totals."
            )
        return compact_lines(lines)
