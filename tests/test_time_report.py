"""Tests for time tracking reporting tools."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from yt_mcp.config import YouTrackConfig
from yt_mcp.resolver import InstanceResolver


def _make_mock_client():
    """Create a mock YouTrackClient."""
    cfg = YouTrackConfig(url="https://test.youtrack.cloud", token="perm:test")
    mock = MagicMock()
    mock._config = cfg
    mock.base_url = cfg.url
    mock.get = AsyncMock()
    return mock


def _make_resolver(client=None):
    """Create a resolver with mock client."""
    if client is None:
        client = _make_mock_client()
    return InstanceResolver({"default": client})


@pytest.mark.asyncio
async def test_monthly_time_report_no_entries():
    """Test monthly time report with no time entries."""
    from yt_mcp.tools.time_report import register
    from mcp.server.fastmcp import FastMCP

    client = _make_mock_client()
    client.get.return_value = {"issue": []}

    mcp = FastMCP("test")
    register(mcp, _make_resolver(client))

    # Get the tool function
    tool_fn = mcp._tool_manager._tools["monthly_time_report_by_user"].fn

    result = await tool_fn(instance="", projects="", year=2026, month=7)
    assert "No time entries for 2026-07" in result


@pytest.mark.asyncio
async def test_monthly_time_report_with_entries():
    """Test monthly time report with time entries."""
    from yt_mcp.tools.time_report import register
    from mcp.server.fastmcp import FastMCP

    client = _make_mock_client()
    client.get.return_value = {
        "issue": [
            {
                "assignee": {"name": "Alice", "email": "alice@example.com", "id": "user-1"},
                "customFields": [
                    {"name": "Spent time", "value": {"id": "120"}},
                ],
            },
            {
                "assignee": {"name": "Bob", "email": "bob@example.com", "id": "user-2"},
                "customFields": [
                    {"name": "Spent time", "value": {"id": "180"}},
                ],
            },
        ]
    }

    mcp = FastMCP("test")
    register(mcp, _make_resolver(client))
    tool_fn = mcp._tool_manager._tools["monthly_time_report_by_user"].fn

    result = await tool_fn(instance="", projects="", year=2026, month=7)
    assert "Time Report: 2026-07" in result
    assert "Alice" in result
    assert "Bob" in result
    assert "120" in result or "180" in result


@pytest.mark.asyncio
async def test_user_time_summary():
    """Test user time summary tool."""
    from yt_mcp.tools.time_report import register
    from mcp.server.fastmcp import FastMCP

    client = _make_mock_client()
    client.get.return_value = {
        "issue": [
            {
                "customFields": [
                    {"name": "Spent time", "value": {"id": "240"}},
                    {"name": "Estimated time", "value": {"id": "300"}},
                ],
            },
        ]
    }

    mcp = FastMCP("test")
    register(mcp, _make_resolver(client))
    tool_fn = mcp._tool_manager._tools["user_time_summary"].fn

    result = await tool_fn(instance="", user_name="Alice", since="")
    assert "Time Summary for: Alice" in result
    assert "240" in result or "Total spent" in result


@pytest.mark.asyncio
async def test_user_time_summary_requires_user_name():
    """Test user time summary requires user_name parameter."""
    from yt_mcp.tools.time_report import register
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    register(mcp, _make_resolver())
    tool_fn = mcp._tool_manager._tools["user_time_summary"].fn

    with pytest.raises(ValueError, match="user_name is required"):
        await tool_fn(instance="", user_name="", since="")


@pytest.mark.asyncio
async def test_invalid_month():
    """Test monthly time report rejects invalid month."""
    from yt_mcp.tools.time_report import register
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    register(mcp, _make_resolver())
    tool_fn = mcp._tool_manager._tools["monthly_time_report_by_user"].fn

    with pytest.raises(ValueError, match="Month must be 1–12"):
        await tool_fn(instance="", projects="", year=2026, month=13)
