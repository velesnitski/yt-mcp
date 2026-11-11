"""get_project_fields and conditional fields (ADR-067).

A field shown only for some values of another field ("Severity only when
Type = Bug") used to be reported as plainly required, so callers set it on
issues that cannot carry it and the whole command was rejected. These tests
pin the label, the `when` filter and the fallback for servers that predate
conditional fields.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from yt_mcp.tools import projects


def _pf(name, required=False, values=(), cond=None):
    f = {"field": {"name": name}, "canBeEmpty": not required}
    if values:
        f["bundle"] = {"values": [{"name": v} for v in values]}
    if cond:
        controller, shown_for, show_null = cond
        f["condition"] = {
            "field": {"field": {"name": controller}},
            "values": [{"name": v} for v in shown_for],
            "showForNullValue": show_null,
        }
    return f


FIELDS = [
    _pf("Type", required=True, values=("Bug", "Tech task", "Product task")),
    _pf("Priority", values=("Normal", "High")),
    _pf("Found in phase", required=True, values=("Dev QA", "Prod"),
        cond=("Type", ["Bug"], False)),
    _pf("Severity", values=("Minor", "Major"), cond=("Type", ["Bug", "Incident"], False)),
    _pf("Triage note", cond=("Type", [], True)),
]


def _tool(get):
    client = MagicMock()
    client.resolve_project_id = AsyncMock(return_value="0-1")
    client.get = AsyncMock(side_effect=get)
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    projects.register(mcp, resolver)
    return mcp._tool_manager._tools["get_project_fields"].fn, client


def _serving(fields=FIELDS):
    async def get(path, params=None):
        return fields
    return get


class TestLabels:
    @pytest.mark.asyncio
    async def test_conditional_fields_carry_their_condition(self):
        fn, _ = _tool(_serving())
        out = await fn(project="DEMO")
        assert "- **Type** **(required)**: Bug, Tech task, Product task" in out
        assert "- **Priority**: Normal, High" in out
        assert "- **Found in phase** **(required when Type = Bug)**: Dev QA, Prod" in out
        assert "- **Severity** **(only when Type = Bug or Incident)**: Minor, Major" in out
        assert "- **Triage note** **(only when Type = empty)**" in out
        # The bare marker never appears on a conditional field.
        assert "Found in phase** **(required)**" not in out

    @pytest.mark.asyncio
    async def test_condition_is_requested(self):
        fn, client = _tool(_serving())
        await fn(project="DEMO")
        assert "condition(" in client.get.call_args.kwargs["params"]["fields"]


class TestWhen:
    @pytest.mark.asyncio
    async def test_hides_fields_the_issue_cannot_carry(self):
        fn, _ = _tool(_serving())
        out = await fn(project="DEMO", when="Type=Tech task")
        assert "**Found in phase**" not in out and "**Severity**" not in out
        assert "- **Type** **(required)**" in out and "- **Priority**" in out
        assert "Not applicable when Type=Tech task (cannot be set): Found in phase, Severity, Triage note" in out

    @pytest.mark.asyncio
    async def test_keeps_fields_whose_condition_is_met(self):
        fn, _ = _tool(_serving())
        out = await fn(project="DEMO", when="type = bug")  # case/space-insensitive
        assert "**(required when Type = Bug)**" in out and "**Severity**" in out
        assert "Triage note" not in out.split("Not applicable", 1)[0]

    @pytest.mark.asyncio
    async def test_empty_value_meets_show_for_null(self):
        fn, _ = _tool(_serving())
        out = await fn(project="DEMO", when="Type=")
        assert "**Triage note**" in out and "Found in phase, Severity" in out

    @pytest.mark.asyncio
    async def test_unmentioned_controller_keeps_the_field(self):
        fn, _ = _tool(_serving())
        out = await fn(project="DEMO", when="Priority=High")
        assert "Not applicable" not in out and "**Severity**" in out

    @pytest.mark.asyncio
    async def test_malformed_when_is_explained(self):
        fn, _ = _tool(_serving())
        out = await fn(project="DEMO", when="Type Bug")
        assert "Field=Value pairs" in out


class TestOlderServers:
    @pytest.mark.asyncio
    async def test_selector_rejected_falls_back_to_plain_list(self):
        plain = [_pf("Type", required=True, values=("Bug",))]

        async def get(path, params=None):
            if "condition(" in params["fields"]:
                raise ValueError("YouTrack query error (400): unknown field condition")
            return plain

        fn, client = _tool(get)
        out = await fn(project="DEMO")
        assert "- **Type** **(required)**: Bug" in out
        assert "from existing issues" not in out  # not degraded to sampling
        assert client.get.await_count == 2
