"""Deadline-control tooling: audit, scorecard, manager-mapping suggester.

Reads ~/.yt-mcp/managers.json (preferred) or ~/.yt-mcp/managers.suggested.json.
Schema:
    {
      "__default__": "fallback.user",
      "alice.user": {
        "primary": "bob.manager",
        "also_accept": ["carol.lead"],
        "manual_review": false
      }
    }

All three tools are read-only against YouTrack. `suggest_managers` writes a
local file at ~/.yt-mcp/managers.suggested.json that the operator reviews and
merges into managers.json by hand.
"""

import asyncio
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from yt_mcp.formatters import compact_lines
from yt_mcp.resolver import InstanceResolver


_CONFIG_DIR = Path(os.environ.get("YT_MCP_CONFIG_DIR", str(Path.home() / ".yt-mcp")))
_MANAGERS_FILE = _CONFIG_DIR / "managers.json"
_MANAGERS_SUGGESTED_FILE = _CONFIG_DIR / "managers.suggested.json"
_POLICY_FILE = _CONFIG_DIR / "policy.json"
_AUDIT_LOG = _CONFIG_DIR / "deadline-audit.log"


_DEADLINE_FIELD_PATTERNS = (
    re.compile(r"^(deadline|due\s*date|due|completion\s*date)$", re.IGNORECASE),
    re.compile(r"^(дедлайн|срок|до|дата\s*выполнения)$", re.IGNORECASE),
)

_APPROVAL_KEYWORDS = (
    "approve", "approved", "ok", "okay", "agreed", "extend", "extended",
    "confirm", "confirmed", "согласен", "одобрено", "ок",
)

_DEFAULT_STANDUP_PATTERNS = (
    r"(?i)devops\s+daily",
    r"(?i)\bdaily\b",
    r"(?i)\bstandup\b",
    r"(?i)\bдейли\b",
    r"(?i)\bстендап\b",
    r"(?i)решение\s+текущих\s+проблем",
)

_DONE_STATES = frozenset({
    "done", "closed", "resolved", "fixed", "completed", "released", "verified",
})

_APPROVAL_WINDOW_BEFORE_SEC = 14 * 86400
_APPROVAL_WINDOW_AFTER_SEC = 24 * 3600

_SUGGEST_WINDOWS = (30, 90, 180)
_PM_FANOUT_PERCENTILE = 0.90
_PM_MIN_FANOUT = 6
_QUARTER_RE = re.compile(r"^(\d{4})Q([1-4])$")


# ---------- config loading ----------

def _load_managers_config() -> dict[str, Any]:
    """Load managers.json or fall back to managers.suggested.json."""
    for path in (_MANAGERS_FILE, _MANAGERS_SUGGESTED_FILE):
        if path.exists():
            try:
                with path.open() as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data["__source_file__"] = str(path)
                    return data
            except (OSError, ValueError):
                continue
    return {"__source_file__": ""}


def _load_policy() -> dict[str, Any]:
    if not _POLICY_FILE.exists():
        return {}
    try:
        with _POLICY_FILE.open() as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _audit(operator: str, tool: str, scope: dict, result_size: int) -> None:
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "operator": operator,
            "tool": tool,
            "scope": scope,
            "result_size": result_size,
        }
        with _AUDIT_LOG.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _get_approvers(login: str, config: dict) -> tuple[set[str], bool]:
    """Return (set of valid approver logins, manual_review flag) for an assignee."""
    entry = config.get(login)
    if entry is None or not isinstance(entry, dict):
        default = config.get("__default__")
        return ({default} if isinstance(default, str) else set()), False
    approvers: set[str] = set()
    primary = entry.get("primary")
    if isinstance(primary, str) and primary:
        approvers.add(primary)
    for acc in entry.get("also_accept") or []:
        if isinstance(acc, str) and acc:
            approvers.add(acc)
    return approvers, bool(entry.get("manual_review"))


