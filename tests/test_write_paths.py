"""Regression tests for the ADR-042 write-path audit fixes.

Mocks mirror REAL YouTrack behavior — most importantly the default POST
serialization: without a `fields` selector the response is ONLY
{"$type", "id"} (Q16). Mocking richer defaults is exactly how the
"Created: **?**" bug survived.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from yt_mcp.client import YouTrackClient
from yt_mcp.config import YouTrackConfig
from yt_mcp.resolver import InstanceResolver
from yt_mcp.tools.issues import register as _register_issues


def _get_tool_fn(mcp, name):
    return mcp._tool_manager._tools[name].fn


def _make(client):
    mcp = FastMCP("test")
    resolver = MagicMock(spec=InstanceResolver)
    resolver.resolve = MagicMock(return_value=client)
    _register_issues(mcp, resolver)
    return mcp, resolver


def _honest_post(commands_seen=None, publish_fails=False):
    """POST mock with REAL default serialization: fields= → rich, else $type,id."""
    async def _post(path, json=None):
        if path.startswith("/api/issues?draftId="):
            if publish_fails:
                raise ValueError("YouTrack query error (400): required field missing")
            return {"idReadable": "PROJ-7", "summary": "t"}
        if path.startswith("/api/issues"):
            if "fields=" in path and "idReadable" in path:
                return {"$type": "Issue", "id": "3-1", "idReadable": "PROJ-7", "summary": "t"}
            return {"$type": "Issue", "id": "3-1"}  # Q16: the real default
        if path == "/api/users/me/drafts":
            return {"id": "87-500"}
        if path == "/api/commands":
            if commands_seen is not None:
                commands_seen.append((json["query"], json["issues"][0]))
            return {}
        return {}
    return _post


class TestCreateResponseFields:
    """Fix 1 / Q16: direct create must request idReadable explicitly."""

    async def test_direct_create_returns_real_id_not_question_mark(self):
        client = MagicMock()
        client.resolve_project_id = AsyncMock(return_value="0-5")
        seen: list = []
        client.post = AsyncMock(side_effect=_honest_post(seen))
        client.get = AsyncMock(return_value=[])
        mcp, _ = _make(client)
        out = await _get_tool_fn(mcp, "create_issue")(
            project="PROJ", summary="t", product="Alpha",
        )
        assert "PROJ-7" in out
        assert "**?**" not in out
        # ...and the follow-up command targeted the real id
        assert seen and seen[0][1] == {"idReadable": "PROJ-7"}

    async def test_create_request_carries_fields_selector(self):
        client = MagicMock()
        client.resolve_project_id = AsyncMock(return_value="0-5")
        client.post = AsyncMock(side_effect=_honest_post())
        client.get = AsyncMock(return_value=[])
        mcp, _ = _make(client)
        await _get_tool_fn(mcp, "create_issue")(project="PROJ", summary="t")
        path = client.post.call_args_list[0].args[0]
        assert "fields=" in path and "idReadable" in path


class TestDraftOrphanCleanup:
    """Fix 3: failed publish must discard the draft."""

    async def test_publish_failure_deletes_draft(self):
        client = MagicMock()
        client.resolve_project_id = AsyncMock(return_value="0-5")

        calls = {"n": 0}
        async def _post(path, json=None):
            if path.startswith("/api/issues?draftId="):
                raise ValueError("YouTrack query error (400): still missing required")
            if path.startswith("/api/issues"):
                raise ValueError("YouTrack query error (400): required field Subsystem")
            if path == "/api/users/me/drafts":
                return {"id": "87-500"}
            return {}
        client.post = AsyncMock(side_effect=_post)
        client.get = AsyncMock(return_value=[])
        client.delete = AsyncMock(return_value={})
        mcp, _ = _make(client)
        out = await _get_tool_fn(mcp, "create_issue")(
            project="PROJ", summary="t", command="Subsystem Core",
        )
        assert "Could not create issue" in out
        client.delete.assert_awaited_once_with("/api/users/me/drafts/87-500")


class TestTransitionInstanceRouting:
    """Fix 2: transition_issue must pass the issue id for URL-based routing."""

    async def test_resolver_receives_issue_identifier(self):
        client = MagicMock()
        client.get = AsyncMock(return_value={
            "idReadable": "PROJ-1", "summary": "t", "project": {"id": "0-5"},
            "customFields": [{"name": "State", "value": {"name": "Open"}}],
        })
        client.post = AsyncMock(return_value={})
        mcp, resolver = _make(client)
        url = "https://example.myjetbrains.com/youtrack/issue/PROJ-1"
        await _get_tool_fn(mcp, "transition_issue")(issue_id=url, state="Closed")
        args = resolver.resolve.call_args.args
        assert len(args) == 2 and args[1] == url


class TestSoftDeleteStateField:
    """Fix 4: soft delete respects Status-named state fields and fails clean."""

    async def test_status_field_project_uses_status_command(self):
        client = MagicMock()
        client.get = AsyncMock(return_value={
            "idReadable": "PROJ-9", "summary": "t",
            "customFields": [{"name": "Status", "value": {"name": "Open"}}],
        })
        client.execute_command = AsyncMock(return_value=None)
        mcp, _ = _make(client)
        out = await _get_tool_fn(mcp, "delete_issue")(issue_id="PROJ-9")
        client.execute_command.assert_awaited_once_with("PROJ-9", "Status Obsolete")
        assert "Soft-deleted" in out and "Status" in out

    async def test_missing_obsolete_value_returns_clean_message(self):
        client = MagicMock()
        client.get = AsyncMock(return_value={
            "idReadable": "PROJ-9", "summary": "t",
            "customFields": [{"name": "State", "value": {"name": "Open"}}],
        })
        client.execute_command = AsyncMock(
            side_effect=ValueError("YouTrack query error (400): Obsolete expected")
        )
        mcp, _ = _make(client)
        out = await _get_tool_fn(mcp, "delete_issue")(issue_id="PROJ-9")
        assert "Could not soft-delete" in out
        assert "transition_issue" in out


class TestBracesNeverReachYouTrack:
    """Fix 5 / ADR-019 invariant enforced at the client choke point."""

    async def test_execute_command_strips_braces(self):
        cfg = YouTrackConfig(token="t", url="https://example.invalid")
        client = YouTrackClient(cfg)
        client.post = AsyncMock(return_value={})
        await client.execute_command("PROJ-1", "State {In Progress} tag {my tag}")
        q = client.post.call_args.kwargs["json"]["query"]
        assert "{" not in q and "}" not in q
        assert q == "State In Progress tag my tag"


class TestUpdateIssueDiffFixes:
    """Fixes 6+7: description visible in diff/rollback; tag order ≠ change."""

    def _client(self, before_tags, after_tags, old_desc="old text"):
        client = MagicMock()
        state = {"n": 0}
        async def _get(path, params=None):
            state["n"] += 1
            tags = before_tags if state["n"] == 1 else after_tags
            return {
                "idReadable": "PROJ-3", "summary": "s",
                "description": old_desc,
                "state": {"name": "Open"}, "assignee": {"name": "Alice A"},
                "tags": [{"name": t} for t in tags],
                "customFields": [],
            }
        client.get = AsyncMock(side_effect=_get)
        client.post = AsyncMock(return_value={})
        client.execute_command = AsyncMock(return_value=None)
        return client

    async def test_description_change_shown_and_rollback_hinted(self):
        client = self._client(["a"], ["a"])
        mcp, _ = _make(client)
        out = await _get_tool_fn(mcp, "update_issue")(
            issue_id="PROJ-3", description="brand new text",
        )
        assert "**Description:** updated" in out
        assert "rollback_issue" in out

    async def test_reordered_tags_not_reported_as_change(self):
        client = self._client(["beta", "alpha"], ["alpha", "beta"])
        mcp, _ = _make(client)
        out = await _get_tool_fn(mcp, "update_issue")(issue_id="PROJ-3", summary="s2")
        assert "**Tags:**" not in out

    async def test_braced_explicit_state_still_applies(self):
        # Braces from create_issue's input convention must not leak to YT.
        client = self._client(["a"], ["a"])
        sent: list = []
        async def _post(path, json=None):
            if path == "/api/commands":
                q = json["query"]
                sent.append(q)
                if "{" in q or "}" in q:
                    raise ValueError("YouTrack query error (400): braces")
            return {}
        client.post = AsyncMock(side_effect=_post)
        async def _exec(issue_id, command):
            # real client now strips braces at the choke point
            if "{" in command or "}" in command:
                command = command.replace("{", "").replace("}", "")
            sent.append(command)
        client.execute_command = AsyncMock(side_effect=_exec)
        mcp, _ = _make(client)
        await _get_tool_fn(mcp, "update_issue")(
            issue_id="PROJ-3", state="{In Progress}",
        )
        assert sent and all("{" not in q and "}" not in q for q in sent)
