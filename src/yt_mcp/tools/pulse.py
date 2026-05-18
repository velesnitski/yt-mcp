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
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from yt_mcp.formatters import (
    _resolve_state, _resolve_assignee, _resolve_assignee_login,
    _get_custom_field, compact_lines,
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


def build_lookback_clause(lookback_days: int, now_ms: int) -> str:
    """Absolute-date range for a YouTrack `resolved:`/`created:` query.

    Returns `YYYY-MM-DD .. YYYY-MM-DD`. We previously used `-Nd .. *` but
    YouTrack rejects the `*` upper bound and bare relative-offset bounds are
    version-dependent — absolute ISO dates are portable.
    """
    end_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)
    return f"{start_dt.strftime('%Y-%m-%d')} .. {end_dt.strftime('%Y-%m-%d')}"


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


# --- Stale / overdue filters --------------------------------------------------
#
# Downstream reporting (velesnitski/youtrack-reports) discovered the raw
# pipeline buckets are full of "100–200d-idle ghosts" — items technically in
# `In Progress`/`For review`/`On testing` but with no activity in months. They
# inflate WIP counts and dilute the velocity-ratio insight flags. Filtering
# them out matched real-world team activity (e.g. 200 → 81 on one board).
#
# `max_idle_days=0` disables the filter (escape hatch for forensic reviews).
# Missing `updated` is treated as active — surface, don't silently drop.

def _is_active(issue: dict, max_idle_days: int, now_ms: int) -> bool:
    """True if issue was updated within `max_idle_days`. Disabled when 0/None."""
    if not max_idle_days:
        return True
    updated = issue.get("updated") or issue.get("created")
    if not updated:
        return True  # missing field — keep (safer than dropping)
    return _days_between_ms(now_ms, updated) <= max_idle_days


def _is_too_overdue(issue: dict, max_overdue_days: int, now_ms: int) -> bool:
    """True if deadline is past by more than `max_overdue_days`. Items deeply
    overdue aren't realistically "next up" — they need a different conversation
    than a pulse report."""
    if not max_overdue_days:
        return False
    dl = _extract_deadline_ms(issue)
    if dl is None:
        return False
    days_past = -_days_between_ms(dl, now_ms)  # positive iff overdue
    return days_past > max_overdue_days


def _filter_active(issues: list[dict], max_idle_days: int, now_ms: int) -> list[dict]:
    return [i for i in issues if _is_active(i, max_idle_days, now_ms)]


def _filter_not_too_overdue(issues: list[dict], max_overdue_days: int, now_ms: int) -> list[dict]:
    return [i for i in issues if not _is_too_overdue(i, max_overdue_days, now_ms)]


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


def _issue_to_dict(issue: dict, score: float | None = None,
                   breakdown: dict | None = None, now_ms: int = 0) -> dict:
    """Serialize an issue + score into a JSON-friendly dict for the JSON output."""
    updated = issue.get("updated") or issue.get("created") or now_ms
    age_days = int(_days_between_ms(now_ms, updated)) if now_ms else 0
    dl_ms = _extract_deadline_ms(issue)
    dl_days = int(_days_between_ms(dl_ms, now_ms)) if (dl_ms and now_ms) else None
    out: dict = {
        "id": issue.get("idReadable", "?"),
        "summary": issue.get("summary", "") or "",
        "state": _resolve_state(issue),
        "assignee": _resolve_assignee(issue),
        # YouTrack `Assignee:` query filter needs the login, not display name —
        # so consumers can build live drilldown URLs without falling back to
        # frozen ID-lists. None when no login is resolvable.
        "assignee_login": _resolve_assignee_login(issue),
        "severity": _get_custom_field(issue, "Severity"),
        "type": _get_custom_field(issue, "Type"),
        "priority": _get_custom_field(issue, "Priority"),
        "age_days_in_state": age_days,
        "deadline_days": dl_days,
    }
    if score is not None:
        out["score"] = round(score, 2)
    if breakdown is not None:
        out["breakdown"] = breakdown
    return out


