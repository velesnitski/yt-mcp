"""Stuck-handoff detection — tasks that crossed a team boundary and stalled.

Distinct from `get_handoff_snapshot` (which sorts by `updated` — polluted by
comments/tag edits) and `track_cross_dept_journey` (forensic per-issue
deep-dive). This tool answers the specific operational question: "which
tasks were handed off N days ago and nobody on the receiving team has
touched them since?"

The signal is state-change-only, not generic `updated`. A task that
crossed dev→QA 14 days ago and got 6 comments but no state change is
still stuck — the handoff stalled regardless of conversation noise.
"""

import asyncio
import json
import re
from datetime import datetime, timezone
from statistics import median
from typing import Any

from yt_mcp.formatters import (
    _resolve_state, _resolve_assignee, _resolve_assignee_login,
    _get_custom_field, compact_lines,
)
from yt_mcp.resolver import InstanceResolver
from yt_mcp.tools.deadlines.fetcher import fetch_activities_only_bounded
from yt_mcp.tools.pulse import (
    _resolve_board_for_pulse, _classify_board_columns, _extract_deadline_ms,
)


_DAY_MS = 86400 * 1000


# Finer-grained role classification than pulse — for handoff detection we
# need to distinguish dev (In Progress, For review) from qa (Ready for test,
# On testing) from release (Ready for release). pulse lumps these under
# "in_progress" because it cares about pipeline flow, not team boundaries.
_HANDOFF_ROLE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("rework",   re.compile(r"(?i)(for\s+revision|reopen|rejected|needs?\s+rework|на\s+доработку)")),
    ("paused",   re.compile(r"(?i)(blocked|on\s+hold|paus[ed]|waiting|wait)")),
    ("release",  re.compile(r"(?i)(ready\s+for\s+release|ready\s+for\s+stage|ready\s+to\s+prod|released)")),
    ("qa",       re.compile(r"(?i)(ready\s+for\s+test|on\s+testing|in\s+testing|dev\s+qa|staging\s+qa|prod\s+qa|qa$)")),
    ("dev",      re.compile(r"(?i)(in\s+progress|for\s+review|in\s+review|code\s+review)")),
    ("done",     re.compile(r"(?i)(closed|done|resolved|fixed|completed|verified)")),
    ("triage",   re.compile(r"(?i)(to\s*do|todo|backlog|ready\s+for\s+dev|selected\s+for\s+dev|scheduled)")),
    ("intake",   re.compile(r"(?i)(submitted|new|open|reported|received)")),
)


def classify_handoff_role(state: str) -> str:
    """Map a state name to its team-ownership role. Unknown → 'unknown' so
    transitions involving it can be flagged as ambiguous rather than silently
    counted as cross-team."""
    n = (state or "").strip()
    if not n:
        return "unknown"
    for role, pat in _HANDOFF_ROLE_PATTERNS:
        if pat.search(n):
            return role
    return "unknown"


# Cross-team transitions: roles that swap ownership when crossed.
# Within-role transitions (dev→dev, qa→qa) are NOT handoffs even when state
# changes — they're workflow progress within a team's column set.
# Transitions involving 'done' or 'paused' aren't operational handoffs we
# can act on.
_CROSS_TEAM_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    # Classic forward handoffs
    ("dev", "qa"),           # dev finished, QA's turn — most common stuck case
    ("qa", "release"),       # QA passed, release/devops's turn
    ("dev", "release"),      # skip-QA path (rare)
    ("triage", "dev"),       # PM kicks off, dev's turn
    ("intake", "dev"),       # PM picks up + routes (skipping explicit triage)
    ("intake", "triage"),    # PM picks up for triage (counts but lower priority)
    # Rework loops
    ("qa", "rework"),        # QA bounced back to dev
    ("qa", "dev"),           # QA reassigned to dev for fixes
    ("release", "dev"),      # release failed, back to dev
    ("rework", "dev"),       # dev resumed rework
    # Cross-team unknown — caller should investigate
    # (unknown excluded from this list — surfaced separately)
})


def _is_cross_team_transition(from_role: str, to_role: str) -> bool:
    """True iff (from→to) is a meaningful team-ownership swap."""
    if from_role == to_role:
        return False
    return (from_role, to_role) in _CROSS_TEAM_TRANSITIONS


