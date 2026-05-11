"""Cross-department journey tracking — bottlenecks, dept load, transit times."""
import asyncio
from datetime import datetime, timezone

from yt_mcp.resolver import InstanceResolver
from yt_mcp.formatters import compact_lines

# Department auto-detection from project shortname.
# Generic patterns — no company names, just technical role conventions.
_DEPT_PATTERNS = {
    "Backend":   ("bac", "back", "be", "api", "srv", "server"),
    "Frontend":  ("fro", "front", "fe", "web", "ui"),
    "DevOps":    ("do", "ops", "devops", "infra", "deploy", "sre"),
    "QA":        ("qa", "test", "qc", "qual"),
    "Mobile":    ("mob", "app", "andr", "android", "ios"),
    "Desktop":   ("dp", "desk", "win", "mac"),
    "Marketing": ("mar", "mkt", "marketing"),
    "Product":   ("man", "pm", "prod", "mgmt"),
    "Design":    ("des", "design", "ux"),
    "Support":   ("sup", "help", "cs"),
    "Data":      ("data", "ds", "ml"),
    "Security":  ("sec", "infosec"),
}

# State-based dept hints — when state implies handoff to a different team
# (overrides project-based dept while issue is in this state).
_STATE_DEPT_HINTS = {
    "for review":        "Review",
    "code review":       "Review",
    "in review":         "Review",
    "dev qa":            "QA",
    "staging qa":        "QA",
    "prod qa":           "QA",
    "on testing":        "QA",
    "ready for stage":   "DevOps",
    "ready to prod":     "DevOps",
    "ready for release": "DevOps",
    "blocked":           "Blocked",
}

_HANDOFF_FIELDS = frozenset({"state", "assignee", "project"})


def _detect_dept(project_shortname: str) -> str:
    """Map project shortname to department via prefix match."""
    if not project_shortname:
        return "Unknown"
    p = project_shortname.lower()
    # Exact match first
    for dept, patterns in _DEPT_PATTERNS.items():
        if p in patterns:
            return dept
    # Longest-prefix match
    best, best_len = None, 0
    for dept, patterns in _DEPT_PATTERNS.items():
        for pat in patterns:
            if p.startswith(pat) and len(pat) > best_len:
                best, best_len = dept, len(pat)
    return best or project_shortname


def _state_dept(state_name: str) -> str | None:
    """Return semantic dept for a state name, or None if no hint."""
    return _STATE_DEPT_HINTS.get((state_name or "").lower())


def _build_journey(
    issue: dict, activities: list[dict], now_ms: int,
) -> list[dict]:
    """Build chronological list of dept-changing events for one issue."""
    project = issue.get("project", {}).get("shortName", "?")
    base_dept = _detect_dept(project)
    initial_state = (issue.get("state") or {}).get("name", "")
    # Initial dept = state hint if present, else project-based
    initial_dept = _state_dept(initial_state) or base_dept

    events: list[dict] = [{
        "ts": issue.get("created", now_ms),
        "dept": initial_dept,
        "state": initial_state or "Created",
    }]

    # Sort activities chronologically (oldest first)
    sorted_acts = sorted(activities, key=lambda a: a.get("timestamp", 0))

    current_state = initial_state
    current_project = project

    for act in sorted_acts:
        field = (act.get("field") or {}).get("name", "")
        if field not in _HANDOFF_FIELDS:
            continue
        added = act.get("added") or []
        ts = act.get("timestamp", 0)

        if field == "state":
            new_state = added[0].get("name", "") if added else ""
            current_state = new_state
            new_dept = _state_dept(new_state) or _detect_dept(current_project)
        elif field == "project":
            new_proj = added[0].get("shortName", "") if added else ""
            if new_proj:
                current_project = new_proj
            new_dept = _state_dept(current_state) or _detect_dept(current_project)
        elif field == "assignee":
            # Assignee changes alone don't shift dept (we use state/project).
            # But carry forward to surface "who currently holds it".
            continue
        else:
            continue

        if new_dept != events[-1]["dept"]:
            events.append({"ts": ts, "dept": new_dept, "state": current_state})

    # Compute durations
    for i, ev in enumerate(events):
        next_ts = events[i + 1]["ts"] if i + 1 < len(events) else now_ms
        ev["duration_days"] = max(0, (next_ts - ev["ts"]) / 86400000)
    return events


