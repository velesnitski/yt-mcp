import httpx
from yt_mcp.config import YouTrackConfig


class YouTrackClient:
    def __init__(self, config: YouTrackConfig):
        self._config = config

    def _headers(self, with_content_type: bool = False) -> dict:
        h = {
            "Authorization": f"Bearer {self._config.token}",
            "Accept": "application/json",
        }
        if with_content_type:
            h["Content-Type"] = "application/json"
        return h

    def _url(self, path: str) -> str:
        return f"{self._config.url}{path}"

    async def get(self, path: str, params: dict | None = None):
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.get(self._url(path), params=params, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def post(self, path: str, json: dict | None = None):
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(
                self._url(path), json=json, headers=self._headers(with_content_type=True)
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    async def delete(self, path: str) -> None:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.delete(self._url(path), headers=self._headers())
            resp.raise_for_status()

    async def execute_command(self, issue_id: str, command: str) -> None:
        await self.post(f"/api/issues/{issue_id}/execute", json={"query": command})

    async def resolve_project_id(self, short_name: str) -> str | None:
        projects = await self.get(
            "/api/admin/projects",
            params={"query": f"shortName: {short_name}", "fields": "id,shortName"},
        )
        return projects[0]["id"] if projects else None
