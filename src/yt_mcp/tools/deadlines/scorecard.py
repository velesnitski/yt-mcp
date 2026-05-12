"""deadline_scorecard tool — quarterly per-assignee compliance rollup."""

from collections import Counter, defaultdict
from datetime import datetime, timezone

from yt_mcp.resolver import InstanceResolver
from yt_mcp.tools.deadlines import config as cfg
from yt_mcp.tools.deadlines import fetcher
from yt_mcp.tools.deadlines.parser import (
    _DONE_STATES,
    _classify_shift,
    _compile_standup_patterns,
    _extract_activity_date,
    _format_date,
    _is_deadline_field,
    _is_standup,
)
from yt_mcp.tools.deadlines.render import render_scorecard


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def deadline_scorecard(
        quarter: str = "",
        user: str = "",
        projects: str = "",
        strict: bool = False,
        exclude_standups: bool = True,
        instance: str = "",
    ) -> str:
        """Per-assignee deadline compliance rollup for a calendar quarter.

        Tracks per-issue compliance (not cumulative): a missed deadline is
        only ``missed_after_extension`` if THIS specific issue had an
        approved shift earlier in the quarter.

        Args:
            quarter: 'YYYYQN' (e.g. '2026Q2'); empty = current calendar quarter
            user: Filter to a single assignee login; empty = all
            projects: Comma-separated project shortnames; empty = all accessible
            strict: If True, only keyword+date comments count as approval
            exclude_standups: Skip recurring daily/standup tickets
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        managers_cfg, metadata = cfg._load_managers_config()
        policy = cfg._load_policy()
        policy_effective_ms = cfg._policy_effective_ms(policy)
        standup_patterns = _compile_standup_patterns(policy) if exclude_standups else []

        q = quarter or cfg._current_quarter()
        try:
            start_dt, end_dt = cfg._quarter_to_range(q)
        except ValueError as e:
            return str(e)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)
        operator = await fetcher.get_operator_login(client)

        proj_clause, proj_list = fetcher.build_project_clause(projects)
        date_clause = (
            f"(updated: {start_dt.strftime('%Y-%m-%d')} .. {end_dt.strftime('%Y-%m-%d')} "
            f"or due date: {start_dt.strftime('%Y-%m-%d')} .. {end_dt.strftime('%Y-%m-%d')})"
        )
        query = (proj_clause + " " + date_clause).strip()
        fallback_used = False
        try:
            issues = await client.get(
                "/api/issues",
                params={
                    "query": query,
                    "fields": fetcher.ISSUE_FIELDS + ",state(name)",
                    "$top": "500",
                },
            )
        except ValueError:
            fallback_used = True
            fallback_query = (
                proj_clause
                + f" updated: {start_dt.strftime('%Y-%m-%d')} .. {end_dt.strftime('%Y-%m-%d')}"
            ).strip()
            issues = await client.get(
                "/api/issues",
                params={
                    "query": fallback_query,
                    "fields": fetcher.ISSUE_FIELDS + ",state(name)",
                    "$top": "500",
                },
            )
            query = fallback_query
        if not issues:
            cfg._audit(operator, "deadline_scorecard", {"quarter": q}, 0)
            return f"## Deadline scorecard — {q}\nNo issues in scope. Query: `{query}`"

        if exclude_standups:
            issues = [i for i in issues if not _is_standup(i.get("summary", ""), standup_patterns)]

        ids = [i.get("idReadable", "") for i in issues if i.get("idReadable")]
        results = await fetcher.fetch_issue_activities_and_comments_bounded(client, ids)
        issue_by_id = {i.get("idReadable", ""): i for i in issues}

        per_user: dict[str, Counter] = defaultdict(Counter)
        per_user_details: dict[str, list[str]] = defaultdict(list)
        coverage_missing: set[str] = set()
        observed_field_names: set[str] = set()
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

        for iid, (activities, comments) in zip(ids, results):
            issue = issue_by_id.get(iid, {})
            assignee = fetcher.extract_assignee_login(issue)
            reporter = (issue.get("reporter") or {}).get("login") or ""
            target = assignee or reporter
            if not target:
                continue
            if user and target != user:
                continue
            approvers, manual_review = cfg._get_approvers(target, managers_cfg)
            if not approvers:
                coverage_missing.add(target)

            # BUG-FIX: track per-issue compliance, not cumulative per-user.
            # `missed_after_extension` was previously triggered if the user
            # had ANY compliant shift on ANY of their issues — silently
            # under-counting penalties.
            issue_buckets: set[str] = set()
            for a in activities:
                field_name = (a.get("field") or {}).get("name", "")
                if field_name:
                    observed_field_names.add(field_name)
                if not _is_deadline_field(field_name):
                    continue
                ts = a.get("timestamp", 0)
                if ts < start_ms or ts > end_ms:
                    continue
                old_ms = _extract_activity_date(a.get("removed"))
                new_ms = _extract_activity_date(a.get("added"))
                author = (a.get("author") or {}).get("login") or "?"
                cls = _classify_shift(
                    shift_ts=ts, shift_author=author, old_ms=old_ms, new_ms=new_ms,
                    approvers=approvers, manual_review=manual_review,
                    comments=comments, strict=strict,
                    policy_effective_ms=policy_effective_ms,
                )
                bucket = cls["classification"]
                per_user[target][bucket] += 1
                issue_buckets.add(bucket)
                if bucket == "unauthorized":
                    per_user_details[target].append(
                        f"- **{iid}** shift {_format_date(old_ms)}→{_format_date(new_ms)} "
                        f"by {author}: {cls['evidence'][0] if cls['evidence'] else ''}"
                    )

            current_deadline = fetcher.extract_current_deadline(issue)
            state = fetcher.extract_current_state(issue).lower()
            if (
                current_deadline
                and current_deadline < min(end_ms, now_ms)
                and current_deadline >= start_ms
                and state not in _DONE_STATES
            ):
                had_approved = bool(
                    issue_buckets & {"compliant_strict", "compliant_loose"}
                )
                bucket = "missed_after_extension" if had_approved else "missed_no_extension"
                per_user[target][bucket] += 1
                per_user_details[target].append(
                    f"- **{iid}** missed deadline {_format_date(current_deadline)} "
                    f"(state: {state or 'unknown'}; {bucket.replace('_', ' ')})"
                )

        cfg._audit(
            operator, "deadline_scorecard",
            {"quarter": q, "user": user, "projects": proj_list, "strict": strict},
            sum(sum(c.values()) for c in per_user.values()),
        )
        return render_scorecard(
            per_user, per_user_details, q, operator, strict,
            metadata.get("source_file", ""),
            coverage_missing,
            fallback_query_used=fallback_used,
            policy_effective_set=policy_effective_ms > 0,
            observed_fields=observed_field_names,
        )
