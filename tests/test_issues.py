import re

from yt_mcp.tools.issues import _CMD_FIELD_RE, _CMD_KEYWORDS


class TestCommandFieldRegex:
    """Test the regex that splits compound commands into field-value pairs."""

    def test_single_braced_value(self):
        matches = list(_CMD_FIELD_RE.finditer("Subsystem {Client Panel}"))
        assert len(matches) == 1
        assert matches[0].group(1) == "Subsystem"
        assert matches[0].group(2) == "Client Panel"

    def test_single_word_value(self):
        matches = list(_CMD_FIELD_RE.finditer("Type Bug"))
        assert len(matches) == 1
        assert matches[0].group(3) == "Type"
        assert matches[0].group(4) == "Bug"

    def test_multiple_fields(self):
        matches = list(_CMD_FIELD_RE.finditer("Type Bug Subsystem {Client Panel}"))
        assert len(matches) == 2
        # First: Type Bug (simple)
        assert (matches[0].group(3), matches[0].group(4)) == ("Type", "Bug")
        # Second: Subsystem {Client Panel} (braced)
        assert (matches[1].group(1), matches[1].group(2)) == ("Subsystem", "Client Panel")

    def test_braced_then_simple(self):
        matches = list(_CMD_FIELD_RE.finditer("Subsystem {Client Panel} Priority High"))
        assert len(matches) == 2
        assert matches[0].group(2) == "Client Panel"
        assert matches[1].group(4) == "High"

    def test_empty_string(self):
        assert list(_CMD_FIELD_RE.finditer("")) == []

    def test_parentheses_in_braces(self):
        matches = list(_CMD_FIELD_RE.finditer("Subsystem {CRM (Admin Panel)}"))
        assert len(matches) == 1
        assert matches[0].group(2) == "CRM (Admin Panel)"


class TestCommandKeywords:
    """Test that tag/untag keywords are filtered when splitting commands."""

    def test_tag_is_keyword(self):
        assert "tag" in _CMD_KEYWORDS

    def test_untag_is_keyword(self):
        assert "untag" in _CMD_KEYWORDS

    def test_field_names_not_keywords(self):
        for name in ("Type", "Priority", "Subsystem", "State", "Assignee"):
            assert name.lower() not in _CMD_KEYWORDS

    def test_keyword_filtering(self):
        """Simulate the filtering logic used in create_issue."""
        command = "Type Bug tag urgent Subsystem {Client Panel}"
        field_commands = []
        for m in _CMD_FIELD_RE.finditer(command):
            name = m.group(1) or m.group(3)
            value = m.group(2) or m.group(4)
            if name.lower() not in _CMD_KEYWORDS:
                field_commands.append(f"{name} {value}")
        assert field_commands == ["Type Bug", "Subsystem Client Panel"]


# --- normalize_issue: JSON-friendly issue shape ---

import json
import httpx
import pytest

from yt_mcp.formatters import normalize_issue


def _yt_issue(**kw) -> dict:
    """Build a mock YT issue response with the new (richer) field shape."""
    return {
        "idReadable": kw.get("id", "PROJ-1"),
        "summary": kw.get("summary", "Title"),
        "description": kw.get("description", "Desc"),
        "state": {"name": kw.get("state", "In Progress")},
        "priority": {"name": kw.get("priority", "Medium")},
        "assignee": kw.get("assignee", {"login": "alice.a", "name": "Alice A"}),
        "created": kw.get("created", 1747584000000),
        "updated": kw.get("updated", 1747670400000),
        "resolved": kw.get("resolved"),
        "tags": kw.get("tags", [{"name": "release-blocker"}, {"name": "v2"}]),
        "customFields": kw.get("customFields", [
            {"name": "Severity", "value": {"name": "Major"}},
            {"name": "Type", "value": {"name": "Bug"}},
            {"name": "Deadline ☠️", "value": {"presentation": "2026-05-30"}},
        ]),
        "links": kw.get("links", []),
    }