async def _fetch_activities(client, iid: str) -> list[dict]:
    """Fetch state+project change activities for one issue."""
    try:
        return await client.get(
            f"/api/issues/{iid}/activities",
            params={
                "fields": "id,timestamp,field(name),added(name,shortName),removed(name,shortName)",
                "categories": "CustomFieldCategory,IssueProjectCategory",
                "$top": "200",
            },
        )
    except (ValueError, KeyError):
        return []


def _gather_subtask_ids(issue: dict) -> list[str]:
    """Extract subtask issue IDs from issue links."""
    ids: list[str] = []
    for link in issue.get("links", []):
        ltype = (link.get("linkType") or {}).get("name", "").lower()
        if link.get("direction") == "OUTWARD" and "subtask" in ltype:
            for sub in link.get("issues", []):
                if sub.get("idReadable"):
                    ids.append(sub["idReadable"])
    return ids


def register(mcp, resolver: InstanceResolver):

    @mcp.tool()
    async def track_cross_dept_journey(
        query: str,
        stale_days: int = 5,
        avg_window_days: int = 14,
        follow_subtasks: bool = True,
        max_issues: int = 50,
        instance: str = "",
    ) -> str:
        """Track cross-department handoffs: bottlenecks, dept load, avg transit times.

        Builds a chronological journey of dept changes (state + project transitions)
        for each issue. Departments are auto-detected from project shortnames using
        generic role-based patterns (Backend/Frontend/DevOps/QA/Mobile/etc).

        Args:
            query: YouTrack query selecting issues (e.g. 'project: MAN #Unresolved')
            stale_days: Flag current station if held >N days (default: 5)
            avg_window_days: Rolling window for transit time averages (default: 14)
            follow_subtasks: Include subtask journeys in parent's chain (default: True)
            max_issues: Cap on issues fetched (default: 50)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        avg_cutoff_ms = now_ms - avg_window_days * 86400000

        issue_fields = (
            "idReadable,summary,created,resolved,project(shortName),"
            "state(name),assignee(name),"
            "links(direction,linkType(name),issues(idReadable,project(shortName),"
            "summary,created,resolved,state(name),assignee(name)))"
        )

        parents = await client.get(
            "/api/issues",
            params={"query": query, "fields": issue_fields, "$top": str(max_issues)},
        )
        if not parents:
            return f"No issues match query: `{query}`"

        # Collect all issues to fetch activities for (parents + optional subtasks)
        all_issues: dict[str, dict] = {}
        parent_to_subs: dict[str, list[str]] = {}
        for p in parents:
            pid = p.get("idReadable", "")
            if not pid:
                continue
            all_issues[pid] = p
            if follow_subtasks:
                sub_ids = _gather_subtask_ids(p)
                parent_to_subs[pid] = sub_ids
                # Include subtask issue dicts (already in links)
                for link in p.get("links", []):
                    if (
                        link.get("direction") == "OUTWARD"
                        and "subtask" in (link.get("linkType") or {}).get("name", "").lower()
                    ):
                        for sub in link.get("issues", []):
                            sid = sub.get("idReadable", "")
                            if sid and sid not in all_issues:
                                all_issues[sid] = sub

        # Fetch activities for all in parallel
        ids = list(all_issues.keys())
        all_activities = await asyncio.gather(*(_fetch_activities(client, i) for i in ids))
        activities_by_id = dict(zip(ids, all_activities))

        # Build per-issue journeys
        journeys: dict[str, list[dict]] = {}
        for iid, issue in all_issues.items():
            journeys[iid] = _build_journey(issue, activities_by_id[iid], now_ms)

        # For parents with subtasks, merge sub-journeys chronologically into parent
        # (only when follow_subtasks=True and subs exist)
        merged: dict[str, list[dict]] = {}
        for pid, p in all_issues.items():
            if pid not in parent_to_subs or not parent_to_subs.get(pid):
                merged[pid] = journeys[pid]
                continue
            chain = list(journeys[pid])
            for sid in parent_to_subs[pid]:
                if sid in journeys:
                    for ev in journeys[sid]:
                        # Tag event with subtask source for the bottleneck output
                        chain.append({**ev, "source": sid})
            chain.sort(key=lambda e: e["ts"])
            merged[pid] = chain

        # --- Aggregations ---

        # 1. Bottlenecks: parents whose CURRENT station > stale_days
        #    "Current station" = last event's dept; duration is days since that event
        bottlenecks: list[tuple[float, str, dict, list[dict]]] = []
        for pid, p in {k: v for k, v in all_issues.items() if k in [x.get("idReadable") for x in parents]}.items():
            chain = merged.get(pid) or journeys.get(pid) or []
            if not chain:
                continue
            current = chain[-1]
            current_duration = current["duration_days"]
            if p.get("resolved"):
                continue  # closed; not a current bottleneck
            if current_duration >= stale_days:
                bottlenecks.append((current_duration, pid, p, chain))
        bottlenecks.sort(key=lambda x: -x[0])

        # 2. Department load (currently holding) — count parents only
        dept_load: dict[str, list[tuple[float, str]]] = {}
        for pid in [x.get("idReadable", "") for x in parents]:
            if not pid:
                continue
            p = all_issues.get(pid, {})
            if p.get("resolved"):
                continue
            chain = merged.get(pid) or journeys.get(pid) or []
            if not chain:
                continue
            current = chain[-1]
            dept_load.setdefault(current["dept"], []).append(
                (current["duration_days"], pid)
            )

        # 3. Avg transit times — from chains (parent journeys) where any hop happened
        #    in the rolling window. Compute per-hop deltas.
        hop_durations: dict[tuple[str, str], list[float]] = {}
        for pid in [x.get("idReadable", "") for x in parents]:
            chain = merged.get(pid) or journeys.get(pid) or []
            for i in range(len(chain) - 1):
                ev = chain[i]
                nxt = chain[i + 1]
                if nxt["ts"] < avg_cutoff_ms:
                    continue
                hop = (ev["dept"], nxt["dept"])
                hop_durations.setdefault(hop, []).append(ev["duration_days"])

        # --- Format output ---

        lines = [
            f"## Cross-department journey",
            f"**Query:** `{query}` | **Issues:** {len(parents)} parents"
            + (f" + {sum(len(s) for s in parent_to_subs.values())} subtasks" if follow_subtasks else "")
            + f" | **Stale threshold:** {stale_days}d | **Avg window:** {avg_window_days}d",
            "",
        ]

        # Bottlenecks
        lines.append(f"### Bottlenecks (>{stale_days}d at current station) — {len(bottlenecks)}")
        if not bottlenecks:
            lines.append("_None — nothing stuck beyond threshold._")
        else:
            for duration, pid, p, chain in bottlenecks[:15]:
                summary = p.get("summary", "?")[:80]
                # Build hop trail
                trail_parts = []
                for i, ev in enumerate(chain):
                    label = f"{ev['dept']} ({ev['duration_days']:.1f}d)"
                    if i == len(chain) - 1:
                        label = f"**{ev['dept']} ({ev['duration_days']:.1f}d)**"
                    src = ev.get("source")
                    if src:
                        label += f" [{src}]"
                    trail_parts.append(label)
                trail = " → ".join(trail_parts)
                total_days = sum(e["duration_days"] for e in chain)
                lines.append(
                    f"- **{pid}** {summary} | total {total_days:.0f}d, "
                    f"{len(chain)} hops"
                )
                lines.append(f"  {trail}")
            if len(bottlenecks) > 15:
                lines.append(f"_...and {len(bottlenecks) - 15} more_")
        lines.append("")

        # Department load
        lines.append("### Department load now")
        if not dept_load:
            lines.append("_No active issues._")
        else:
            lines.append("| Dept | Holding | Oldest |")
            lines.append("|---|---|---|")
            for dept in sorted(dept_load, key=lambda d: -len(dept_load[d])):
                items = dept_load[dept]
                oldest = max(d for d, _ in items)
                lines.append(f"| {dept} | {len(items)} | {oldest:.0f}d |")
        lines.append("")

        # Avg transit
        lines.append(f"### Avg transit (rolling {avg_window_days}d)")
        if not hop_durations:
            lines.append(f"_No hops occurred in the last {avg_window_days}d window._")
        else:
            lines.append("| Hop | N | Avg | p90 | Slowest |")
            lines.append("|---|---|---|---|---|")
            sorted_hops = sorted(
                hop_durations.items(),
                key=lambda kv: -(sum(kv[1]) / len(kv[1])),
            )
            for (src, dst), durs in sorted_hops:
                durs_sorted = sorted(durs)
                avg = sum(durs) / len(durs)
                p90_idx = max(0, int(len(durs_sorted) * 0.9) - 1)
                p90 = durs_sorted[p90_idx]
                slowest = durs_sorted[-1]
                lines.append(
                    f"| {src} → {dst} | {len(durs)} | {avg:.1f}d | {p90:.1f}d | {slowest:.1f}d |"
                )

        return compact_lines(lines)
