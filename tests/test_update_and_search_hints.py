"""update_issue restore hints, assignee resolution, free-text search hint (ADR-058).

Three small defects in how the server talks to its caller: a restore hint
printed for a write that never happened, a display name rejected with an
error echoing the name back, and a broad free-text match presented with the
same confidence as a scoped query.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
from mcp.server.fastmcp import FastMCP

from yt_mcp.tools import issues


def _issue(assignee="Alice A", summary="Original title", description="old body"):
    return {
        "idReadable": "PROJ-1",
        "summary": summary,
        "description": description,
        "state": {"name": "Open"},
        "assignee": {"name": assignee} if assignee else None,
        "tags": [],
        "customFields": [],
    }


def _tools(before, after, users=None, execute=None):
    """A client whose issue reads return `before`, then `after`."""
    reads = iter([before, after])

    async def get(path, params=None, **kw):
        if path.startswith("/api/users"):
            if isinstance(users, Exception):
                raise users
            return users if users is not None else []
        return next(reads)

    client = MagicMock()
    client.get = AsyncMock(side_effect=get)
    client.post = AsyncMock(return_value={})
    client.execute_command = AsyncMock(side_effect=execute) if execute else AsyncMock(return_value={})
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    issues.register(mcp, resolver)
    return client, {n: t.fn for n, t in mcp._tool_manager._tools.items()}


class TestRestoreHintOnlyForRealChanges:
    """A restore hint is a claim that something changed."""

    async def test_rejected_assignee_prints_no_restore_hint(self):
        async def execute(issue_id, command):
            raise ValueError("Assignee expected: Bob B")

        _, tools = _tools(_issue("Alice A"), _issue("Alice A"), execute=execute)
        out = await tools["update_issue"](issue_id="PROJ-1", assignee="bob_b")
        assert "No field changes detected" in out
        assert "To restore" not in out, (
            "a restore hint next to 'no changes' reads as a pending write"
        )

    async def test_real_assignee_change_offers_the_previous_value(self):
        _, tools = _tools(_issue("Alice A"), _issue("Bob B"))
        out = await tools["update_issue"](issue_id="PROJ-1", assignee="bob_b")
        assert "To restore" in out
        assert 'assignee="Alice A"' in out

    async def test_summary_set_to_its_current_value_offers_nothing(self):
        _, tools = _tools(_issue(summary="Same"), _issue(summary="Same"))
        out = await tools["update_issue"](issue_id="PROJ-1", summary="Same")
        assert "To restore" not in out

    async def test_summary_change_offers_the_old_title(self):
        _, tools = _tools(_issue(summary="Old"), _issue(summary="New"))
        out = await tools["update_issue"](issue_id="PROJ-1", summary="New")
        assert 'summary="Old"' in out

    async def test_unchanged_description_offers_nothing(self):
        _, tools = _tools(_issue(description="same"), _issue(description="same"))
        out = await tools["update_issue"](issue_id="PROJ-1", description="same")
        assert "previous description" not in out


class TestAssigneeLogin:
    """The command grammar wants a login; callers have display names."""

    async def test_unique_full_name_resolves_to_login(self):
        client = MagicMock()
        client.get = AsyncMock(return_value=[
            {"login": "alice_a", "fullName": "Alice A", "name": "Alice A"},
        ])
        assert await issues._assignee_login(client, "Alice A") == "alice_a"

    async def test_match_is_case_insensitive(self):
        client = MagicMock()
        client.get = AsyncMock(return_value=[{"login": "alice_a", "fullName": "Alice A"}])
        assert await issues._assignee_login(client, "alice a") == "alice_a"

    async def test_login_passes_through_without_a_lookup(self):
        client = MagicMock()
        client.get = AsyncMock()
        assert await issues._assignee_login(client, "alice_a") == "alice_a"
        client.get.assert_not_called()

    async def test_me_passes_through(self):
        client = MagicMock()
        client.get = AsyncMock()
        assert await issues._assignee_login(client, "me") == "me"
        client.get.assert_not_called()

    async def test_ambiguous_name_is_not_guessed(self):
        """Two people with the same name: picking one would assign the
        issue to someone the caller did not mean."""
        client = MagicMock()
        client.get = AsyncMock(return_value=[
            {"login": "alice_a", "fullName": "Alice A"},
            {"login": "alice_a2", "fullName": "Alice A"},
        ])
        assert await issues._assignee_login(client, "Alice A") == "Alice A"

    async def test_partial_match_is_not_substituted(self):
        client = MagicMock()
        client.get = AsyncMock(return_value=[{"login": "alice_ab", "fullName": "Alice A B"}])
        assert await issues._assignee_login(client, "Alice A") == "Alice A"

    async def test_unreadable_directory_falls_back_to_the_input(self):
        request = httpx.Request("GET", "https://example.invalid/api/users")
        err = httpx.HTTPStatusError(
            "forbidden", request=request, response=httpx.Response(403, request=request)
        )
        client = MagicMock()
        client.get = AsyncMock(side_effect=err)
        assert await issues._assignee_login(client, "Alice A") == "Alice A"

    async def test_update_issue_sends_the_login(self):
        users = [{"login": "bob_b", "fullName": "Bob B"}]
        client, tools = _tools(_issue("Alice A"), _issue("Bob B"), users=users)
        await tools["update_issue"](issue_id="PROJ-1", assignee="Bob B")
        sent = " ".join(c.args[1] for c in client.execute_command.await_args_list)
        assert "Assignee bob_b" in sent
        assert "Assignee Bob B" not in sent


class TestFreeTextSearchHint:
    def _tools(self, n):
        rows = [{"idReadable": f"PROJ-{i}", "summary": f"s{i}"} for i in range(n)]
        client = MagicMock()
        client.get = AsyncMock(return_value=rows)
        resolver = MagicMock()
        resolver.resolve = MagicMock(return_value=client)
        mcp = FastMCP("test")
        issues.register(mcp, resolver)
        return mcp._tool_manager._tools["search_issues"].fn

    async def test_broad_bare_phrase_gets_a_hint(self):
        out = await self._tools(25)(query="upload timeout")
        assert "free-text match" in out
        assert "project: KEY" in out

    async def test_scoped_query_gets_no_hint(self):
        out = await self._tools(25)(query="project: PROJ upload")
        assert "free-text match" not in out

    async def test_tag_query_gets_no_hint(self):
        out = await self._tools(25)(query="#urgent")
        assert "free-text match" not in out

    async def test_narrow_bare_phrase_gets_no_hint(self):
        """A handful of results from a phrase is probably the answer."""
        out = await self._tools(3)(query="upload timeout")
        assert "free-text match" not in out

    def test_operator_detection(self):
        assert issues._HAS_OPERATOR.search("project: PROJ")
        assert issues._HAS_OPERATOR.search("State:{Open}")
        assert issues._HAS_OPERATOR.search("#tag")
        assert issues._HAS_OPERATOR.search("created: {Last week}")
        assert not issues._HAS_OPERATOR.search("upload timeout on login")
