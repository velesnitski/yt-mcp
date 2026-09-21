"""Field selectors and async fetch helpers shared by the deadline tools."""

import asyncio
import logging
import os
from typing import Any

import httpx

_logger = logging.getLogger("yt_mcp")

# YouTrack gracefully closes HTTP/2 connections after ~1000 streams. When a
# batch hits the closed connection simultaneously, httpx raises
# RemoteProtocolError. Retrying transparently gives the client pool a chance
# to open a fresh connection. Three attempts with brief backoff is enough in
# practice — if it fails 3x in a row, something else is wrong.
_FETCH_RETRIES = 3
_FETCH_RETRY_BACKOFF_SEC = 0.2


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
    for attempt in range(_FETCH_RETRIES):
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
        except httpx.RemoteProtocolError:
            if attempt < _FETCH_RETRIES - 1:
                await asyncio.sleep(_FETCH_RETRY_BACKOFF_SEC * (attempt + 1))
                continue
            _logger.warning(
                "fetch_issue_activities_and_comments giving up on %s after %d retries",
                issue_id, _FETCH_RETRIES,
            )
            return [], []
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
    for attempt in range(_FETCH_RETRIES):
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
        except httpx.RemoteProtocolError:
            if attempt < _FETCH_RETRIES - 1:
                await asyncio.sleep(_FETCH_RETRY_BACKOFF_SEC * (attempt + 1))
                continue
            _logger.warning(
                "fetch_activities_only giving up on %s after %d retries",
                issue_id, _FETCH_RETRIES,
            )
            return []
    return []


async def get_operator_login(client: Any) -> str:
    try:
        me = await client.get("/api/users/me", params={"fields": "login"})
        return (me.get("login") or "?")
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
        if _is_deadline_field((cf.get("name") or "")):
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
                return (v.get("name") or "")
    return ""


def build_project_clause(projects: str) -> tuple[str, list[str]]:
    """`project: A, B` — the comma-list idiom (registry Q17).

    The OR-joined form `(project: A or project: B)` this used to emit is
    rejected outright with a generic 400, so every multi-project call to
    the deadline tools failed. Single-project calls were unaffected, which
    is why it survived: the shape only breaks once a second key appears.
    """
    from yt_mcp import contract

    proj_list = [p.strip() for p in projects.split(",") if p.strip()]
    if not proj_list:
        return "", []
    return contract.project_clause(proj_list), proj_list


async def resolve_deadline_field(client: Any, proj_clause: str) -> str | None:
    """Name of the deadline custom field in scope, or None if absent.

    There is no `due date:` search attribute — the deadline lives in a
    per-project custom field whose name varies by project ("Deadline ☠️",
    "Due Date", localized variants). Querying the literal `due date:`
    returns a parse error, so the caller has to learn the real name first.

    Read from issues rather than from the admin API: the field names come
    back on a normal issue read, so this works with a plain reporting
    token and needs no admin rights.
    """
    from yt_mcp.tools.deadlines.parser import _is_deadline_field

    issues = await client.get(
        "/api/issues",
        params={
            "query": (proj_clause + " sort by: updated desc").strip(),
            "fields": "customFields(name)",
            "$top": "50",
        },
    ) or []
    for issue in issues:
        for cf in issue.get("customFields", []) or []:
            name = cf.get("name") or ""
            if _is_deadline_field(name):
                return name
    return None