class TestNormalizeIssueBasics:
    def test_top_level_fields_extracted(self):
        out = normalize_issue(_yt_issue())
        assert out["id"] == "PROJ-1"
        assert out["summary"] == "Title"
        assert out["description"] == "Desc"
        assert out["state"] == "In Progress"
        assert out["priority"] == "Medium"
        assert out["assignee"] == "Alice A"
        assert out["assignee_login"] == "alice.a"
        assert out["created"] == 1747584000000

    def test_tags_normalized_to_string_list(self):
        out = normalize_issue(_yt_issue())
        assert out["tags"] == ["release-blocker", "v2"]

    def test_tags_empty_when_absent(self):
        issue = _yt_issue()
        issue.pop("tags")
        out = normalize_issue(issue)
        assert out["tags"] == []

    def test_custom_fields_dict_shape(self):
        out = normalize_issue(_yt_issue())
        assert out["custom_fields"]["Severity"] == "Major"
        assert out["custom_fields"]["Type"] == "Bug"
        # Deadline uses `presentation` not `name` — handler falls through
        assert out["custom_fields"]["Deadline ☠️"] == "2026-05-30"

    def test_custom_fields_list_value(self):
        issue = _yt_issue(customFields=[
            {"name": "Subsystems", "value": [
                {"name": "API"}, {"name": "Auth"},
            ]},
        ])
        out = normalize_issue(issue)
        assert out["custom_fields"]["Subsystems"] == ["API", "Auth"]

    def test_custom_fields_handles_null_value(self):
        issue = _yt_issue(customFields=[
            {"name": "Optional", "value": None},
        ])
        out = normalize_issue(issue)
        assert out["custom_fields"]["Optional"] is None

    def test_unassigned_yields_none_login(self):
        issue = _yt_issue(assignee=None)
        out = normalize_issue(issue)
        assert out["assignee"] == "Unassigned"
        assert out["assignee_login"] is None

    def test_links_flattened(self):
        issue = _yt_issue(links=[{
            "direction": "outward",
            "linkType": {"name": "Depend"},
            "issues": [
                {"idReadable": "PROJ-99", "summary": "blocker", "state": {"name": "Open"}},
                {"idReadable": "PROJ-100", "summary": "other", "state": {"name": "Closed"}},
            ],
        }])
        out = normalize_issue(issue)
        assert len(out["links"]) == 2
        first = out["links"][0]
        assert first["id"] == "PROJ-99"
        assert first["link_type"] == "Depend"
        assert first["direction"] == "outward"
        assert first["state"] == "Open"


class TestNormalizeIssueComments:
    def test_comments_included_when_present(self):
        issue = _yt_issue()
        issue["comments"] = [
            {"id": "c1", "text": "first", "author": {"login": "bob.b", "name": "Bob B"},
             "created": 1747000000000},
        ]
        out = normalize_issue(issue, include_comments=True)
        assert len(out["comments"]) == 1
        assert out["comments"][0]["author"] == "Bob B"
        assert out["comments"][0]["author_login"] == "bob.b"

    def test_comments_omitted_when_absent_from_response(self):
        # When the YT response doesn't have a `comments` key at all,
        # we don't fabricate an empty list — keeps shape honest about
        # what was fetched.
        out = normalize_issue(_yt_issue())
        assert "comments" not in out

    def test_comments_skipped_when_include_comments_false(self):
        issue = _yt_issue()
        issue["comments"] = [{"id": "c1", "text": "x", "author": {"name": "x"}}]
        out = normalize_issue(issue, include_comments=False)
        assert "comments" not in out


class TestNormalizeIssueJSONRoundtrip:
    """Real consumer flow: normalize → json.dumps → json.loads → walk dict."""

    def test_roundtrip_preserves_all_keys(self):
        out = normalize_issue(_yt_issue())
        s = json.dumps(out, indent=2, ensure_ascii=False)
        parsed = json.loads(s)
        for key in ("id", "summary", "description", "state", "priority",
                    "assignee", "assignee_login", "created", "updated",
                    "resolved", "tags", "custom_fields", "links"):
            assert key in parsed

    def test_unicode_preserved_in_custom_field_names(self):
        out = normalize_issue(_yt_issue())
        s = json.dumps(out, indent=2, ensure_ascii=False)
        assert "☠️" in s


# --- get_issues batch tool: OR-query composition + ID parsing ---

from unittest.mock import AsyncMock, MagicMock

from mcp.server.fastmcp import FastMCP
from yt_mcp.resolver import InstanceResolver
from yt_mcp.config import YouTrackConfig
from yt_mcp.tools.issues import register as _register_issues


def _get_tool_fn(mcp, name):
    """Pull the unwrapped function out of FastMCP for direct await."""
    return mcp._tool_manager._tools[name].fn