def _build_team_balanced(re_entry_ranked, triaged_ranked, limit: int, now_ms: int):
    """Round-robin top forward items across assignees, returning
    JSON-friendly per-user lists plus a 'pool' (unassigned + team-pool) list.

    Each item is an issue-dict from `_issue_to_dict` with score embedded."""
    combined = (
        [(i, t) for t, _b, i in re_entry_ranked]
        + [(i, t) for t, _b, i in triaged_ranked]
    )[: limit * 2]
    if not combined:
        return [], []
    balanced = _round_robin_balance(combined)
    per_user = []
    pool_serialized = [_issue_to_dict(issue, score, now_ms=now_ms)
                       for issue, score in balanced.get("__pool__", [])]
    for user, items in balanced.items():
        if user == "__pool__":
            continue
        serialized = [_issue_to_dict(issue, score, now_ms=now_ms)
                      for issue, score in items]
        per_user.append({"assignee": user, "items": serialized})
    return per_user, pool_serialized


def _render_markdown(payload: dict, limit: int) -> str:
    """Render a payload dict into the markdown report."""
    board_display = payload["board"]
    lookback_days = payload["lookback_days"]
    horizon_days = payload["horizon_days"]
    metrics = payload["metrics"]
    pipeline_counts = payload["pipeline_counts"]
    insights = payload["insights"]
    unknown_columns = payload.get("unknown_columns", [])
    re_entry_items = payload["re_entry"]
    triaged_items = payload["triaged"]
    incoming_items = payload["incoming"]
    team_balanced = payload["team_balanced"]
    pool = payload["team_pool"]
    has_released = payload.get("has_released_states", False)

    lines: list[str] = []
    lines.append(f"# {board_display} — Team pulse  (←{lookback_days}d  |  {horizon_days}d→)")
    lines.append("")

    lines.append("## At a glance")
    lines.append(f"- **Throughput:**    {metrics['closed']} closed / {lookback_days}d")
    if has_released:
        lines.append(f"- **Shipped:**       {metrics['released']} released / {lookback_days}d")
    lines.append(
        f"- **Pipeline now:**  {pipeline_counts['in_progress']} in progress · "
        f"{pipeline_counts['for_review']} in review · "
        f"{pipeline_counts['ready_for_test'] + pipeline_counts['on_testing']} on test · "
        f"{pipeline_counts['ready_for_release']} ready to ship"
    )
    lines.append(f"- **Incoming:**      {metrics['incoming']} new in {lookback_days}d")
    lines.append(f"- **Reopened:**      {metrics['reopened']} in {lookback_days}d")
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

    lines.append(f"## → Coming in next {horizon_days}d")
    lines.append("")
    if re_entry_items:
        lines.append(f"### Pipeline-unblockers (re-entry) — {len(re_entry_items)}")
        for it in re_entry_items[:limit]:
            lines.append(_format_issue_line_from_dict(it))
        lines.append("")
    if triaged_items:
        lines.append(f"### Ready to pull (triaged) — {len(triaged_items)}")
        for it in triaged_items[:limit]:
            lines.append(_format_issue_line_from_dict(it))
        lines.append("")
    if incoming_items:
        lines.append(f"### Incoming — needs PM triage (last {lookback_days}d) — {len(incoming_items)}")
        for it in incoming_items[:limit]:
            lines.append(_format_issue_line_from_dict(it))
        lines.append("")

    if team_balanced or pool:
        lines.append(f"## Team-balanced (next {horizon_days}d)")
        lines.append("")
        for entry in team_balanced:
            lines.append(f"### {entry['assignee']}")
            for d in entry["items"]:
                lines.append(_format_issue_line_from_dict(d))
            lines.append("")
        if pool:
            lines.append("### Available to claim (Team pool / unassigned)")
            for d in pool:
                lines.append(_format_issue_line_from_dict(d))
            lines.append("")

    if not (re_entry_items or triaged_items or incoming_items):
        lines.append("_Nothing in the forward queue — every column upstream of In Progress is empty._")
        lines.append("")

    return compact_lines(lines)


def _format_issue_line_from_dict(d: dict) -> str:
    extras = [
        f"{d.get('severity') or '-'}",
        f"{d.get('type') or '-'}",
        f"{d.get('age_days_in_state', 0)}d in {d.get('state', '?')}",
    ]
    dl = d.get("deadline_days")
    if dl is not None:
        marker = f"deadline in {dl}d" if dl >= 0 else f"overdue {-dl}d"
        extras.append(marker)
    score_str = f" (s:{d['score']:.1f})" if "score" in d and d["score"] is not None else ""
    summary = (d.get("summary") or "?")[:90]
    return f"- **{d['id']}**{score_str} — {summary} [{', '.join(extras)}]"


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

