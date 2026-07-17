"""Tests for time_report tools.

Mocks mirror the REAL /api/workItems contract (validated live, ADR-032):
a bare JSON list of IssueWorkItem objects with duration.minutes,
author(login,name), issue(idReadable), epoch-ms date — NOT a dict wrapper.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from yt_mcp.tools import time_report


def _item(minutes, author_name="Alice", author_login="alice", issue="PROJ-1"):
    return {
        "id": "111-1",
        "date": 1780272000000,
        "duration": {"minutes": minutes, "$type": "DurationValue"},
        "author": {"login": author_login, "name": author_name, "$type": "User"},
        "issue": {"idReadable": issue, "$type": "Issue"},
        "$type": "IssueWorkItem",
    }


def _setup(response):
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    time_report.register(mcp, resolver)
    tools = mcp._tool_manager._tools
    return client, tools


class TestMonthlyReport:
    async def test_aggregates_by_author_not_assignee(self):
        client, tools = _setup([
            _item(120, "Alice", "alice", "PROJ-1"),
            _item(60, "Bob", "bob", "PROJ-1"),   # same issue, different logger
            _item(30, "Alice", "alice", "PROJ-2"),
        ])
        out = await tools["monthly_time_report_by_user"].fn(year=2026, month=6)
        assert "Alice" in out and "Bob" in out
        assert "2h 30m" in out          # Alice: 120 + 30
        assert "1h" in out              # Bob: 60
        assert "3h 30m" in out          # total: 210m

    async def test_totals_and_issue_counts(self):
        client, tools = _setup([
            _item(90, "Alice", "alice", "PROJ-1"),
            _item(30, "Alice", "alice", "PROJ-2"),
        ])
        out = await tools["monthly_time_report_by_user"].fn(year=2026, month=6)
        assert "2 issues" in out
        assert "2 entries" in out
        assert "**Total:** 2h by 1 user(s), 2 work item(s)" in out

    async def test_sends_month_date_range(self):
        client, tools = _setup([])
        await tools["monthly_time_report_by_user"].fn(year=2026, month=2)
        params = client.get.call_args.kwargs["params"]
        assert params["startDate"] == "2026-02-01"
        assert params["endDate"] == "2026-02-28"
        assert "duration(minutes)" in params["fields"]

    async def test_defaults_to_current_month(self):
        from datetime import datetime, timezone
        client, tools = _setup([])
        await tools["monthly_time_report_by_user"].fn()
        now = datetime.now(timezone.utc)
        params = client.get.call_args.kwargs["params"]
        assert params["startDate"] == f"{now.year}-{now.month:02d}-01"

    async def test_projects_filter_uses_comma_list_not_or(self):
        client, tools = _setup([])
        await tools["monthly_time_report_by_user"].fn(
            projects="PROJ, OPS", year=2026, month=6
        )
        query = client.get.call_args.kwargs["params"]["query"]
        assert query == "project: PROJ, OPS"   # YT 400s on OR-joined clauses
        assert " OR " not in query

    async def test_invalid_month_rejected(self):
        client, tools = _setup([])
        with pytest.raises(ValueError, match="month must be 1-12"):
            await tools["monthly_time_report_by_user"].fn(year=2026, month=13)

    async def test_empty_month(self):
        client, tools = _setup([])
        out = await tools["monthly_time_report_by_user"].fn(year=2026, month=6)
        assert "No work items logged in 2026-06" in out

    async def test_paginates_until_short_page(self, monkeypatch):
        monkeypatch.setattr(time_report, "_PAGE_SIZE", 2)
        pages = [
            [_item(10), _item(20)],
            [_item(30), _item(40)],
            [_item(50)],
        ]
        client, tools = _setup(None)
        client.get = AsyncMock(side_effect=pages)
        out = await tools["monthly_time_report_by_user"].fn(year=2026, month=6)
        assert client.get.call_count == 3
        skips = [c.kwargs["params"]["$skip"] for c in client.get.call_args_list]
        assert skips == [0, 2, 4]
        assert "5 work item(s)" in out
        assert "Truncated" not in out

    async def test_truncation_is_reported(self, monkeypatch):
        monkeypatch.setattr(time_report, "_PAGE_SIZE", 2)
        monkeypatch.setattr(time_report, "_MAX_ITEMS", 4)
        client, tools = _setup(None)
        client.get = AsyncMock(return_value=[_item(10), _item(20)])  # always-full pages
        out = await tools["monthly_time_report_by_user"].fn(year=2026, month=6)
        assert "Truncated at 4 work items" in out
        assert client.get.call_count == 2  # stopped at the cap, no runaway


class TestUserTimeSummary:
    async def test_requires_user(self):
        client, tools = _setup([])
        with pytest.raises(ValueError, match="user is required"):
            await tools["user_time_summary"].fn(user="")

    async def test_date_format_validated(self):
        client, tools = _setup([])
        with pytest.raises(ValueError, match="since must be YYYY-MM-DD"):
            await tools["user_time_summary"].fn(user="alice", since="June 2026")

    async def test_totals_and_top_issues(self):
        client, tools = _setup([
            _item(120, "Alice", "alice", "PROJ-1"),
            _item(45, "Alice", "alice", "PROJ-2"),
            _item(15, "Alice", "alice", "PROJ-1"),
        ])
        out = await tools["user_time_summary"].fn(user="alice", since="2026-06-01")
        params = client.get.call_args.kwargs["params"]
        assert params["author"] == "alice"
        assert params["startDate"] == "2026-06-01"
        assert "**Total:** 3h across 2 issue(s), 3 work item(s)" in out
        # per-issue breakdown, largest first
        assert out.index("PROJ-1: 2h 15m") < out.index("PROJ-2: 45m")

    async def test_empty_result(self):
        client, tools = _setup([])
        out = await tools["user_time_summary"].fn(user="alice", since="2026-06-01")
        assert "No work items logged by alice" in out
