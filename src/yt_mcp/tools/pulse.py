"""Team Pulse — backward + forward board reporting with velocity insights.

One tool: get_team_pulse(board_name, horizon_days=14, lookback_days=30, limit=10).

Reads a board's column structure, classifies columns into roles
(triaged/incoming/re_entry/paused/in_progress/done), fetches lookback metrics
(closed, released, reopened, new incoming) and forward sections (ready to pull,
re-entry items, recent incoming), computes velocity-aware insight flags
(backlog growing, quality concern, pipeline bottleneck, deadline cliff,
stale triaged), and renders a markdown report with sectioned + team-balanced
views.

The "smart" part is the asymmetric default window (30d lookback / 14d horizon)
and the heuristic insight flags — they convert raw data into "should I act?"
guidance.
"""

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

from yt_mcp.formatters import (
    _resolve_state, _resolve_assignee, _get_custom_field,
    compact_lines,
)
from yt_mcp.resolver import InstanceResolver
from yt_mcp.tools.deadlines.parser import (
    _DEFAULT_STANDUP_PATTERNS, _is_standup, _compile_standup_patterns,
)


# --- Column-role classifier -------------------------------------------------
# Pattern-based mapping from a column/state name to its workflow role.
# Order matters: more-specific patterns checked first. Unknown columns default
# to "triaged" with a diagnostic emitted in the rendered output.

_COLUMN_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    # Re-entry: came back from review/test/release — block in-flight pipeline.
    ("re_entry", re.compile(r"(?i)(for\s+revision|reopen|rejected|needs?\s+rework|на\s+доработку)")),
    # Paused: not pullable.
    ("paused", re.compile(r"(?i)(blocked|on\s+hold|paus[ed]|waiting|wait)")),
    # In-progress lane: dev or QA.
    ("in_progress", re.compile(r"(?i)(in\s+progress|for\s+review|ready\s+for\s+test|on\s+testing|ready\s+for\s+release|in\s+review)")),
    # Done lane.
    ("done", re.compile(r"(?i)(closed|done|resolved|fixed|completed|released|verified)")),
    # Triaged: explicitly placed for pickup.
    ("triaged", re.compile(r"(?i)(to\s*do|todo|backlog|ready\s+for\s+dev|selected\s+for\s+dev|scheduled)")),
    # Incoming: raw new work that hasn't been triaged.
    ("incoming", re.compile(r"(?i)(submitted|new|open|reported|received)")),
)


def classify_column(name: str) -> str:
    """Map a column/state name to its role. Unknown → 'triaged' (safer than dropping)."""
    n = (name or "").strip()
    if not n:
        return "triaged"
    for role, pat in _COLUMN_PATTERNS:
        if pat.search(n):
            return role
    return "triaged"


# --- Ranking ---------------------------------------------------------------

_SEVERITY_WEIGHT = {
    "blocker": 5, "critical": 4, "major": 3, "minor": 1, "trivial": 0,
}
_TYPE_BONUS = {"bug": 2, "tech task": 1}
_PRIORITY_TIEBREAK = {"high": 1.0, "medium": 0.5, "med": 0.5}

_DAY_MS = 86400 * 1000


def _days_between_ms(later_ms: int, earlier_ms: int) -> float:
    return (later_ms - earlier_ms) / _DAY_MS


def _deadline_bonus(deadline_ms: int | None, now_ms: int) -> float:
    if not deadline_ms:
        return 0.0
    days = _days_between_ms(deadline_ms, now_ms)
    if days <= 0:
        # Already overdue — same weight as ≤7d (don't reward harder).
        return 4.0
    if days <= 7:
        return 4.0
    if days <= 14:
        return 2.0
    if days <= 30:
        return 1.0
    return 0.0


def _extract_deadline_ms(issue: dict) -> int | None:
    """Pull the Deadline ☠️ / Due Date value as a millis timestamp."""
    from yt_mcp.tools.deadlines.parser import _is_deadline_field, _extract_deadline_ts
    for cf in issue.get("customFields", []):
        if _is_deadline_field(cf.get("name", "")):
            return _extract_deadline_ts(cf.get("value"))
    return None


