"""Tests for find_comments and service-comment filtering (ADR-037)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from yt_mcp.errors import UserInputError
from yt_mcp.formatters import split_service_comments
from yt_mcp.tools import comments as comments_mod


def _comment(text, login="alice", name="Alice A", created=1747584000000):
    return {"text": text, "author": {"login": login, "name": name}, "created": created}


def _issue(iid, summary, comments):
    return {"idReadable": iid, "summary": summary, "comments": comments}


def _setup(response):
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    comments_mod.register(mcp, resolver)
    return client, mcp._tool_manager._tools["find_comments"].fn


class TestSplitServiceComments:
    def test_workflow_bot_and_service_stamps_hidden(self):
        real, hidden = split_service_comments([
            _comment("real analysis"),
            _comment("Deadline missed, please comment!", login="workflow_user_123"),
            _comment("[yt-mcp] Translated. Batch: b-1"),
        ])
        assert [c["text"] for c in real] == ["real analysis"]
        assert hidden == 2

    def test_humans_kept_and_empty_safe(self):
        real, hidden = split_service_comments([])
        assert real == [] and hidden == 0
        real, hidden = split_service_comments([_comment("hello")])
        assert len(real) == 1 and hidden == 0


class TestFindComments:
    async def test_phrase_match_returns_issue_author_snippet(self):
        client, fn = _setup([
            _issue("PROJ-9", "Server research", [
                _comment("Decision: use cheaper servers in region B for now."),
            ]),
        ])
        out = await fn(text="cheaper servers")
        assert "PROJ-9" in out
        assert "Alice A" in out
        assert "cheaper servers in region B" in out

    async def test_all_words_fallback_when_phrase_not_verbatim(self):
        client, fn = _setup([
            _issue("PROJ-2", "Infra", [
                _comment("servers there are considerably cheaper than expected"),
            ]),
        ])
        out = await fn(text="cheaper servers")
        assert "PROJ-2" in out

    async def test_non_matching_comment_excluded(self):
        client, fn = _setup([
            _issue("PROJ-3", "Misc", [_comment("totally unrelated note")]),
        ])
        out = await fn(text="cheaper servers")
        assert "No comments" in out

    async def test_author_filters_comments_and_query(self):
        client, fn = _setup([
            _issue("PROJ-4", "Infra", [
                _comment("cheaper option chosen", login="alice"),
                _comment("cheaper option rejected", login="bob", name="Bob B"),
            ]),
        ])
        out = await fn(text="cheaper option", author="alice")
        assert "Alice A" in out and "Bob B" not in out
        query = client.get.call_args.kwargs["params"]["query"]
        assert "commenter: alice" in query

    async def test_service_comments_never_match(self):
        client, fn = _setup([
            _issue("PROJ-5", "Infra", [
                _comment("[yt-mcp] cheaper servers stamp"),
                _comment("cheaper nag", login="workflow_user_9"),
            ]),
        ])
        out = await fn(text="cheaper")
        assert "No comments" in out

    async def test_newest_first_and_max_results(self):
        client, fn = _setup([
            _issue("PROJ-6", "Infra", [
                _comment("cheaper v1", created=1000000000000),
                _comment("cheaper v2", created=1747584000000),
                _comment("cheaper v3", created=1500000000000),
            ]),
        ])
        out = await fn(text="cheaper", max_results=2)
        assert out.index("cheaper v2") < out.index("cheaper v3")
        assert "cheaper v1" not in out
        assert "1 more" in out

    async def test_empty_text_rejected(self):
        client, fn = _setup([])
        with pytest.raises(UserInputError, match="text is required"):
            await fn(text="   ")

    async def test_project_clause_in_query(self):
        client, fn = _setup([])
        await fn(text="anything", project="PROJ")
        query = client.get.call_args.kwargs["params"]["query"]
        assert query.startswith("project: PROJ")
