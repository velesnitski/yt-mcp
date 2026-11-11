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
        "OPS-4101": {"summary": "Nightly database copy to staging, then cache warmup"},
        "OPS-4102": {"summary": "Rotate TLS certificates on the edge"},
        "WEB-4102": {"summary": "Footer layout on the pricing page"},
    },
    "second": {
        "CORE-4101": {"summary": "Automatic restart for the cache queue worker",
                      "assignee": {"login": "dana.lee", "name": "Dana Lee"}},
        "CORE-77": {"summary": "Автоматический перезапуск воркера очереди"},
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
            q = (params or {}).get("query", "")
            if q.startswith("#"):  # get_issues batch: "#A or #B"
                ids = [t.strip()[1:] for t in q.split(" or ")]
                return [_issue(i, ISSUES[name][i]) for i in ids if i in ISSUES[name]]
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
        m = tm.score(tm.content_words("занимаются перезапуском воркеров"),
                     "Автоматический перезапуск воркера очереди")
        assert m.score >= 0.6 and len(m.in_summary) == 2

    def test_english_variants_and_hyphens(self):
        m = tm.score(tm.content_words("deploying the cache warmup"), "Deploy cache warm-up job")
        assert m.score == 1.0

    def test_unrelated_scores_zero(self):
        assert tm.score(tm.content_words("отчёт по продажам"), "Footer layout").score == 0

    def test_refs_and_keys_are_found(self):
        assert tm.BARE_REF.findall("задача 4101 и task #4102, тикет №55") == ["4101", "4102", "55"]
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
        [r] = _json(await tools["resolve_ticket_refs"](refs=["4101 (restart cache queue)"], format="json"))
        assert r["confidence"] == "confident"
        [c] = r["candidates"]
        assert (c["id"], c["instance"]) == ("CORE-4101", "second")
        assert c["url"] == "https://second.example.youtrack.cloud/issue/CORE-4101"
        assert "summary: restart, cache, queue" in c["why"]

    @pytest.mark.asyncio
    async def test_existing_number_with_contradicting_context_is_none(self):
        _, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](refs=["4101"], context="invoice fonts", format="json"))
        assert r["confidence"] == "none" and r["candidates"] == []
        assert "OPS-4101" in r["note"] and "CORE-4101" in r["note"]

    @pytest.mark.asyncio
    async def test_no_context_gives_unverified_candidates(self):
        _, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](refs=["4101"], format="json"))
        assert r["confidence"] == "candidates"
        assert {c["id"] for c in r["candidates"]} == {"OPS-4101", "CORE-4101"}

    @pytest.mark.asyncio
    async def test_routes_narrow_the_search(self, monkeypatch):
        monkeypatch.setenv("YOUTRACK_REF_ROUTES", "ops:main:OPS,proxies:second:CORE")
        _, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](refs=["proxies 4101 restart"], format="json"))
        assert r["checked"] == ["CORE-4101", "OPS-4101"]  # route named in the words first, WEB never tried
        assert r["candidates"][0]["id"] == "CORE-4101"

    @pytest.mark.asyncio
    async def test_a_key_said_aloud_is_tried_first_and_alone(self):
        clients, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](
            refs=["4101 restart"], context="CORE team, queue", format="json"))
        assert r["confidence"] == "confident" and r["checked"] == ["CORE-4101"]
        assert not any(p.startswith("/api/issues/") for p, _ in clients["main"].calls)

    @pytest.mark.asyncio
    async def test_named_route_widens_when_it_does_not_confirm(self):
        _, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](
            refs=["4101 database copy"], context="CORE", format="json"))
        assert r["checked"][0] == "CORE-4101" and "OPS-4101" in r["checked"]
        assert r["candidates"][0]["id"] == "OPS-4101"

    @pytest.mark.asyncio
    async def test_instance_hint_limits_instances(self):
        clients, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](refs=["4101"], instance_hint="main", format="json"))
        assert [c["id"] for c in r["candidates"]] == ["OPS-4101"]
        assert not any(p.startswith("/api/issues/") for p, _ in clients["second"].calls)

    @pytest.mark.asyncio
    async def test_explicit_key_is_exact_or_flagged(self):
        _, tools = _setup()
        [a, b] = _json(await tools["resolve_ticket_refs"](
            refs=["CORE-4101", "OPS-4101 (pricing page footer layout)"], format="json"))
        assert a["confidence"] == "exact" and a["candidates"][0]["instance"] == "second"
        assert b["confidence"] == "weak"  # exists, but the words say otherwise

    @pytest.mark.asyncio
    async def test_tie_is_weak_and_owner_breaks_it(self):
        _, tools = _setup()
        # "cache" is in both summaries, "tuesday" in neither: a tie.
        [tie] = _json(await tools["resolve_ticket_refs"](refs=["4101 cache tuesday"], format="json"))
        assert tie["confidence"] == "weak"
        assert {c["id"] for c in tie["candidates"]} == {"OPS-4101", "CORE-4101"}
        [r] = _json(await tools["resolve_ticket_refs"](
            refs=["4101 cache tuesday"], owner="Dana Lee", format="json"))
        assert r["confidence"] == "confident" and r["candidates"][0]["id"] == "CORE-4101"
        assert "assignee matches owner" in r["candidates"][0]["why"]

    @pytest.mark.asyncio
    async def test_russian_reference(self):
        _, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](refs=["задача 77 (перезапуск очереди)"], format="json"))
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
        _, tools = _setup(_search_returning(("second", "CORE-77"), ("main", "OPS-4102")))
        [r] = _json(await tools["find_issues_for_text"](
            texts=["девопсы занимаются перезапуском воркеров очереди"], format="json"))
        assert r["match"] == "confident" and r["candidates"][0]["id"] == "CORE-77"
        assert set(r["candidates"][0]) >= {"id", "url", "summary", "state", "assignee", "score", "why"}
        assert "OPS-4102" not in [c["id"] for c in r["candidates"]]  # below threshold

    @pytest.mark.asyncio
    async def test_unrelated_text_is_no_match_not_noise(self):
        _, tools = _setup(_search_returning(("main", "OPS-4102"), ("main", "WEB-4102")))
        [r] = _json(await tools["find_issues_for_text"](texts=["quarterly budget review"], format="json"))
        assert r["match"] == "none" and r["candidates"] == []
        assert "no confident match among 2" in r["note"]

    @pytest.mark.asyncio
    async def test_explicit_key_bypasses_search(self):
        clients, tools = _setup(_search_returning(("main", "WEB-4102")))
        [r] = _json(await tools["find_issues_for_text"](texts=["see OPS-4101 today"], format="json"))
        assert r["match"] == "exact" and r["candidates"][0]["id"] == "OPS-4101"
        assert not any(p == "/api/issues" for p, _ in clients["main"].calls)

    @pytest.mark.asyncio
    async def test_bare_number_tried_against_projects(self):
        _, tools = _setup()
        [r] = _json(await tools["find_issues_for_text"](
            texts=["task 4102: rotate the certificates"], projects="WEB,OPS", format="json"))
        assert r["candidates"][0]["id"] == "OPS-4102"  # WEB-4102 exists but is about a footer

    @pytest.mark.asyncio
    async def test_query_is_scoped_and_one_word_per_query(self):
        clients, tools = _setup()
        await tools["find_issues_for_text"](texts=["cache queue"], projects="OPS, Big Team", since="2026-10-01")
        queries = [p["query"] for path, p in clients["main"].calls if path == "/api/issues"]
        assert queries == ["project: OPS, {Big Team} updated: 2026-10-01 .. Today cache",
                           "project: OPS, {Big Team} updated: 2026-10-01 .. Today queue"]

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
        out = _json(await fn(ids="OPS-1, 4101, OPS-1, do-", format="json"))
        assert client.get.await_args.kwargs["params"]["query"] == "#OPS-1"
        assert out[1] == {"id": "4101", "error": "not an issue ID"} and len(out) == 3

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


