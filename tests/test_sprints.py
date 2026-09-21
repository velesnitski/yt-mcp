"""Sprint writes: resolution, date parsing, batch reporting (ADR-054).

Three write tools at 7% coverage. The resolution helpers matter most:
every write here is aimed by a name match, so a loose match does not fail
— it succeeds against the wrong sprint.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from yt_mcp.errors import UserInputError
from yt_mcp.tools import sprints


def _board(name="Team Board", sprint_names=("Sprint 1",)):
    return {
        "id": "b1", "name": name,
        "sprints": [
            {"id": f"s{i}", "name": n, "archived": False}
            for i, n in enumerate(sprint_names, 1)
        ],
    }


def _tools(boards, post=None, execute=None):
    client = MagicMock()
    client.get = AsyncMock(return_value=boards)
    client.post = AsyncMock(return_value=post if post is not None else {"id": "s9"})
    client.execute_command = AsyncMock(side_effect=execute) if execute else AsyncMock(return_value={})
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    sprints.register(mcp, resolver)
    return client, {n: t.fn for n, t in mcp._tool_manager._tools.items()}


class TestSprintResolution:
    """A write aimed by name must never land on a sprint the caller did
    not name."""

    def test_exact_name_wins_over_a_longer_substring_match(self):
        """`Sprint 1` must target `Sprint 1`, not `Sprint 10`.

        This is the whole point: a substring match returns whichever the
        board happens to list first, so the same call could hit different
        sprints on different boards.
        """
        board = _board(sprint_names=("Sprint 10", "Sprint 1", "Sprint 11"))
        sprint, err = sprints._find_sprint(board, "Sprint 1")
        assert err == ""
        assert sprint["name"] == "Sprint 1"

    def test_ambiguous_substring_is_refused_not_guessed(self):
        board = _board(sprint_names=("Sprint 10", "Sprint 11"))
        sprint, err = sprints._find_sprint(board, "Sprint 1")
        assert sprint is None
        assert "Multiple sprints match" in err
        assert "Sprint 10" in err and "Sprint 11" in err

    def test_unique_substring_still_resolves(self):
        board = _board(sprint_names=("Q1 Planning", "Q2 Delivery"))
        sprint, err = sprints._find_sprint(board, "planning")
        assert err == "" and sprint["name"] == "Q1 Planning"

    def test_missing_sprint_names_the_board(self):
        sprint, err = sprints._find_sprint(_board(name="Team Board"), "Nope")
        assert sprint is None
        assert "Nope" in err and "Team Board" in err

    def test_case_and_padding_insensitive(self):
        board = _board(sprint_names=("Sprint 1",))
        sprint, err = sprints._find_sprint(board, "  sPrInT 1 ")
        assert err == "" and sprint["name"] == "Sprint 1"

    def test_board_with_no_sprints(self):
        sprint, err = sprints._find_sprint({"name": "B", "sprints": []}, "x")
        assert sprint is None and "not found" in err


class TestBoardResolution:
    async def test_ambiguous_board_is_refused(self):
        _, tools = _tools([{"id": "1", "name": "Alpha Board", "sprints": []},
                           {"id": "2", "name": "Alpha Board 2", "sprints": []}])
        out = await tools["create_sprint"](board_name="Alpha", sprint_name="S1")
        assert "Multiple boards match" in out

    async def test_unknown_board_writes_nothing(self):
        client, tools = _tools([])
        out = await tools["create_sprint"](board_name="Ghost", sprint_name="S1")
        assert "No agile board found" in out
        client.post.assert_not_called()


class TestDateParsing:
    """A caller typo is caller input, not a server fault.

    A bare ValueError is not a UserInputError, so error reporting would
    not filter it and a mistyped date would page as a production error.
    """

    @pytest.mark.parametrize("bad", ["yesterday", "31-01-2026", "2026-13-45", ""])
    async def test_bad_start_is_user_input_error(self, bad):
        if bad == "":
            pytest.skip("empty means 'not supplied' and is skipped by design")
        client, tools = _tools([_board()])
        with pytest.raises(UserInputError, match="start must be an ISO date"):
            await tools["create_sprint"](board_name="Team", sprint_name="S", start=bad)
        client.post.assert_not_called()

    async def test_bad_finish_names_the_finish_field(self):
        _, tools = _tools([_board()])
        with pytest.raises(UserInputError, match="finish must be an ISO date"):
            await tools["create_sprint"](board_name="Team", sprint_name="S", finish="soon")

    async def test_valid_dates_become_epoch_millis(self):
        client, tools = _tools([_board()])
        await tools["create_sprint"](
            board_name="Team", sprint_name="S", start="2026-01-01", finish="2026-01-14"
        )
        body = client.post.await_args.kwargs["json"]
        assert body["start"] == 1767225600000
        assert body["finish"] > body["start"]

    def test_parser_is_reusable_and_typed(self):
        assert sprints._parse_iso_day("2026-01-01", "start") == 1767225600000
        with pytest.raises(UserInputError):
            sprints._parse_iso_day("nope", "start")


class TestCreateRequestsFieldsBack:
    """Q16: a creating POST returns only {$type, id} unless asked."""

    async def test_post_path_carries_a_fields_selector(self):
        client, tools = _tools([_board()])
        await tools["create_sprint"](board_name="Team", sprint_name="Q3")
        path = client.post.await_args.args[0]
        assert "fields=" in path, "created sprint cannot be read back without a selector"
        assert "name" in path

    async def test_name_from_the_response_is_preferred(self):
        client, tools = _tools([_board()], post={"id": "s9", "name": "Server Name"})
        out = await tools["create_sprint"](board_name="Team", sprint_name="Asked Name")
        assert "Server Name" in out


class TestUpdateSprint:
    async def test_no_fields_writes_nothing(self):
        client, tools = _tools([_board()])
        out = await tools["update_sprint"](board_name="Team", sprint_name="Sprint 1")
        assert "Nothing to update" in out
        client.post.assert_not_called()

    async def test_archived_false_is_a_change_not_an_absence(self):
        """`archived=False` must reach the server; only None means 'keep'."""
        client, tools = _tools([_board()])
        await tools["update_sprint"](
            board_name="Team", sprint_name="Sprint 1", archived=False
        )
        assert client.post.await_args.kwargs["json"] == {"archived": False}

    async def test_ambiguous_sprint_blocks_the_write(self):
        client, tools = _tools([_board(sprint_names=("Sprint 10", "Sprint 11"))])
        out = await tools["update_sprint"](
            board_name="Team", sprint_name="Sprint 1", new_name="X"
        )
        assert "Multiple sprints match" in out
        client.post.assert_not_called()


class TestAddIssuesToSprint:
    async def test_transport_failure_does_not_abort_the_batch(self):
        """One rejected issue must not discard the report for the rest.

        Only ValueError was caught, so an HTTP error propagated and the
        partial result — which issues did land — was lost.
        """
        request = httpx.Request("POST", "https://example.invalid/api")
        response = httpx.Response(500, request=request)

        async def execute(issue_id, command):
            if issue_id == "PROJ-2":
                raise httpx.HTTPStatusError("boom", request=request, response=response)
            return {}

        _, tools = _tools([_board()], execute=execute)
        out = await tools["add_issues_to_sprint"](
            board_name="Team", sprint_name="Sprint 1", issue_ids="PROJ-1,PROJ-2,PROJ-3"
        )
        assert "PROJ-1" in out and "PROJ-3" in out
        assert "Failed (1)" in out and "PROJ-2" in out

    async def test_command_error_is_reported_per_issue(self):
        async def execute(issue_id, command):
            if issue_id == "PROJ-2":
                raise ValueError("workflow refused")
            return {}

        _, tools = _tools([_board()], execute=execute)
        out = await tools["add_issues_to_sprint"](
            board_name="Team", sprint_name="Sprint 1", issue_ids="PROJ-1,PROJ-2"
        )
        assert "Added (1)" in out and "Failed (1)" in out

    async def test_blank_and_padded_ids_are_cleaned(self):
        client, tools = _tools([_board()])
        await tools["add_issues_to_sprint"](
            board_name="Team", sprint_name="Sprint 1", issue_ids=" PROJ-1 , ,PROJ-2,"
        )
        called = [c.args[0] for c in client.execute_command.await_args_list]
        assert called == ["PROJ-1", "PROJ-2"]

    async def test_no_usable_ids_writes_nothing(self):
        client, tools = _tools([_board()])
        out = await tools["add_issues_to_sprint"](
            board_name="Team", sprint_name="Sprint 1", issue_ids=" , ,"
        )
        assert "No issue IDs" in out
        client.execute_command.assert_not_called()
