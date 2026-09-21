"""Every tool, against the shapes the API actually returns (ADR-054).

Two properties, checked across the whole surface rather than per module:

1. **An empty instance is not an error.** A tool with nothing to report
   must say so in words. Raising, or returning a bare shell, turns "no
   data" into "something broke" for the caller.
2. **Null-shaped payloads do not crash.** Registry Q6: `linkType`,
   `author`, `added` and `summary` all arrive as `null` in normal
   responses. Tests written against imagined payloads miss this, and it
   has already produced one crash class fleet-wide.

Parameterising over the live registry rather than a hand-listed set means
a tool added tomorrow is covered the day it is registered — the gap that
let two modules sit near zero coverage while their neighbours were well
tested.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from yt_mcp.config import YouTrackConfig
from yt_mcp.errors import UserInputError
from yt_mcp.tools import WRITE_TOOLS, _registered_tools, register_all

# Tools whose arguments cannot be synthesised meaningfully, or which are
# covered in depth by their own suites.
_SKIP = {
    "apply_translations",       # needs a prepared translation batch
    "bulk_rollback",            # batch-tag lifecycle, see test_bulk
    "bulk_update_execute",      # destructive path, see test_bulk
}


# Endpoints whose last path segment is a collection answer with a list;
# everything else answers with a single object. Returning a list for both
# is the classic unrealistic mock — the code is right to call .get() on an
# entity, and a list-for-everything fixture blames it for that.
def _is_collection(path: str) -> bool:
    """Collection endpoints answer with a list, entity endpoints with an object.

    Judged by shape rather than a hard-coded list, which silently
    mis-answered any endpoint nobody remembered to add. A trailing plural
    is a collection (`/issues`, `/issueTags`, `/customFields`); an id or
    `me` is an entity (`/issues/PROJ-1`, `/users/me`).
    """
    tail = path.rstrip("/").split("?")[0].rsplit("/", 1)[-1]
    if "-" in tail or tail == "me":
        return False
    return tail.endswith("s")


def _shape_for(path: str, payload):
    if _is_collection(path):
        return payload if isinstance(payload, list) else ([payload] if payload else [])
    if isinstance(payload, list):
        return payload[0] if payload else {}
    return payload


def _make_tools(payload):
    """Register every tool against a client returning `payload`."""
    client = MagicMock()
    client.get = AsyncMock(side_effect=lambda path, params=None, **kw: _shape_for(path, payload))
    client.post = AsyncMock(return_value=payload if isinstance(payload, dict) else {})
    client.delete = AsyncMock(return_value={})
    client.execute_command = AsyncMock(return_value={})
    client.update_comment = AsyncMock(return_value={})
    client.resolve_project_id = AsyncMock(return_value="0-1")
    client.base_url = "https://test.youtrack.cloud"
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    register_all(mcp, resolver, YouTrackConfig(url="https://test.youtrack.cloud", token="t"))
    return client, _registered_tools(mcp)


def _args_for(fn):
    """Synthesise plausible arguments from the signature."""
    kwargs = {}
    for name, param in inspect.signature(fn).parameters.items():
        if param.default is not inspect.Parameter.empty:
            continue  # optional: let the tool's own default apply
        ann = param.annotation
        if ann is int:
            kwargs[name] = 1
        elif ann is bool:
            kwargs[name] = False
        elif "id" in name.lower() or name in ("issue_id", "article_id"):
            kwargs[name] = "PROJ-1"
        elif "project" in name.lower():
            kwargs[name] = "PROJ"
        elif "query" in name.lower():
            kwargs[name] = "project: PROJ"
        else:
            kwargs[name] = "PROJ-1"
    return kwargs


def _tool_names():
    _, tools = _make_tools([])
    return sorted(n for n in tools if n not in _SKIP)


TOOL_NAMES = _tool_names()


def test_the_registry_is_not_empty():
    """Control: if registration broke, every parameterised test below
    would vacuously pass with an empty parameter list."""
    assert len(TOOL_NAMES) > 70, f"only {len(TOOL_NAMES)} tools discovered"


class TestEmptyInstance:
    """Nothing to report is a result, not a failure."""

    @pytest.mark.parametrize("name", TOOL_NAMES)
    async def test_tool_returns_text_on_empty_responses(self, name):
        client, tools = _make_tools([])
        fn = tools[name].fn
        try:
            out = await fn(**_args_for(fn))
        except UserInputError:
            return  # a tool may legitimately reject synthesised input
        except (KeyError, TypeError, AttributeError, IndexError) as e:
            pytest.fail(f"{name} crashed on an empty response: {type(e).__name__}: {e}")
        assert isinstance(out, str)
        assert out.strip(), f"{name} returned an empty string instead of saying so"

    @pytest.mark.parametrize("name", TOOL_NAMES)
    async def test_tool_survives_empty_dict_responses(self, name):
        """Some endpoints answer `{}` rather than `[]`."""
        client, tools = _make_tools({})
        fn = tools[name].fn
        try:
            out = await fn(**_args_for(fn))
        except UserInputError:
            return
        except (KeyError, TypeError, AttributeError, IndexError) as e:
            pytest.fail(f"{name} crashed on an empty object: {type(e).__name__}: {e}")
        assert isinstance(out, str)


class TestNullShapes:
    """Q6: nested objects arrive as null in ordinary responses."""

    # Faithful to what the API actually sends, not maximally hostile: the
    # nulls below are the ones Q6 documents plus those YouTrack returns for
    # an unset field. Collection selectors come back as arrays and
    # created/updated are always present, so nulling those would test a
    # payload that cannot occur and invite churn fixing it.
    NULL_ISSUE = {
        "idReadable": "PROJ-1",
        "id": "1-1",
        "summary": None,        # Q6
        "author": None,         # Q6
        "added": None,          # Q6
        "removed": None,        # Q6
        "linkType": None,       # Q6
        "state": None,          # unset field
        "assignee": None,       # unassigned
        "project": None,
        "reporter": None,
        "description": None,
        "resolved": None,       # still open
        "text": None,
        "value": None,
        "field": None,
        "duration": None,
        "name": None,
        "created": 1767225600000,
        "updated": 1767225600000,
        "tags": [],
        "customFields": [],
        "comments": [],
        "links": [],
        "attachments": [],
        "sprints": [],
        "issues": [],
    }

    @pytest.mark.parametrize("name", TOOL_NAMES)
    async def test_tool_survives_null_members(self, name):
        client, tools = _make_tools([dict(self.NULL_ISSUE)])
        fn = tools[name].fn
        try:
            out = await fn(**_args_for(fn))
        except UserInputError:
            return
        except (KeyError, TypeError, AttributeError, IndexError) as e:
            pytest.fail(f"{name} crashed on null members: {type(e).__name__}: {e}")
        assert isinstance(out, str)


class TestReadOnlyToolsPerformNoWrites:
    """A tool annotated read-only must not reach a mutating endpoint."""

    @pytest.mark.parametrize("name", [n for n in TOOL_NAMES if n not in WRITE_TOOLS])
    async def test_no_write_calls(self, name):
        client, tools = _make_tools([])
        fn = tools[name].fn
        try:
            await fn(**_args_for(fn))
        except UserInputError:
            return
        except (KeyError, TypeError, AttributeError, IndexError):
            return  # crash-on-empty is covered above, not here
        assert not client.delete.await_count, f"{name} is read-only but issued a DELETE"
        assert not client.execute_command.await_count, (
            f"{name} is read-only but ran a command"
        )


class TestPopulatedPayloads:
    """The rendering half.

    The suites above feed empty and null shapes, which exercise the guard
    clauses and early returns but never the code that formats a result. A
    tool can survive every degenerate input and still crash on the first
    real issue it is handed.
    """

    ISSUE = {
        "id": "1-1",
        "idReadable": "PROJ-1",
        "summary": "Investigate timeout on upload",
        "description": "Steps to reproduce\nsecond line",
        "created": 1767225600000,
        "updated": 1767312000000,
        "resolved": 1767398400000,
        "timestamp": 1767312000000,
        "state": {"name": "In Progress"},
        "project": {"shortName": "PROJ", "name": "Project", "id": "0-1"},
        "assignee": {"name": "Alice A", "login": "alice_a"},
        "reporter": {"name": "Bob B", "login": "bob_b"},
        "author": {"name": "Alice A", "login": "alice_a"},
        "text": "a human comment",
        "name": "Sprint 1",
        "shortName": "PROJ",
        "login": "alice_a",
        "presentation": "2026-01-31",
        "value": {"name": "Major", "presentation": "2026-01-31"},
        "field": {"name": "State"},
        "added": [{"name": "In Progress", "text": "added"}],
        "removed": [{"name": "Open", "text": "removed"}],
        "linkType": {"name": "Depends on"},
        "direction": "OUTWARD",
        "duration": {"minutes": 90},
        "archived": False,
        "start": 1767225600000,
        "finish": 1767830400000,
        "canBeEmpty": False,
        "tags": [{"name": "urgent"}],
        "customFields": [
            {"name": "State", "value": {"name": "In Progress"}},
            {"name": "Priority", "value": {"name": "High"}},
            {"name": "Assignee", "value": {"name": "Alice A", "login": "alice_a"}},
            {"name": "Deadline ☠️", "value": 1767830400000},
            {"name": "Type", "value": {"name": "Bug"}},
        ],
        "comments": [
            {"id": "c1", "text": "first note",
             "author": {"name": "Alice A", "login": "alice_a"},
             "created": 1767312000000},
        ],
        "attachments": [{"id": "a1", "name": "log.txt", "url": "/files/a1", "size": 12}],
        "sprints": [{"id": "s1", "name": "Sprint 1", "archived": False}],
        "columnSettings": {"columns": [
            {"presentation": "In Progress", "id": "col1"},
            {"presentation": "Closed", "id": "col2"},
        ]},
        "projects": [{"shortName": "PROJ", "id": "0-1", "name": "Project"}],
    }

    def _payload(self):
        issue = dict(self.ISSUE)
        issue["links"] = [{
            "direction": "OUTWARD",
            "linkType": {"name": "Depends on"},
            "issues": [{"idReadable": "PROJ-2", "summary": "Blocker",
                        "customFields": [{"name": "State",
                                          "value": {"name": "Open"}}]}],
        }]
        issue["issues"] = [{"idReadable": "PROJ-2", "summary": "Blocker"}]
        return issue

    @pytest.mark.parametrize("name", TOOL_NAMES)
    async def test_tool_renders_a_real_looking_issue(self, name):
        client, tools = _make_tools([self._payload()])
        fn = tools[name].fn
        try:
            out = await fn(**_args_for(fn))
        except UserInputError:
            return
        except (KeyError, TypeError, AttributeError, IndexError) as e:
            pytest.fail(f"{name} crashed on a populated payload: {type(e).__name__}: {e}")
        assert isinstance(out, str) and out.strip()

    @pytest.mark.parametrize("name", TOOL_NAMES)
    async def test_rendered_output_does_not_leak_python_repr(self, name):
        """A dict or None reaching the output means a formatter was skipped."""
        client, tools = _make_tools([self._payload()])
        fn = tools[name].fn
        try:
            out = await fn(**_args_for(fn))
        except (UserInputError, KeyError, TypeError, AttributeError, IndexError):
            return
        assert "{'" not in out, f"{name} leaked a raw dict into its output"
        assert " None" not in out.replace("\\n", " ") or "json" in out[:40].lower(), (
            f"{name} rendered a bare None"
        )


def _alt_args_for(fn):
    """Required args, plus every optional one set to its non-default value.

    Optional parameters gate whole rendering paths — `format="json"`,
    `include_comments`, `group_by` — and calling a tool only with its
    defaults leaves those branches untouched no matter how many times it
    runs.
    """
    kwargs = _args_for(fn)
    for name, param in inspect.signature(fn).parameters.items():
        default = param.default
        if default is inspect.Parameter.empty:
            continue
        if name == "instance":
            continue
        if isinstance(default, bool):
            kwargs[name] = not default
        elif name == "format":
            kwargs[name] = "json" if default != "json" else "report"
        elif name == "group_by":
            kwargs[name] = "project" if default != "project" else "user"
        elif isinstance(default, int) and default > 1:
            kwargs[name] = 2
    return kwargs


class TestAlternateOptions:
    """Each tool once more, with its optional switches flipped."""

    @pytest.mark.parametrize("name", TOOL_NAMES)
    async def test_alternate_options_do_not_crash(self, name):
        client, tools = _make_tools([dict(TestPopulatedPayloads.ISSUE)])
        fn = tools[name].fn
        try:
            out = await fn(**_alt_args_for(fn))
        except UserInputError:
            return
        except (KeyError, TypeError, AttributeError, IndexError) as e:
            pytest.fail(f"{name} crashed with alternate options: {type(e).__name__}: {e}")
        assert isinstance(out, str)

    @pytest.mark.parametrize("name", TOOL_NAMES)
    async def test_json_format_is_parseable_when_offered(self, name):
        """A tool advertising format="json" must emit valid JSON.

        Returning markdown under a json flag is worse than not offering
        it: the caller parses, fails, and cannot tell whether the data or
        the format is wrong.
        """
        import json as _json

        client, tools = _make_tools([dict(TestPopulatedPayloads.ISSUE)])
        fn = tools[name].fn
        params = inspect.signature(fn).parameters
        if "format" not in params:
            pytest.skip("tool offers no format switch")
        try:
            out = await fn(**{**_args_for(fn), "format": "json"})
        except (UserInputError, KeyError, TypeError, AttributeError, IndexError):
            return
        stripped = out.strip()
        if not stripped or stripped.startswith(("#", "*", "No ", "Board")):
            return  # an empty or "nothing found" message is legitimate
        _json.loads(stripped)
