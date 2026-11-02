"""Latency and reply-size work (ADR-064).

Pins the semantics each speed-up must keep: parallel writes report in input
order and never exceed their bound; bulk still tags every issue before
changing any; the client retries only statuses that applied nothing; the
project cache still resolves a project created a minute ago; hints never
replace the real error; caps never cut JSON or translation input.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from yt_mcp import client as client_mod
from yt_mcp.client import YouTrackClient
from yt_mcp.config import YouTrackConfig
from yt_mcp.errors import UserInputError
from yt_mcp.fanout import bounded_map
from yt_mcp.formatters import clip_text
from yt_mcp.query_hints import SYNTAX_TIPS, project_names, with_query_hints
from yt_mcp.resolver import InstanceResolver
from yt_mcp.response_cap import cap_text, capped
from yt_mcp.tools import articles, bulk, issues, translate

URL = "https://example.youtrack.cloud"


# ---------------------------------------------------------------- client


def _client(handler, url=URL) -> YouTrackClient:
    c = YouTrackClient(YouTrackConfig(url=url, token="perf-fake"))
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=url)
    return c


@pytest.fixture
def sleeps(monkeypatch):
    """Record retry sleeps instead of taking them."""
    taken: list[float] = []

    async def fake_sleep(s):
        taken.append(s)

    monkeypatch.setattr(client_mod.asyncio, "sleep", fake_sleep)
    return taken


def _sequence(*responses):
    calls = []

    def handler(req):
        calls.append(req)
        return responses[min(len(calls), len(responses)) - 1]

    return handler, calls


class TestRetry:
    @pytest.mark.asyncio
    async def test_429_then_success_is_retried(self, sleeps):
        handler, calls = _sequence(httpx.Response(429), httpx.Response(200, json=[1]))
        assert await _client(handler).get("/api/issues") == [1]
        assert len(calls) == 2
        assert sleeps == [client_mod._RETRY_BACKOFF[0]]

    @pytest.mark.asyncio
    async def test_retry_after_is_honoured_and_capped(self, sleeps):
        handler, _ = _sequence(
            httpx.Response(503, headers={"Retry-After": "2"}),
            httpx.Response(429, headers={"Retry-After": "3600"}),
            httpx.Response(200, json={}),
        )
        await _client(handler).get("/api/x")
        assert sleeps == [2.0, client_mod._RETRY_AFTER_CAP]

    @pytest.mark.asyncio
    async def test_gives_up_after_three_attempts(self, sleeps):
        handler, calls = _sequence(httpx.Response(503))
        with pytest.raises(httpx.HTTPStatusError):
            await _client(handler).get("/api/x")
        assert len(calls) == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [500, 502, 504])
    async def test_other_5xx_are_never_retried(self, sleeps, status):
        # They may have been applied server-side: a retried POST could
        # double-write.
        handler, calls = _sequence(httpx.Response(status))
        with pytest.raises(httpx.HTTPStatusError):
            await _client(handler).post("/api/commands", json={})
        assert len(calls) == 1 and sleeps == []

    @pytest.mark.asyncio
    async def test_post_and_delete_go_through_the_retry(self, sleeps):
        handler, calls = _sequence(httpx.Response(429), httpx.Response(200, json={}))
        await _client(handler).post("/api/issues/A-1", json={"summary": "s"})
        assert [r.method for r in calls] == ["POST", "POST"]
        assert json.loads(calls[1].content) == {"summary": "s"}

        handler, calls = _sequence(httpx.Response(429), httpx.Response(200))
        await _client(handler).delete("/api/issues/A-1/comments/1")
        assert [r.method for r in calls] == ["DELETE", "DELETE"]

    @pytest.mark.asyncio
    async def test_400_is_not_retried(self, sleeps):
        handler, calls = _sequence(httpx.Response(400, json={"error": "bad"}))
        with pytest.raises(UserInputError):
            await _client(handler).get("/api/issues")
        assert len(calls) == 1


class TestProjectCache:
    @staticmethod
    def _projects_handler(listing):
        calls = []

        def handler(req):
            calls.append(req)
            return httpx.Response(200, json=listing())

        return handler, calls

    @pytest.mark.asyncio
    async def test_repeat_resolution_uses_the_cache(self):
        handler, calls = self._projects_handler(lambda: [{"id": "0-1", "shortName": "ALPHA"}])
        c = _client(handler)
        assert await c.resolve_project_id("alpha") == "0-1"
        assert await c.resolve_project_id("ALPHA") == "0-1"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_listing_is_cached_and_shared_with_resolution(self):
        # The query hints read the listing on every parse error; it must not
        # cost a fetch each time, nor a second one for a later resolution.
        handler, calls = self._projects_handler(lambda: [{"id": "0-1", "shortName": "ALPHA"}])
        c = _client(handler)
        await c.list_projects_cached()
        await c.list_projects_cached()
        assert await c.resolve_project_id("ALPHA") == "0-1"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_miss_refreshes_once_so_new_projects_resolve(self):
        listing = [{"id": "0-1", "shortName": "ALPHA"}]
        handler, calls = self._projects_handler(lambda: listing)
        c = _client(handler)
        await c.resolve_project_id("ALPHA")
        listing.append({"id": "0-2", "shortName": "BETA"})  # created meanwhile
        assert await c.resolve_project_id("BETA") == "0-2"
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_real_miss_costs_one_fetch_not_two(self):
        handler, calls = self._projects_handler(lambda: [{"id": "0-1", "shortName": "ALPHA"}])
        assert await _client(handler).resolve_project_id("NOPE") is None
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_failure_is_not_cached(self):
        state = {"fail": True}

        def handler(req):
            if state["fail"]:
                return httpx.Response(403)
            return httpx.Response(200, json=[{"id": "0-1", "shortName": "ALPHA"}])

        c = _client(handler)
        assert await c.resolve_project_id("ALPHA") is None
        state["fail"] = False
        assert await c.resolve_project_id("ALPHA") == "0-1"


# ---------------------------------------------------------------- fan-out


class TestBoundedMap:
    @pytest.mark.asyncio
    async def test_input_order_and_bound(self):
        live = peak = 0

        async def work(x):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01 * (10 - x))  # later items finish first
            live -= 1
            return x * 2

        assert await bounded_map(work, range(10), limit=3) == [x * 2 for x in range(10)]
        assert peak == 3

    @pytest.mark.asyncio
    async def test_exception_surfaces_only_after_all_items_ran(self):
        done = []

        async def work(x):
            if x == 0:
                raise RuntimeError("boom")
            await asyncio.sleep(0.01)
            done.append(x)

        with pytest.raises(RuntimeError):
            await bounded_map(work, range(5), limit=2)
        assert sorted(done) == [1, 2, 3, 4]


def _fastmcp_tools(module, client):
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    module.register(mcp, resolver)
    return {n: t.fn for n, t in mcp._tool_manager._tools.items()}


class TestParallelWrites:
    @pytest.mark.asyncio
    async def test_bulk_tags_every_issue_before_any_command(self):
        log = []
        client = MagicMock()
        client.get = AsyncMock(return_value=[{"idReadable": f"A-{i}"} for i in range(8)])

        async def execute(issue_id, command):
            await asyncio.sleep(0)
            log.append(("tag" if command.startswith("tag ") else "cmd", issue_id))

        client.execute_command = AsyncMock(side_effect=execute)
        out = await _fastmcp_tools(bulk, client)["bulk_update_execute"](
            query="project: A", command="State Closed")
        kinds = [k for k, _ in log]
        assert kinds == ["tag"] * 8 + ["cmd"] * 8
        assert "A-0, A-1, A-2, A-3, A-4, A-5, A-6, A-7" in out

    @pytest.mark.asyncio
    async def test_bulk_reports_failures_per_issue_in_order(self):
        client = MagicMock()
        client.get = AsyncMock(return_value=[{"idReadable": f"A-{i}"} for i in range(4)])

        async def execute(issue_id, command):
            if issue_id == "A-2" and not command.startswith("tag "):
                raise ValueError("rejected")

        client.execute_command = AsyncMock(side_effect=execute)
        out = await _fastmcp_tools(bulk, client)["bulk_update_execute"](
            query="project: A", command="State Closed")
        assert "**Updated:** 3 issues" in out
        assert "A-0, A-1, A-3" in out
        assert "A-2: command failed (rejected)" in out

    @staticmethod
    def _translate_tools(client):
        tools = {}

        class FakeMcp:
            def tool(self, **kwargs):
                def deco(fn):
                    tools[fn.__name__] = fn
                    return fn
                return deco

        resolver = MagicMock()
        resolver.resolve = MagicMock(return_value=client)
        translate.register(FakeMcp(), resolver)
        return tools

    @staticmethod
    def _blocks(ids):
        return "\n---\n".join(f"ISSUE: {i}\nSUMMARY: t {i}\n" for i in ids)

    @pytest.mark.asyncio
    async def test_translations_report_in_input_order(self):
        client = MagicMock()
        delays = {"A-1": 0.03, "A-2": 0.0, "A-3": 0.01}

        async def execute(issue_id, command):
            await asyncio.sleep(delays[issue_id])

        client.execute_command = AsyncMock(side_effect=execute)
        client.post = AsyncMock(return_value={})
        client.get = AsyncMock(return_value={})
        out = await self._translate_tools(client)["apply_translations"](
            translations=self._blocks(["A-1", "A-2", "A-3"]),
            batch_tag="yt-translate-1", preserve_original=False)
        assert "**IDs:** A-1, A-2, A-3" in out

    async def _peak(self, ids):
        live = peak = 0
        client = MagicMock()

        async def execute(issue_id, command):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1

        client.execute_command = AsyncMock(side_effect=execute)
        client.post = AsyncMock(return_value={})
        await self._translate_tools(client)["apply_translations"](
            translations=self._blocks(ids), batch_tag="yt-translate-1",
            preserve_original=False)
        return peak

    @pytest.mark.asyncio
    async def test_distinct_issues_run_in_parallel_within_the_bound(self):
        from yt_mcp.fanout import WRITE_CONCURRENCY
        peak = await self._peak([f"A-{i}" for i in range(10)])
        assert peak > 1, "distinct issues ran one at a time"
        assert peak == WRITE_CONCURRENCY

    @pytest.mark.asyncio
    async def test_repeated_issue_is_applied_sequentially(self):
        assert await self._peak(["A-1", "A-2", "A-1"]) == 1


# ---------------------------------------------------------------- hints


class TestQueryHints:
    @pytest.mark.parametrize("query,expected", [
        ("project: ALPHA #Unresolved", ["ALPHA"]),
        ("project: ALPHA, BETA, {Gamma Ops} State: Open", ["ALPHA", "BETA", "Gamma Ops"]),
        ("(project: ALPHA or project: alpha) Assignee: jdoe", ["ALPHA"]),
        ("project: DEMO (server OR rack)", ["DEMO"]),
        ("State: Open", []),
    ])
    def test_project_names(self, query, expected):
        assert project_names(query) == expected

    @staticmethod
    def _resolver(listings):
        clients = {}
        for name, projects in listings.items():
            c = MagicMock()
            c.base_url = f"https://{name}.example.youtrack.cloud"
            c.list_projects_cached = AsyncMock(return_value=[{"shortName": p} for p in projects])
            clients[name] = c
        return InstanceResolver(clients)

    @staticmethod
    def _failing(msg="YouTrack query error (400): Can't parse search query, please check"):
        async def search_issues(query: str, instance: str = ""):
            raise UserInputError(msg)
        return search_issues

    @pytest.mark.asyncio
    async def test_points_at_the_instance_that_has_the_project(self):
        r = self._resolver({"main": ["ALPHA"], "second": ["OPS"]})
        with pytest.raises(UserInputError) as exc:
            await with_query_hints(self._failing(), r)(query="project: OPS #Unresolved")
        msg = str(exc.value)
        assert msg.startswith("YouTrack query error (400): Can't parse")
        assert 'Project `OPS` is not on this instance' in msg
        assert 'instance="second"' in msg
        assert SYNTAX_TIPS in msg

    @pytest.mark.asyncio
    async def test_project_on_current_instance_gets_only_syntax_tips(self):
        r = self._resolver({"main": ["ALPHA"], "second": ["OPS"]})
        with pytest.raises(UserInputError) as exc:
            await with_query_hints(self._failing(), r)(query="project: OPS x", instance="second")
        assert "not on this instance" not in str(exc.value)
        assert SYNTAX_TIPS in str(exc.value)

    @pytest.mark.asyncio
    async def test_unknown_everywhere(self):
        r = self._resolver({"main": ["ALPHA"], "second": ["OPS"]})
        with pytest.raises(UserInputError) as exc:
            await with_query_hints(self._failing(), r)(query="project: NOPE")
        assert "No configured instance has a project `NOPE`" in str(exc.value)

    @pytest.mark.asyncio
    async def test_lookup_failure_keeps_the_original_error(self):
        r = self._resolver({"main": ["ALPHA"]})
        r.resolve().list_projects_cached = AsyncMock(side_effect=RuntimeError("down"))
        with pytest.raises(UserInputError) as exc:
            await with_query_hints(self._failing(), r)(query="project: ALPHA")
        assert str(exc.value).startswith("YouTrack query error (400): Can't parse")

    @pytest.mark.asyncio
    async def test_other_errors_pass_through_untouched(self):
        r = self._resolver({"main": ["ALPHA"]})
        with pytest.raises(UserInputError) as exc:
            await with_query_hints(self._failing("Type is required"), r)(query="project: X")
        assert str(exc.value) == "Type is required"

    @pytest.mark.asyncio
    async def test_success_is_untouched(self):
        async def ok(query: str):
            return "fine"
        assert await with_query_hints(ok, self._resolver({"main": []}))(query="x") == "fine"


# ---------------------------------------------------------------- sizes


class TestReplyCaps:
    def test_clip_text(self):
        assert clip_text("abc", 0) == "abc"
        assert clip_text("abcdef", 3, "see more") == "abc… [+3 chars — see more]"

    def test_cap_text_cuts_at_a_line_break_with_marker(self):
        text = "\n".join(f"line {i:03d}" for i in range(100))
        out = cap_text(text, 200)
        body, marker = out.split("\n\n_[yt-mcp: reply cut at ")
        assert len(body) <= 200 and body.endswith(("0", "1", "2", "3", "4", "5", "6", "7", "8", "9"))
        assert f"of {len(text)} characters" in marker

    @pytest.mark.parametrize("text", ['{"a": "' + "x" * 500 + '"}', "  [" + "1," * 500 + "1]"])
    def test_json_is_never_cut(self, text):
        assert cap_text(text, 50) == text

    def test_zero_disables(self):
        assert cap_text("x" * 500, 0) == "x" * 500

    @pytest.mark.asyncio
    async def test_translation_source_is_exempt(self):
        async def get_issues_for_translation():
            return "x" * 500

        async def search_issues():
            return "x" * 500

        assert len(await capped(get_issues_for_translation, 50)()) == 500
        assert len(await capped(search_issues, 50)()) < 500

    @pytest.mark.asyncio
    async def test_get_issues_json_clips_descriptions(self):
        client = MagicMock()
        client.get = AsyncMock(return_value=[
            {"idReadable": "A-1", "summary": "s", "description": "d" * 5000,
             "comments": [{"id": "1", "text": "c" * 3000}]},
        ])
        tools = _fastmcp_tools(issues, client)
        data = json.loads(await tools["get_issues"](ids="A-1", format="json", include_comments=True))
        assert data[0]["description"].startswith("d" * 2000 + "… [+3000 chars")
        assert data[0]["comments"][0]["text"].startswith("c" * 2000 + "… [+1000 chars")
        full = json.loads(await tools["get_issues"](ids="A-1", format="json", description_chars=0))
        assert full[0]["description"] == "d" * 5000

    @pytest.mark.asyncio
    async def test_get_article_pages_long_content(self):
        content = "".join(f"para {i:04d}\n" for i in range(4000))  # 40 000 chars
        client = MagicMock()
        client.get = AsyncMock(return_value={
            "idReadable": "KB-1", "summary": "Doc", "content": content,
            "comments": [{"id": "c1", "text": "a remark", "author": {"fullName": "Ann"}}],
        })
        get_article = _fastmcp_tools(articles, client)["get_article"]

        first = await get_article(article_id="KB-1")
        assert "Continue with get_article(article_id=\"KB-1\", offset=30000)" in first
        assert "a remark" not in first  # comments follow the last part

        last = await get_article(article_id="KB-1", offset=30000)
        assert "Continue with" not in last and "a remark" in last
        # Parts tile the content exactly: nothing dropped, nothing repeated.
        # The reply joins its sections with "\n", so one separator newline
        # sits between the part and its marker.
        part1 = first.split("\n---\n", 1)[1].split("\n\n_[Part:")[0]
        part2 = last.split("\n---\n", 1)[1].split("\n---\n## Comments")[0]
        assert part1 + part2.rstrip("\n") == content.rstrip("\n")

        whole = await get_article(article_id="KB-1", max_chars=0)
        assert "Continue with" not in whole and content.strip() in whole

    @pytest.mark.asyncio
    async def test_get_article_offset_past_end(self):
        client = MagicMock()
        client.get = AsyncMock(return_value={"idReadable": "KB-1", "content": "short"})
        out = await _fastmcp_tools(articles, client)["get_article"](article_id="KB-1", offset=99)
        assert "past the end" in out


def test_release_pins_the_commit_not_the_tag():
    import pathlib
    script = (pathlib.Path(__file__).resolve().parents[1] / "scripts" / "release.sh").read_text()
    assert 'sync-mcp-label.py --pin "$sha"' in script
    assert 'sha="$(git rev-parse "$tag^{commit}")"' in script