def _make_mcp_with_mock(mock_response: list[dict]):
    """Spin up a real FastMCP with the issues tools registered, but with
    a resolver that returns a client whose .get() yields `mock_response`."""
    mcp = FastMCP("test")
    client = MagicMock()
    client.get = AsyncMock(return_value=mock_response)
    resolver = MagicMock(spec=InstanceResolver)
    resolver.resolve = MagicMock(return_value=client)
    _register_issues(mcp, resolver)
    return mcp, client


def _params_of(client) -> dict:
    """Pull the `params` kwarg from the most recent client.get call."""
    return client.get.call_args.kwargs.get("params") or client.get.call_args[0][1]


class TestGetIssuesBatch:
    @pytest.mark.asyncio
    async def test_empty_ids_returns_error_message(self):
        mcp, client = _make_mcp_with_mock([])
        out = await _get_tool_fn(mcp, "get_issues")(ids="")
        assert "No issue IDs" in out
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_whitespace_only_ids_returns_error(self):
        mcp, client = _make_mcp_with_mock([])
        out = await _get_tool_fn(mcp, "get_issues")(ids=" , ,  ")
        assert "No issue IDs" in out
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_composes_or_query(self):
        mcp, client = _make_mcp_with_mock([
            {"idReadable": "PROJ-1", "summary": "a", "state": {"name": "Open"},
             "customFields": []},
            {"idReadable": "PROJ-2", "summary": "b", "state": {"name": "Open"},
             "customFields": []},
        ])
        await _get_tool_fn(mcp, "get_issues")(ids="PROJ-1, PROJ-2")
        assert _params_of(client)["query"] == "#PROJ-1 or #PROJ-2"

    @pytest.mark.asyncio
    async def test_urls_stripped_to_ids(self):
        mcp, client = _make_mcp_with_mock([])
        url = "https://example.youtrack.cloud/issue/PROJ-99/some-slug"
        await _get_tool_fn(mcp, "get_issues")(ids=url)
        assert _params_of(client)["query"] == "#PROJ-99"

    @pytest.mark.asyncio
    async def test_over_100_ids_rejected_with_message(self):
        ids = ",".join(f"PROJ-{i}" for i in range(101))
        mcp, client = _make_mcp_with_mock([])
        out = await _get_tool_fn(mcp, "get_issues")(ids=ids)
        assert "Too many IDs" in out
        assert "101" in out
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_format_json_returns_normalized_array(self):
        mcp, client = _make_mcp_with_mock([
            {"idReadable": "PROJ-1", "summary": "first",
             "state": {"name": "In Progress"}, "customFields": [],
             "tags": [{"name": "urgent"}]},
            {"idReadable": "PROJ-2", "summary": "second",
             "state": {"name": "Closed"}, "customFields": []},
        ])
        out = await _get_tool_fn(mcp, "get_issues")(
            ids="PROJ-1,PROJ-2", format="json",
        )
        parsed = json.loads(out)
        assert isinstance(parsed, list)
        assert len(parsed) == 2
        assert parsed[0]["id"] == "PROJ-1"
        assert parsed[0]["tags"] == ["urgent"]
        assert "custom_fields" in parsed[0]  # normalized shape

    @pytest.mark.asyncio
    async def test_fields_override_returns_raw_in_json_mode(self):
        raw_response = [{"idReadable": "PROJ-1", "weird_custom_field": "preserved"}]
        mcp, client = _make_mcp_with_mock(raw_response)
        out = await _get_tool_fn(mcp, "get_issues")(
            ids="PROJ-1", fields="idReadable,weird_custom_field", format="json",
        )
        parsed = json.loads(out)
        # No normalization applied — raw passthrough
        assert parsed[0]["weird_custom_field"] == "preserved"
        assert "custom_fields" not in parsed[0]
        assert _params_of(client)["fields"] == "idReadable,weird_custom_field"

    @pytest.mark.asyncio
    async def test_report_mode_shows_count_and_missing(self):
        mcp, client = _make_mcp_with_mock([
            {"idReadable": "PROJ-1", "summary": "exists",
             "state": {"name": "Open"}, "customFields": []},
        ])
        out = await _get_tool_fn(mcp, "get_issues")(ids="PROJ-1,PROJ-99")
        assert "1 of 2 issues fetched" in out
        assert "PROJ-99" in out  # listed as missing
        assert "PROJ-1" in out

    @pytest.mark.asyncio
    async def test_top_param_matches_id_count(self):
        ids = ",".join(f"PROJ-{i}" for i in range(15))
        mcp, client = _make_mcp_with_mock([])
        await _get_tool_fn(mcp, "get_issues")(ids=ids)
        assert _params_of(client)["$top"] == "15"

    @pytest.mark.asyncio
    async def test_include_comments_false_by_default(self):
        mcp, client = _make_mcp_with_mock([])
        await _get_tool_fn(mcp, "get_issues")(ids="PROJ-1")
        assert "comments(" not in _params_of(client)["fields"]

    @pytest.mark.asyncio
    async def test_include_comments_true_widens_field_set(self):
        mcp, client = _make_mcp_with_mock([])
        await _get_tool_fn(mcp, "get_issues")(ids="PROJ-1", include_comments=True)
        assert "comments(" in _params_of(client)["fields"]