# ---------------------------------------------------------------- R (live findings, invented data)


class TestRouting:
    """get_issues sends each ID to the instance that has its project."""

    @staticmethod
    def _tool():
        # A key present on both instances: tried on each until found.
        PROJECTS["second"] = ["CORE", "OPS"]
        clients = {"main": _client("main"), "second": _client("second")}
        resolver = InstanceResolver(clients)
        mcp = FastMCP("test")
        issues.register(mcp, resolver)
        return clients, mcp._tool_manager._tools["get_issues"].fn

    @pytest.fixture(autouse=True)
    def _restore(self):
        before = {k: list(v) for k, v in PROJECTS.items()}
        yield
        PROJECTS.clear(); PROJECTS.update(before)

    @pytest.mark.asyncio
    async def test_key_from_the_other_instance_is_found_there(self):
        clients, fn = self._tool()
        out = _json(await fn(ids="CORE-4101,OPS-4102", fields="idReadable,summary", format="json"))
        assert [o.get("idReadable") for o in out] == ["CORE-4101", "OPS-4102"]
        assert out[0]["instance"] == "second" and out[0]["url"].startswith("https://second.")
        assert out[1]["instance"] == "main"

    @pytest.mark.asyncio
    async def test_shared_key_is_tried_on_each_instance(self):
        ISSUES["second"]["OPS-9"] = {"summary": "Only the second instance has this one"}
        try:
            _, fn = self._tool()
            [o] = _json(await fn(ids="OPS-9", fields="idReadable", format="json"))
            assert o["idReadable"] == "OPS-9" and o["instance"] == "second"
        finally:
            del ISSUES["second"]["OPS-9"]


