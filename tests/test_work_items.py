"""Tests for get_work_items: text truncation (token cost) and date filters."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from yt_mcp.tools import history


def _ms(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def _item(minutes, day, text="", author="Alice"):
    return {
        "id": f"111-{day}",
        "date": _ms(2026, 6, day),
        "duration": {"minutes": minutes},
        "author": {"name": author},
        "text": text,
        "type": {"name": "Development"},
    }


def _setup(response):
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    history.register(mcp, resolver)
    return client, mcp._tool_manager._tools["get_work_items"].fn


class TestTextTruncation:
    async def test_long_text_truncated_by_default(self):
        long_text = "word " * 200  # ~1000 chars of work journal
        client, fn = _setup([_item(60, 17, long_text)])
        out = await fn(issue_id="PROJ-1")
        assert len(out) < 600
        assert "include_text=True for full" in out
        assert f"+{len(long_text) - 200} chars" in out

    async def test_include_text_true_keeps_full_text(self):
        long_text = "word " * 200
        client, fn = _setup([_item(60, 17, long_text)])
        out = await fn(issue_id="PROJ-1", include_text=True)
        assert long_text.rstrip() in out
        assert "include_text=True for full" not in out

    async def test_short_text_never_truncated(self):
        client, fn = _setup([_item(60, 17, "quick fix")])
        out = await fn(issue_id="PROJ-1")
        assert "quick fix" in out
        assert "chars" not in out


class TestDateFilters:
    async def test_since_until_bound_inclusive(self):
        client, fn = _setup([
            _item(60, 10),
            _item(30, 17),
            _item(15, 25),
        ])
        out = await fn(issue_id="PROJ-1", since="2026-06-17", until="2026-06-17")
        assert "2026-06-17" in out
        assert "2026-06-10" not in out and "2026-06-25" not in out
        assert "**Total:** 30m" in out

    async def test_no_filter_keeps_all(self):
        client, fn = _setup([_item(60, 10), _item(30, 17)])
        out = await fn(issue_id="PROJ-1")
        assert "**Total:** 1h 30m" in out

    async def test_empty_after_filter_mentions_period(self):
        client, fn = _setup([_item(60, 10)])
        out = await fn(issue_id="PROJ-1", since="2026-06-20")
        assert "No work items found" in out
        assert "2026-06-20" in out

    async def test_bad_date_rejected(self):
        client, fn = _setup([])
        with pytest.raises(ValueError, match="since must be YYYY-MM-DD"):
            await fn(issue_id="PROJ-1", since="June 17")
