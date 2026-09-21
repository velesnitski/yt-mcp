"""Single-issue rollback and work-item writes (ADR-055).

`rollback_issue` restores one prior value and `delete_work_item` removes
logged time; in both, the message returned is the caller's only record of
what changed. These paths were the largest uncovered blocks left in
`history.py` after the cross-cutting suite, which reaches a tool once but
not through each of its field-type branches.
"""

from unittest.mock import AsyncMock, MagicMock

from mcp.server.fastmcp import FastMCP

from yt_mcp.tools import history, projects


def _tools(module, get=None, post=None):
    client = MagicMock()
    client.get = AsyncMock(return_value=get if get is not None else [])
    client.post = AsyncMock(return_value=post if post is not None else {})
    client.delete = AsyncMock(return_value={})
    client.execute_command = AsyncMock(return_value={})
    client.resolve_project_id = AsyncMock(return_value="0-1")
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    module.register(mcp, resolver)
    return client, {n: t.fn for n, t in mcp._tool_manager._tools.items()}


def _activity(field, removed, added=None, aid="a1"):
    return {
        "id": aid, "timestamp": 1767225600000,
        "field": {"name": field},
        "removed": removed,
        "added": added if added is not None else [{"name": "new"}],
    }


class TestRollbackIssue:
    async def test_unknown_activity_is_reported_not_guessed(self):
        client, tools = _tools(history, get=[_activity("State", [{"name": "Open"}])])
        out = await tools["rollback_issue"](issue_id="PROJ-1", activity_id="nope")
        assert "not found" in out
        client.post.assert_not_called()
        client.execute_command.assert_not_called()

    async def test_summary_is_restored_from_the_removed_value(self):
        client, tools = _tools(history, get=[_activity("summary", "the old title")])
        out = await tools["rollback_issue"](issue_id="PROJ-1", activity_id="a1")
        assert "the old title" in out
        client.post.assert_awaited()
        assert client.post.await_args.kwargs["json"]["summary"] == "the old title"

    async def test_description_is_restored(self):
        client, tools = _tools(history, get=[_activity("description", "old body")])
        await tools["rollback_issue"](issue_id="PROJ-1", activity_id="a1")
        assert client.post.await_args.kwargs["json"]["description"] == "old body"

    async def test_custom_field_is_restored_by_command(self):
        client, tools = _tools(history, get=[_activity("Priority", [{"name": "Low"}])])
        out = await tools["rollback_issue"](issue_id="PROJ-1", activity_id="a1")
        client.execute_command.assert_awaited()
        cmd = client.execute_command.await_args.args[1]
        assert "Priority" in cmd and "Low" in cmd
        assert "Low" in out

    async def test_a_change_with_no_previous_value_is_refused(self):
        """Restoring "nothing" would clear the field rather than undo."""
        client, tools = _tools(history, get=[_activity("Priority", [])])
        out = await tools["rollback_issue"](issue_id="PROJ-1", activity_id="a1")
        client.execute_command.assert_not_called()
        assert out.strip()


class TestWorkItemWrites:
    async def test_update_reports_previous_values(self):
        client, tools = _tools(
            history,
            get={"duration": {"minutes": 60}, "date": 1767225600000, "text": "old note"},
        )
        out = await tools["update_work_item"](
            issue_id="PROJ-1", work_item_id="w1", duration_minutes=90
        )
        assert "60" in out or "1h" in out
        client.post.assert_awaited()

    async def test_update_sends_only_what_changed(self):
        client, tools = _tools(
            history,
            get={"duration": {"minutes": 60}, "date": 1767225600000, "text": "old note"},
        )
        await tools["update_work_item"](
            issue_id="PROJ-1", work_item_id="w1", description="new note"
        )
        body = client.post.await_args.kwargs["json"]
        assert body.get("text") == "new note"
        assert "duration" not in body, "an unspecified field must not be overwritten"

    async def test_delete_reports_what_it_removed(self):
        client, tools = _tools(
            history,
            get={"duration": {"minutes": 45}, "date": 1767225600000, "text": "some work"},
        )
        out = await tools["delete_work_item"](issue_id="PROJ-1", work_item_id="w1")
        client.delete.assert_awaited_once()
        assert "45" in out or "45m" in out
        assert "some work" in out

    async def test_delete_reads_before_removing(self):
        order = []
        client, tools = _tools(history, get={"duration": {"minutes": 45}, "text": "x"})
        client.get = AsyncMock(side_effect=lambda *a, **k: (
            order.append("get"), {"duration": {"minutes": 45}, "text": "x"})[1])
        client.delete = AsyncMock(side_effect=lambda *a, **k: order.append("delete"))
        await tools["delete_work_item"](issue_id="PROJ-1", work_item_id="w1")
        assert order == ["get", "delete"], "nothing to report if read after deletion"


class TestAgileBoardTools:
    BOARD = {
        "id": "b1", "name": "Team Board",
        "projects": [{"shortName": "PROJ", "name": "Project"}],
        "columnSettings": {"columns": [
            {"presentation": "In Progress"}, {"presentation": "Closed"},
        ]},
        "sprints": [{"id": "s1", "name": "Sprint 1", "archived": False}],
    }

    async def test_board_details_render(self):
        _, tools = _tools(projects, get=[self.BOARD])
        out = await tools["get_agile_board"](name="Team")
        assert "Team Board" in out
        assert "PROJ" in out

    async def test_unknown_board_says_so(self):
        _, tools = _tools(projects, get=[])
        out = await tools["get_agile_board"](name="Ghost")
        assert "No agile board found" in out

    async def test_delete_board_reports_what_it_removed(self):
        client, tools = _tools(projects, get=[self.BOARD])
        out = await tools["delete_agile_board"](board_name="Team")
        client.delete.assert_awaited_once()
        assert "Team Board" in out

    async def test_delete_refuses_an_ambiguous_name(self):
        """A destructive call must not pick between candidates."""
        boards = [dict(self.BOARD, id="b1", name="Team Board"),
                  dict(self.BOARD, id="b2", name="Team Board Archive")]
        client, tools = _tools(projects, get=boards)
        out = await tools["delete_agile_board"](board_name="Team Board")
        assert client.delete.await_count == 0 or "Multiple" in out