# Set of role pairs we treat as "receiving a handoff" — used to short-list
# issues to investigate. Anything currently in these roles is a candidate
# for "was handed off; did it stall?".
_HANDOFF_RECEIVING_ROLES = frozenset({"qa", "release", "rework", "dev"})


def _latest_state_change(activities: list[dict]) -> dict | None:
    """Find the most recent State-field activity. Returns the normalized event
    or None when there's no state change in the activity log."""
    state_acts = [
        a for a in activities
        if (a.get("field") or {}).get("name", "").lower() == "state"
    ]
    if not state_acts:
        return None
    state_acts.sort(key=lambda a: a.get("timestamp", 0), reverse=True)
    latest = state_acts[0]
    added = latest.get("added") or []
    removed = latest.get("removed") or []
    return {
        "ts": latest.get("timestamp", 0),
        "from_state": (removed[0].get("name", "") if removed else ""),
        "to_state": (added[0].get("name", "") if added else ""),
        "author_login": (latest.get("author") or {}).get("login", ""),
        "author_name": (latest.get("author") or {}).get("name", ""),
    }


def _format_iso_date(ms: int) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _issue_to_stuck_dict(
    issue: dict, change: dict, from_role: str, to_role: str,
    days_stuck: float, now_ms: int,
) -> dict:
    """JSON-friendly summary for one stuck issue."""
    dl_ms = _extract_deadline_ms(issue)
    dl_days = None
    if dl_ms:
        dl_days = int((dl_ms - now_ms) / _DAY_MS)
    return {
        "id": issue.get("idReadable", "?"),
        "summary": issue.get("summary", "") or "",
        "current_state": _resolve_state(issue),
        "current_role": to_role,
        "previous_state": change["from_state"],
        "previous_role": from_role,
        "transition": f"{from_role}→{to_role}",
        "days_stuck": round(days_stuck, 1),
        "transitioned_at": _format_iso_date(change["ts"]),
        "last_mover": change["author_login"] or change["author_name"] or None,
        "current_assignee": _resolve_assignee(issue),
        "current_assignee_login": _resolve_assignee_login(issue),
        "severity": _get_custom_field(issue, "Severity"),
        "type": _get_custom_field(issue, "Type"),
        "priority": _get_custom_field(issue, "Priority"),
        "deadline_days": dl_days,
    }


# --- Field selectors ----------------------------------------------------

HANDOFF_ISSUE_FIELDS = (
    "idReadable,summary,state(name),priority(name),"
    "assignee(login,name),"
    "customFields(name,value(login,presentation,name,text))"
)


# --- Payload builder + renderer ---------------------------------------

