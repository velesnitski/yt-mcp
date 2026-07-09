import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import (
    _resolve_state, _resolve_assignee, _get_custom_field,
    ISSUE_FIELDS, COMPACT,
    compile_exclude_patterns, should_exclude, compact_lines,
)
from yt_mcp.scoring import _get_priority_name, _days_since_update
from yt_mcp.tools.deadlines.parser import _is_deadline_field

_SNAPSHOTS_DIR = Path.home() / ".yt-mcp" / "snapshots"
# Project short names are simple tokens (PROJ, PROJ, PROJ, PROJ). Anything
# else — path separators, '..', dots — could traverse out of the snapshots
# dir when interpolated into the filename (ADR-027), so it disables snapshot
# tracking for that call rather than writing/reading an attacker-chosen path.
_SAFE_PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _snapshot_path(project: str):
    """Confined snapshot Path for a project, or None if the name is unsafe."""
    name = (project or "").lower()
    if not _SAFE_PROJECT_RE.match(name):
        return None
    return _SNAPSHOTS_DIR / f"{name}.json"


def _load_snapshot(project: str) -> dict | None:
    """Load previous health snapshot for a project."""
    path = _snapshot_path(project)
    if path is None:
        return None
    try:
        if path.exists():
            return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _save_snapshot(project: str, data: dict) -> None:
    """Save current health snapshot for delta tracking."""
    import logging
    path = _snapshot_path(project)
    if path is None:
        return
    try:
        _SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except OSError as e:
        logging.getLogger("yt_mcp").warning("Failed to save snapshot for %s: %s", project, e)

_SINCE_RE = re.compile(r"^(\d+)\s*(h|d|m)$", re.IGNORECASE)

_SINCE_MULTIPLIERS = {"m": 60, "h": 3600, "d": 86400}

# Risk detection constants (shared with youtrack-reports scoring logic)
BLOCKED_RISK_DAYS = 14
NEW_ISSUE_GRACE_HOURS = 4

WORKING_STATES = frozenset({"in progress", "in review", "ready for test"})
WAITING_STATES = frozenset({"submitted", "pause", "to do", "reopen", "open"})
# Completion-pending states — dev work is done, further age isn't the team's
# problem. Excluded from Ancient and Forgotten checks to avoid noise from
# release queues and won't-fix piles. Do NOT add "On Testing"/"For Review"
# to WORKING_STATES — old items there belong in Ancient (weight 2), not
# Stalled (weight 3); shifting category doesn't change the signal.
COMPLETION_STATES = frozenset({
    "ready for release",
    "backlog",
    "won't fix", "wontfix",
    "rejected", "declined", "duplicate", "archived",
})

# Health score dedup: each issue deducts once at its worst category's weight
_RISK_WEIGHTS = {
    "stalled": 3,
    "ancient": 2,
    "forgotten": 1,
    "blocked": 1,
    "unassigned": 1,
}


def _count_flagged_issues(risks: dict[str, list]) -> int:
    """Distinct issues across all risk categories (no double-counting)."""
    seen: set[str] = set()
    for category in _RISK_WEIGHTS:
        for issue in risks.get(category, []):
            iid = issue.get("idReadable") or issue.get("id")
            if iid:
                seen.add(iid)
    return len(seen)


def _hours_since(ts_ms) -> float:
    """Return hours since a millisecond timestamp."""
    if not ts_ms:
        return 0.0
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return (datetime.now(tz=timezone.utc) - dt).total_seconds() / 3600