def _extract_severity(issue: dict) -> str:
    return (_get_custom_field(issue, "Severity") or "").lower()


def _extract_type(issue: dict) -> str:
    return (_get_custom_field(issue, "Type") or "").lower()


def _extract_priority(issue: dict) -> str:
    return (_get_custom_field(issue, "Priority") or "").lower()


def compute_pulse_score(issue: dict, now_ms: int) -> tuple[float, dict]:
    """Score an issue for ranking inside a pulse section.

    Severity > Type > Deadline-proximity > stale-in-state > priority tiebreak.
    Returns (total, breakdown) so the renderer can show why.
    """
    severity = _SEVERITY_WEIGHT.get(_extract_severity(issue), 0)
    type_bonus = _TYPE_BONUS.get(_extract_type(issue), 0)
    deadline = _deadline_bonus(_extract_deadline_ms(issue), now_ms)
    updated = issue.get("updated") or issue.get("created") or now_ms
    stale_days = max(0.0, _days_between_ms(now_ms, updated))
    stale_bonus = min(stale_days * 0.05, 3.0)
    prio = _PRIORITY_TIEBREAK.get(_extract_priority(issue), 0.0)
    total = severity + type_bonus + deadline + stale_bonus + prio
    return total, {
        "severity": severity, "type": type_bonus, "deadline": deadline,
        "stale": round(stale_bonus, 2), "priority": prio,
    }


# --- Filters --------------------------------------------------------------

def _is_blocked_by_unresolved(issue: dict) -> bool:
    """True iff issue has an unresolved 'is depended by' / 'is required for'
    relationship — we look for a link the issue 'depends on' that points to an
    unresolved blocker."""
    for link in issue.get("links", []) or []:
        link_type = (link.get("linkType") or {}).get("name", "").lower()
        direction = (link.get("direction") or "").lower()
        if direction == "outward" and ("depend" in link_type or "subtask" in link_type):
            for blocker in link.get("issues", []) or []:
                state = ""
                s = blocker.get("state")
                if isinstance(s, dict):
                    state = (s.get("name") or "").lower()
                if state and state not in {"closed", "done", "resolved", "fixed", "completed", "released", "verified"}:
                    return True
    return False


def _filter_issues(issues: list[dict], standup_patterns) -> list[dict]:
    """Drop standup/report meta-tasks and blocked-by-unresolved items."""
    keep = []
    for it in issues:
        summary = it.get("summary", "") or ""
        if _is_standup(summary, standup_patterns):
            continue
        if _is_blocked_by_unresolved(it):
            continue
        keep.append(it)
    return keep


# --- Field selectors -----------------------------------------------------

PULSE_ISSUE_FIELDS = (
    "idReadable,summary,created,updated,resolved,"
    "state(name),priority(name),"
    "assignee(login,name),"
    "customFields(name,value(login,presentation,name,text)),"
    "links(direction,linkType(name),issues(idReadable,state(name)))"
)

BOARD_FIELDS = (
    "id,name,projects(shortName,name),"
    "columnSettings(field(name),columns(presentation,fieldValues(name)))"
)


# --- Renderers -----------------------------------------------------------

_TEAM_POOL_RE = re.compile(r"(?i)\b(team|команда)\b")


def _is_team_pool(assignee: str) -> bool:
    """Synthetic '<group> Team' assignees (e.g. team-pool placeholders) = claim-by-anyone bucket."""
    return bool(assignee) and bool(_TEAM_POOL_RE.search(assignee))


def _round_robin_balance(items: list[tuple[dict, float]]) -> dict[str, list[tuple[dict, float]]]:
    """Group by assignee and round-robin so each dev gets one item per round.

    Returns dict {assignee_name: [(issue, score), ...]} in round-robin order.
    Team-pool assignees are gathered into a single 'Anyone' key.
    """
    per_user: dict[str, list[tuple[dict, float]]] = {}
    pool_items: list[tuple[dict, float]] = []
    for issue, score in items:
        a = _resolve_assignee(issue)
        if not a or a == "Unassigned":
            pool_items.append((issue, score))
        elif _is_team_pool(a):
            pool_items.append((issue, score))
        else:
            per_user.setdefault(a, []).append((issue, score))

    # Sort each user's items by score desc
    for k in per_user:
        per_user[k].sort(key=lambda x: x[1], reverse=True)
    pool_items.sort(key=lambda x: x[1], reverse=True)

    # Round-robin: take top of each user's queue in turn until exhausted
    ordered: dict[str, list[tuple[dict, float]]] = {k: [] for k in per_user}
    queues = {k: list(v) for k, v in per_user.items()}
    while any(queues.values()):
        for user in list(queues.keys()):
            if queues[user]:
                ordered[user].append(queues[user].pop(0))

    if pool_items:
        ordered["__pool__"] = pool_items
    return ordered