class TestLiveFindings:
    @pytest.mark.asyncio
    async def test_russian_speech_matches_english_summary(self):
        _, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](refs=["задача 4101 (перезапуск очереди)"], format="json"))
        assert r["confidence"] == "confident" and r["candidates"][0]["id"] == "CORE-4101"

    def test_bridge_is_exact_for_short_stems(self):
        assert tm.score(tm.content_words("продажи за месяц"), "Prod deploy checklist").score == 0
        assert tm.score(tm.content_words("логин не работает"), "Collect server logs").score == 0
        assert tm.score(tm.content_words("логи сервера"), "Collect server logs").score == 1.0

    def test_clear_lead_is_confident_and_says_why(self):
        ranked = [{"score": 0.417, "distinctive": True, "why": "summary: x"},
                  {"score": 0.167, "distinctive": True, "why": ""}]
        assert matching._verdict(ranked, True) == "confident"
        assert "clear lead (0.417 vs 0.167)" in ranked[0]["why"]
        close = [{"score": 0.417, "distinctive": True, "why": ""},
                 {"score": 0.3, "distinctive": True, "why": ""}]
        assert matching._verdict(close, True) == "weak"

    def test_generic_only_evidence_is_never_confident(self):
        ranked = [{"score": 0.9, "distinctive": False, "why": ""}]
        assert matching._verdict(ranked, True) == "weak"

    @pytest.mark.asyncio
    async def test_generic_words_make_no_match(self):
        ISSUES["main"]["WEB-5"] = {"summary": "Remove desktop client from the client-api on dev"}
        try:
            _, tools = _setup(_search_returning(("main", "WEB-5")))
            [r] = _json(await tools["find_issues_for_text"](texts=["CORS client-api dev"], format="json"))
            assert r["match"] == "none" and r["candidates"] == []
        finally:
            del ISSUES["main"]["WEB-5"]

    @pytest.mark.asyncio
    async def test_local_generic_words_extend_the_list(self, monkeypatch):
        monkeypatch.setenv("YOUTRACK_GENERIC_WORDS", "tunnelx")
        ISSUES["main"]["WEB-6"] = {"summary": "Tunnelx switch fails on connect"}
        try:
            _, tools = _setup(_search_returning(("main", "WEB-6")))
            [r] = _json(await tools["find_issues_for_text"](
                texts=["Riverton tunnelx нет подключения"], format="json"))
            assert r["match"] == "none"  # the distinctive word (a place) matched nothing
        finally:
            del ISSUES["main"]["WEB-6"]

    def test_generic_words_weigh_less(self):
        # "dev" alone matching is a weak signal next to the distinctive word.
        assert tm.score(["deploy", "dev"], "Notes for the dev environment").score < 0.3
        assert tm.score(["deploy", "dev"], "Deploy checklist").score > 0.7

    def test_idf_downweights_words_every_candidate_shares(self):
        w = tm.idf_weights(["cache", "warmup"], ["cache queue", "cache warmup job", "cache size"])
        assert w["warmup"] > w["cache"]

    @pytest.mark.asyncio
    async def test_duplicate_checks_name_the_instance(self):
        PROJECTS["second"] = ["CORE", "OPS"]
        try:
            _, tools = _setup()
            [r] = _json(await tools["resolve_ticket_refs"](refs=["4101"], format="json"))
            assert "OPS-4101 (main)" in r["checked"] and "OPS-4101 (second)" in r["checked"]
        finally:
            PROJECTS["second"] = ["CORE"]


# ---------------------------------------------------------------- S (live re-test, invented data)