async def _build_stuck_payload(
    client, board: dict, stuck_days: int, lookback_days: int, limit: int, now_ms: int,
) -> dict | str:
    """Resolve the board's receiving states, fetch in-flight issues, walk
    activities, return a JSON-friendly payload of stuck items."""
    board_display = board.get("name", "?")
    projects = [p.get("shortName", "") for p in board.get("projects", []) if p.get("shortName")]
    if not projects:
        return f"Board '{board_display}' has no projects bound."

    state_to_role, _unknown_cols = _classify_board_columns(board)

    # Build the set of states whose role is a handoff-receiving role.
    # Re-classify using OUR finer-grained role patterns (pulse's classifier
    # lumps dev/qa/release together as "in_progress").
    receiving_states: list[str] = []
    receiving_role_by_state: dict[str, str] = {}
    for state in state_to_role.keys():
        role = classify_handoff_role(state)
        if role in _HANDOFF_RECEIVING_ROLES:
            receiving_states.append(state)
            receiving_role_by_state[state] = role

    if not receiving_states:
        return f"Board '{board_display}' has no handoff-receiving states (post-dev/QA/release columns)."

    if len(projects) == 1:
        project_clause = f"project: {projects[0]}"
    else:
        project_clause = "(" + " or ".join(f"project: {p}" for p in projects) + ")"

    state_clause = "(" + " or ".join(f"State: {{{s}}}" for s in receiving_states) + ")"
    query = f"{project_clause} {state_clause} #Unresolved"

    issues = await client.get(
        "/api/issues",
        params={"query": query, "fields": HANDOFF_ISSUE_FIELDS, "$top": "500"},
    )
    issues = issues or []

    if not issues:
        return {
            "board": board_display,
            "stuck_days": stuck_days,
            "lookback_days": lookback_days,
            "total_stuck": 0,
            "stuck": [],
            "by_transition": {},
            "by_receiving_assignee": {},
            "median_days_stuck": 0,
            "worst": None,
            "candidates_examined": 0,
        }

    issue_ids = [i.get("idReadable", "") for i in issues if i.get("idReadable")]
    # Bounded fetch — already proven against HTTP/2 stream exhaustion.
    activities_per = await fetch_activities_only_bounded(client, issue_ids)

    cutoff_ms = now_ms - stuck_days * _DAY_MS
    stuck_items: list[dict] = []

    for issue, activities in zip(issues, activities_per):
        change = _latest_state_change(activities)
        if not change or not change["ts"]:
            # No state change in the activity window (or no activity log at all).
            # We can't confirm a recent handoff — skip rather than guess.
            continue
        if change["ts"] > cutoff_ms:
            # The last state change was more recent than stuck_days — not stuck.
            continue
        from_role = classify_handoff_role(change["from_state"])
        to_role = classify_handoff_role(change["to_state"])
        if not _is_cross_team_transition(from_role, to_role):
            continue
        days_stuck = (now_ms - change["ts"]) / _DAY_MS
        stuck_items.append(
            _issue_to_stuck_dict(issue, change, from_role, to_role, days_stuck, now_ms)
        )

    # Sort: worst stall first, severity tiebreak
    _sev_weight = {"blocker": 5, "critical": 4, "major": 3, "minor": 1, "trivial": 0}

    def _sort_key(it):
        sev = _sev_weight.get((it.get("severity") or "").lower(), 0)
        return (-it["days_stuck"], -sev)

    stuck_items.sort(key=_sort_key)

    # Aggregates
    by_transition: dict[str, int] = {}
    by_receiver: dict[str, int] = {}
    for it in stuck_items:
        by_transition[it["transition"]] = by_transition.get(it["transition"], 0) + 1
        rcv = it.get("current_assignee") or "Unassigned"
        by_receiver[rcv] = by_receiver.get(rcv, 0) + 1

    stall_days = [it["days_stuck"] for it in stuck_items]
    worst = stuck_items[0] if stuck_items else None

    return {
        "board": board_display,
        "stuck_days": stuck_days,
        "lookback_days": lookback_days,
        "total_stuck": len(stuck_items),
        "candidates_examined": len(issues),
        "stuck": stuck_items[:limit] if limit else stuck_items,
        "stuck_all_count": len(stuck_items),
        "by_transition": dict(sorted(by_transition.items(), key=lambda x: -x[1])),
        "by_receiving_assignee": dict(sorted(by_receiver.items(), key=lambda x: -x[1])),
        "median_days_stuck": round(median(stall_days), 1) if stall_days else 0,
        "worst": {"id": worst["id"], "days_stuck": worst["days_stuck"]} if worst else None,
    }


def _format_stuck_line(it: dict) -> str:
    extras = [it.get("severity") or "-", it.get("type") or "-"]
    dl = it.get("deadline_days")
    if dl is not None:
        marker = f"deadline in {dl}d ⚠️" if 0 <= dl <= 7 else (f"deadline in {dl}d" if dl >= 0 else f"overdue {-dl}d")
        extras.append(marker)
    mover = it.get("last_mover") or "?"
    rcv = it.get("current_assignee") or "Unassigned"
    extras_str = ", ".join(extras)
    return (
        f"- **{it['id']}** · {it['days_stuck']:.0f}d stuck · "
        f"`{mover}` → `{it['previous_state']}` → `{it['current_state']}` "
        f"({it['transitioned_at']}) · now → **{rcv}** [{extras_str}]\n"
        f"  {it['summary'][:90]}"
    )


