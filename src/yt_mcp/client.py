import asyncio
import logging
import time

import httpx
from yt_mcp.config import YouTrackConfig
from yt_mcp.errors import UserInputError, YouTrackPermissionError
from yt_mcp.formatters import rewrite_or_clauses

_logger = logging.getLogger("yt_mcp")

_JSON_HEADERS = {"Content-Type": "application/json"}

# Statuses that mean "not processed, try again later" (ADR-064). 429 is
# YouTrack's rate limit; 503 is a gateway or maintenance refusal. Neither
# applied the request, so a retry is safe even for a non-idempotent POST.
# Other 5xx may have run server-side and are never retried.
_RETRY_STATUSES = (429, 503)
_RETRY_BACKOFF = (0.5, 1.5)  # seconds before attempts 2 and 3
_RETRY_AFTER_CAP = 10.0      # never sleep longer than this on a server hint

# The project list changes rarely and every create/command path needs it.
_PROJECTS_TTL = 300.0


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait before the next attempt: the server's Retry-After
    (delta-seconds form) when given, else the fixed backoff — capped."""
    hint = resp.headers.get("Retry-After", "")
    try:
        delay = float(hint)
    except ValueError:
        delay = _RETRY_BACKOFF[attempt]
    return max(0.0, min(delay, _RETRY_AFTER_CAP))


def _preprocess_query_params(params: dict | None) -> dict | None:
    """Auto-rewrite YT query 'OR' footguns before they hit the API.

    Callers naturally express disjunctions as `summary: X OR summary: Y` —
    YT rejects this with a generic 400. The correct syntax is the
    comma-list `summary: X, Y`. Rewriting here means every query path
    (search_issues, count_issues, get_issues_digest, …) benefits without
    each tool needing to remember the idiom.

    Returns the (possibly-modified) params dict; original is not mutated.
    """
    if not params or "query" not in params:
        return params
    q = params["query"]
    if not isinstance(q, str):
        return params
    rewritten, changes = rewrite_or_clauses(q)
    if not changes:
        return params
    _logger.info(
        "YT query auto-rewrite: %s",
        changes[0],
        extra={"original_query": q[:200], "rewritten_query": rewritten[:200]},
    )
    new_params = dict(params)
    new_params["query"] = rewritten
    return new_params


class YouTrackClient:
    def __init__(self, config: YouTrackConfig):
        self._config = config
        self._client = httpx.AsyncClient(
            timeout=30,
            http2=True,
            limits=httpx.Limits(
                max_connections=5,
                max_keepalive_connections=2,
                keepalive_expiry=30,
            ),
            headers={
                "Authorization": f"Bearer {config.token}",
                "Accept": "application/json",
            },
            base_url=config.url,
        )
        self._projects: list[dict] | None = None
        self._projects_at = 0.0

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One request, retried on 429/503 only (see _RETRY_STATUSES)."""
        for attempt in range(len(_RETRY_BACKOFF) + 1):
            resp = await self._client.request(method, path, **kwargs)
            if resp.status_code not in _RETRY_STATUSES or attempt == len(_RETRY_BACKOFF):
                return resp
            delay = _retry_delay(resp, attempt)
            _logger.info(
                "YouTrack %s, retrying in %.1fs", resp.status_code, delay,
                extra={"error_type": "youtrack_retry", "tool": path},
            )
            await asyncio.sleep(delay)
        return resp  # unreachable; keeps type checkers content

    async def aclose(self) -> None:
        """Close the HTTP pool (one-shot callers; servers keep it open)."""
        await self._client.aclose()

    @property
    def base_url(self) -> str:
        """Configured YouTrack base URL (e.g. https://example.youtrack.cloud).
        Surfaced so tools can build issue hyperlinks without poking _config."""
        return self._config.url

    async def _handle_error(self, resp: httpx.Response) -> None:
        """Extract YouTrack error message for 400/404 responses, raise for other errors."""
        if resp.status_code in (400, 404):
            try:
                error_data = resp.json()
                error_msg = error_data.get(
                    "error_description",
                    error_data.get("error", "Unknown error"),
                )
            except (ValueError, KeyError):
                error_msg = "Unknown error"
            # Truncate to avoid leaking internal details
            if isinstance(error_msg, str) and len(error_msg) > 200:
                error_msg = error_msg[:200] + "..."
            error = UserInputError(
                f"YouTrack {'query' if resp.status_code == 400 else 'not found'} error "
                f"({resp.status_code}): {error_msg}"
            )
            # warning, not error: 400/404 = bad caller input (invalid query
            # syntax, missing issue), not a yt-mcp bug. Sentry LoggingIntegration
            # only escalates >=ERROR to events; warnings stay as breadcrumbs.
            _logger.warning(
                str(error),
                extra={"error_type": "youtrack_api", "tool": resp.request.url.path},
            )
            raise error
        if resp.status_code in (401, 403):
            # Clean, URL-free ValueError subclass so EVERY tool's existing
            # `except ValueError` handles permission failures gracefully —
            # previously these escaped as httpx.HTTPStatusError (whose str()
            # embeds the request URL) in any module that hadn't opted in
            # (ADR-017 fixed issues.py piecemeal; this fixes the fleet).
            perm = YouTrackPermissionError(resp.status_code)
            _logger.warning(
                str(perm),
                extra={"error_type": "youtrack_permission", "tool": resp.request.url.path},
            )
            raise perm
        resp.raise_for_status()

    async def get(self, path: str, params: dict | None = None):
        params = _preprocess_query_params(params)
        resp = await self._send("GET", path, params=params)
        await self._handle_error(resp)
        return resp.json()

    async def post(self, path: str, json: dict | None = None):
        # POST bodies don't typically use the YT search-query string the same
        # way GET params do (commands use `query` as a command, not a search
        # filter), so we don't rewrite POSTs.
        resp = await self._send("POST", path, json=json, headers=_JSON_HEADERS)
        await self._handle_error(resp)
        return resp.json() if resp.content else {}

    async def post_multipart(self, path: str, files: dict, params: dict | None = None):
        """POST multipart/form-data — for file uploads (e.g. attachments).

        Deliberately does NOT set the JSON content-type: httpx derives the
        multipart boundary from `files=`, which the JSON `post()` can't do.
        `files` is httpx-shaped: {"file": (filename, bytes, mime_type)}.
        """
        resp = await self._send("POST", path, files=files, params=params)
        await self._handle_error(resp)
        return resp.json() if resp.content else {}

    async def delete(self, path: str) -> None:
        resp = await self._send("DELETE", path)
        await self._handle_error(resp)

    async def execute_command(self, issue_id: str, command: str) -> None:
        # ADR-019 invariant, enforced at the choke point: braces are an
        # input-only grouping convention and 400 at /api/commands — but
        # callers imitating create_issue's docstring style pass them into
        # update/bulk/delete paths too (ADR-042). Strip here so no caller
        # can leak them to YouTrack.
        command = command.replace("{", "").replace("}", "")
        await self.post(
            "/api/commands",
            json={
                "query": command,
                "issues": [{"idReadable": issue_id}],
            },
        )

    async def update_comment(self, issue_id: str, comment_id: str, text: str) -> dict:
        """Update an existing comment's text."""
        return await self.post(
            f"/api/issues/{issue_id}/comments/{comment_id}",
            json={"text": text},
        )

    def _cached_projects(self) -> list[dict] | None:
        """The cached project list, or None when absent or older than the TTL."""
        if self._projects is None or time.monotonic() - self._projects_at >= _PROJECTS_TTL:
            return None
        return self._projects

    async def _fetch_projects(self) -> list[dict]:
        # /api/admin/projects is YouTrack's ONLY projects resource. Despite the
        # "admin" path segment, GET is permission-FILTERED, not admin-gated: it
        # returns just the projects the current user can read, so a
        # low-permission user gets their own subset (200) rather than a 403
        # (verified against a live instance + JetBrains REST docs). There is no
        # /api/projects endpoint — it 404s — so there is nothing to fall back
        # to. Catch defensively so a genuinely access-less token degrades to
        # "not found" instead of propagating an uncaught error. A failure is
        # not cached, so the next call tries again.
        try:
            projects = await self.get(
                "/api/admin/projects",
                params={"fields": "id,shortName", "$top": "500"},
            )
        except (httpx.HTTPStatusError, ValueError):
            return []
        self._projects, self._projects_at = (projects or []), time.monotonic()
        return self._projects

    async def list_projects_cached(self) -> list[dict]:
        """`[{id, shortName}]` for the projects this token can read, cached
        for _PROJECTS_TTL seconds. Every create/command path resolved the
        project by downloading the whole list again (ADR-064)."""
        cached = self._cached_projects()
        return cached if cached is not None else await self._fetch_projects()

    async def resolve_project_id(self, short_name: str) -> str | None:
        want = short_name.lower()

        def find(projects: list[dict]) -> str | None:
            for p in projects:
                if (p.get("shortName") or "").lower() == want:
                    return p["id"]
            return None

        # A miss against the cache refetches once, so a project created a
        # minute ago still resolves; a miss against a fresh list is final.
        cached = self._cached_projects()
        if cached is not None and (hit := find(cached)) is not None:
            return hit
        return find(await self._fetch_projects())
