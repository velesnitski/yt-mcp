"""Client-level permission-error mapping (ADR-022).

401/403 must surface as YouTrackPermissionError — a ValueError subclass with
clean, URL-free text — so EVERY tool's existing `except ValueError` handles
permission failures, and no tool can leak the instance host. 5xx must still
raise httpx.HTTPStatusError (retryable server trouble).
"""
import httpx
import pytest

from yt_mcp.client import YouTrackClient
from yt_mcp.config import YouTrackConfig
from yt_mcp.errors import YouTrackPermissionError

URL = "https://example.youtrack.cloud"


def _client(status: int) -> YouTrackClient:
    cfg = YouTrackConfig(url=URL, token="perm-fake")
    c = YouTrackClient(cfg)
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(status, json={"error": "x"})),
        base_url=URL,
    )
    return c


class TestPermissionErrorMapping:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_401_403_raise_permission_error(self, status):
        with pytest.raises(YouTrackPermissionError) as exc:
            await _client(status).get("/api/issues")
        assert exc.value.status_code == status
        # ValueError subclass: every existing catch site handles it.
        assert isinstance(exc.value, ValueError)
        # Clean text, no URL leak.
        assert URL not in str(exc.value)
        assert f"({status})" in str(exc.value)

    @pytest.mark.asyncio
    async def test_5xx_still_raises_httpx_error(self):
        with pytest.raises(httpx.HTTPStatusError):
            await _client(503).get("/api/issues")

    @pytest.mark.asyncio
    async def test_400_still_maps_to_plain_valueerror(self):
        with pytest.raises(ValueError) as exc:
            await _client(400).get("/api/issues")
        assert not isinstance(exc.value, YouTrackPermissionError)
        assert "query error (400)" in str(exc.value)

    @pytest.mark.asyncio
    async def test_fleet_wide_no_url_leak_on_read_tools(self):
        # A read tool with NO special permission handling (search_issues)
        # now propagates a clean ValueError instead of a URL-bearing httpx
        # error — the fleet-wide effect of mapping at the client layer.
        from unittest.mock import MagicMock
        from mcp.server.fastmcp import FastMCP
        from yt_mcp.resolver import InstanceResolver
        from yt_mcp.tools.issues import register

        mcp = FastMCP("test")
        resolver = MagicMock(spec=InstanceResolver)
        resolver.resolve = MagicMock(return_value=_client(403))
        register(mcp, resolver)
        fn = mcp._tool_manager._tools["search_issues"].fn
        with pytest.raises(ValueError) as exc:
            await fn(query="project: X")
        assert URL not in str(exc.value)
        assert "permission error (403)" in str(exc.value)