def _render_stuck_markdown(payload: dict, limit: int) -> str:
    """Markdown report grouped by transition type."""
    lines: list[str] = []
    board = payload["board"]
    stuck_days = payload["stuck_days"]
    total = payload["total_stuck"]
    candidates = payload.get("candidates_examined", 0)
    by_transition = payload["by_transition"]
    by_receiver = payload["by_receiving_assignee"]
    median_d = payload["median_days_stuck"]
    worst = payload["worst"]
    stuck = payload["stuck"]

    lines.append(f"# Stuck handoffs on {board} — {total} items ≥{stuck_days}d after team transition")
    lines.append(f"_({candidates} candidates examined; {total} confirmed stuck via state-change history)_")
    lines.append("")

    if total == 0:
        lines.append("_No stuck handoffs — every receiving-state task moved within the threshold._")
        return compact_lines(lines)

    lines.append("## At a glance")
    if worst:
        lines.append(f"- **Worst stuck:** {worst['id']} ({worst['days_stuck']:.0f}d)")
    lines.append(f"- **Median stall:** {median_d:.0f}d")
    if by_transition:
        bt_str = " · ".join(f"{k} ({v})" for k, v in by_transition.items())
        lines.append(f"- **By transition:** {bt_str}")
    if by_receiver:
        # Top 3 receivers
        top_receivers = list(by_receiver.items())[:3]
        rcv_str = ", ".join(f"{k} ({v})" for k, v in top_receivers)
        lines.append(f"- **Top receivers:** {rcv_str}")
    cliff_count = sum(1 for it in stuck if isinstance(it.get("deadline_days"), int) and 0 <= it["deadline_days"] <= 7)
    if cliff_count:
        lines.append(f"- ⏰ **{cliff_count}** also flagged deadline cliff (≤7d)")
    lines.append("")

    # Group by transition
    by_t: dict[str, list[dict]] = {}
    for it in stuck:
        by_t.setdefault(it["transition"], []).append(it)

    # Preserve sort order from aggregate (most-common transition first)
    transitions_sorted = list(by_transition.keys())
    for t in transitions_sorted:
        items = by_t.get(t)
        if not items:
            continue
        # Friendlier label
        from_role, to_role = t.split("→", 1)
        label = _transition_label(from_role, to_role)
        lines.append(f"## {label} — {len(items)} stuck")
        for it in items[:limit]:
            lines.append(_format_stuck_line(it))
        lines.append("")

    return compact_lines(lines)


def _transition_label(from_role: str, to_role: str) -> str:
    """Human-friendly section heading per transition."""
    pretty = {
        ("dev", "qa"):       "Dev → QA stalls",
        ("dev", "release"):  "Dev → Release stalls (skipping QA)",
        ("qa", "release"):   "QA → Release stalls",
        ("qa", "rework"):    "QA → Rework stalls (bounced back)",
        ("qa", "dev"):       "QA → Dev (rework) stalls",
        ("release", "dev"):  "Release → Dev stalls (failed release)",
        ("rework", "dev"):   "Rework → Dev resumption stalls",
        ("triage", "dev"):   "Triage → Dev kickoff stalls",
        ("intake", "dev"):   "Intake → Dev stalls (PM routed but dev hasn't started)",
        ("intake", "triage"):"Intake → Triage stalls",
    }
    return pretty.get((from_role, to_role), f"{from_role} → {to_role} stalls")


# --- Tool entry ----------------------------------------------------------

def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def get_stuck_handoffs(
        board_name: str,
        stuck_days: int = 4,
        lookback_days: int = 30,
        limit: int = 10,
        format: str = "report",
        instance: str = "",
    ) -> str:
        """Tasks that crossed a team boundary and haven't moved since.

        Distinct from `get_handoff_snapshot` (sorts by `updated`, polluted by
        comments) and `track_cross_dept_journey` (forensic per-issue
        deep-dive). This walks state-change activities specifically, so an
        item in `Ready for test` with fresh comments but no state change in
        14 days IS surfaced as stuck.

        Algorithm:
          1. Find issues currently in handoff-receiving states (qa, release,
             rework, dev) on the board.
          2. For each, fetch state-change activities.
          3. Find the latest state change. If it happened ≥ stuck_days ago AND
             that transition crossed a team-ownership role boundary (dev→qa,
             qa→release, etc.) → STUCK.

        Output is grouped by transition pattern (Dev→QA stalls, QA→Release
        stalls, etc.) so you see which handoff most often gets dropped.

        Args:
            board_name: Board name (partial match), ID, or URL.
            stuck_days: Days without a state change to count as stuck (default 4).
            lookback_days: Velocity-window framing in the header (default 30).
            limit: Max items shown per transition section (default 10).
            format: "report" (markdown) or "json" (structured payload).
            instance: YouTrack instance (optional).
        """
        client = resolver.resolve(instance, board_name)
        board, err = await _resolve_board_for_pulse(client, board_name)
        if not board:
            return err
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        payload = await _build_stuck_payload(
            client, board, stuck_days, lookback_days, limit, now_ms,
        )
        if isinstance(payload, str):
            return payload
        if format == "json":
            return json.dumps(payload, indent=2, ensure_ascii=False)
        return _render_stuck_markdown(payload, limit)
