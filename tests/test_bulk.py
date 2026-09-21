"""Bulk update/rollback — the highest blast radius in the server (ADR-053).

`bulk_update_execute` rewrites a field across up to 100 issues, and
`bulk_rollback` is the only way back. Both were at 10% coverage, and
reading the uncovered lines found three defects in the undo path alone.
These tests pin the dangerous semantics: what the rollback window spans,
when the batch tag may be removed, and what the tool says when it
reverted nothing.
"""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from yt_mcp.tools import bulk

MINUTE = 60_000


def _tools(client):
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    bulk.register(mcp, resolver)
    return {n: t.fn for n, t in mcp._tool_manager._tools.items()}


def _client(issues=None, activities=None):
    client = MagicMock()
    client.get = AsyncMock(side_effect=lambda path, params=None, **kw: (
        activities if "/activities" in path else (issues if issues is not None else [])
    ))
    client.execute_command = AsyncMock(return_value={})
    client.post = AsyncMock(return_value={})
    client.delete = AsyncMock(return_value={})
    client.update_comment = AsyncMock(return_value={})
    return client


def _activity(ts, field="State", removed_name="Open"):
    return {
        "id": "a1", "timestamp": ts, "field": {"name": field},
        "added": [{"name": "Closed"}], "removed": [{"name": removed_name}],
    }


def _tag_activity(ts):
    return {"id": "t1", "timestamp": ts, "field": {"name": "tag"},
            "added": [{"name": "yt-mcp-1"}], "removed": []}


class TestBatchTagValidation:
    """The tag is the handle on a destructive batch; it is parsed, so it
    is validated rather than trusted."""

    @pytest.mark.parametrize("tag", [
        "yt-mcp-1758400000", "yt-translate-1758400000",
    ])
    def test_accepts_well_formed_tags(self, tag):
        assert bulk._validate_batch_tag(tag) is None

    @pytest.mark.parametrize("tag", [
        "yt-mcp-123",           # too few digits
        "yt-other-1758400000",  # unknown prefix
        "yt-mcp-",              # no timestamp
        "'; drop",              # not a tag at all
        "yt-mcp-1758400000 or 1=1",
    ])
    def test_rejects_anything_else(self, tag):
        assert bulk._validate_batch_tag(tag) is not None

    async def test_rollback_refuses_a_bad_tag_before_querying(self):
        client = _client()
        out = await _tools(client)["bulk_rollback"](batch_tag="not-a-tag")
        assert "Invalid batch tag" in out
        client.get.assert_not_called()


class TestRollbackWindow:
    """The window must outlive the batch that created it.

    Execute issues two sequential commands per issue, so a 100-issue batch
    is ~200 round trips. A one-minute window could not contain it, and
    everything after the first minute was silently left changed.
    """

    async def _rollback(self, change_offset_ms):
        now = int(time.time())
        tag = f"yt-mcp-{now}"
        base = now * 1000
        client = _client(
            issues=[{"idReadable": "PROJ-1", "summary": "s"}],
            activities=[_tag_activity(base), _activity(base + change_offset_ms)],
        )
        tools = _tools(client)
        return await tools["bulk_rollback"](batch_tag=tag), client

    async def test_change_inside_the_first_minute_is_reverted(self):
        out, client = await self._rollback(30 * 1000)
        assert "**Changes reverted:** 1" in out
        client.execute_command.assert_awaited()

    async def test_change_five_minutes_in_is_still_reverted(self):
        """The regression: this is a normal large batch, not an edge case."""
        out, _ = await self._rollback(5 * MINUTE)
        assert "**Changes reverted:** 1" in out

    async def test_change_long_after_the_window_is_not_reverted(self):
        """The window is generous, not unbounded — unrelated later edits by
        other people must not be undone by a rollback."""
        out, _ = await self._rollback(90 * MINUTE)
        assert "**Changes reverted:** 0" in out


class TestTagIsNotRemovedWhenNothingWasReverted:
    """The tag is the only record of which issues a batch touched."""

    async def test_untouched_issue_keeps_its_tag(self):
        now = int(time.time())
        client = _client(
            issues=[{"idReadable": "PROJ-1", "summary": "s"}],
            activities=[_tag_activity(now * 1000),
                        _activity(now * 1000 + 90 * MINUTE)],
        )
        tools = _tools(client)
        out = await tools["bulk_rollback"](batch_tag=f"yt-mcp-{now}")
        untags = [c for c in client.execute_command.await_args_list
                  if "untag" in str(c)]
        assert not untags, "untagged an issue whose change was never reverted"
        assert "left tagged" in out

    async def test_reverted_issue_is_untagged(self):
        now = int(time.time())
        client = _client(
            issues=[{"idReadable": "PROJ-1", "summary": "s"}],
            activities=[_tag_activity(now * 1000), _activity(now * 1000 + 1000)],
        )
        tools = _tools(client)
        await tools["bulk_rollback"](batch_tag=f"yt-mcp-{now}")
        untags = [c for c in client.execute_command.await_args_list
                  if "untag" in str(c)]
        assert untags, "a fully reverted issue must drop the batch tag"


