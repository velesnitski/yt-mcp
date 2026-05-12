"""Field selectors and async fetch helpers shared by the deadline tools."""

import asyncio
import os
from typing import Any


# Cap parallel HTTP/2 streams against YouTrack. The shared client pool runs
# with max_connections=5 (client.py); without a semaphore, a 500-issue audit
# fans out to ~1000 simultaneous requests and saturates the HTTP/2 stream
# pool — observed in production as ConnectionTerminated last_stream_id:1999.
_CONCURRENCY_LIMIT = int(os.environ.get("YT_MCP_FETCH_CONCURRENCY", "8"))


ISSUE_FIELDS = (
    "idReadable,summary,created,updated,"
    "reporter(login,name),"
    # `login` must be requested here so user-typed custom fields (Assignee,
    # etc.) return a stable identifier — `name` is the display string and
    # mismatches activity-log authors (which always use login).
    "customFields(name,value(login,presentation,name,text))"
)

ACTIVITY_FIELDS = (
    "id,timestamp,author(login,name),field(name),"
    "added(presentation,name,text),removed(presentation,name,text)"
)


async def fetch_issue_activities_and_comments(
    client: Any, issue_id: str,
) -> tuple[list[dict], list[dict]]:
    """Fetch CustomFieldCategory activities + comments for one issue, in parallel."""
    try:
        activities, comments = await asyncio.gather(
            client.get(
                f"/api/issues/{issue_id}/activities",
                params={
                    "fields": ACTIVITY_FIELDS,
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


async def fetch_issue_activities_and_comments_bounded(
    client: Any, issue_ids: list[str], limit: int | None = None,
) -> list[tuple[list[dict], list[dict]]]:
    """Same as `_and_comments` but bounded by a semaphore so a 500-issue
    audit doesn't exhaust the HTTP/2 stream pool."""
    sem = asyncio.Semaphore(limit or _CONCURRENCY_LIMIT)

    async def _one(iid: str) -> tuple[list[dict], list[dict]]:
        async with sem:
            return await fetch_issue_activities_and_comments(client, iid)

    return await asyncio.gather(*(_one(iid) for iid in issue_ids))


async def fetch_activities_only_bounded(
    client: Any, issue_ids: list[str], limit: int | None = None,
) -> list[list[dict]]:
    sem = asyncio.Semaphore(limit or _CONCURRENCY_LIMIT)

    async def _one(iid: str) -> list[dict]:
        async with sem:
            return await fetch_activities_only(client, iid)

    return await asyncio.gather(*(_one(iid) for iid in issue_ids))


async def fetch_activities_only(client: Any, issue_id: str) -> list[dict]:
    """Fetch CustomFieldCategory activities (no comments) for the suggester."""
    try:
        return await client.get(
            f"/api/issues/{issue_id}/activities",
            params={
                "fields": ACTIVITY_FIELDS,
                "categories": "CustomFieldCategory",
                "$top": "500",
            },
        ) or []
    except (ValueError, KeyError):
        return []


async def get_operator_login(client: Any) -> str:
    try:
        me = await client.get("/api/users/me", params={"fields": "login"})
        return me.get("login", "?")
    except (ValueError, KeyError):
        return "?"


def extract_assignee_login(issue: dict) -> str:
    """Pull assignee login from customFields → top-level fallback."""
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


def extract_current_deadline(issue: dict) -> int | None:
    from yt_mcp.tools.deadlines.parser import _is_deadline_field, _extract_deadline_ts
    for cf in issue.get("customFields", []):
        if _is_deadline_field(cf.get("name", "")):
            return _extract_deadline_ts(cf.get("value"))
    return None


def extract_current_state(issue: dict) -> str:
    state = issue.get("state")
    if isinstance(state, dict) and state.get("name"):
        return state["name"]
    for cf in issue.get("customFields", []):
        if cf.get("name") == "State":
            v = cf.get("value")
            if isinstance(v, dict):
                return v.get("name", "")
    return ""


def build_project_clause(projects: str) -> tuple[str, list[str]]:
    proj_list = [p.strip() for p in projects.split(",") if p.strip()]
    if not proj_list:
        return "", []
    if len(proj_list) == 1:
        return f"project: {proj_list[0]}", proj_list
    return "(" + " or ".join(f"project: {p}" for p in proj_list) + ")", proj_list