# --- create_issue split fallback emits BARE values ----------------------------
#
# Verified live: YouTrack's command parser REJECTS braces in values
# (`Status {New Employee}` -> 400 "expected: {New Employee}", even single-word
# `Priority {High}`) and matches multi-word enum values greedily, so
# `Status New Employee` is the correct command form. Braces are only an input
# convention so _CMD_FIELD_RE can capture a multi-word value as one group;
# they must be stripped before the command reaches YT. The mock below mirrors
# real YT by rejecting any braced command — this is the test that catches the
# ADR-016/v1.16.6 re-wrapping regression.
class TestCreateIssueBareCommandValues:
    def _make(self):
        mcp = FastMCP("test")
        client = MagicMock()
        client.resolve_project_id = AsyncMock(return_value="0-5")
        seen: list[str] = []

        async def _post(path, json=None):
            if path == "/api/issues":
                return {"idReadable": "PROJ-999", "summary": "test"}
            if path == "/api/commands":
                q = json["query"]
                seen.append(q)
                # Mirror real YouTrack: braces in a command are a 400...
                if "{" in q or "}" in q:
                    raise ValueError("YouTrack query error (400): expected: " + q)
                # ...and a combined multi-field command doesn't parse, forcing
                # the per-field split path.
                if ("Status" in q and "Department" in q) or q.count("Assignee") > 1:
                    raise ValueError("YouTrack query error (400): parse")
                return {}
            return {}

        client.post = AsyncMock(side_effect=_post)
        resolver = MagicMock(spec=InstanceResolver)
        resolver.resolve = MagicMock(return_value=client)
        _register_issues(mcp, resolver)
        return mcp, seen

    @pytest.mark.asyncio
    async def test_split_clauses_are_bare_multiword_values(self):
        mcp, seen = self._make()
        out = await _get_tool_fn(mcp, "create_issue")(
            project="HR", summary="test",
            command="Status {New Employee} Department DevOps Assignee {Jane Q Public}",
        )
        # Multi-word values reach YT BARE (braces stripped), which is what the
        # command parser accepts...
        assert "Status New Employee" in seen
        assert "Assignee Jane Q Public" in seen
        assert "Department DevOps" in seen
        # ...and no braced clause was ever sent (real YT would 400 on it).
        assert not any("{" in q or "}" in q for q in seen)
        assert "Created" in out

    @pytest.mark.asyncio
    async def test_multiword_value_set_without_false_failure(self):
        # Bare split clauses succeed against the brace-rejecting mock -> no
        # false "Could not set" for the multi-word field.
        mcp, seen = self._make()
        out = await _get_tool_fn(mcp, "create_issue")(
            project="HR", summary="test",
            command="Status {New Employee} Department DevOps",
        )
        assert "Status New Employee" in seen
        assert "Could not set" not in out

    # The durable regression guard (ADR-019): whatever command shape goes in,
    # and whichever path runs (whole-command or per-field split), no query sent
    # to /api/commands may contain a brace — real YT 400s on braces. The mock
    # above rejects braces, so a regression that re-braces would both fail the
    # command AND be caught by the brace-free assertion here.
    @pytest.mark.parametrize("command", [
        "Status {New Employee}",                       # single multi-word
        "Priority {High}",                             # braced single-word
        "Status {New Employee} Department DevOps",      # multi-word + bare
        "Type {Product task} Subsystem {Client Panel}", # two multi-word
        "Assignee {Jane Q Public}",                    # multi-word person
    ])
    @pytest.mark.asyncio
    async def test_no_command_query_ever_contains_braces(self, command):
        mcp, seen = self._make()
        out = await _get_tool_fn(mcp, "create_issue")(
            project="HR", summary="t", command=command,
        )
        assert "Created" in out
        assert seen, "expected at least one /api/commands call"
        assert not any("{" in q or "}" in q for q in seen), seen
        # braces in must still set the field (bare) — no false failure
        assert "Could not set" not in out