def compute_insights(
    metrics: dict, pipeline_counts: dict, triaged: list[dict], now_ms: int,
    lookback_days: int = 30,
) -> list[str]:
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

    in_progress = pipeline_counts.get("in_progress", 0)
    in_flight = in_progress + pipeline_counts.get("for_review", 0)
    pipeline_total = (
        in_flight
        + pipeline_counts.get("ready_for_test", 0)
        + pipeline_counts.get("on_testing", 0)
    )
    if closed > 0:
        if in_flight < closed / 3:
            flags.append(f"💤 Team underloaded — only {in_flight} in flight vs {closed} closed in lookback")
        # WIP overload = devs juggling too many concurrent items. Measured
        # against weekly velocity, not total throughput — a team that closes
        # 28/30d has weekly_velocity ≈ 6.5 and a healthy WIP cap of ~13.
        weekly_velocity = closed / max(1.0, lookback_days / 7.0)
        if weekly_velocity > 0 and in_progress > weekly_velocity * 2:
            flags.append(
                f"🚧 WIP overload — {in_progress} in progress vs "
                f"{weekly_velocity:.1f}/wk velocity (cap ~{int(weekly_velocity * 2)})"
            )
        # Pipeline overload = clog downstream of dev (test queue, review queue).
        # Distinct from WIP — e.g. a team with 5 in_progress / 6 for_review /
        # 34 ready_for_test flags pipeline congestion without falsely
        # accusing devs of overcommitting at the dev stage.
        if pipeline_total > closed * 2:
            flags.append(
                f"🪣 Pipeline overload — {pipeline_total} in pipeline vs "
                f"{closed} closed in lookback"
            )

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
        format: str = "report",
        max_idle_days: int = 60,
        max_overdue_days: int = 30,
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
        reopen rate, WIP overload (concurrent dev work), pipeline overload
        (downstream-of-dev clog), bottlenecks, deadline cliff, stale queue.

        Args:
            board_name: Board name (partial match), ID, or URL.
            horizon_days: Forward planning window (default 14).
            lookback_days: Backward velocity window (default 30 — smooths weekly variance).
            limit: Max items per section (default 10).
            format: "report" (default, markdown) or "json" (JSON-stringified
                payload for programmatic consumption — board, metrics,
                pipeline_counts, ranked section lists, team_balanced, insights).
            max_idle_days: Drop pipeline + re_entry items not updated within
                this many days (default 60). Pass 0 to disable. Justified by
                real-data measurement showing 50%+ noise from abandoned tickets
                that still technically sit in In Progress/For review states.
            max_overdue_days: Drop forward items whose deadline is past by more
                than this many days (default 30). Pass 0 to disable. Deeply
                overdue items need a different conversation than "next up".
            instance: YouTrack instance (optional).
        """
        client = resolver.resolve(instance, board_name)
        board, err = await _resolve_board_for_pulse(client, board_name)
        if not board:
            return err
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        payload = await _build_pulse_payload(
            client, board, horizon_days, lookback_days, limit,
            max_idle_days, max_overdue_days, now_ms,
        )
        if isinstance(payload, str):
            return payload  # error string (e.g. "Board has no projects")
        if format == "json":
            return json.dumps(payload, indent=2, ensure_ascii=False)
        return _render_markdown(payload, limit)

    @mcp.tool()
    async def get_multi_team_pulse(
        boards: str,
        horizon_days: int = 14,
        lookback_days: int = 30,
        limit: int = 5,
        format: str = "report",
        max_idle_days: int = 60,
        max_overdue_days: int = 30,
        instance: str = "",
    ) -> str:
        """Parallel pulse across multiple boards with aggregated org-wide view.

        Same per-board logic as `get_team_pulse`, fanned out via asyncio.gather
        so 7 boards take ~one board's worth of time instead of seven. Failed
        boards (no projects, unresolvable name) are listed in the output but
        don't kill the rest.

        Output adds an org-wide aggregate (summed metrics + pipeline counts +
        flag counts across boards) above per-board summaries.

        Args:
            boards: Comma-separated board names (partial match each).
            horizon_days: Forward planning window (default 14).
            lookback_days: Backward velocity window (default 30).
            limit: Max items per section per board (default 5 — multi-board
                view defaults lower than single-board to keep output compact).
            format: "report" (default, markdown) or "json" (parseable payload
                with `aggregate` + `boards` keys).
            max_idle_days: Drop pipeline + re_entry items not updated within
                this many days (default 60). Pass 0 to disable.
            max_overdue_days: Drop forward items past their deadline by more
                than this many days (default 30). Pass 0 to disable.
            instance: YouTrack instance (optional).
        """
        client = resolver.resolve(instance)
        board_names = [b.strip() for b in (boards or "").split(",") if b.strip()]
        if not board_names:
            return "No board names provided. Pass comma-separated board names."

        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

        # Resolve all boards in parallel
        resolutions = await asyncio.gather(
            *(_resolve_board_for_pulse(client, name) for name in board_names)
        )
        resolved: list[tuple[str, dict]] = []
        errors: list[str] = []
        for name, (board, err) in zip(board_names, resolutions):
            if board:
                resolved.append((name, board))
            else:
                errors.append(f"`{name}`: {err}")

        if not resolved:
            return "No boards could be resolved:\n" + "\n".join(f"- {e}" for e in errors)

        # Build payloads in parallel
        results = await asyncio.gather(
            *(
                _build_pulse_payload(
                    client, board, horizon_days, lookback_days, limit,
                    max_idle_days, max_overdue_days, now_ms,
                )
                for _name, board in resolved
            ),
            return_exceptions=True,
        )

        payloads: list[dict] = []
        for (name, _board), result in zip(resolved, results):
            if isinstance(result, dict):
                payloads.append(result)
            elif isinstance(result, str):
                errors.append(f"`{name}`: {result}")
            else:
                # asyncio.gather captured an exception
                errors.append(f"`{name}`: {type(result).__name__}: {result}")

        if not payloads:
            return "All boards failed:\n" + "\n".join(f"- {e}" for e in errors)

        aggregate = _aggregate_payloads(payloads, lookback_days, horizon_days)

        if format == "json":
            out = {"aggregate": aggregate, "boards": payloads}
            if errors:
                out["errors"] = errors
            return json.dumps(out, indent=2, ensure_ascii=False)

        md = _render_multi_markdown(aggregate, payloads, limit)
        if errors:
            md += "\n\n_Errors:_\n" + "\n".join(f"- {e}" for e in errors)
        return md


def _COLUMN_PATTERNS_match_any(name: str) -> bool:
    """True iff `name` matches any known pattern (not the triaged fallback)."""
    for _role, pat in _COLUMN_PATTERNS:
        if pat.search(name):
            return True
    return False


# --- Cross-tool helpers (reused by get_team_pulse + get_multi_team_pulse) ----

async def _resolve_board_for_pulse(client, board_name: str) -> tuple[dict | None, str]:
    """Find an agile board by partial-name match. Returns (board, err_msg).
    Multi-match returns (None, err_msg)."""
    boards = await client.get("/api/agiles", params={"fields": BOARD_FIELDS})
    query_lower = board_name.lower()
    matches = [b for b in boards if query_lower in b.get("name", "").lower()]
    if not matches:
        return None, f"No agile board matching '{board_name}'."
    if len(matches) > 1:
        names = ", ".join(f"'{b.get('name')}'" for b in matches)
        return None, f"Multiple boards match '{board_name}': {names}. Be more specific."
    return matches[0], ""


def _build_pipeline_lane_states(state_to_role: dict[str, str]) -> dict[str, list[str]]:
    """Group in_progress-role states into pipeline lanes for bottleneck detection."""
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
    return lane_states


def _classify_board_columns(board: dict) -> tuple[dict[str, str], list[str]]:
    """Map board's state values to roles. Returns (state→role, unknown_columns)."""
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
        pres = col.get("presentation") or ""
        if pres and not col.get("fieldValues"):
            role = classify_column(pres)
            state_to_role[pres] = role
            if role == "triaged" and not _COLUMN_PATTERNS_match_any(pres):
                unknown_columns.append(pres)
    return state_to_role, unknown_columns


