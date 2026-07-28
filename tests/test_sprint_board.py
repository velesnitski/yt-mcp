"""Tests for get_sprint_board — sprint-level issues, client-side grouping.

Mocks mirror the REAL agile API shape (ADR-038): issues live on the sprint
entity; board.columns carry ONLY presentation — a nested issues selector is
silently ignored by YouTrack, which is exactly how every column rendered
empty while the sprint held dozens of issues.
"""

from unittest.mock import AsyncMock, MagicMock

from mcp.server.fastmcp import FastMCP

from yt_mcp.tools import projects


def _issue(iid, state, assignee="Alice A"):
    return {
        "idReadable": iid,
        "summary": f"Task {iid}",
        "assignee": {"name": assignee},
        "customFields": [{"name": "State", "value": {"name": state}}],
    }


def _setup(sprint_payload):
    client = MagicMock()
    board = {
        "id": "1-1",
        "name": "Team Board",
        "currentSprint": {"id": "2-1", "name": "Sprint 1"},
        "sprints": [{"id": "2-1", "name": "Sprint 1", "archived": False}],
    }
    client.get = AsyncMock(side_effect=[board, sprint_payload])
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    projects.register(mcp, resolver)
    return client, mcp._tool_manager._tools["get_sprint_board"].fn


class TestSprintBoardGrouping:
    async def test_sprint_level_issues_grouped_into_columns(self):
        client, fn = _setup({
            "name": "Sprint 1",
            "issues": [
                _issue("PROJ-1", "Submitted"),
                _issue("PROJ-2", "In Progress", "Bob B"),
                _issue("PROJ-3", "In Progress"),
            ],
            "board": {"columns": [
                {"presentation": "Submitted"},
                {"presentation": "In Progress"},
                {"presentation": "Closed"},
            ]},
        })
        out = await fn(board_name="1-1")
        assert "### Submitted (1)" in out
        assert "### In Progress (2)" in out
        assert "### Closed (0)" in out
        assert "PROJ-2" in out and "Bob B" in out
        # the request must ask for sprint-level issues, not column-nested
        fields = client.get.call_args.kwargs["params"]["fields"]
        assert "issues(" in fields.split("board(")[0]

    async def test_unmapped_state_bucketed_not_dropped(self):
        client, fn = _setup({
            "name": "Sprint 1",
            "issues": [_issue("PROJ-9", "Blocked")],
            "board": {"columns": [{"presentation": "Submitted"}]},
        })
        out = await fn(board_name="1-1")
        assert "PROJ-9" in out
        assert "states without a column" in out

    async def test_column_match_is_case_insensitive(self):
        client, fn = _setup({
            "name": "Sprint 1",
            "issues": [_issue("PROJ-4", "in progress")],
            "board": {"columns": [{"presentation": "In Progress"}]},
        })
        out = await fn(board_name="1-1")
        assert "### In Progress (1)" in out

    async def test_empty_sprint_says_so(self):
        client, fn = _setup({
            "name": "Sprint 1",
            "issues": [],
            "board": {"columns": [{"presentation": "Submitted"}]},
        })
        out = await fn(board_name="1-1")
        assert "No issues in this sprint" in out