class TestReTestFindings:
    @pytest.mark.asyncio
    async def test_long_filler_context_does_not_sink_the_verdict(self):
        _, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](
            refs=["4101 restart итоги созвона заметки по формату"],
            context="Quarterly platform sync notes", format="json"))
        assert r["confidence"] == "confident" and r["candidates"][0]["id"] == "CORE-4101"

    @pytest.mark.asyncio
    async def test_said_key_acts_as_the_hint(self):
        clients, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](
            refs=["4101 worker"], context="CORE weekly sync", format="json"))
        assert r["checked"] == ["CORE-4101"] and r["candidates"][0]["id"] == "CORE-4101"
        assert not any(p.startswith("/api/issues/") for p, _ in clients["main"].calls)

    @pytest.mark.asyncio
    async def test_said_key_holds_even_when_only_weak(self):
        ISSUES["second"]["CORE-4103"] = {"summary": "Core queue fix on dev"}
        ISSUES["main"]["OPS-4103"] = {"summary": "Dev server certificate renewal"}
        try:
            _, tools = _setup()
            [r] = _json(await tools["resolve_ticket_refs"](refs=["4103 dev"], context="CORE", format="json"))
            # Generic word only → weak; the key itself is routing, not evidence;
            # and the said key is not abandoned for a weak answer.
            assert r["confidence"] == "weak" and r["checked"] == ["CORE-4103"]
        finally:
            del ISSUES["second"]["CORE-4103"], ISSUES["main"]["OPS-4103"]

    @pytest.mark.asyncio
    async def test_said_key_without_such_ticket_widens(self):
        _, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](
            refs=["4102 rotate certificates"], context="CORE weekly sync", format="json"))
        assert r["checked"][0] == "CORE-4102" and r["candidates"][0]["id"] == "OPS-4102"

    @pytest.mark.asyncio
    async def test_said_key_is_not_a_word_to_match(self):
        _, tools = _setup()
        [r] = _json(await tools["resolve_ticket_refs"](refs=["CORE 4101 queue"], format="json"))
        assert "CORE" not in r["candidates"][0]["why"]


def _dated(iid, summary, days_ago, touched_days_ago=None):
    import time
    ms = lambda d: int((time.time() - d * 86400) * 1000)  # noqa: E731
    return {"idReadable": iid, "summary": summary, "description": "", "resolved": None,
            "created": ms(days_ago),
            "updated": ms(days_ago if touched_days_ago is None else touched_days_ago),
            "customFields": []}


class TestOneDistinctiveWord:
    @staticmethod
    async def _find(issue, text):
        _, tools = _setup(lambda params: [issue])
        [r] = _json(await tools["find_issues_for_text"](texts=[text], format="json"))
        return r

    @pytest.mark.asyncio
    async def test_old_ticket_sharing_one_word_is_only_weak(self):
        r = await self._find(_dated("WEB-8", "Missing CORS headers on the password reset endpoint", 40),
                             "CORS client-api dev")
        assert r["match"] == "weak" and "one distinctive word" in r["candidates"][0]["why"]

    @pytest.mark.asyncio
    async def test_old_ticket_touched_today_is_still_only_weak(self):
        r = await self._find(
            _dated("WEB-11", "Missing CORS headers on the password reset endpoint", 40, touched_days_ago=0),
            "CORS client-api dev")
        assert r["match"] == "weak"

    @pytest.mark.asyncio
    async def test_one_word_on_a_ticket_created_today_is_confident(self):
        r = await self._find(_dated("WEB-9", "CORS failures on the client api", 0), "CORS client-api dev")
        assert r["match"] == "confident"

    @pytest.mark.asyncio
    async def test_two_distinctive_words_are_confident_even_if_old(self):
        r = await self._find(_dated("WEB-10", "Rotate signing keys for webhooks", 90),
                             "webhooks signing keys rotation")
        assert r["match"] == "confident"


class TestJsonFields:
    @pytest.mark.asyncio
    async def test_verdict_and_top_are_top_level_fields(self):
        _, tools = _setup(_search_returning(("second", "CORE-77")))
        [r] = _json(await tools["resolve_ticket_refs"](refs=["4101 restart cache queue"], format="json"))
        assert (r["verdict"], r["top"]) == ("confident", "CORE-4101")
        [t] = _json(await tools["find_issues_for_text"](texts=["quarterly budget"], format="json"))
        assert (t["verdict"], t["top"]) == ("none", None)
