import httpx
from yt_mcp.config import YouTrackConfig


class YouTrackClient:
    def __init__(self, config: YouTrackConfig):
        self._config = config
        self._client = httpx.AsyncClient(
            timeout=30,
            headers={
                "Authorization": f"Bearer {config.token}",
                "Accept": "application/json",
            },
            base_url=config.url,
        )

    def _content_type_headers(self) -> dict:
        return {"Content-Type": "application/json"}

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
            raise ValueError(
                f"YouTrack {'query' if resp.status_code == 400 else 'not found'} error "
                f"({resp.status_code}): {error_msg}"
            )
        resp.raise_for_status()

    async def get(self, path: str, params: dict | None = None):
        resp = await self._client.get(path, params=params)
        await self._handle_error(resp)
        return resp.json()

    async def post(self, path: str, json: dict | None = None):
        resp = await self._client.post(
            path, json=json, headers=self._content_type_headers()
        )
        await self._handle_error(resp)
        return resp.json() if resp.content else {}

    async def delete(self, path: str) -> None:
        resp = await self._client.delete(path)
        await self._handle_error(resp)

    async def execute_command(self, issue_id: str, command: str) -> None:
        await self.post(f"/api/issues/{issue_id}/execute", json={"query": command})

    async def update_comment(self, issue_id: str, comment_id: str, text: str) -> dict:
        """Update an existing comment's text."""
        return await self.post(
            f"/api/issues/{issue_id}/comments/{comment_id}",
            json={"text": text},
        )

    async def resolve_project_id(self, short_name: str) -> str | None:
        projects = await self.get(
            "/api/admin/projects",
            params={"query": f"shortName: {short_name}", "fields": "id,shortName"},
        )
        return projects[0]["id"] if projects else None
