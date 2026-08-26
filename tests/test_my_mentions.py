"""Tests for get_my_mentions (ADR-043). Generic fixtures only."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from yt_mcp.errors import UserInputError
from yt_mcp.tools import comments as comments_mod

NOW = datetime.now(timezone.utc)  # tool windows from real now


def _ms(days_ago):
    return int((NOW - timedelta(days=days_ago)).timestamp() * 1000)


def _comment(text, login="bob_b", name="Bob B", days_ago=1):
    return {"text": text, "author": {"login": login, "name": name},
            "created": _ms(days_ago)}


def _issue(iid, comments, state="Open"):
    return {
        "idReadable": iid, "summary": f"Task {iid}",
        "customFields": [{"name": "State", "value": {"name": state}}],
        "comments": comments,
    }


def _setup(issues):
    client = MagicMock()
    async def _get(path, params=None):
        if path == "/api/users/me":
            return {"login": "alice_a", "name": "Alice A"}
        return issues
    client.get = AsyncMock(side_effect=_get)
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    comments_mod.register(mcp, resolver)
    return client, mcp._tool_manager._tools["get_my_mentions"].fn


class TestMentions:
    async def test_mention_by_login_and_name_variants(self):
        client, fn = _setup([
            _issue("PROJ-1", [_comment("ping @AliceA please review")]),
            _issue("PROJ-2", [_comment("cc @alice_a on this")]),
        ])
        out = await fn()
        assert "PROJ-1" in out and "PROJ-2" in out
        assert "Mentions (2)" in out

    async def test_own_comments_never_reported(self):
        client, fn = _setup([
            _issue("PROJ-3", [_comment("note to self @alice_a", login="alice_a", name="Alice A")]),
        ])
        out = await fn()
        assert "No mentions or replies" in out

    async def test_fyi_template_filtered_as_noise(self):
        client, fn = _setup([
            _issue("PROJ-4", [_comment("deadline missed 🚨 FYI @AliceA ☝️")]),
        ])
        out = await fn()
        assert "No mentions or replies" in out
        assert "1 notification/bot pings filtered" in out

    async def test_workflow_bot_authors_filtered(self):
        client, fn = _setup([
            _issue("PROJ-5", [_comment("@AliceA look", login="workflow_user_1", name="Bot")]),
        ])
        out = await fn()
        assert "No mentions or replies" in out

    async def test_old_comments_outside_window_excluded(self):
        client, fn = _setup([
            _issue("PROJ-6", [_comment("hey @AliceA", days_ago=40)]),
        ])
        out = await fn(days=14)
        assert "No mentions or replies" in out


class TestReplies:
    async def test_comment_after_mine_is_possible_reply(self):
        client, fn = _setup([
            _issue("PROJ-7", [
                _comment("my question here", login="alice_a", name="Alice A", days_ago=3),
                _comment("here is the answer", days_ago=1),
            ]),
        ])
        out = await fn()
        assert "Possible replies after my comment (1)" in out
        assert "here is the answer" in out

    async def test_comment_before_mine_is_not_a_reply(self):
        client, fn = _setup([
            _issue("PROJ-8", [
                _comment("earlier context", days_ago=5),
                _comment("my later note", login="alice_a", name="Alice A", days_ago=2),
            ]),
        ])
        out = await fn()
        assert "No mentions or replies" in out

    async def test_mention_wins_over_reply_classification(self):
        client, fn = _setup([
            _issue("PROJ-9", [
                _comment("my ask", login="alice_a", name="Alice A", days_ago=3),
                _comment("@AliceA done", days_ago=1),
            ]),
        ])
        out = await fn()
        assert "Mentions (1)" in out
        assert "Possible replies" not in out


class TestGuards:
    async def test_invalid_days_rejected(self):
        client, fn = _setup([])
        with pytest.raises(UserInputError, match="days must be"):
            await fn(days=0)

    async def test_queries_use_mentions_and_commenter(self):
        client, fn = _setup([])
        await fn(days=7)
        queries = [c.kwargs["params"]["query"] for c in client.get.call_args_list
                   if c.args and c.args[0] == "/api/issues"]
        assert any(q.startswith("mentions: me") for q in queries)
        assert any(q.startswith("commenter: me") for q in queries)