class TestRollbackNeverReportsSilentSuccess:
    async def test_zero_reverted_is_called_out(self):
        now = int(time.time())
        client = _client(
            issues=[{"idReadable": "PROJ-1", "summary": "s"}],
            activities=[_tag_activity(now * 1000),
                        _activity(now * 1000 + 90 * MINUTE)],
        )
        out = await _tools(client)["bulk_rollback"](batch_tag=f"yt-mcp-{now}")
        assert "Nothing was reverted" in out
        assert "do not read this as a successful undo" in out

    async def test_missing_batch_says_so(self):
        client = _client(issues=[])
        out = await _tools(client)["bulk_rollback"](batch_tag="yt-mcp-1758400000")
        assert "No issues found" in out


class TestPreviewAndExecuteAgree:
    """Preview exists so an operator can consent to a mass mutation; it
    must describe the same set execute will touch."""

    async def test_preview_makes_no_writes(self):
        client = _client(issues=[{"idReadable": "PROJ-1", "summary": "s"}])
        out = await _tools(client)["bulk_update_preview"](query="project: PROJ", command="State Closed")
        client.execute_command.assert_not_called()
        client.post.assert_not_called()
        assert "PROJ-1" in out

    async def test_preview_and_execute_use_the_same_cap(self):
        client = _client(issues=[])
        tools = _tools(client)
        await tools["bulk_update_preview"](query="q", command="c", max_results=5000)
        preview_top = client.get.await_args.kwargs["params"]["$top"]
        client.get.reset_mock()
        await tools["bulk_update_execute"](query="q", command="c", max_results=5000)
        execute_top = client.get.await_args.kwargs["params"]["$top"]
        assert preview_top == execute_top == str(bulk.MAX_BULK_RESULTS)

    async def test_empty_match_writes_nothing(self):
        client = _client(issues=[])
        out = await _tools(client)["bulk_update_execute"](query="project: NONE", command="State Closed")
        assert "No issues match" in out
        client.execute_command.assert_not_called()


class TestExecutePartialFailure:
    async def test_a_failed_tag_excludes_that_issue_from_the_command(self):
        """An issue that could not be tagged must not be mutated: it would
        be changed with no way to find it again."""
        import httpx

        client = _client(issues=[{"idReadable": "PROJ-1", "summary": "a"},
                                 {"idReadable": "PROJ-2", "summary": "b"}])

        async def cmd(issue_id, command):
            if command.startswith("tag") and issue_id == "PROJ-1":
                raise ValueError("tag refused")
            return {}

        client.execute_command = AsyncMock(side_effect=cmd)
        out = await _tools(client)["bulk_update_execute"](query="q", command="State Closed")
        applied = [c.args for c in client.execute_command.await_args_list
                   if not str(c.args[1]).startswith("tag")]
        assert all(a[0] != "PROJ-1" for a in applied), "mutated an untagged issue"
        assert "PROJ-1" in out and "tag failed" in out

    async def test_command_failures_are_reported_per_issue(self):
        client = _client(issues=[{"idReadable": "PROJ-1", "summary": "a"},
                                 {"idReadable": "PROJ-2", "summary": "b"}])

        async def cmd(issue_id, command):
            if command == "State Closed" and issue_id == "PROJ-2":
                raise ValueError("workflow refused")
            return {}

        client.execute_command = AsyncMock(side_effect=cmd)
        out = await _tools(client)["bulk_update_execute"](query="q", command="State Closed")
        assert "**Updated:** 1 issues" in out
        assert "PROJ-2" in out and "command failed" in out

    async def test_batch_tag_is_reported_for_undo(self):
        client = _client(issues=[{"idReadable": "PROJ-1", "summary": "a"}])
        out = await _tools(client)["bulk_update_execute"](query="q", command="State Closed")
        assert "bulk_rollback" in out
        assert bulk._validate_batch_tag(
            out.split("**Batch tag:** `")[1].split("`")[0]
        ) is None, "execute advertised a tag its own validator would reject"