def _format_issue_line(issue: dict, score: float | None = None, now_ms: int = 0) -> str:
    iid = issue.get("idReadable", "?")
    summary = (issue.get("summary", "") or "?")[:90]
    state = _resolve_state(issue)
    sev = _get_custom_field(issue, "Severity") or "-"
    typ = _get_custom_field(issue, "Type") or "-"
    updated = issue.get("updated") or issue.get("created") or now_ms
    age_days = int(_days_between_ms(now_ms, updated)) if now_ms else 0
    dl_ms = _extract_deadline_ms(issue)
    extras = [f"{sev}", f"{typ}", f"{age_days}d in {state}"]
    if dl_ms:
        dl_days = int(_days_between_ms(dl_ms, now_ms))
        marker = f"deadline in {dl_days}d" if dl_days >= 0 else f"overdue {-dl_days}d"
        extras.append(marker)
    score_str = f" (s:{score:.1f})" if score is not None else ""
    return f"- **{iid}**{score_str} — {summary} [{', '.join(extras)}]"


# --- Insights ------------------------------------------------------------

def compute_insights(metrics: dict, pipeline_counts: dict, triaged: list[dict], now_ms: int) -> list[str]:
    """Heuristic flags from velocity + pipeline state. Empty list when healthy."""
    flags: list[str] = []
    closed = metrics["closed"]
    incoming = metrics["incoming"]
    reopened = metrics["reopened"]

    if closed == 0:
        flags.append("⚠️ No work closed in lookback window — velocity unknown")
    else:
        if incoming > closed * 1.3:
            flags.append(f"📈 Backlog growing — {incoming} new vs {closed} closed in lookback ({incoming/closed:.1f}× rate)")
        if reopened / closed > 0.2:
            flags.append(f"🐛 Quality concern — {reopened} reopened / {closed} closed ({reopened*100//closed}% reopen rate)")

    in_flight = pipeline_counts.get("in_progress", 0) + pipeline_counts.get("for_review", 0)
    pipeline_total = (
        in_flight
        + pipeline_counts.get("ready_for_test", 0)
        + pipeline_counts.get("on_testing", 0)
    )
    if closed > 0:
        if in_flight < closed / 3:
            flags.append(f"💤 Team underloaded — only {in_flight} in flight vs {closed} closed in lookback")
        if pipeline_total > closed * 2:
            flags.append(f"🚧 WIP overload — {pipeline_total} in pipeline vs {closed} closed in lookback")

    # Bottleneck: any one downstream column has ≥3 more items than the next.
    order = ("in_progress", "for_review", "ready_for_test", "on_testing", "ready_for_release")
    for i, col in enumerate(order):
        cnt = pipeline_counts.get(col, 0)
        if cnt < 3:
            continue
        next_cnt = pipeline_counts.get(order[i + 1], 0) if i + 1 < len(order) else None
        if next_cnt is not None and cnt - next_cnt >= 3:
            display = col.replace("_", " ").title()
            flags.append(f"🔻 Bottleneck — `{display}` has {cnt} items, +{cnt - next_cnt} vs next column")
            break

    # Deadline cliff: ≥3 items with deadlines ≤7d not in in_progress.
    cliff = 0
    for i in triaged:
        dl = _extract_deadline_ms(i)
        if dl is None:
            continue
        days = _days_between_ms(dl, now_ms)
        if 0 <= days <= 7:
            cliff += 1
    if cliff >= 3:
        flags.append(f"⏰ Deadline cliff — {cliff} ready-but-not-started items due ≤7d")

    # Stale triaged: items sitting in triaged states for >30d.
    stale = 0
    for i in triaged:
        upd = i.get("updated") or i.get("created") or now_ms
        if _days_between_ms(now_ms, upd) > 30:
            stale += 1
    if stale >= 2:
        flags.append(f"🕸️ Stale triaged — {stale} items >30d in queue (investigate why no pickup)")

    return flags


