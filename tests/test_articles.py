"""Knowledge Base writes (ADR-054).

Six write tools, two of them destructive, at 13% coverage. The delete
path matters most: the message it prints is the only record of what was
removed, so what that message omits is what is actually lost.
"""

from unittest.mock import AsyncMock, MagicMock

from mcp.server.fastmcp import FastMCP

from yt_mcp.tools import articles


def _tools(get=None, post=None):
    client = MagicMock()
    client.get = AsyncMock(return_value=get if get is not None else {})
    client.post = AsyncMock(return_value=post if post is not None else {"id": "1-1"})
    client.delete = AsyncMock(return_value={})
    client.resolve_project_id = AsyncMock(return_value="0-1")
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    articles.register(mcp, resolver)
    return client, {n: t.fn for n, t in mcp._tool_manager._tools.items()}


class TestCreateArticle:
    async def test_post_requests_a_readable_id(self):
        """Q16: without a selector the response carries only the internal
        id, so the caller is handed something it cannot use to navigate."""
        client, tools = _tools(post={"idReadable": "PROJ-A-1", "summary": "Runbook"})
        await tools["create_article"](project="PROJ", summary="Runbook")
        path = client.post.await_args.args[0]
        assert "fields=" in path and "idReadable" in path

    async def test_reports_the_readable_id_when_present(self):
        _, tools = _tools(post={"idReadable": "PROJ-A-1", "summary": "Runbook"})
        out = await tools["create_article"](project="PROJ", summary="Runbook")
        assert "PROJ-A-1" in out

    async def test_unknown_project_writes_nothing(self):
        client, tools = _tools()
        client.resolve_project_id = AsyncMock(return_value=None)
        out = await tools["create_article"](project="GHOST", summary="x")
        assert "not found" in out
        client.post.assert_not_called()

    async def test_parent_is_sent_only_when_given(self):
        client, tools = _tools()
        await tools["create_article"](project="PROJ", summary="x")
        assert "parentArticle" not in client.post.await_args.kwargs["json"]
        await tools["create_article"](project="PROJ", summary="x", parent_article_id="1-9")
        assert client.post.await_args.kwargs["json"]["parentArticle"] == {"id": "1-9"}


class TestDeleteArticleTellsTheTruthAboutWhatIsLost:
    """The delete message is the only surviving copy of the body."""

    def _article(self, content):
        return {
            "idReadable": "PROJ-A-1", "summary": "Runbook",
            "content": content, "project": {"shortName": "PROJ"},
        }

    async def test_short_article_is_reproduced_in_full(self):
        body = "step one\nstep two"
        _, tools = _tools(get=self._article(body))
        out = await tools["delete_article"](article_id="PROJ-A-1")
        assert body in out
        assert "truncated" not in out.lower()
        assert "To restore" in out

    async def test_long_article_says_what_it_dropped(self):
        """Previously cut at 500 characters under a heading promising the
        output was enough to restore — a silent, unrecoverable loss."""
        body = "x" * (articles._RESTORE_CONTENT_LIMIT + 250)
        _, tools = _tools(get=self._article(body))
        out = await tools["delete_article"](article_id="PROJ-A-1")
        assert "250 more character(s) are NOT shown" in out
        assert "not a backup" in out
        assert "only the shown portion" in out

    def test_the_limit_is_large_enough_to_be_worth_printing(self):
        """Pinned separately from the behaviour tests above.

        Those build their fixture from the constant, so they verify that
        truncation is *reported honestly* at any limit — including a limit
        so small the message is useless. This pins the value itself: 500
        characters, the old behaviour, is a sentence, not an article.
        """
        assert articles._RESTORE_CONTENT_LIMIT >= 2000

    async def test_boundary_length_is_not_reported_as_truncated(self):
        body = "y" * articles._RESTORE_CONTENT_LIMIT
        _, tools = _tools(get=self._article(body))
        out = await tools["delete_article"](article_id="PROJ-A-1")
        assert "NOT shown" not in out

    async def test_the_delete_actually_happens(self):
        client, tools = _tools(get=self._article("short"))
        await tools["delete_article"](article_id="PROJ-A-1")
        client.delete.assert_awaited_once()
        assert "PROJ-A-1" in client.delete.await_args.args[0]

    async def test_snapshot_is_taken_before_the_delete(self):
        """Reading after the delete would return nothing to report."""
        order = []
        client, tools = _tools(get=self._article("short"))
        client.get = AsyncMock(side_effect=lambda *a, **k: (
            order.append("get"), self._article("short"))[1])
        client.delete = AsyncMock(side_effect=lambda *a, **k: order.append("delete"))
        await tools["delete_article"](article_id="PROJ-A-1")
        assert order == ["get", "delete"]


class TestArticleComments:
    async def test_update_comment_reports_previous_text_for_rollback(self):
        client, tools = _tools(get={"id": "c1", "text": "old wording"})
        out = await tools["update_article_comment"](
            article_id="PROJ-A-1", comment_id="c1", text="new wording"
        )
        assert "old wording" in out
        client.post.assert_awaited()

    async def test_delete_comment_reports_what_it_removed(self):
        client, tools = _tools(get={"id": "c1", "text": "the removed text"})
        out = await tools["delete_article_comment"](article_id="PROJ-A-1", comment_id="c1")
        assert "the removed text" in out
        client.delete.assert_awaited_once()
