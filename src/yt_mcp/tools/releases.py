"""Release calendar assembled from release/RC-shaped tickets.

Convention-driven: teams name release tickets "Release X.Y.Z", "release-X.Y.Z",
"Release_lite", "[Tag] Release X.Y.Z" or "RC-X.Y.Z". Tickets that merely
*mention* a release ("Validate payments before release", "…after release",
CI-failure noise) are filtered out by shape.

Query-syntax notes (validated live, ADR-039): `summary: <word>` works; a
`resolved date:` range must come FIRST in the query — the reversed clause
order is rejected by the parser.
"""

import re
import statistics
from datetime import datetime, timedelta, timezone

from mcp.types import ToolAnnotations
from yt_mcp.formatters import compact_lines
from yt_mcp.resolver import InstanceResolver

_RELEASE_RE = re.compile(r"^(\[[^\]]*\]\s*)?(rc|release)[\s_.\-]*v?(\d|lite)", re.IGNORECASE)
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")

# Lower rank = closer to the store/prod door.
_STATE_RANK = {
    "store review": 0, "prod verification": 0, "ready for store": 0,
    "ready for release": 0, "on testing": 1, "ready for test": 1,
    "in progress": 2,
}

_FIELDS = "idReadable,summary,resolved,updated,customFields(name,value(name))"


def _cf(issue: dict, name: str):
    for c in issue.get("customFields", []) or []:
        if c.get("name") == name:
            v = c.get("value")
            if isinstance(v, dict):
                return v.get("name")
            if isinstance(v, list):
                return ", ".join(x.get("name", "") for x in v if isinstance(x, dict))
            return v
    return None


def _deadline_ms(issue: dict):
    for c in issue.get("customFields", []) or []:
        if c.get("name") == "Deadline ☠️" and isinstance(c.get("value"), (int, float)):
            return c["value"]
    return None


def _day(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _is_release_ticket(summary: str) -> bool:
    return bool(_RELEASE_RE.match(summary or ""))


def register(mcp, resolver: InstanceResolver):
    """Register release calendar tools."""

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True))
    async def get_release_calendar(
        projects: str = "",
        lookback_days: int = 30,
        instance: str = "",
    ) -> str:
        """Release calendar: in-flight releases, recent ships, cadence ETAs.

        Scans release/RC-titled tickets across projects. Per project:
        in-flight releases ordered closest-to-the-door first, ships within
        the lookback, the median gap between ships (cadence), and — for
        projects whose releases carry no deadlines — a cadence-based ETA
        for the next ship. Flags parked store queues (3+ releases sitting
        in ready/review states).

        Args:
            projects: Comma-separated project keys to include (all if blank)
            lookback_days: Shipped-release window for cadence (default: 30)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        wanted = {p.strip().upper() for p in projects.split(",") if p.strip()}

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days)
        window = f"{start.strftime('%Y-%m-%d')} .. {end.strftime('%Y-%m-%d')}"

        async def _search(query: str) -> list:
            return await client.get(
                "/api/issues",
                params={"query": query, "fields": _FIELDS, "$top": "60"},
            ) or []

        raw: list = []
        for term in ("Release", "RC"):
            raw += await _search(f"summary: {term} #Unresolved sort by: updated desc")
            # resolved-date range MUST precede other clauses (parser quirk).
            raw += await _search(f"resolved date: {window} summary: {term}")

        seen: set = set()
        tickets: list[dict] = []
        for i in raw:
            iid = i.get("idReadable", "")
            if iid in seen or not _is_release_ticket(i.get("summary", "")):
                continue
            seen.add(iid)
            proj = iid.rsplit("-", 1)[0] if "-" in iid else "?"
            if wanted and proj.upper() not in wanted:
                continue
            tickets.append({
                "id": iid, "project": proj,
                "summary": i.get("summary") or "",
                "state": _cf(i, "State") or "?",
                "assignee": _cf(i, "Assignee") or "?",
                "resolved": i.get("resolved"),
                "deadline": _deadline_ms(i),
            })

        if not tickets:
            scope = f" in projects {', '.join(sorted(wanted))}" if wanted else ""
            return (
                f"No release/RC-titled tickets found{scope} "
                f"(searched open + resolved {window}). Release tickets are "
                "matched by summary shape: 'Release X.Y.Z' / 'RC-X.Y.Z'."
            )

        by_proj: dict[str, dict] = {}
        for t in tickets:
            g = by_proj.setdefault(t["project"], {"open": [], "shipped": []})
            g["shipped" if t["resolved"] else "open"].append(t)

        lines = [f"# Release calendar — {len(tickets)} tickets, lookback {lookback_days}d", ""]
        flagged: list[str] = []

        for proj in sorted(by_proj, key=lambda p: -len(by_proj[p]["open"])):
            g = by_proj[proj]
            g["open"].sort(key=lambda t: _STATE_RANK.get(t["state"].lower(), 3))
            g["shipped"].sort(key=lambda t: -(t["resolved"] or 0))

            ship_days = [t["resolved"] for t in g["shipped"]]
            cadence = None
            if len(ship_days) >= 2:
                gaps = [
                    (a - b) / 86_400_000
                    for a, b in zip(ship_days, ship_days[1:])
                ]
                cadence = statistics.median(gaps)

            lines.append(f"## {proj} — {len(g['open'])} in flight, {len(g['shipped'])} shipped")
            for t in g["open"]:
                dl = f" · deadline {_day(t['deadline'])}" if t["deadline"] else ""
                lines.append(
                    f"- {t['id']} [{t['state']}] {t['summary'][:60]}"
                    f" → {t['assignee'].split(',')[0]}{dl}"
                )
            if g["shipped"]:
                shipped_str = ", ".join(
                    f"{_VERSION_RE.search(t['summary']).group(0) if _VERSION_RE.search(t['summary']) else t['id']}"
                    f" ({_day(t['resolved'])[5:]})"
                    for t in g["shipped"][:6]
                )
                lines.append(f"  shipped: {shipped_str}")
            if cadence:
                eta = ""
                if g["open"] and not any(t["deadline"] for t in g["open"]):
                    next_day = datetime.fromtimestamp(
                        ship_days[0] / 1000, tz=timezone.utc
                    ) + timedelta(days=cadence)
                    eta = f"; next ETA ~{next_day.strftime('%Y-%m-%d')} (no deadlines set)"
                lines.append(f"  cadence: ~{cadence:.0f}d between ships{eta}")

            parked = [t for t in g["open"] if _STATE_RANK.get(t["state"].lower(), 3) == 0]
            if len(parked) >= 3:
                flagged.append(
                    f"{proj}: {len(parked)} releases parked in ready/review states "
                    f"({', '.join(t['id'] for t in parked[:5])}) — submission bottleneck or dead tickets"
                )
            lines.append("")

        if flagged:
            lines.append("## ⚠️ Flags")
            lines += [f"- {f}" for f in flagged]

        return compact_lines(lines)