async def _build_pulse_payload(
    client, board: dict,
    horizon_days: int, lookback_days: int, limit: int,
    max_idle_days: int, max_overdue_days: int, now_ms: int,
) -> dict | str:
    """Full per-board pulse pipeline. Returns the JSON-friendly payload dict,
    or an error message string if the board can't be processed."""
    board_display = board.get("name", "?")
    projects = [p.get("shortName", "") for p in board.get("projects", []) if p.get("shortName")]
    if not projects:
        return f"Board '{board_display}' has no projects bound."

    state_to_role, unknown_columns = _classify_board_columns(board)
    standup_patterns = _compile_standup_patterns({})

    if len(projects) == 1:
        project_clause = f"project: {projects[0]}"
    else:
        project_clause = "(" + " or ".join(f"project: {p}" for p in projects) + ")"

    triaged_states = [s for s, r in state_to_role.items() if r == "triaged"]
    re_entry_states = [s for s, r in state_to_role.items() if r == "re_entry"]
    incoming_states = [s for s, r in state_to_role.items() if r == "incoming"]
    lane_states = _build_pipeline_lane_states(state_to_role)

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

    lookback_clause = build_lookback_clause(lookback_days, now_ms)
    closed_q = f"{project_clause} resolved: {lookback_clause}"
    incoming_q = f"{project_clause} created: {lookback_clause} #Unresolved"
    released_states = [s for s, r in state_to_role.items() if r == "done" and "release" in s.lower()]
    recency_clause_incoming = f"created: {lookback_clause}"

    (
        triaged_issues, re_entry_issues, incoming_issues,
        in_progress_issues, for_review_issues, ready_for_test_issues,
        on_testing_issues, ready_for_release_issues,
        closed_count, incoming_count, released_count,
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
        _count_by_query(
            f"{project_clause} resolved: {lookback_clause} "
            + ("State: " + ", ".join(f"{{{s}}}" for s in released_states) if released_states else "")
        ) if released_states else asyncio.sleep(0, result=0),
    )

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

    # Filters: standup/blocked-by/stale/overdue
    triaged_filtered = _filter_not_too_overdue(
        _filter_issues(triaged_issues, standup_patterns), max_overdue_days, now_ms,
    )
    re_entry_filtered = _filter_not_too_overdue(
        _filter_active(_filter_issues(re_entry_issues, standup_patterns), max_idle_days, now_ms),
        max_overdue_days, now_ms,
    )
    incoming_filtered = _filter_not_too_overdue(
        _filter_issues(incoming_issues, standup_patterns), max_overdue_days, now_ms,
    )
    in_progress_issues = _filter_active(in_progress_issues, max_idle_days, now_ms)
    for_review_issues = _filter_active(for_review_issues, max_idle_days, now_ms)
    ready_for_test_issues = _filter_active(ready_for_test_issues, max_idle_days, now_ms)
    on_testing_issues = _filter_active(on_testing_issues, max_idle_days, now_ms)
    ready_for_release_issues = _filter_active(ready_for_release_issues, max_idle_days, now_ms)

    def _rank(items: list[dict]) -> list[tuple[float, dict, dict]]:
        scored = [(*compute_pulse_score(i, now_ms), i) for i in items]
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    triaged_ranked = _rank(triaged_filtered)
    re_entry_ranked = _rank(re_entry_filtered)
    incoming_ranked = _rank(incoming_filtered)

    pipeline_counts = {
        "in_progress": len(in_progress_issues),
        "for_review": len(for_review_issues),
        "ready_for_test": len(ready_for_test_issues),
        "on_testing": len(on_testing_issues),
        "ready_for_release": len(ready_for_release_issues),
    }
    metrics = {
        "closed": closed_count, "released": released_count,
        "incoming": incoming_count, "reopened": reopened_count,
    }
    insights = compute_insights(
        metrics, pipeline_counts, triaged_filtered + re_entry_filtered,
        now_ms, lookback_days,
    )
    team_balanced, team_pool = _build_team_balanced(
        re_entry_ranked, triaged_ranked, limit, now_ms,
    )
    return {
        "board": board_display,
        "lookback_days": lookback_days,
        "horizon_days": horizon_days,
        "metrics": metrics,
        "pipeline_counts": pipeline_counts,
        "has_released_states": bool(released_states),
        "triaged": [_issue_to_dict(i, t, b, now_ms) for t, b, i in triaged_ranked],
        "re_entry": [_issue_to_dict(i, t, b, now_ms) for t, b, i in re_entry_ranked],
        "incoming": [_issue_to_dict(i, t, b, now_ms) for t, b, i in incoming_ranked],
        "team_balanced": team_balanced,
        "team_pool": team_pool,
        "insights": insights,
        "unknown_columns": unknown_columns,
    }


