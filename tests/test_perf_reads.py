"""Read-side latency work (ADR-068).

Each speed-up must keep its answer identical: the exact count equals what
the list would have said (minus the 500 cap), the cached user is the same
user, parallel lookups merge in the old order, and the level-parallel impact
walk visits the same issues in the same order as the one-by-one walk.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from yt_mcp import lookups
from yt_mcp.errors import UserInputError
from yt_mcp.tools import comments as comments_mod
from yt_mcp.tools import impact, issues


def _tools(module, client):
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    module.register(mcp, resolver)
    return {n: t.fn for n, t in mcp._tool_manager._tools.items()}


class _Peak:
    """Counts how many awaited calls are in flight at once."""

    def __init__(self):
        self.live = self.peak = 0

    async def __call__(self, result, delay=0.01):
        self.live += 1
        self.peak = max(self.peak, self.live)
        await asyncio.sleep(delay)
        self.live -= 1
        return result


@pytest.fixture
def no_sleep(monkeypatch):
    taken = []

    async def fake(s):
        taken.append(s)
    monkeypatch.setattr(lookups.asyncio, "sleep", fake)
    return taken


# ---------------------------------------------------------------- count


class TestCount:
    @staticmethod
    def _client(post_results, listing=()):
        client = MagicMock()
        client.post = AsyncMock(side_effect=list(post_results))
        client.get = AsyncMock(return_value=list(listing))
        return client

    @pytest.mark.asyncio
    async def test_exact_count_without_listing(self, no_sleep):
        client = self._client([{"count": 1234}])
        out = await _tools(issues, client)["count_issues"](query="project: DEMO")
        assert out == "**1234** issues match query: `project: DEMO`"
        client.get.assert_not_called()  # nothing downloaded
        path = client.post.await_args.args[0]
        assert path.startswith("/api/issuesGetter/count") and "fields=count" in path

    @pytest.mark.asyncio
    async def test_still_counting_is_polled(self, no_sleep):
        client = self._client([{"count": -1}, {"count": -1}, {"count": 7}])
        out = await _tools(issues, client)["count_issues"](query="x")
        assert out.startswith("**7**") and no_sleep == list(lookups.COUNT_POLL_DELAYS[:2])

    @pytest.mark.asyncio
    async def test_never_ready_falls_back_to_listing(self, no_sleep):
        n = len(lookups.COUNT_POLL_DELAYS) + 1
        client = self._client([{"count": -1}] * n, listing=[{"idReadable": "A-1"}] * 3)
        out = await _tools(issues, client)["count_issues"](query="x")
        assert out.startswith("**3**") and client.post.await_count == n

    @pytest.mark.asyncio
    async def test_missing_endpoint_falls_back(self, no_sleep):
        client = MagicMock()
        client.post = AsyncMock(side_effect=UserInputError(
            "YouTrack not found error (404): HTTP 404 Not Found"))
        client.get = AsyncMock(return_value=[{"idReadable": "A-1"}] * 500)
        out = await _tools(issues, client)["count_issues"](query="x")
        assert out.startswith("**500+**")

    @pytest.mark.asyncio
    async def test_bad_query_is_not_retried_by_listing(self, no_sleep):
        client = MagicMock()
        client.post = AsyncMock(side_effect=UserInputError(
            "YouTrack query error (400): Can't parse search query"))
        client.get = AsyncMock(return_value=[])
        with pytest.raises(UserInputError):
            await _tools(issues, client)["count_issues"](query="State: {")
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_or_rewrite_applies_to_the_post_body(self, no_sleep):
        client = self._client([{"count": 2}])
        await lookups.count_issues(client, "summary: a OR summary: b")
        assert client.post.await_args.kwargs["json"]["query"] == "summary: a, b"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [{"count": True}, {"count": "5"}, {}, []])
    async def test_malformed_count_is_not_trusted(self, no_sleep, payload):
        n = len(lookups.COUNT_POLL_DELAYS) + 1
        client = self._client([payload] * n)
        assert await lookups.count_issues(client, "x") is None


# ---------------------------------------------------------------- user


class TestCurrentUser:
    @pytest.mark.asyncio
    async def test_fetched_once_per_client(self):
        client = MagicMock()
        client.get = AsyncMock(return_value={"login": "alice_a", "name": "Alice A"})
        assert (await lookups.current_user(client))["login"] == "alice_a"
        await lookups.current_user(client)
        assert client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_failure_is_not_cached(self):
        client = MagicMock()
        client.get = AsyncMock(side_effect=[ValueError("down"), {"login": "alice_a"}])
        with pytest.raises(ValueError):
            await lookups.current_user(client)
        assert (await lookups.current_user(client))["login"] == "alice_a"


# ---------------------------------------------------------------- mentions


class TestMentionsParallel:
    @pytest.mark.asyncio
    async def test_both_searches_in_flight_together(self):
        peak = _Peak()
        client = MagicMock()

        async def get(path, params=None):
            if path == "/api/users/me":
                return {"login": "alice_a", "name": "Alice A"}
            return await peak([])

        client.get = AsyncMock(side_effect=get)
        await _tools(comments_mod, client)["get_my_mentions"]()
        assert peak.peak == 2


# ---------------------------------------------------------------- impact


def _graph_client(links, peak=None):
    """links: {issue: [linked, ...]} — every link of type Depend (outward)."""
    def issue(iid):
        return {"idReadable": iid, "summary": iid, "customFields": [],
                "links": [{"linkType": {"name": "Depend"}, "direction": "OUTWARD",
                           "issues": [{"idReadable": x} for x in links.get(iid, [])]}]}
    fetched = []

    async def get(path, params=None):
        if path.startswith("/api/issues/"):
            iid = path.rsplit("/", 1)[1]
            fetched.append(iid)
            return await peak(issue(iid)) if peak else issue(iid)
        return []

    client = MagicMock()
    client.get = AsyncMock(side_effect=get)
    return client, fetched


class TestImpactWalk:
    LINKS = {"R-1": ["A-1", "A-2", "A-3"], "A-1": ["B-1", "A-2"], "A-2": ["B-1"],
             "A-3": ["R-1"], "B-1": ["C-1"]}

    @pytest.mark.asyncio
    async def test_level_is_fetched_in_parallel(self):
        peak = _Peak()
        client, _ = _graph_client(self.LINKS, peak)
        await _tools(impact, client)["get_impact_map"](issue_id="R-1", depth=2)
        assert peak.peak == 3  # A-1, A-2, A-3 together

    @pytest.mark.asyncio
    async def test_same_issues_once_each_and_depth_respected(self):
        client, fetched = _graph_client(self.LINKS)
        out = await _tools(impact, client)["get_impact_map"](issue_id="R-1", depth=2)
        assert fetched == ["R-1", "A-1", "A-2", "A-3", "B-1"]  # BFS order, no repeats
        assert "C-1" not in fetched  # depth 2 stops before level 3
        assert "B-1" in out


# ---------------------------------------------------------------- scorecard


class TestScorecardStartup:
    @pytest.mark.asyncio
    async def test_opening_lookups_run_together(self):
        from yt_mcp.tools import deadlines
        peak = _Peak()
        client = MagicMock()

        async def get(path, params=None):
            return await peak({"login": "op"} if path == "/api/users/me" else [])

        client.get = AsyncMock(side_effect=get)
        tools = {}

        class FakeMcp:
            def tool(self, **kwargs):
                def deco(fn):
                    tools[fn.__name__] = fn
                    return fn
                return deco

        resolver = MagicMock()
        resolver.resolve = MagicMock(return_value=client)
        deadlines.register(FakeMcp(), resolver)
        await tools["deadline_scorecard"](quarter="2026Q2")
        assert peak.peak == 3  # operator, issue search, deadline-field probe


# ---------------------------------------------------------------- sprints


class TestSprintAddParallel:
    @pytest.mark.asyncio
    async def test_issues_added_in_parallel_reported_in_order(self):
        from yt_mcp.tools import sprints
        peak = _Peak()
        client = MagicMock()
        board = {"id": "1", "name": "Team",
                 "sprints": [{"id": "s1", "name": "Sprint 1", "archived": False}]}
        client.get = AsyncMock(return_value=[board])

        async def execute(iid, command):
            await peak(None, delay=0.01 * (5 - int(iid.split("-")[1])))
            if iid == "PROJ-2":
                raise ValueError("refused")

        client.execute_command = AsyncMock(side_effect=execute)
        out = await _tools(sprints, client)["add_issues_to_sprint"](
            board_name="Team", sprint_name="Sprint 1", issue_ids="PROJ-1,PROJ-2,PROJ-3,PROJ-4")
        assert peak.peak > 1
        assert "PROJ-1, PROJ-3, PROJ-4" in out and "PROJ-2: refused" in out
