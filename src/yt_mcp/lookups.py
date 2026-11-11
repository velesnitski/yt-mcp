"""Cheap lookups shared by several tools (ADR-068).

Built on the client's public `get`/`post` only, so every test double that
fakes those two works unchanged.
"""

import asyncio

from yt_mcp.client import _preprocess_query_params

# /api/issuesGetter/count answers -1 while the server is still counting;
# poll briefly, then let the caller fall back.
COUNT_POLL_DELAYS = (0.1, 0.25, 0.5, 1.0)

_ME_ATTR = "_yt_mcp_current_user"


async def current_user(client) -> dict:
    """`{id, login, name}` of the token's owner, fetched once per client.

    A token cannot change owner, so the answer is kept for the client's
    life (stored on the client itself). A failure is not cached.
    """
    cached = vars(client).get(_ME_ATTR)
    if cached is None:
        cached = await client.get("/api/users/me", params={"fields": "id,login,name"}) or {}
        setattr(client, _ME_ATTR, cached)
    return cached


async def count_issues(client, query: str) -> int | None:
    """Exact match count from YouTrack's count endpoint, or None when the
    server is still counting after the poll budget.

    Query errors raise like any search. The OR-rewrite that GET queries get
    is applied here too, since this endpoint takes the query in a POST body.
    """
    q = (_preprocess_query_params({"query": query}) or {}).get("query", query)
    for delay in (*COUNT_POLL_DELAYS, None):
        data = await client.post("/api/issuesGetter/count?fields=count", json={"query": q})
        count = data.get("count") if isinstance(data, dict) else None
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            return count
        if delay is None:
            return None
        await asyncio.sleep(delay)
    return None
