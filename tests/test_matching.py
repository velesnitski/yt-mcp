"""Words → tickets: list_instances, resolve_ticket_refs, find_issues_for_text,
and the get_issues batch fallback (ADR-069).

All projects, summaries and people are invented (public repo).
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from yt_mcp import textmatch as tm
from yt_mcp.errors import UserInputError, YouTrackPermissionError
from yt_mcp.resolver import InstanceResolver
from yt_mcp.tools import issues, matching

ISSUES = {
    "main": {
        "OPS-1947": {"summary": "Nightly database copy to staging, then cache warmup"},
        "OPS-2317": {"summary": "Rotate TLS certificates on the edge"},
        "WEB-2317": {"summary": "Footer layout on the pricing page"},
    },
    "second": {
        "CORE-1947": {"summary": "Automatic restart for the cache proxy container",
                      "assignee": {"login": "dana.lee", "name": "Dana Lee"}},
        "CORE-77": {"summary": "Автоматический перезапуск контейнера прокси"},
    },
}
PROJECTS = {"main": ["OPS", "WEB"], "second": ["CORE"]}


def _issue(iid, data):
    return {"idReadable": iid, "summary": data["summary"], "description": data.get("description", ""),
            "assignee": data.get("assignee"), "resolved": None, "updated": 0, "customFields": []}


def _client(name, search=None):
    c = MagicMock()
    c.base_url = f"https://{name}.example.youtrack.cloud"
    c.list_projects_cached = AsyncMock(return_value=[{"shortName": k} for k in PROJECTS[name]])
    calls = []

    async def get(path, params=None):
        calls.append((path, params))
        if path.startswith("/api/issues/"):
            iid = path.rsplit("/", 1)[1]
            if iid in ISSUES[name]:
                return _issue(iid, ISSUES[name][iid])
            raise UserInputError("YouTrack not found error (404): HTTP 404 Not Found")
        if path == "/api/issues":
            return search(params) if search else []
        return {}

    c.get = AsyncMock(side_effect=get)
    c.calls = calls
    return c


def _setup(search=None):
    clients = {"main": _client("main", search), "second": _client("second", search)}
    resolver = InstanceResolver(clients)
    mcp = FastMCP("test")
    matching.register(mcp, resolver)
    return clients, {n: t.fn for n, t in mcp._tool_manager._tools.items()}


def _json(out):
    return json.loads(out)


@pytest.fixture(autouse=True)
def _no_routes(monkeypatch):
    monkeypatch.delenv("YOUTRACK_REF_ROUTES", raising=False)


# ---------------------------------------------------------------- textmatch


class TestTextmatch:
    def test_russian_inflections_meet(self):
        m = tm.score(tm.content_words("занимаются перезапуском контейнеров"),
                     "Автоматический перезапуск контейнера прокси")
        assert m.score >= 0.6 and len(m.in_summary) == 2

    def test_english_variants_and_hyphens(self):
        m = tm.score(tm.content_words("deploying the cache warmup"), "Deploy cache warm-up job")
        assert m.score == 1.0

    def test_unrelated_scores_zero(self):
        assert tm.score(tm.content_words("отчёт по продажам"), "Footer layout").score == 0

    def test_refs_and_keys_are_found(self):
        assert tm.BARE_REF.findall("задача 1947 и task #2317, тикет №55") == ["1947", "2317", "55"]
        assert tm.ISSUE_KEY.findall("see OPS-12 and core_x-3") == ["OPS-12", "core_x-3"]

    def test_stopwords_and_numbers_dropped(self):
        assert tm.content_words("the team is working on 42 things today") == ["things"]


# ---------------------------------------------------------------- Q


class TestListInstances:
    @pytest.mark.asyncio
    async def test_every_instance_with_url_and_keys(self):
        _, tools = _setup()
        rows = _json(await tools["list_instances"](format="json"))
        assert rows == [
            {"name": "main", "base_url": "https://main.example.youtrack.cloud", "default": True,
             "projects": ["OPS", "WEB"]},
            {"name": "second", "base_url": "https://second.example.youtrack.cloud", "default": False,
             "projects": ["CORE"]},
        ]


# ---------------------------------------------------------------- O


class TestResolveRefs:
    @pytest.mark.asyncio
    async def test_context_picks_the_right_instance(self):
        _, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](refs=["1947 (restart cache proxy)"], format="json"))
        assert r["confidence"] == "confident"
        [c] = r["candidates"]
        assert (c["id"], c["instance"]) == ("CORE-1947", "second")
        assert c["url"] == "https://second.example.youtrack.cloud/issue/CORE-1947"
        assert "summary: restart, cache, proxy" in c["why"]

    @pytest.mark.asyncio
    async def test_existing_number_with_contradicting_context_is_none(self):
        _, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](refs=["1947"], context="invoice fonts", format="json"))
        assert r["confidence"] == "none" and r["candidates"] == []
        assert "OPS-1947" in r["note"] and "CORE-1947" in r["note"]

    @pytest.mark.asyncio
    async def test_no_context_gives_unverified_candidates(self):
        _, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](refs=["1947"], format="json"))
        assert r["confidence"] == "candidates"
        assert {c["id"] for c in r["candidates"]} == {"OPS-1947", "CORE-1947"}

    @pytest.mark.asyncio
    async def test_routes_narrow_the_search(self, monkeypatch):
        monkeypatch.setenv("YOUTRACK_REF_ROUTES", "ops:main:OPS,core:second:CORE")
        _, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](refs=["core 1947 proxy restart"], format="json"))
        assert r["checked"] == ["CORE-1947", "OPS-1947"]  # route named in the words first, WEB never tried
        assert r["candidates"][0]["id"] == "CORE-1947"

    @pytest.mark.asyncio
    async def test_instance_hint_limits_instances(self):
        clients, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](refs=["1947"], instance_hint="main", format="json"))
        assert [c["id"] for c in r["candidates"]] == ["OPS-1947"]
        assert not any(p.startswith("/api/issues/") for p, _ in clients["second"].calls)

    @pytest.mark.asyncio
    async def test_explicit_key_is_exact_or_flagged(self):
        _, tools = _setup()
        [a, b] = _json(await tools["resolve_ticket_refs"](
            refs=["CORE-1947", "OPS-1947 (pricing page footer layout)"], format="json"))
        assert a["confidence"] == "exact" and a["candidates"][0]["instance"] == "second"
        assert b["confidence"] == "weak"  # exists, but the words say otherwise

    @pytest.mark.asyncio
    async def test_tie_is_weak_and_owner_breaks_it(self):
        _, tools = _setup()
        # "cache" is in both summaries, "tuesday" in neither: a tie.
        [tie] = _json(await tools["resolve_ticket_refs"](refs=["1947 cache tuesday"], format="json"))
        assert tie["confidence"] == "weak"
        assert {c["id"] for c in tie["candidates"]} == {"OPS-1947", "CORE-1947"}
        [r] = _json(await tools["resolve_ticket_refs"](
            refs=["1947 cache tuesday"], owner="Dana Lee", format="json"))
        assert r["confidence"] == "confident" and r["candidates"][0]["id"] == "CORE-1947"
        assert "assignee matches owner" in r["candidates"][0]["why"]

    @pytest.mark.asyncio
    async def test_russian_reference(self):
        _, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](refs=["задача 77 (перезапуск прокси)"], format="json"))
        assert r["confidence"] == "confident" and r["candidates"][0]["id"] == "CORE-77"

    @pytest.mark.asyncio
    async def test_unknown_hint_and_empty_refs_are_input_errors(self):
        _, tools = _setup()
        with pytest.raises(UserInputError):
            await tools["resolve_ticket_refs"](refs=["1"], instance_hint="nope")
        with pytest.raises(UserInputError):
            await tools["resolve_ticket_refs"](refs=[])


# ---------------------------------------------------------------- M


def _search_returning(*iids_by_instance):
    def search(params):
        return [_issue(i, ISSUES[inst][i]) for inst, i in iids_by_instance]
    return search


class TestFindForText:
    @pytest.mark.asyncio
    async def test_inflected_russian_phrase_matches_summary(self):
        _, tools = _setup(_search_returning(("second", "CORE-77"), ("main", "OPS-2317")))
        [r] = _json(await tools["find_issues_for_text"](
            texts=["девопсы занимаются перезапуском контейнеров прокси"], format="json"))
        assert r["match"] == "words" and r["candidates"][0]["id"] == "CORE-77"
        assert set(r["candidates"][0]) >= {"id", "url", "summary", "state", "assignee", "score", "why"}
        assert "OPS-2317" not in [c["id"] for c in r["candidates"]]  # below threshold

    @pytest.mark.asyncio
    async def test_unrelated_text_is_no_match_not_noise(self):
        _, tools = _setup(_search_returning(("main", "OPS-2317"), ("main", "WEB-2317")))
        [r] = _json(await tools["find_issues_for_text"](texts=["quarterly budget review"], format="json"))
        assert r["match"] == "none" and r["candidates"] == []
        assert "no confident match among 2" in r["note"]

    @pytest.mark.asyncio
    async def test_explicit_key_bypasses_search(self):
        clients, tools = _setup(_search_returning(("main", "WEB-2317")))
        [r] = _json(await tools["find_issues_for_text"](texts=["see OPS-1947 today"], format="json"))
        assert r["match"] == "reference" and r["candidates"][0]["id"] == "OPS-1947"
        assert not any(p == "/api/issues" for p, _ in clients["main"].calls)

    @pytest.mark.asyncio
    async def test_bare_number_tried_against_projects(self):
        _, tools = _setup()
        [r] = _json(await tools["find_issues_for_text"](
            texts=["task 2317: rotate the certificates"], projects="WEB,OPS", format="json"))
        assert r["candidates"][0]["id"] == "OPS-2317"  # WEB-2317 exists but is about a footer

    @pytest.mark.asyncio
    async def test_query_is_scoped_and_one_word_per_query(self):
        clients, tools = _setup()
        await tools["find_issues_for_text"](texts=["cache proxy"], projects="OPS, Big Team", since="2026-10-01")
        queries = [p["query"] for path, p in clients["main"].calls if path == "/api/issues"]
        assert queries == ["project: OPS, {Big Team} updated: 2026-10-01 .. Today cache",
                           "project: OPS, {Big Team} updated: 2026-10-01 .. Today proxy"]

    @pytest.mark.asyncio
    async def test_bad_since_is_explained(self):
        _, tools = _setup()
        with pytest.raises(UserInputError):
            await tools["find_issues_for_text"](texts=["x"], since="last month")


# ---------------------------------------------------------------- P + N


def _issues_tool(get):
    client = MagicMock()
    client.base_url = "https://main.example.youtrack.cloud/"
    client.get = AsyncMock(side_effect=get)
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    issues.register(mcp, resolver)
    return client, mcp._tool_manager._tools["get_issues"].fn


class TestGetIssuesBatch:
    KNOWN = {"OPS-1": "first", "OPS-3": "third"}

    def _get(self, batch_fails):
        async def get(path, params=None):
            if path == "/api/issues":
                if batch_fails:
                    raise UserInputError("YouTrack query error (400): Can't parse search query")
                return [{"idReadable": i, "summary": s} for i, s in self.KNOWN.items()]
            iid = path.rsplit("/", 1)[1]
            if iid in self.KNOWN:
                return {"idReadable": iid, "summary": self.KNOWN[iid]}
            raise UserInputError("YouTrack not found error (404): HTTP 404 Not Found")
        return get

    @pytest.mark.asyncio
    async def test_refused_batch_falls_back_per_id(self):
        client, fn = _issues_tool(self._get(batch_fails=True))
        out = _json(await fn(ids="OPS-3,OPS-2,OPS-1", format="json"))
        assert [o["id"] for o in out] == ["OPS-3", "OPS-2", "OPS-1"]  # input order
        assert out[1] == {"id": "OPS-2", "error": "not found or no access"}
        assert out[0]["url"] == "https://main.example.youtrack.cloud/issue/OPS-3"

    @pytest.mark.asyncio
    async def test_good_batch_is_one_request_and_reports_misses(self):
        client, fn = _issues_tool(self._get(batch_fails=False))
        out = await fn(ids="OPS-1,OPS-2,OPS-3")
        assert client.get.await_count == 1
        assert "2 of 3 issues fetched" in out and "Missing (not found or access denied): OPS-2" in out

    @pytest.mark.asyncio
    async def test_non_ids_never_reach_the_query(self):
        client, fn = _issues_tool(self._get(batch_fails=False))
        out = _json(await fn(ids="OPS-1, 1947, OPS-1, do-", format="json"))
        assert client.get.await_args.kwargs["params"]["query"] == "#OPS-1"
        assert out[1] == {"id": "1947", "error": "not an issue ID"} and len(out) == 3

    @pytest.mark.asyncio
    async def test_permission_error_is_not_masked(self):
        async def get(path, params=None):
            raise YouTrackPermissionError(403)
        _, fn = _issues_tool(get)
        with pytest.raises(YouTrackPermissionError):
            await fn(ids="OPS-1")

    @pytest.mark.asyncio
    async def test_raw_fields_override_gets_url_too(self):
        _, fn = _issues_tool(self._get(batch_fails=False))
        out = _json(await fn(ids="OPS-1", fields="idReadable,summary", format="json"))
        assert out == [{"idReadable": "OPS-1", "summary": "first",
                        "url": "https://main.example.youtrack.cloud/issue/OPS-1"}]
