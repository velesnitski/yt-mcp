import logging

import httpx
from yt_mcp.config import YouTrackConfig

_logger = logging.getLogger("yt_mcp")

_JSON_HEADERS = {"Content-Type": "application/json"}


class YouTrackClient:
    def __init__(self, config: YouTrackConfig):
        self._config = config
        self._client = httpx.AsyncClient(
            timeout=30,
            http2=True,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30,
            ),
            headers={
                "Authorization": f"Bearer {config.token}",
                "Accept": "application/json",
            },
            base_url=config.url,
        )

    async def _handle_error(self, resp: httpx.Response) -> None:
        """Extract YouTrack error message for 400/404 responses, raise for other errors."""
        if resp.status_code in (400, 404):
            try:
                error_data = resp.json()
                error_msg = error_data.get(
                    "error_description",
                    error_data.get("error", "Unknown error"),
                )
            except Exception:
                error_msg = "Unknown error"
            # Truncate to avoid leaking internal details
            if isinstance(error_msg, str) and len(error_msg) > 200:
                error_msg = error_msg[:200] + "..."
            error = ValueError(
                f"YouTrack {'query' if resp.status_code == 400 else 'not found'} error "
                f"({resp.status_code}): {error_msg}"
            )
            _logger.error(
                str(error),
                extra={"error_type": "youtrack_api", "tool": resp.request.url.path},
            )
            raise error
        resp.raise_for_status()

    async def get(self, path: str, params: dict | None = None):
        resp = await self._client.get(path, params=params)
        await self._handle_error(resp)
        return resp.json()

    async def post(self, path: str, json: dict | None = None):
        resp = await self._client.post(
            path, json=json, headers=_JSON_HEADERS
        )
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
        # Try admin endpoint first, fall back to non-admin
        for endpoint in ("/api/admin/projects", "/api/projects"):
            try:
                projects = await self.get(
                    endpoint,
                    params={"query": f"shortName: {short_name}", "fields": "id,shortName"},
                )
                if projects:
                    return projects[0]["id"]
            except (ValueError, Exception):
                continue
        return None