class TestCreateIssueInsufficientPermissions:
    """403/401 on create or on a field command must degrade gracefully — no
    raw httpx error, no orphaned bare issue via an uncaught exception."""

    @staticmethod
    def _http_error(code: int) -> httpx.HTTPStatusError:
        req = httpx.Request("POST", "https://example.youtrack.cloud/api/commands")
        resp = httpx.Response(code, request=req)
        return httpx.HTTPStatusError(f"{code}", request=req, response=resp)

    def _make(self, *, create_code: int | None = None, command_code: int | None = None):
        mcp = FastMCP("test")
        client = MagicMock()
        client.resolve_project_id = AsyncMock(return_value="0-5")

        async def _post(path, json=None):
            if path == "/api/issues":
                if create_code is not None:
                    raise self._http_error(create_code)
                return {"idReadable": "PROJ-999", "summary": "test"}
            if path == "/api/commands":
                if command_code is not None:
                    raise self._http_error(command_code)
                return {}
            return {}

        client.post = AsyncMock(side_effect=_post)
        resolver = MagicMock(spec=InstanceResolver)
        resolver.resolve = MagicMock(return_value=client)
        _register_issues(mcp, resolver)
        return mcp

    @pytest.mark.asyncio
    async def test_field_command_403_reported_not_raised(self):
        # Issue creates fine, but the account can't set the field -> 403 on
        # /api/commands. Must return "Created" + "Could not set", never raise.
        mcp = self._make(command_code=403)
        out = await _get_tool_fn(mcp, "create_issue")(
            project="HR", summary="test", command="Department DevOps",
        )
        assert "Created" in out
        assert "Could not set" in out
        assert "HTTP 403 (insufficient permissions)" in out
        # The instance host must not leak into the message.
        assert "youtrack.cloud" not in out

    @pytest.mark.asyncio
    async def test_create_403_returns_clean_message(self):
        # No Create Issue permission -> 403 on /api/issues. Friendly message,
        # no raw httpx error, no URL leak, and it must NOT hit the draft path.
        mcp = self._make(create_code=403)
        out = await _get_tool_fn(mcp, "create_issue")(
            project="HR", summary="test", command="Department DevOps",
        )
        assert "insufficient permissions" in out
        assert "HTTP 403" in out
        assert "youtrack.cloud" not in out

    @pytest.mark.asyncio
    async def test_create_500_still_raises(self):
        # A genuine server error must not be swallowed as a permission message.
        mcp = self._make(create_code=500)
        with pytest.raises(httpx.HTTPStatusError):
            await _get_tool_fn(mcp, "create_issue")(
                project="HR", summary="test", command="Department DevOps",
            )

    @pytest.mark.asyncio
    async def test_product_command_403_reported_not_raised(self):
        # Product is applied as its own /api/commands call; a 403 there must
        # also be caught and reported (covers the product catch site).
        mcp = self._make(command_code=403)
        out = await _get_tool_fn(mcp, "create_issue")(
            project="HR", summary="test", product="Alpha",
        )
        assert "Created" in out
        assert "Could not set" in out
        assert "Product Alpha" in out
        assert "HTTP 403 (insufficient permissions)" in out

    @pytest.mark.asyncio
    async def test_create_401_also_handled_cleanly(self):
        # 401 (rejected credentials) on create is handled the same graceful
        # way as 403 — clean message, no raw error, no URL leak.
        mcp = self._make(create_code=401)
        out = await _get_tool_fn(mcp, "create_issue")(
            project="HR", summary="test", command="Department DevOps",
        )
        assert "insufficient permissions" in out
        assert "HTTP 401" in out
        assert "youtrack.cloud" not in out