def _aggregate_payloads(payloads: list[dict], lookback_days: int, horizon_days: int) -> dict:
    """Sum metrics + pipeline counts across boards; collect per-board flag totals."""
    agg_metrics = {"closed": 0, "released": 0, "incoming": 0, "reopened": 0}
    agg_pipeline = {
        "in_progress": 0, "for_review": 0,
        "ready_for_test": 0, "on_testing": 0, "ready_for_release": 0,
    }
    boards_with_flags = 0
    total_flags = 0
    boards_with_released = 0
    for p in payloads:
        for k in agg_metrics:
            agg_metrics[k] += p["metrics"].get(k, 0)
        for k in agg_pipeline:
            agg_pipeline[k] += p["pipeline_counts"].get(k, 0)
        if p.get("insights"):
            boards_with_flags += 1
            total_flags += len(p["insights"])
        if p.get("has_released_states"):
            boards_with_released += 1
    return {
        "lookback_days": lookback_days,
        "horizon_days": horizon_days,
        "board_count": len(payloads),
        "metrics": agg_metrics,
        "pipeline_counts": agg_pipeline,
        "boards_with_flags": boards_with_flags,
        "total_flags": total_flags,
        "has_any_released_state": boards_with_released > 0,
    }


def _render_multi_markdown(aggregate: dict, payloads: list[dict], limit: int) -> str:
    """Compact org-wide markdown: header sums + per-board sections (truncated)."""
    lb = aggregate["lookback_days"]
    hz = aggregate["horizon_days"]
    m = aggregate["metrics"]
    pc = aggregate["pipeline_counts"]
    n = aggregate["board_count"]

    lines: list[str] = []
    lines.append(f"# Org pulse — {n} boards  (←{lb}d  |  {hz}d→)")
    lines.append("")
    lines.append("## At a glance (combined)")
    lines.append(f"- **Throughput:**    {m['closed']} closed / {lb}d")
    if aggregate["has_any_released_state"]:
        lines.append(f"- **Shipped:**       {m['released']} released / {lb}d")
    lines.append(
        f"- **Pipeline now:**  {pc['in_progress']} in progress · "
        f"{pc['for_review']} in review · "
        f"{pc['ready_for_test'] + pc['on_testing']} on test · "
        f"{pc['ready_for_release']} ready to ship"
    )
    lines.append(f"- **Incoming:**      {m['incoming']} new in {lb}d")
    lines.append(f"- **Reopened:**      {m['reopened']} in {lb}d")
    lines.append(f"- **Flags:**         {aggregate['total_flags']} across {aggregate['boards_with_flags']}/{n} boards")
    lines.append("")

    for p in payloads:
        board = p["board"]
        bm = p["metrics"]
        bpc = p["pipeline_counts"]
        lines.append(f"## {board}")
        lines.append(
            f"- closed {bm['closed']} · incoming {bm['incoming']} · "
            f"reopened {bm['reopened']} · "
            f"WIP {bpc['in_progress']} · review {bpc['for_review']} · "
            f"test {bpc['ready_for_test'] + bpc['on_testing']}"
        )
        if p.get("insights"):
            for f in p["insights"]:
                lines.append(f"  - {f}")
        # Top forward items (truncated per board)
        top_n = max(1, min(limit, 3))
        if p.get("re_entry"):
            lines.append(f"  - **Re-entry top {min(top_n, len(p['re_entry']))}:**")
            for it in p["re_entry"][:top_n]:
                lines.append(f"    - {it['id']} — {it['summary'][:60]}")
        if p.get("triaged"):
            lines.append(f"  - **Ready to pull top {min(top_n, len(p['triaged']))}:**")
            for it in p["triaged"][:top_n]:
                lines.append(f"    - {it['id']} — {it['summary'][:60]}")
        lines.append("")

    return compact_lines(lines)