def _compute_health_score(total: int, risks: dict[str, list]) -> int:
    """Compute health score with per-issue dedup.

    Each issue contributes at most once, using the worst category's weight.
    Avoids triple-counting issues that are ancient + unassigned + blocked.
    """
    if total == 0:
        return 100
    worst_per_issue: dict[str, int] = {}
    for category, weight in _RISK_WEIGHTS.items():
        for issue in risks.get(category, []):
            iid = issue.get("idReadable") or issue.get("id")
            if iid is None:
                continue
            if iid not in worst_per_issue or weight > worst_per_issue[iid]:
                worst_per_issue[iid] = weight
    deductions = sum(worst_per_issue.values())
    return max(0, 100 - deductions * 100 // total)


_DEFAULT_EXCLUDE_PATTERNS = [re.compile(r"DevOps Daily", re.IGNORECASE)]


def _parse_since(since: str) -> int:
    """Parse since string (duration or ISO date) to epoch milliseconds."""
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


# Estimate / spent-time field matchers. Same decorated-name problem as the
# deadline field: real projects name these `Evaluation time 🕙` /
# `Spent time 🚴🏻‍♂️` / `Dev Estimate`, none of which equal a bare
# "estimate"/"spent" literal. The `([\W_]|$)` boundary lets a trailing
# emoji or space match while still rejecting partial words ("Estimated").
_ESTIMATE_FIELD_PATTERNS = (
    re.compile(
        r"^(estimation|estimate|dev\s*estimate|dev\s*estimation|"
        r"evaluation\s*time|total\s*estimate)([\W_]|$)",
        re.IGNORECASE,
    ),
    re.compile(r"^(оценка|оценка\s*времени)([\W_]|$)", re.IGNORECASE),
)
_SPENT_FIELD_PATTERNS = (
    re.compile(r"^(spent\s*time|spent|time\s*spent|logged\s*time)([\W_]|$)", re.IGNORECASE),
    re.compile(r"^(затрачено|потрачено)([\W_]|$)", re.IGNORECASE),
)


def _is_estimate_field(name: str) -> bool:
    return bool(name) and any(p.match(name.strip()) for p in _ESTIMATE_FIELD_PATTERNS)


def _is_spent_field(name: str) -> bool:
    return bool(name) and any(p.match(name.strip()) for p in _SPENT_FIELD_PATTERNS)


def _period_to_minutes(val) -> int:
    """Extract minutes from a YT period custom-field value.

    Prefers YT's authoritative `minutes` (correct under the project's work
    schedule, e.g. 1d = 8h). Falls back to a raw numeric value. We do NOT
    parse the `presentation` string ("1w 2d 1h") — day/week length is
    project-configurable, so parsing it ourselves would risk wrong ratios.
    """
    if isinstance(val, dict):
        m = val.get("minutes")
        return int(m) if isinstance(m, (int, float)) and m else 0
    if isinstance(val, (int, float)):
        return int(val)
    return 0


def _extract_deadline_ts(val) -> int | None:
    """Pull an epoch-ms deadline from a date custom-field value (raw int or
    a dict carrying a YYYY-MM-DD `presentation`)."""
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, dict):
        pres = val.get("presentation", "")
        if pres:
            try:
                return int(
                    datetime.strptime(pres, "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc)
                    .timestamp() * 1000
                )
            except ValueError:
                return None
    return None


# QA-gating field. Projects that enforce QA add a required enum like
# `QA Required: Yes/No`. Matched by pattern (decoration-tolerant, like the
# deadline/estimate fields) so it works across teams without per-project
# config — absent field simply yields no candidates (zero cost, zero noise).
_QA_REQUIRED_FIELD_PATTERNS = (
    re.compile(r"^(qa\s*required|qa\s*needed|requires?\s*qa|needs?\s*qa|qa\s*gate)([\W_]|$)", re.IGNORECASE),
    re.compile(r"^(нужно\s*qa|требуется\s*qa|qa\s*обязателен)([\W_]|$)", re.IGNORECASE),
)
_QA_AFFIRMATIVE = frozenset({"yes", "y", "true", "required", "да", "требуется", "нужно"})

# Cap on how many QA-skip suspects get a (per-issue) history walk, so a team
# with an unusually large release queue can't make this tool expensive. If
# exceeded we walk the stalest N and note the rest (no silent truncation).
_QA_SKIP_CHECK_MAX = 80


def _is_qa_required_field(name: str) -> bool:
    return bool(name) and any(p.match(name.strip()) for p in _QA_REQUIRED_FIELD_PATTERNS)


def _qa_required_affirmative(val) -> bool:
    """True when a QA-Required field value means 'QA is required'."""
    if isinstance(val, bool):
        return val
    name = ""
    if isinstance(val, dict):
        name = (val.get("name") or "").strip().lower()
    elif isinstance(val, str):
        name = val.strip().lower()
    elif isinstance(val, list) and val and isinstance(val[0], dict):
        name = (val[0].get("name") or "").strip().lower()
    return name in _QA_AFFIRMATIVE


def _passed_qa_state(activities: list[dict]) -> bool:
    """True if the issue's state history ever touched a QA-role state.

    Walks both `added` and `removed` sides so an issue that *was* in QA and
    moved on still counts as 'passed QA'. Role classification is shared with
    the handoff tool (single source of pipeline-role truth)."""
    from yt_mcp.tools.handoffs import classify_handoff_role
    for act in activities or []:
        if (act.get("field") or {}).get("name", "").lower() != "state":
            continue
        for side in ("added", "removed"):
            for s in act.get(side) or []:
                if isinstance(s, dict) and classify_handoff_role(s.get("name", "")) == "qa":
                    return True
    return False


# Canonical category keys + display titles, in render order. The `category`
# filter accepts a key or a friendly alias (see _CATEGORY_ALIASES).
_AT_RISK_CATEGORIES = (
    ("overdue", "Overdue"),
    ("qa_skipped", "QA skipped — required but never passed a QA state"),
    ("approaching", "Deadline approaching"),
    ("stalled", "Stalled — actively worked on but went silent"),
    ("over_estimate", "Over estimate"),
    ("unestimated", "Unestimated"),
    ("ancient", "Ancient — open too long"),
    ("forgotten", "Forgotten — filed/paused but idle"),
)
_CATEGORY_ALIASES = {
    "overdue": "overdue",
    "qa_skipped": "qa_skipped",
    "qa skipped": "qa_skipped",
    "qa": "qa_skipped",
    "qa skip": "qa_skipped",
    "skipped qa": "qa_skipped",
    "qa compliance": "qa_skipped",
    "qa_compliance": "qa_skipped",
    "approaching": "approaching",
    "deadline approaching": "approaching",
    "deadline": "approaching",
    "stalled": "stalled",
    "stale": "stalled",
    "over_estimate": "over_estimate",
    "over estimate": "over_estimate",
    "overestimate": "over_estimate",
    "unestimated": "unestimated",
    "no estimate": "unestimated",
    "unestimate": "unestimated",
    "ancient": "ancient",
    "forgotten": "forgotten",
}

# Current-state roles that mean "QA should already be done" — the window
# where a QA-required-but-skipped issue is catchable before final close.
_QA_GATE_ROLES = frozenset({"release", "done"})


def _risk_record(
    issue_id: str, state: str, summary: str, assignee: str, priority: str, detail: str,
) -> dict:
    """A JSON-friendly at-risk record. `detail` is the human metric string
    (e.g. '5d overdue'); markdown renders from the same dict."""
    return {
        "id": issue_id, "state": state, "summary": summary,
        "assignee": assignee, "priority": priority, "detail": detail,
    }


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def get_issues_digest(
        query: str,
        since: str = "24h",
        limit: int = 10,
        instance: str = "",
    ) -> str:
        """Get a digest of recent changes for issues matching a query.

        Args:
            query: YouTrack search query
            since: Duration ('24h', '7d') or date ('2026-03-18'). Default: 24h
            limit: Max issues (default: 10)
            instance: YouTrack instance (optional)
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
            except ValueError:
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
                field_name = (a.get("field") or {}).get("name", "")
                author = (a.get("author") or {}).get("name", "?")
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

        return compact_lines(lines)

    @mcp.tool()
    async def get_at_risk_issues(
        project: str,
        stale_days: int = 7,
        forgotten_days: int = 30,
        ancient_days: int = 200,
        limit_per_category: int = 10,
        deadline_warning_days: int = 7,
        exclude_patterns: str = "",
        category: str = "",
        format: str = "report",
        instance: str = "",
    ) -> str:
        """Find at-risk issues: overdue, qa_skipped, stalled, forgotten, unestimated, over estimate, ancient.

        Deadline / estimate / spent-time / QA-required fields are matched by
        name *pattern*, so decorated field names (`Deadline ☠️`,
        `Evaluation time 🕙`, `Spent time 🚴🏻‍♂️`, `Dev Estimate`,
        `QA Required`) are recognized — not just bare literals.

        QA-skip: when a project has a QA-gating field (e.g. `QA Required: Yes`),
        an issue that reached a release/done state while QA was required is
        confirmed against its state history — flagged only if it NEVER passed
        a QA state (true skip, not merely "QA pending"). Projects without such
        a field produce no QA-skip candidates (zero added cost). The per-issue
        history walk runs only when this category is in scope.

        Args:
            project: Project short name
            stale_days: Days idle for In Progress (default: 7)
            forgotten_days: Days idle for Submitted/Pause (default: 30)
            ancient_days: Days open to flag (default: 200)
            limit_per_category: Max per category in report mode (default: 10).
                Ignored when format="json" — JSON returns the full set.
            deadline_warning_days: Days before deadline warning (default: 7)
            exclude_patterns: Comma-separated regex to exclude
            category: Restrict to one bucket — overdue, qa_skipped, approaching,
                stalled, over_estimate, unestimated, ancient, forgotten
                (aliases accepted). Empty = all categories.
            format: "report" (default markdown) or "json" (structured payload
                with per-category counts + full issue lists, for programmatic
                consumers like a daily deadline bot).
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        patterns = compile_exclude_patterns(exclude_patterns) or _DEFAULT_EXCLUDE_PATTERNS

        # Resolve the category filter early so a typo fails fast with help.
        category_key = ""
        if category:
            category_key = _CATEGORY_ALIASES.get(category.strip().lower(), "")
            if not category_key:
                valid = ", ".join(k for k, _ in _AT_RISK_CATEGORIES)
                return (
                    f"Unknown category '{category}'. Valid categories: {valid}."
                )

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
        qa_skipped: list[tuple[int, dict]] = []

        # Only walk per-issue history for QA-skip when that category is in
        # scope (no filter, or category=qa_skipped). Candidates are gathered
        # cheaply in the bulk loop; the (bounded) history walk happens after.
        from yt_mcp.tools.handoffs import classify_handoff_role
        qa_in_scope = (not category_key) or category_key == "qa_skipped"
        qa_candidates: list[tuple[int, str, dict]] = []  # (days_idle, issue_id, record)

        for issue in all_issues:
            issue_id = issue.get("idReadable", "?")
            summary = issue.get("summary", "?")
            state = _resolve_state(issue)
            state_lower = state.lower()
            assignee = _resolve_assignee(issue)
            priority = _get_priority_name(issue)
            days_idle = _days_since_update(issue)

            if state_lower in working_states and days_idle >= stale_days:
                stalled.append((days_idle, _risk_record(
                    issue_id, state, summary, assignee, priority,
                    f"{days_idle}d without updates",
                )))
            elif (
                state_lower in waiting_states
                and state_lower != "pause"
                and state_lower not in COMPLETION_STATES
                and days_idle >= forgotten_days
            ):
                forgotten.append((days_idle, _risk_record(
                    issue_id, state, summary, assignee, priority,
                    f"{days_idle}d without updates",
                )))

            # Scan custom fields once for deadline and estimate data.
            # Field names are matched by PATTERN (not exact literal) so
            # decorated names like `Deadline ☠️` / `Evaluation time 🕙` /
            # `Spent time 🚴🏻‍♂️` are recognized. First non-zero estimate/
            # spent field wins (deterministic when several exist).
            custom_fields = issue.get("customFields", [])
            estimate_minutes = 0
            spent_minutes = 0
            qa_required_yes = False

            for cf in custom_fields:
                cf_name = cf.get("name", "")
                val = cf.get("value")
                if val is None:
                    continue

                if _is_deadline_field(cf_name):
                    deadline_ts = _extract_deadline_ts(val)
                    if deadline_ts:
                        deadline_dt = datetime.fromtimestamp(deadline_ts / 1000, tz=timezone.utc)
                        days_left = (deadline_dt - now).days
                        deadline_str = deadline_dt.strftime("%Y-%m-%d")

                        if days_left < 0:
                            overdue.append((abs(days_left), _risk_record(
                                issue_id, state, summary, assignee, priority,
                                f"Deadline {deadline_str} ({abs(days_left)}d overdue)",
                            )))
                        elif days_left <= deadline_warning_days:
                            approaching.append((days_left, _risk_record(
                                issue_id, state, summary, assignee, priority,
                                f"Deadline {deadline_str} ({days_left}d left)",
                            )))
                elif _is_qa_required_field(cf_name):
                    qa_required_yes = _qa_required_affirmative(val)
                elif estimate_minutes == 0 and _is_estimate_field(cf_name):
                    estimate_minutes = _period_to_minutes(val)
                elif spent_minutes == 0 and _is_spent_field(cf_name):
                    spent_minutes = _period_to_minutes(val)

            # QA-skip candidate: QA is required AND the issue has reached the
            # release/done gate (where QA should already be done). Whether it
            # actually skipped QA is confirmed from history after the loop.
            if (
                qa_in_scope
                and qa_required_yes
                and classify_handoff_role(state) in _QA_GATE_ROLES
            ):
                qa_candidates.append((days_idle, issue_id, _risk_record(
                    issue_id, state, summary, assignee, priority,
                    f"QA Required, now [{state}] — verifying QA history",
                )))

            if estimate_minutes > 0 and spent_minutes > estimate_minutes:
                ratio = spent_minutes / estimate_minutes
                est_str = f"{estimate_minutes // 60}h" if estimate_minutes >= 60 else f"{estimate_minutes}m"
                spent_str = f"{spent_minutes // 60}h" if spent_minutes >= 60 else f"{spent_minutes}m"
                over_estimate.append((ratio, _risk_record(
                    issue_id, state, summary, assignee, priority,
                    f"Estimate {est_str}, Spent {spent_str} ({ratio:.0%})",
                )))

            # Unestimated: active issues without estimation
            if estimate_minutes == 0 and state_lower in (working_states | waiting_states):
                unestimated.append((days_idle, _risk_record(
                    issue_id, state, summary, assignee, priority,
                    "no estimation",
                )))

            # Ancient: open for too long (pause + completion states excluded)
            created_ms = issue.get("created", 0)
            if (
                created_ms
                and state_lower != "pause"
                and state_lower not in COMPLETION_STATES
            ):
                days_open = (now - datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)).days
                if days_open >= ancient_days:
                    ancient.append((days_open, _risk_record(
                        issue_id, state, summary, assignee, priority,
                        f"{days_open}d old",
                    )))

        # QA-skip confirmation: walk history for the (small) candidate set.
        # Only candidates whose history NEVER touched a QA state are real
        # skips. Empty/unavailable history is inconclusive — not flagged,
        # to avoid false alarms.
        qa_skip_unchecked = 0
        if qa_candidates:
            from yt_mcp.tools.deadlines.fetcher import fetch_activities_only_bounded

            qa_candidates.sort(key=lambda x: x[0], reverse=True)  # stalest first
            checked = qa_candidates[:_QA_SKIP_CHECK_MAX]
            qa_skip_unchecked = len(qa_candidates) - len(checked)

            cand_ids = [iid for _d, iid, _r in checked]
            activities_per = await fetch_activities_only_bounded(client, cand_ids)
            for (days_idle, _iid, rec), activities in zip(checked, activities_per):
                if activities and not _passed_qa_state(activities):
                    rec = dict(rec)
                    rec["detail"] = f"QA Required but never entered QA — now [{rec['state']}]"
                    qa_skipped.append((days_idle, rec))

        # `approaching` sorts ascending (soonest deadline first); the rest
        # sort descending on their severity metric (most overdue / stalest
        # / oldest first).
        approaching.sort(key=lambda x: x[0])
        for bucket in (overdue, qa_skipped, stalled, forgotten, over_estimate, unestimated, ancient):
            bucket.sort(key=lambda x: x[0], reverse=True)

        bucket_by_key = {
            "overdue": overdue, "qa_skipped": qa_skipped, "approaching": approaching,
            "stalled": stalled, "over_estimate": over_estimate,
            "unestimated": unestimated, "ancient": ancient, "forgotten": forgotten,
        }
        # Apply the optional single-category filter.
        active_categories = (
            [(category_key, dict(_AT_RISK_CATEGORIES)[category_key])]
            if category_key
            else list(_AT_RISK_CATEGORIES)
        )

        total_risks = sum(len(b) for b in bucket_by_key.values())
        thresholds = {
            "stale_days": stale_days, "forgotten_days": forgotten_days,
            "ancient_days": ancient_days, "deadline_warning_days": deadline_warning_days,
        }

        if format == "json":
            categories_out: dict[str, dict] = {}
            for key, _title in active_categories:
                items = bucket_by_key[key]
                # JSON returns the full set (source already bounded by $top=500).
                categories_out[key] = {
                    "count": len(items),
                    "issues": [rec for _v, rec in items],
                }
            payload = {
                "project": project,
                "total_at_risk": total_risks,
                "thresholds": thresholds,
                "categories": categories_out,
            }
            if category_key:
                payload["filtered_category"] = category_key
            if qa_skip_unchecked:
                payload["qa_skip_unchecked"] = qa_skip_unchecked
            return json.dumps(payload, indent=2, ensure_ascii=False)

        if total_risks == 0:
            return f"No at-risk issues found in **{project}** (stale: {stale_days}d, forgotten: {forgotten_days}d)."

        header = f"# At Risk Issues — {project}"
        if category_key:
            header += f" · {dict(_AT_RISK_CATEGORIES)[category_key]}"
        lines = [header, f"**Total at risk:** {total_risks}", ""]

        def _append_category(title: str, items: list[tuple[int | float, dict]]) -> None:
            if not items:
                return
            lines.append(f"## {title} ({len(items)})")
            for _, rec in items[:limit_per_category]:
                lines.append(_format_at_risk_line(
                    rec["id"], rec["state"], rec["summary"],
                    rec["assignee"], rec["priority"], f"**{rec['detail']}**",
                ))
            if len(items) > limit_per_category:
                lines.append(f"_...and {len(items) - limit_per_category} more_")
            lines.append("")

        for key, title in active_categories:
            _append_category(title, bucket_by_key[key])

        if qa_skip_unchecked:
            lines.append(
                f"_QA-skip: checked stalest {_QA_SKIP_CHECK_MAX} of "
                f"{_QA_SKIP_CHECK_MAX + qa_skip_unchecked} candidates; "
                f"{qa_skip_unchecked} not history-walked._"
            )

        return compact_lines(lines)

    @mcp.tool()
    async def check_task_creation(
        keywords: str,
        project: str = "",
        created_since: str = "7d",
        expected_priority: str = "",
        instance: str = "",
    ) -> str:
        """Check if a task matching keywords was created and assess its quality.

        Args:
            keywords: Search keywords
            project: Project short name (optional)
            created_since: Duration ('7d', '24h') or date. Default: 7d
            expected_priority: Priority to verify (optional)
            instance: YouTrack instance (optional)
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
            return compact_lines(lines)

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
                if link.get("direction") == "OUTWARD" and "subtask" in (link.get("linkType") or {}).get("name", "").lower():
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

            # State moved beyond Submitted? (+2) — with grace period for new issues
            hours_open = _hours_since(created_ms)
            if state.lower() not in ("submitted", "open", ""):
                quality_score += 2
                checks.append(f"State: **{state}** (progressing)")
            elif hours_open <= NEW_ISSUE_GRACE_HOURS:
                quality_score += 2
                checks.append(f"State: **{state}** (just created)")
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

        return compact_lines(lines)

    @mcp.tool()
    async def get_creation_activity(
        project: str,
        since: str = "7d",
        creator: str = "",
        limit: int = 20,
        instance: str = "",
    ) -> str:
        """Report of recently created issues with quality indicators.

        Args:
            project: Project short name
            since: Duration ('7d', '24h') or date. Default: 7d
            creator: Filter by creator name (optional)
            limit: Max issues (default: 20)
            instance: YouTrack instance (optional)
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

            # Quality indicators (with grace period for brand-new issues)
            hours_open = _hours_since(created_ms)
            flags: list[str] = []
            if assignee == "Unassigned" and hours_open > NEW_ISSUE_GRACE_HOURS:
                flags.append("no assignee")
            if not description:
                flags.append("no description")
            if priority in ("", "?"):
                flags.append("no priority")
            if state.lower() in ("submitted", "open") and hours_open > NEW_ISSUE_GRACE_HOURS:
                flags.append("not started")

            flag_str = f" — {', '.join(flags)}" if flags else ""

            lines.append(
                f"- **{issue_id}** [{state}] {summary}\n"
                f"  {reporter_name} → {assignee} | {priority} | {created_str}{flag_str}"
            )

        return compact_lines(lines)

    @mcp.tool()
    async def get_project_health(
        project: str,
        since: str = "24h",
        exclude_patterns: str = "",
        instance: str = "",
    ) -> str:
        """Project health report: state distribution, health metrics, and recently resolved issues.

        Args:
            project: Project short name
            since: Period for resolved issues (default: '24h')
            exclude_patterns: Comma-separated regex to exclude
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        patterns = compile_exclude_patterns(exclude_patterns) or _DEFAULT_EXCLUDE_PATTERNS
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

        # Track issues per risk category for score dedup
        risks: dict[str, list] = {
            "stalled": [],
            "forgotten": [],
            "ancient": [],
            "blocked": [],
            "unassigned": [],
        }

        for issue in all_unresolved:
            state = _resolve_state(issue).lower()
            state_counts[state] = state_counts.get(state, 0) + 1

            product = _get_custom_field(issue, "Product") or "No product"
            product_counts[product] = product_counts.get(product, 0) + 1

            days_idle = _days_since_update(issue)
            created_ms = issue.get("created", 0)
            days_open = (now - datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)).days if created_ms else 0
            hours_open = _hours_since(created_ms)

            # Unassigned: grace period for brand-new issues
            if _resolve_assignee(issue) == "Unassigned" and hours_open > NEW_ISSUE_GRACE_HOURS:
                unassigned_count += 1
                risks["unassigned"].append(issue)

            # Blocked: only flag if idle for BLOCKED_RISK_DAYS+
            if state == "blocked" and days_idle >= BLOCKED_RISK_DAYS:
                blocked += 1
                risks["blocked"].append(issue)

            if state in WORKING_STATES and days_idle > 7:
                stuck += 1
                risks["stalled"].append(issue)
            if days_idle > 30 and state != "pause" and state not in COMPLETION_STATES:
                stale += 1
                risks["forgotten"].append(issue)
            if days_open > 200 and state != "pause" and state not in COMPLETION_STATES:
                ancient += 1
                risks["ancient"].append(issue)

            has_estimate = False
            for cf in issue.get("customFields", []):
                if cf.get("name", "").lower() in ("estimation", "estimate", "dev estimate", "dev estimation"):
                    if cf.get("value") is not None:
                        has_estimate = True
                    break
            if not has_estimate:
                unestimated += 1

        health_score = _compute_health_score(len(all_unresolved), risks)

        # Build current snapshot
        current = {
            "total": len(all_unresolved),
            "unestimated": unestimated,
            "stuck": stuck,
            "stale": stale,
            "ancient": ancient,
            "blocked": blocked,
            "unassigned": unassigned_count,
            "ts": now.isoformat(),
        }

        # Load previous snapshot for delta
        prev = _load_snapshot(project)

        def pct(n: int) -> str:
            return f"{n * 100 // total}%"

        def delta(key: str, cur: int) -> str:
            if not prev or key not in prev:
                return ""
            diff = cur - prev[key]
            if diff == 0:
                return ""
            sign = "+" if diff > 0 else ""
            return f" ({sign}{diff})"

        lines = [f"# {project} — Project Health", ""]

        if prev:
            lines.append(f"_Compared to previous snapshot ({(prev.get('ts') or '?')[:10]})_")
            lines.append("")

        score_line = f"**Health score: {health_score}/100**"
        # Floor-aware: when score hits 0, surface flagged ratio for signal
        if health_score == 0 and len(all_unresolved) > 0:
            flagged = _count_flagged_issues(risks)
            score_line += f" ({flagged}/{len(all_unresolved)} flagged)"
        lines.append(score_line)
        lines.append("")
        lines.append("## Health metrics")
        lines.append("| Metric | Count | % | Delta | Severity |")
        lines.append("|---|---|---|---|---|")
        lines.append(f"| Total unresolved | {len(all_unresolved)} | 100% | {delta('total', len(all_unresolved))} | — |")
        lines.append(f"| Unestimated | {unestimated} | {pct(unestimated)} | {delta('unestimated', unestimated)} | {'CRITICAL' if unestimated > total // 4 else 'HIGH'} |")
        lines.append(f"| Stuck (>7d in progress) | {stuck} | {pct(stuck)} | {delta('stuck', stuck)} | CRITICAL |")
        lines.append(f"| Stale (>30d no update) | {stale} | {pct(stale)} | {delta('stale', stale)} | HIGH |")
        lines.append(f"| Ancient (>200d open) | {ancient} | {pct(ancient)} | {delta('ancient', ancient)} | CRITICAL |")
        lines.append(f"| Blocked | {blocked} | {pct(blocked)} | {delta('blocked', blocked)} | MEDIUM |")
        lines.append(f"| Unassigned | {unassigned_count} | {pct(unassigned_count)} | {delta('unassigned', unassigned_count)} | MEDIUM |")
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

        # Save current as new snapshot
        _save_snapshot(project, current)

        return compact_lines(lines)