class TestCreateIssueLowPermissionIntegration:
    """Faithful low-permission simulation: drive the REAL YouTrackClient
    through a mocked httpx transport so the true status -> raise_for_status ->
    HTTPStatusError chain runs end-to-end.

    Ground truth (verified against a live YouTrack + JetBrains REST docs):
      * GET /api/admin/projects is permission-FILTERED, not admin-gated — a
        low-permission user gets ONLY their accessible projects (200), never a
        blanket 403. The "admin" path segment is just a namespace.
      * There is NO /api/projects endpoint (it 404s). It must never be called.
    """

    URL = "https://example.youtrack.cloud"

    def _make(self, handler):
        from yt_mcp.client import YouTrackClient
        cfg = YouTrackConfig(url=self.URL, token="perm-fake")
        client = YouTrackClient(cfg)
        seen: list[str] = []

        def recording(request):
            seen.append(request.url.path)
            return handler(request)

        # Swap only the network transport; keep the real _handle_error mapping.
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(recording),
            base_url=self.URL,
            headers={"Authorization": "Bearer perm-fake"},
        )
        mcp = FastMCP("test")
        resolver = MagicMock(spec=InstanceResolver)
        resolver.resolve = MagicMock(return_value=client)
        _register_issues(mcp, resolver)
        return mcp, seen

    @staticmethod
    def _lowperm_project_list(request):
        """A low-perm user's view: admin/projects returns ONLY their own
        project (filtered). Returns None for other paths."""
        if request.url.path == "/api/admin/projects":
            return httpx.Response(200, json=[{"id": "0-5", "shortName": "HR"}])
        return None

    @pytest.mark.asyncio
    async def test_resolves_own_project_without_touching_api_projects(self):
        # The account can create + set fields and only sees its own project.
        # Resolution must work off the filtered admin/projects list and must
        # NEVER call the nonexistent /api/projects.
        def handler(request):
            base = self._lowperm_project_list(request)
            if base is not None:
                return base
            if request.url.path == "/api/issues":
                return httpx.Response(200, json={"idReadable": "PROJ-999", "summary": "test"})
            if request.url.path == "/api/commands":
                return httpx.Response(200, json={})
            return httpx.Response(200, json=[])
        mcp, seen = self._make(handler)
        out = await _get_tool_fn(mcp, "create_issue")(
            project="HR", summary="test", command="Department DevOps",
        )
        assert "Created" in out
        assert "Could not set" not in out
        assert "/api/admin/projects" in seen
        assert "/api/projects" not in seen  # dead endpoint, never hit

    @pytest.mark.asyncio
    async def test_field_command_forbidden_reported_cleanly(self):
        # Can create, cannot set the field -> real 403 on /api/commands.
        def handler(request):
            base = self._lowperm_project_list(request)
            if base is not None:
                return base
            if request.url.path == "/api/issues":
                return httpx.Response(200, json={"idReadable": "PROJ-999", "summary": "test"})
            if request.url.path == "/api/commands":
                return httpx.Response(403, json={"error": "Forbidden"})
            return httpx.Response(200, json=[])
        mcp, seen = self._make(handler)
        out = await _get_tool_fn(mcp, "create_issue")(
            project="HR", summary="test", command="Department DevOps",
        )
        assert "Created" in out
        assert "HTTP 403 (insufficient permissions)" in out
        assert "example.youtrack.cloud" not in out  # URL must not leak
        assert "/api/projects" not in seen

    @pytest.mark.asyncio
    async def test_cannot_create_at_all(self):
        # No Create Issue permission -> real 403 on /api/issues.
        def handler(request):
            base = self._lowperm_project_list(request)
            if base is not None:
                return base
            if request.url.path == "/api/issues":
                return httpx.Response(403, json={"error": "Forbidden"})
            return httpx.Response(200, json=[])
        mcp, seen = self._make(handler)
        out = await _get_tool_fn(mcp, "create_issue")(
            project="HR", summary="test", command="Department DevOps",
        )
        assert "insufficient permissions" in out
        assert "HTTP 403" in out
        assert "example.youtrack.cloud" not in out

    @pytest.mark.asyncio
    async def test_access_less_token_degrades_to_not_found(self):
        # A token with NO project visibility -> admin/projects 403. Defensive
        # catch must degrade to a clean "not found", never an uncaught error,
        # and must not attempt to create.
        def handler(request):
            if request.url.path == "/api/admin/projects":
                return httpx.Response(403, json={"error": "Forbidden"})
            return httpx.Response(200, json=[])
        mcp, seen = self._make(handler)
        out = await _get_tool_fn(mcp, "create_issue")(
            project="HR", summary="test", command="Department DevOps",
        )
        assert "not found" in out.lower()
        assert "/api/issues" not in seen  # never tried to create
        assert "/api/projects" not in seen