# --- Tool entry ------------------------------------------------------------

def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def get_team_pulse(
        board_name: str,
        horizon_days: int = 14,
        lookback_days: int = 30,
        limit: int = 10,
        instance: str = "",
    ) -> str:
        """Team pulse for a board: what shipped in the last {lookback_days}d
        and what's coming in the next {horizon_days}d, with velocity-aware
        insight flags and a team-balanced per-dev view.

        Reads the board's column structure, classifies columns by role
        (triaged/incoming/re_entry/in_progress/done), then fetches:
          - Lookback: closed, released, reopened, new incoming
          - Forward: ready-to-pull, pipeline-unblockers, recent incoming

        Heuristic flags surface "should I act?" signals: backlog growth,
        reopen rate, WIP overload, bottlenecks, deadline cliff, stale queue.

        Args:
            board_name: Board name (partial match), ID, or URL.
            horizon_days: Forward planning window (default 14).
            lookback_days: Backward velocity window (default 30 — smooths weekly variance).
            limit: Max items per section (default 10).
            instance: YouTrack instance (optional).
        """
        client = resolver.resolve(instance, board_name)

        # 1) Resolve the board → get projects, column structure, state→role mapping.
        boards = await client.get(
            "/api/agiles",
            params={"fields": BOARD_FIELDS},
        )
        query_lower = board_name.lower()
        matches = [b for b in boards if query_lower in b.get("name", "").lower()]
        if not matches:
            return f"No agile board matching '{board_name}'."
        if len(matches) > 1:
            names = ", ".join(f"'{b.get('name')}'" for b in matches)
            return f"Multiple boards match '{board_name}': {names}. Be more specific."
        board = matches[0]
        board_display = board.get("name", board_name)
        projects = [p.get("shortName", "") for p in board.get("projects", []) if p.get("shortName")]
        if not projects:
            return f"Board '{board_display}' has no projects bound."

        # Column → role mapping (auto-classify, log unknowns)
        col_settings = board.get("columnSettings") or {}
        columns = col_settings.get("columns") or []
        state_to_role: dict[str, str] = {}
        unknown_columns: list[str] = []
        for col in columns:
            for fv in col.get("fieldValues") or []:
                state_name = fv.get("name") or ""
                if not state_name:
                    continue
                role = classify_column(state_name)
                state_to_role[state_name] = role
            # Also classify the column presentation itself in case fieldValues empty.
            pres = col.get("presentation") or ""
            if pres and not col.get("fieldValues"):
                role = classify_column(pres)
                state_to_role[pres] = role
                if role == "triaged" and not _COLUMN_PATTERNS_match_any(pres):
                    unknown_columns.append(pres)

        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        standup_patterns = _compile_standup_patterns({})

        # 2) Build the project clause once.
        if len(projects) == 1:
            project_clause = f"project: {projects[0]}"
        else:
            project_clause = "(" + " or ".join(f"project: {p}" for p in projects) + ")"

        # 3) Fetch forward sections (triaged + re_entry + incoming) and pipeline + done in parallel.
        triaged_states = [s for s, r in state_to_role.items() if r == "triaged"]
        re_entry_states = [s for s, r in state_to_role.items() if r == "re_entry"]
        incoming_states = [s for s, r in state_to_role.items() if r == "incoming"]

        # Each pipeline lane separately for bottleneck detection
        pipeline_lanes = {
            "in_progress": re.compile(r"(?i)in\s+progress"),
            "for_review": re.compile(r"(?i)for\s+review|in\s+review"),
            "ready_for_test": re.compile(r"(?i)ready\s+for\s+test"),
            "on_testing": re.compile(r"(?i)on\s+testing"),
            "ready_for_release": re.compile(r"(?i)ready\s+for\s+release"),
        }
        lane_states: dict[str, list[str]] = {k: [] for k in pipeline_lanes}
        for state, role in state_to_role.items():
            if role != "in_progress":
                continue
            for lane, pat in pipeline_lanes.items():
                if pat.search(state):
                    lane_states[lane].append(state)
                    break

        async def _fetch_by_states(states: list[str], recency_clause: str = "") -> list[dict]:
            if not states:
                return []
            state_clause = "State: " + ", ".join(f"{{{s}}}" for s in states)
            q = f"{project_clause} {state_clause} #Unresolved {recency_clause}".strip()
            data = await client.get(
                "/api/issues",
                params={"query": q, "fields": PULSE_ISSUE_FIELDS, "$top": "200"},
            )
            return data or []

        async def _count_by_query(query: str) -> int:
            data = await client.get(
                "/api/issues",
                params={"query": query, "fields": "idReadable", "$top": "500"},
            )
            return len(data or [])

        # Lookback queries — use `resolved:` for closed (exact) and `created:` for incoming.
        lookback_clause = f"-{lookback_days}d .. *"
        closed_q = f"{project_clause} resolved: {lookback_clause}"
        incoming_q = f"{project_clause} created: {lookback_clause} #Unresolved"
        # Released = resolved AND state matches release-like name. Done lane has
        # patterns "released", "closed", etc. — count via state filter where
        # available, else fall back to a 0 with a note.
        released_states = [s for s, r in state_to_role.items() if r == "done" and "release" in s.lower()]

        recency_clause_incoming = f"created: {lookback_clause}"

        # All fetches in parallel
        (
            triaged_issues,
            re_entry_issues,
            incoming_issues,
            in_progress_issues,
            for_review_issues,
            ready_for_test_issues,
            on_testing_issues,
            ready_for_release_issues,
            closed_count,
            incoming_count,
            released_count,
        ) = await asyncio.gather(
            _fetch_by_states(triaged_states),
            _fetch_by_states(re_entry_states),
            _fetch_by_states(incoming_states, recency_clause_incoming),
            _fetch_by_states(lane_states["in_progress"]),
            _fetch_by_states(lane_states["for_review"]),
            _fetch_by_states(lane_states["ready_for_test"]),
            _fetch_by_states(lane_states["on_testing"]),
            _fetch_by_states(lane_states["ready_for_release"]),
            _count_by_query(closed_q),
            _count_by_query(incoming_q),
            _count_by_query(f"{project_clause} resolved: {lookback_clause} "
                            + ("State: " + ", ".join(f"{{{s}}}" for s in released_states) if released_states else ""))
            if released_states else asyncio.sleep(0, result=0),
        )

        # Reopened in lookback: items with ReopenCount > 0 AND updated in window.
        # Cheap heuristic — exact would need activities. We query for items where
        # ReopenCount exists and updated falls in the window, then filter.
        reopened_count = 0
        try:
            reopened_data = await client.get(
                "/api/issues",
                params={
                    "query": f"{project_clause} updated: {lookback_clause} has: ReopenCount",
                    "fields": "idReadable,customFields(name,value(text,name,presentation))",
                    "$top": "500",
                },
            )
            for it in reopened_data or []:
                rc = _get_custom_field(it, "ReopenCount")
                try:
                    if rc and int(str(rc)) > 0:
                        reopened_count += 1
                except (ValueError, TypeError):
                    continue
        except (ValueError, KeyError):
            reopened_count = 0

        # 4) Apply filters (standup + blocked-by) to forward sections.
        triaged_filtered = _filter_issues(triaged_issues, standup_patterns)
        re_entry_filtered = _filter_issues(re_entry_issues, standup_patterns)
        incoming_filtered = _filter_issues(incoming_issues, standup_patterns)

        # 5) Score & rank forward sections.
        def _rank(items: list[dict]) -> list[tuple[float, dict, dict]]:
            scored = [(*compute_pulse_score(i, now_ms), i) for i in items]
            # scored is [(total, breakdown, issue), ...]
            scored.sort(key=lambda x: x[0], reverse=True)
            return scored

        triaged_ranked = _rank(triaged_filtered)
        re_entry_ranked = _rank(re_entry_filtered)
        incoming_ranked = _rank(incoming_filtered)

        # 6) Pipeline counts for insights.
        pipeline_counts = {
            "in_progress": len(in_progress_issues),
            "for_review": len(for_review_issues),
            "ready_for_test": len(ready_for_test_issues),
            "on_testing": len(on_testing_issues),
            "ready_for_release": len(ready_for_release_issues),
        }
        metrics = {
            "closed": closed_count,
            "released": released_count,
            "incoming": incoming_count,
            "reopened": reopened_count,
        }
        insights = compute_insights(metrics, pipeline_counts, triaged_filtered + re_entry_filtered, now_ms)

        # 7) Render.
        lines: list[str] = []
        lines.append(f"# {board_display} — Team pulse  (←{lookback_days}d  |  {horizon_days}d→)")
        lines.append("")

        lines.append("## At a glance")
        lines.append(f"- **Throughput:**    {closed_count} closed / {lookback_days}d")
        if released_states:
            lines.append(f"- **Shipped:**       {released_count} released / {lookback_days}d")
        lines.append(
            f"- **Pipeline now:**  {pipeline_counts['in_progress']} in progress · "
            f"{pipeline_counts['for_review']} in review · "
            f"{pipeline_counts['ready_for_test'] + pipeline_counts['on_testing']} on test · "
            f"{pipeline_counts['ready_for_release']} ready to ship"
        )
        lines.append(f"- **Incoming:**      {incoming_count} new in {lookback_days}d")
        lines.append(f"- **Reopened:**      {reopened_count} in {lookback_days}d")
        lines.append("")

        if insights:
            lines.append("### Flags")
            for f in insights:
                lines.append(f"- {f}")
            lines.append("")

        if unknown_columns:
            lines.append(
                f"_Diagnostic: unrecognized columns treated as `triaged`: "
                f"{', '.join(unknown_columns)}_"
            )
            lines.append("")

        # Forward sections
        lines.append(f"## → Coming in next {horizon_days}d")
        lines.append("")
        if re_entry_ranked:
            lines.append(f"### Pipeline-unblockers (re-entry) — {len(re_entry_ranked)}")
            for total, _bd, issue in re_entry_ranked[:limit]:
                lines.append(_format_issue_line(issue, total, now_ms))
            lines.append("")
        if triaged_ranked:
            lines.append(f"### Ready to pull (triaged) — {len(triaged_ranked)}")
            for total, _bd, issue in triaged_ranked[:limit]:
                lines.append(_format_issue_line(issue, total, now_ms))
            lines.append("")
        if incoming_ranked:
            lines.append(f"### Incoming — needs PM triage (last {lookback_days}d) — {len(incoming_ranked)}")
            for total, _bd, issue in incoming_ranked[:limit]:
                lines.append(_format_issue_line(issue, total, now_ms))
            lines.append("")

        # Team-balanced view
        combined = (
            [(i, t) for t, _b, i in re_entry_ranked]
            + [(i, t) for t, _b, i in triaged_ranked]
        )[:limit * 2]
        if combined:
            balanced = _round_robin_balance(combined)
            lines.append(f"## Team-balanced (next {horizon_days}d)")
            lines.append("")
            for user, items in balanced.items():
                if user == "__pool__":
                    continue
                lines.append(f"### {user}")
                for issue, score in items:
                    lines.append(_format_issue_line(issue, score, now_ms))
                lines.append("")
            pool = balanced.get("__pool__")
            if pool:
                lines.append("### Available to claim (Team pool / unassigned)")
                for issue, score in pool:
                    lines.append(_format_issue_line(issue, score, now_ms))
                lines.append("")

        if not (re_entry_ranked or triaged_ranked or incoming_ranked):
            lines.append("_Nothing in the forward queue — every column upstream of In Progress is empty._")
            lines.append("")

        return compact_lines(lines)


def _COLUMN_PATTERNS_match_any(name: str) -> bool:
    """True iff `name` matches any known pattern (not the triaged fallback)."""
    for _role, pat in _COLUMN_PATTERNS:
        if pat.search(name):
            return True
    return False