def _get_reports(manager_login: str, config: dict) -> list[str]:
    """Reverse lookup: assignees whose primary is the given manager."""
    out = []
    for login, entry in config.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("primary") == manager_login:
            out.append(login)
    return sorted(out)


# ---------- date / quarter helpers ----------

def _quarter_to_range(quarter: str) -> tuple[datetime, datetime]:
    m = _QUARTER_RE.match(quarter)
    if not m:
        raise ValueError(f"Invalid quarter '{quarter}'. Expected '2026Q2'.")
    year, q = int(m.group(1)), int(m.group(2))
    start_month = (q - 1) * 3 + 1
    end_month = q * 3
    start = datetime(year, start_month, 1, tzinfo=timezone.utc)
    next_month_year = year + (1 if end_month == 12 else 0)
    next_month = 1 if end_month == 12 else end_month + 1
    end = datetime(next_month_year, next_month, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
    return start, end


def _current_quarter() -> str:
    now = datetime.now(tz=timezone.utc)
    return f"{now.year}Q{(now.month - 1) // 3 + 1}"


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _format_date(ms: int | None) -> str:
    if not ms:
        return "(none)"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# ---------- field / activity parsing ----------

def _is_deadline_field(name: str) -> bool:
    return bool(name) and any(p.match(name.strip()) for p in _DEADLINE_FIELD_PATTERNS)


def _extract_deadline_ts(val: Any) -> int | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, dict):
        pres = val.get("presentation")
        if pres:
            try:
                return int(
                    datetime.strptime(str(pres), "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc).timestamp() * 1000
                )
            except ValueError:
                pass
    return None


def _extract_activity_date(item: Any) -> int | None:
    if not item:
        return None
    if isinstance(item, list):
        if not item:
            return None
        item = item[0]
    if isinstance(item, dict):
        for key in ("presentation", "name", "text"):
            v = item.get(key)
            if v:
                try:
                    return int(
                        datetime.strptime(str(v), "%Y-%m-%d")
                        .replace(tzinfo=timezone.utc).timestamp() * 1000
                    )
                except ValueError:
                    continue
    if isinstance(item, (int, float)):
        return int(item)
    return None


def _compile_standup_patterns(policy: dict) -> list[re.Pattern]:
    patterns = policy.get("standup_patterns") or list(_DEFAULT_STANDUP_PATTERNS)
    return [re.compile(p) for p in patterns]


def _is_standup(summary: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(summary or "") for p in patterns)


# ---------- approval classifier ----------

def _classify_shift(
    *,
    shift_ts: int,
    shift_author: str,
    old_ms: int | None,
    new_ms: int | None,
    approvers: set[str],
    manual_review: bool,
    comments: list[dict],
    strict: bool,
    policy_effective_ms: int,
) -> dict:
    """Classify a Due-Date shift event into a compliance bucket with evidence."""
    if shift_ts < policy_effective_ms:
        return {"classification": "pre_policy", "evidence": ["before policy effective date"]}
    if old_ms is None or (new_ms is not None and new_ms <= old_ms):
        return {"classification": "informational", "evidence": ["first-time set or earlier date"]}
    if not approvers:
        return {"classification": "approver_unknown", "evidence": ["no approver mapping for assignee"]}
    if manual_review:
        return {"classification": "approver_unknown", "evidence": ["mapping flagged manual_review"]}
    if shift_author in approvers:
        return {
            "classification": "compliant_strict",
            "evidence": [f"shift author {shift_author} is an approver"],
        }

    win_start = shift_ts - _APPROVAL_WINDOW_BEFORE_SEC * 1000
    win_end = shift_ts + _APPROVAL_WINDOW_AFTER_SEC * 1000
    new_date_str = _format_date(new_ms)
    strict_ev: list[str] = []
    loose_ev: list[str] = []
    for c in comments:
        c_ts = c.get("created") or c.get("ts") or 0
        if c_ts < win_start or c_ts > win_end:
            continue
        author = (c.get("author") or {})
        c_author = author.get("login") or author.get("name") or ""
        if c_author not in approvers:
            continue
        c_text = (c.get("text") or "").lower()
        c_id = c.get("id", "?")
        has_kw = any(kw in c_text for kw in _APPROVAL_KEYWORDS)
        has_date = bool(new_date_str) and new_date_str in c_text
        if has_kw and has_date:
            strict_ev.append(f"comment {c_id} by {c_author}: keyword + new date")
        elif has_kw or c_ts <= shift_ts:
            loose_ev.append(f"comment {c_id} by {c_author}: in window")

    if strict_ev:
        return {"classification": "compliant_strict", "evidence": strict_ev}
    if loose_ev and not strict:
        return {"classification": "compliant_loose", "evidence": loose_ev}
    return {
        "classification": "unauthorized",
        "evidence": [f"no approval from {sorted(approvers)} in window"],
    }


# ---------- per-issue scan ----------

_ISSUE_FIELDS = (
    "idReadable,summary,created,updated,"
    "reporter(login,name),"
    "customFields(name,value(presentation,name,text))"
)

_ACTIVITY_FIELDS = (
    "id,timestamp,author(login,name),field(name),"
    "added(presentation,name,text),removed(presentation,name,text)"
)


async def _fetch_issue_activities_and_comments(
    client: Any, issue_id: str,
) -> tuple[list[dict], list[dict]]:
    try:
        activities, comments = await asyncio.gather(
            client.get(
                f"/api/issues/{issue_id}/activities",
                params={
                    "fields": _ACTIVITY_FIELDS,
                    "categories": "CustomFieldCategory",
                    "$top": "500",
                },
            ),
            client.get(
                f"/api/issues/{issue_id}/comments",
                params={"fields": "id,text,created,author(login,name)", "$top": "200"},
            ),
        )
        return activities or [], comments or []
    except (ValueError, KeyError):
        return [], []


def _extract_assignee_login(issue: dict) -> str:
    for cf in issue.get("customFields", []):
        if cf.get("name") == "Assignee":
            v = cf.get("value")
            if isinstance(v, dict):
                return v.get("login") or v.get("name") or ""
            if isinstance(v, list) and v:
                first = v[0]
                if isinstance(first, dict):
                    return first.get("login") or first.get("name") or ""
    a = issue.get("assignee")
    if isinstance(a, dict):
        return a.get("login") or a.get("name") or ""
    return ""


def _extract_current_deadline(issue: dict) -> int | None:
    for cf in issue.get("customFields", []):
        if _is_deadline_field(cf.get("name", "")):
            return _extract_deadline_ts(cf.get("value"))
    return None


def _extract_current_state(issue: dict) -> str:
    state = issue.get("state")
    if isinstance(state, dict) and state.get("name"):
        return state["name"]
    for cf in issue.get("customFields", []):
        if cf.get("name") == "State":
            v = cf.get("value")
            if isinstance(v, dict):
                return v.get("name", "")
    return ""


# ---------- tool registration ----------

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
        config = _load_managers_config()
        policy = _load_policy()
        standup_patterns = _compile_standup_patterns(policy) if exclude_standups else []
        policy_effective_ms = 0
        if policy.get("policy_effective_date"):
            dt = _parse_iso(policy["policy_effective_date"])
            if dt:
                policy_effective_ms = int(dt.timestamp() * 1000)

        if not period_start or not period_finish:
            qs, qe = _quarter_to_range(_current_quarter())
            start_dt = _parse_iso(period_start) or qs
            end_dt = _parse_iso(period_finish) or qe
        else:
            start_dt = _parse_iso(period_start)
            end_dt = _parse_iso(period_finish)
            if not start_dt or not end_dt:
                return "Invalid period dates. Use YYYY-MM-DD."

        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        operator = await _get_operator_login(client)

        proj_list = [p.strip() for p in projects.split(",") if p.strip()]
        proj_clause = ""
        if proj_list:
            if len(proj_list) == 1:
                proj_clause = f"project: {proj_list[0]}"
            else:
                proj_clause = "(" + " or ".join(f"project: {p}" for p in proj_list) + ")"
        date_clause = f"updated: {start_dt.strftime('%Y-%m-%d')} .. {end_dt.strftime('%Y-%m-%d')}"
        query = (proj_clause + " " + date_clause).strip()

        issues = await client.get(
            "/api/issues",
            params={"query": query, "fields": _ISSUE_FIELDS, "$top": "500"},
        )
        if not issues:
            _audit(operator, "audit_deadline_changes", {"query": query}, 0)
            return f"## Deadline audit\nNo issues match query: `{query}`"

        if exclude_standups:
            issues = [i for i in issues if not _is_standup(i.get("summary", ""), standup_patterns)]

        ids = [i.get("idReadable", "") for i in issues if i.get("idReadable")]
        results = await asyncio.gather(*(
            _fetch_issue_activities_and_comments(client, iid) for iid in ids
        ))
        issue_by_id = {i.get("idReadable", ""): i for i in issues}

        rows: list[dict] = []
        coverage_missing: set[str] = set()

        for iid, (activities, comments) in zip(ids, results):
            issue = issue_by_id.get(iid, {})
            reporter = (issue.get("reporter") or {}).get("login") or ""
            assignee = _extract_assignee_login(issue)
            target_login = assignee or reporter
            if not target_login:
                continue
            approvers, manual_review = _get_approvers(target_login, config)
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
                    shift_ts=ts,
                    shift_author=author,
                    old_ms=old_ms,
                    new_ms=new_ms,
                    approvers=approvers,
                    manual_review=manual_review,
                    comments=comments,
                    strict=strict,
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

        _audit(
            operator, "audit_deadline_changes",
            {"query": query, "strict": strict, "projects": proj_list},
            len(rows),
        )
        return _render_audit(rows, operator, query, strict, config, coverage_missing)

    @mcp.tool()
    async def deadline_scorecard(
        quarter: str = "",
        user: str = "",
        projects: str = "",
        strict: bool = False,
        exclude_standups: bool = True,
        instance: str = "",
    ) -> str:
        """Per-assignee deadline compliance rollup for a quarter.

        Aggregates shifts and missed deadlines into a coaching/penalty breakdown.

        Args:
            quarter: 'YYYYQN' (e.g. '2026Q2'); empty = current calendar quarter
            user: Filter to a single assignee login; empty = all
            projects: Comma-separated project shortnames; empty = all accessible
            strict: If True, only keyword+date comments count as approval
            exclude_standups: Skip recurring daily/standup tickets
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        config = _load_managers_config()
        policy = _load_policy()
        standup_patterns = _compile_standup_patterns(policy) if exclude_standups else []
        policy_effective_ms = 0
        if policy.get("policy_effective_date"):
            dt = _parse_iso(policy["policy_effective_date"])
            if dt:
                policy_effective_ms = int(dt.timestamp() * 1000)

        q = quarter or _current_quarter()
        try:
            start_dt, end_dt = _quarter_to_range(q)
        except ValueError as e:
            return str(e)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        operator = await _get_operator_login(client)
        proj_list = [p.strip() for p in projects.split(",") if p.strip()]
        proj_clause = ""
        if proj_list:
            if len(proj_list) == 1:
                proj_clause = f"project: {proj_list[0]}"
            else:
                proj_clause = "(" + " or ".join(f"project: {p}" for p in proj_list) + ")"
        # Issues active in or with deadline in the quarter
        date_clause = (
            f"(updated: {start_dt.strftime('%Y-%m-%d')} .. {end_dt.strftime('%Y-%m-%d')} "
            f"or due date: {start_dt.strftime('%Y-%m-%d')} .. {end_dt.strftime('%Y-%m-%d')})"
        )
        query = (proj_clause + " " + date_clause).strip()
        try:
            issues = await client.get(
                "/api/issues",
                params={
                    "query": query,
                    "fields": _ISSUE_FIELDS + ",state(name)",
                    "$top": "500",
                },
            )
        except ValueError:
            # Fallback for instances without "due date" in query syntax
            issues = await client.get(
                "/api/issues",
                params={
                    "query": (proj_clause + f" updated: {start_dt.strftime('%Y-%m-%d')} .. {end_dt.strftime('%Y-%m-%d')}").strip(),
                    "fields": _ISSUE_FIELDS + ",state(name)",
                    "$top": "500",
                },
            )
        if not issues:
            _audit(operator, "deadline_scorecard", {"quarter": q}, 0)
            return f"## Deadline scorecard — {q}\nNo issues in scope. Query: `{query}`"

        if exclude_standups:
            issues = [i for i in issues if not _is_standup(i.get("summary", ""), standup_patterns)]

        ids = [i.get("idReadable", "") for i in issues if i.get("idReadable")]
        results = await asyncio.gather(*(
            _fetch_issue_activities_and_comments(client, iid) for iid in ids
        ))
        issue_by_id = {i.get("idReadable", ""): i for i in issues}

        per_user: dict[str, Counter] = defaultdict(Counter)
        per_user_details: dict[str, list[str]] = defaultdict(list)
        coverage_missing: set[str] = set()

        for iid, (activities, comments) in zip(ids, results):
            issue = issue_by_id.get(iid, {})
            assignee = _extract_assignee_login(issue)
            reporter = (issue.get("reporter") or {}).get("login") or ""
            target = assignee or reporter
            if not target:
                continue
            if user and target != user:
                continue
            approvers, manual_review = _get_approvers(target, config)
            if not approvers:
                coverage_missing.add(target)
            # Classify each shift
            for a in activities:
                if not _is_deadline_field((a.get("field") or {}).get("name", "")):
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
                per_user[target][cls["classification"]] += 1
                if cls["classification"] == "unauthorized":
                    per_user_details[target].append(
                        f"- **{iid}** shift {_format_date(old_ms)}→{_format_date(new_ms)} "
                        f"by {author}: {cls['evidence'][0] if cls['evidence'] else ''}"
                    )
            # Missed deadlines
            current_deadline = _extract_current_deadline(issue)
            state = _extract_current_state(issue).lower()
            now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            if (
                current_deadline
                and current_deadline < min(end_ms, now_ms)
                and current_deadline >= start_ms
                and state not in _DONE_STATES
            ):
                # Was extension approved?
                approved_shift = any(
                    per_user[target][k] for k in ("compliant_strict", "compliant_loose")
                )
                bucket = "missed_after_extension" if approved_shift else "missed_no_extension"
                per_user[target][bucket] += 1
                per_user_details[target].append(
                    f"- **{iid}** missed deadline {_format_date(current_deadline)} "
                    f"(state: {state or 'unknown'}; {bucket.replace('_', ' ')})"
                )

        _audit(
            operator, "deadline_scorecard",
            {"quarter": q, "user": user, "projects": proj_list, "strict": strict},
            sum(sum(c.values()) for c in per_user.values()),
        )
        return _render_scorecard(per_user, per_user_details, q, operator, strict, config, coverage_missing)

    @mcp.tool()
    async def suggest_managers(
        lookback_days: int = 90,
        projects: str = "",
        write: bool = True,
        instance: str = "",
    ) -> str:
        """Bootstrap a manager mapping from recent YouTrack activity.

        Detects PMs by reporter fan-out (auto-exclude), then scores remaining
        candidates by who edits non-trivial fields on each user's tasks and who
        moves their tasks to terminal states. Writes managers.suggested.json.

        Args:
            lookback_days: Activity window (default: 90)
            projects: Comma-separated project shortnames; empty = all accessible
            write: If False, just print the suggestion without saving (default: True)
            instance: YouTrack instance (optional)
        """
        client = resolver.resolve(instance)
        now = datetime.now(tz=timezone.utc)
        operator = await _get_operator_login(client)
        proj_list = [p.strip() for p in projects.split(",") if p.strip()]

        # Build query for active issues
        proj_clause = ""
        if proj_list:
            if len(proj_list) == 1:
                proj_clause = f"project: {proj_list[0]}"
            else:
                proj_clause = "(" + " or ".join(f"project: {p}" for p in proj_list) + ")"
        date_clause = f"updated: {{minus {lookback_days}d}} .. Today"
        query = (proj_clause + " " + date_clause).strip()

        issues = await client.get(
            "/api/issues",
            params={"query": query, "fields": _ISSUE_FIELDS, "$top": "500"},
        )
        if not issues:
            return f"## Manager suggester\nNo issues in lookback. Query: `{query}`"

        ids = [i.get("idReadable", "") for i in issues if i.get("idReadable")]
        results = await asyncio.gather(*(
            _fetch_full_activities(client, iid) for iid in ids
        ))

        # PM detection: reporter fanout
        reporter_assignees: dict[str, set[str]] = defaultdict(set)
        for issue in issues:
            r = (issue.get("reporter") or {}).get("login")
            a = _extract_assignee_login(issue)
            if r and a:
                reporter_assignees[r].add(a)
        fanouts = sorted(((r, len(s)) for r, s in reporter_assignees.items()),
                          key=lambda x: -x[1])
        pms: set[str] = set()
        if fanouts:
            # Top decile or hard min, whichever has fewer
            top_n = max(1, int(len(fanouts) * (1 - _PM_FANOUT_PERCENTILE)))
            for r, fanout in fanouts[:top_n]:
                if fanout >= _PM_MIN_FANOUT:
                    pms.add(r)

        # Field edits and resolvers per (target_assignee, editor_login)
        field_edits: dict[tuple[str, str], int] = defaultdict(int)
        resolvers: dict[tuple[str, str], int] = defaultdict(int)
        issue_by_id = {i.get("idReadable", ""): i for i in issues}
        for iid, activities in zip(ids, results):
            issue = issue_by_id.get(iid, {})
            target = _extract_assignee_login(issue)
            if not target:
                continue
            for a in activities:
                fname = (a.get("field") or {}).get("name", "")
                author = (a.get("author") or {}).get("login") or ""
                if not author or author == target or author in pms:
                    continue
                if fname == "State":
                    added = a.get("added") or []
                    if isinstance(added, list) and added:
                        new_state = (added[0].get("name") or "").lower()
                        if new_state in _DONE_STATES:
                            resolvers[(target, author)] += 1
                elif fname in ("Priority", "Assignee") or _is_deadline_field(fname):
                    field_edits[(target, author)] += 1

        # Combine into per-target candidate scores
        all_targets = {t for t, _ in field_edits} | {t for t, _ in resolvers} | {
            _extract_assignee_login(i) for i in issues if _extract_assignee_login(i)
        }
        suggestion: dict[str, Any] = {}
        for target in sorted(all_targets):
            if target in pms:
                continue
            candidates: dict[str, dict] = {}
            for (t, editor), n in field_edits.items():
                if t == target:
                    candidates.setdefault(editor, {"edits": 0, "resolves": 0})["edits"] = n
            for (t, closer), n in resolvers.items():
                if t == target:
                    candidates.setdefault(closer, {"edits": 0, "resolves": 0})["resolves"] = n
            if not candidates:
                suggestion[target] = {
                    "primary": None,
                    "also_accept": [],
                    "manual_review": True,
                    "evidence": ["no signal in window"],
                }
                continue
            # Score: 0.55 * edits + 0.35 * resolves + 0.10 * reporter_signal
            scored = []
            for cand, c in candidates.items():
                reporter_n = sum(
                    1 for i in issues
                    if (i.get("reporter") or {}).get("login") == cand
                    and _extract_assignee_login(i) == target
                )
                score = 0.55 * c["edits"] + 0.35 * c["resolves"] + 0.10 * reporter_n
                scored.append((cand, score, c["edits"], c["resolves"], reporter_n))
            scored.sort(key=lambda x: -x[1])
            top = scored[0]
            second = scored[1] if len(scored) > 1 else None
            # Ambiguity: refuse if top score isn't clearly ahead
            if second and top[1] > 0 and (top[1] - second[1]) / top[1] < 0.25:
                suggestion[target] = {
                    "primary": None,
                    "also_accept": [c[0] for c in scored[:3]],
                    "manual_review": True,
                    "evidence": [
                        f"{c[0]}: edits={c[2]}, resolves={c[3]}, reports={c[4]}"
                        for c in scored[:3]
                    ],
                }
            else:
                suggestion[target] = {
                    "primary": top[0],
                    "also_accept": [c[0] for c in scored[1:3] if c[1] > 0],
                    "manual_review": False,
                    "evidence": [
                        f"{c[0]}: edits={c[2]}, resolves={c[3]}, reports={c[4]}"
                        for c in scored[:3]
                    ],
                }

        suggestion["__generated__"] = now.isoformat()
        suggestion["__pms_excluded__"] = sorted(pms)
        suggestion["__lookback_days__"] = lookback_days

        written_path = ""
        if write:
            try:
                _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                with _MANAGERS_SUGGESTED_FILE.open("w") as f:
                    json.dump(suggestion, f, indent=2, ensure_ascii=False, sort_keys=True)
                written_path = str(_MANAGERS_SUGGESTED_FILE)
            except OSError as e:
                written_path = f"FAILED: {e}"

        _audit(operator, "suggest_managers", {"lookback_days": lookback_days, "projects": proj_list},
                len(suggestion) - 3)  # minus metadata keys
        return _render_suggestion(suggestion, operator, lookback_days, written_path, pms)


async def _get_operator_login(client: Any) -> str:
    try:
        me = await client.get("/api/users/me", params={"fields": "login"})
        return me.get("login", "?")
    except (ValueError, KeyError):
        return "?"


async def _fetch_full_activities(client: Any, issue_id: str) -> list[dict]:
    try:
        return await client.get(
            f"/api/issues/{issue_id}/activities",
            params={
                "fields": _ACTIVITY_FIELDS,
                "categories": "CustomFieldCategory",
                "$top": "500",
            },
        ) or []
    except (ValueError, KeyError):
        return []


# ---------- rendering ----------

def _bucket_emoji(c: str) -> str:
    return {
        "compliant_strict": "✓",
        "compliant_loose": "~",
        "unauthorized": "✗",
        "approver_unknown": "?",
        "pre_policy": "·",
        "informational": "·",
    }.get(c, "?")


def _render_audit(rows, operator, query, strict, config, coverage_missing) -> str:
    counts = Counter(r["classification"] for r in rows)
    lines = [
        "## Deadline audit",
        f"**Operator:** {operator} | **Strict:** {strict} | "
        f"**Config:** {config.get('__source_file__') or '(none)'}",
        f"**Query:** `{query}`",
        f"**Shifts found:** {len(rows)}",
        "",
        "### Rollup",
    ]
    for bucket in (
        "compliant_strict", "compliant_loose", "unauthorized",
        "approver_unknown", "pre_policy", "informational",
    ):
        if counts.get(bucket):
            lines.append(f"- {_bucket_emoji(bucket)} **{bucket}**: {counts[bucket]}")
    lines.append("")
    # Show all unauthorized first, then approver_unknown
    for cls_filter in ("unauthorized", "approver_unknown", "compliant_loose"):
        section_rows = [r for r in rows if r["classification"] == cls_filter]
        if not section_rows:
            continue
        lines.append(f"### {cls_filter} ({len(section_rows)})")
        for r in section_rows[:50]:
            lines.append(
                f"- **{r['issue']}** {_format_date(r['old'])} → {_format_date(r['new'])} "
                f"by **{r['author']}** | assignee: {r['assignee']} | "
                f"activity: `{r['activity_id']}` | "
                f"approvers: {sorted(r['approvers']) or '(none)'}"
            )
            for ev in r["evidence"][:2]:
                lines.append(f"  - _{ev}_")
        if len(section_rows) > 50:
            lines.append(f"_(+{len(section_rows) - 50} more)_")
        lines.append("")
    if coverage_missing:
        lines.append("### Coverage gaps")
        lines.append(
            f"**{len(coverage_missing)} assignees** have no approver mapping — "
            "their shifts cannot be classified. Run `suggest_managers` then update "
            "`managers.json`:"
        )
        for u in sorted(coverage_missing)[:20]:
            lines.append(f"- {u}")
        if len(coverage_missing) > 20:
            lines.append(f"_(+{len(coverage_missing) - 20} more)_")
    return compact_lines(lines)


def _render_scorecard(per_user, per_user_details, quarter, operator, strict, config, coverage_missing) -> str:
    lines = [
        f"## Deadline scorecard — {quarter}",
        f"**Operator:** {operator} | **Strict:** {strict} | "
        f"**Config:** {config.get('__source_file__') or '(none)'}",
        "",
    ]
    if not per_user:
        lines.append("_No deadline activity in scope._")
        return compact_lines(lines)
    # Sort by penalty count desc
    def _penalty(u):
        c = per_user[u]
        return c.get("unauthorized", 0) + c.get("missed_no_extension", 0) + c.get("missed_after_extension", 0)
    for user in sorted(per_user, key=lambda u: -_penalty(u)):
        c = per_user[user]
        lines.append(f"### {user}")
        lines.append(
            f"- On-time: {c.get('compliant_strict', 0)} strict, {c.get('compliant_loose', 0)} loose"
        )
        if c.get("unauthorized"):
            lines.append(f"- ✗ **Unauthorized shifts:** {c['unauthorized']}")
        if c.get("missed_no_extension"):
            lines.append(f"- ✗ **Missed (no approved extension):** {c['missed_no_extension']}")
        if c.get("missed_after_extension"):
            lines.append(f"- ⚠ **Missed after extension:** {c['missed_after_extension']}")
        if c.get("approver_unknown"):
            lines.append(f"- ? **Approver unknown:** {c['approver_unknown']} (mapping gap, not penalty)")
        if c.get("pre_policy"):
            lines.append(f"- · Pre-policy shifts (informational): {c['pre_policy']}")
        details = per_user_details.get(user, [])
        if details:
            lines.append("")
            lines.extend(details[:20])
            if len(details) > 20:
                lines.append(f"_(+{len(details) - 20} more)_")
        lines.append("")
    if coverage_missing:
        lines.append("### Coverage gaps")
        lines.append(
            f"_{len(coverage_missing)} assignees have no approver mapping. "
            "Results above may understate penalties or misclassify as 'approver_unknown'._"
        )
    return compact_lines(lines)


def _render_suggestion(suggestion, operator, lookback_days, written_path, pms) -> str:
    entries = {k: v for k, v in suggestion.items() if not k.startswith("__")}
    committed = sum(1 for v in entries.values() if v.get("primary"))
    flagged = sum(1 for v in entries.values() if v.get("manual_review"))
    lines = [
        "## Manager suggester",
        f"**Operator:** {operator} | **Lookback:** {lookback_days}d",
        f"**Targets analyzed:** {len(entries)}",
        f"**PMs detected (excluded as approvers):** {len(pms)}{': ' + ', '.join(sorted(pms)) if pms else ''}",
        f"**Committed primaries:** {committed} | **Flagged for review:** {flagged}",
        f"**File:** {written_path or '(not written)'}",
        "",
        "### Sample entries",
    ]
    for target, entry in list(entries.items())[:10]:
        primary = entry.get("primary") or "?"
        also = entry.get("also_accept") or []
        ev = entry.get("evidence", [])
        marker = "?" if entry.get("manual_review") else "✓"
        lines.append(f"- {marker} **{target}** → `{primary}`{f' (also: {also})' if also else ''}")
        for e in ev[:2]:
            lines.append(f"  - _{e}_")
    lines.extend([
        "",
        "Review the file, hand-correct flagged entries, copy to `managers.json`.",
    ])
    return compact_lines(lines)
