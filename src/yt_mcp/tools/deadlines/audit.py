"""audit_deadline_changes tool — forensic view of every Due-Date shift."""

import asyncio

from yt_mcp.resolver import InstanceResolver
from yt_mcp.tools.deadlines import config as cfg
from yt_mcp.tools.deadlines import fetcher
from yt_mcp.tools.deadlines.parser import (
    _classify_shift,
    _compile_standup_patterns,
    _extract_activity_date,
    _is_deadline_field,
    _is_standup,
)
from yt_mcp.tools.deadlines.render import render_audit


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def audit_deadline_changes(
        period_start: str = "",
        period_finish: str = "",
        projects: str = "",
        strict: bool = False,
        exclude_standups: bool = True,
        instance: str = "",
    ) -> str:
        """Audit Due-Date shifts in a period; classify each as compliant/unauthorized.

        Reads ~/.yt-mcp/managers.json for approver mapping. Each shift is bucketed:
        compliant_strict, compliant_loose, unauthorized, approver_unknown,
        pre_policy, or informational (first-time set / earlier date).

        Args:
            period_start: ISO date (YYYY-MM-DD); defaults to current quarter start
            period_finish: ISO date; defaults to current quarter end
            projects: Comma-separated project shortnames; empty = all accessible
            strict: If True, only keyword+date comments count as approval
            exclude_standups: Skip recurring daily/standup tickets (default: True)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        managers_cfg, metadata = cfg._load_managers_config()
        policy = cfg._load_policy()
        policy_effective_ms = cfg._policy_effective_ms(policy)
        standup_patterns = _compile_standup_patterns(policy) if exclude_standups else []

        if not period_start or not period_finish:
            qs, qe = cfg._quarter_to_range(cfg._current_quarter())
            start_dt = cfg._parse_iso(period_start) or qs
            end_dt = cfg._parse_iso(period_finish) or qe
        else:
            start_dt = cfg._parse_iso(period_start)
            end_dt = cfg._parse_iso(period_finish)
            if not start_dt or not end_dt:
                return "Invalid period dates. Use YYYY-MM-DD."

        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)
        operator = await fetcher.get_operator_login(client)

        proj_clause, proj_list = fetcher.build_project_clause(projects)
        date_clause = f"updated: {start_dt.strftime('%Y-%m-%d')} .. {end_dt.strftime('%Y-%m-%d')}"
        query = (proj_clause + " " + date_clause).strip()

        issues = await client.get(
            "/api/issues",
            params={"query": query, "fields": fetcher.ISSUE_FIELDS, "$top": "500"},
        )
        if not issues:
            cfg._audit(operator, "audit_deadline_changes", {"query": query}, 0)
            return f"## Deadline audit\nNo issues match query: `{query}`"

        if exclude_standups:
            issues = [i for i in issues if not _is_standup(i.get("summary", ""), standup_patterns)]

        ids = [i.get("idReadable", "") for i in issues if i.get("idReadable")]
        results = await asyncio.gather(*(
            fetcher.fetch_issue_activities_and_comments(client, iid) for iid in ids
        ))
        issue_by_id = {i.get("idReadable", ""): i for i in issues}

        rows: list[dict] = []
        coverage_missing: set[str] = set()

        for iid, (activities, comments) in zip(ids, results):
            issue = issue_by_id.get(iid, {})
            reporter = (issue.get("reporter") or {}).get("login") or ""
            assignee = fetcher.extract_assignee_login(issue)
            target_login = assignee or reporter
            if not target_login:
                continue
            approvers, manual_review = cfg._get_approvers(target_login, managers_cfg)
            if not approvers:
                coverage_missing.add(target_login)
            for a in activities:
                if not _is_deadline_field((a.get("field") or {}).get("name", "")):
                    continue
                ts = a.get("timestamp", 0)
                if ts < start_ms or ts > end_ms:
                    continue
                old_ms = _extract_activity_date(a.get("removed"))
                new_ms = _extract_activity_date(a.get("added"))
                author = (a.get("author") or {}).get("login") or (a.get("author") or {}).get("name") or "?"
                cls = _classify_shift(
                    shift_ts=ts, shift_author=author, old_ms=old_ms, new_ms=new_ms,
                    approvers=approvers, manual_review=manual_review,
                    comments=comments, strict=strict,
                    policy_effective_ms=policy_effective_ms,
                )
                rows.append({
                    "issue": iid,
                    "summary": issue.get("summary", ""),
                    "old": old_ms,
                    "new": new_ms,
                    "author": author,
                    "ts": ts,
                    "assignee": target_login,
                    "approvers": approvers,
                    "activity_id": a.get("id", ""),
                    **cls,
                })

        cfg._audit(
            operator, "audit_deadline_changes",
            {"query": query, "strict": strict, "projects": proj_list},
            len(rows),
        )
        return render_audit(
            rows, operator, query, strict,
            metadata.get("source_file", ""),
            coverage_missing,
            policy_effective_set=policy_effective_ms > 0,
        )
