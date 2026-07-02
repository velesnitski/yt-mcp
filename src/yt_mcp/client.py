import logging

import httpx
from yt_mcp.config import YouTrackConfig
from yt_mcp.formatters import rewrite_or_clauses

_logger = logging.getLogger("yt_mcp")

_JSON_HEADERS = {"Content-Type": "application/json"}


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
            error = ValueError(
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
        resp.raise_for_status()

    async def get(self, path: str, params: dict | None = None):
        params = _preprocess_query_params(params)
        resp = await self._client.get(path, params=params)
        await self._handle_error(resp)
        return resp.json()

    async def post(self, path: str, json: dict | None = None):
        # POST bodies don't typically use the YT search-query string the same
        # way GET params do (commands use `query` as a command, not a search
        # filter), so we don't rewrite POSTs.
        resp = await self._client.post(
            path, json=json, headers=_JSON_HEADERS
        )
        await self._handle_error(resp)
        return resp.json() if resp.content else {}

    async def post_multipart(self, path: str, files: dict, params: dict | None = None):
        """POST multipart/form-data — for file uploads (e.g. attachments).

        Deliberately does NOT set the JSON content-type: httpx derives the
        multipart boundary from `files=`, which the JSON `post()` can't do.
        `files` is httpx-shaped: {"file": (filename, bytes, mime_type)}.
        """
        resp = await self._client.post(path, files=files, params=params)
        await self._handle_error(resp)
        return resp.json() if resp.content else {}

    async def delete(self, path: str) -> None:
        resp = await self._client.delete(path)
        await self._handle_error(resp)

    async def execute_command(self, issue_id: str, command: str) -> None:
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

    async def resolve_project_id(self, short_name: str) -> str | None:
        # /api/admin/projects is YouTrack's ONLY projects resource. Despite the
        # "admin" path segment, GET is permission-FILTERED, not admin-gated: it
        # returns just the projects the current user can read, so a
        # low-permission user gets their own subset (200) rather than a 403
        # (verified against a live instance + JetBrains REST docs). There is no
        # /api/projects endpoint — it 404s — so there is nothing to fall back
        # to. Catch defensively so a genuinely access-less token degrades to
        # "not found" instead of propagating an uncaught error.
        try:
            projects = await self.get(
                "/api/admin/projects",
                params={"fields": "id,shortName", "$top": "500"},
            )
        except (httpx.HTTPStatusError, ValueError):
            return None
        for p in projects:
            if p.get("shortName", "").lower() == short_name.lower():
                return p["id"]
        return None
