"""Digest and creation-activity rendering (ADR-055).

`get_issues_digest` and `get_creation_activity` summarise change history,
and their rendering branches only run when the activity feed actually
contains changes. The cross-cutting suite reaches each tool once with a
uniform payload, which leaves the per-change-type formatting untouched —
the largest uncovered block left in this module.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from mcp.server.fastmcp import FastMCP

from yt_mcp.tools import monitoring

DAY = 86_400_000
# Anchored to the real clock: these tools compute their window from now,
# so a hard-coded timestamp puts every fixture outside it and the suite
# silently exercises the "nothing found" path instead of the renderer.
NOW = int(datetime.now(timezone.utc).timestamp() * 1000)


def _issue(iid="PROJ-1", summary="Upload times out", created=NOW - DAY, **kw):
    base = {
        "idReadable": iid,
        "summary": summary,
        "created": created,
        "updated": NOW,
        "resolved": None,
        "state": {"name": "In Progress"},
        "assignee": {"name": "Alice A", "login": "alice_a"},
        "reporter": {"name": "Bob B", "login": "bob_b"},
        "project": {"shortName": "PROJ", "name": "Project"},
        "customFields": [
            {"name": "State", "value": {"name": "In Progress"}},
            {"name": "Priority", "value": {"name": "High"}},
            {"name": "Type", "value": {"name": "Bug"}},
        ],
        "comments": [],
        "tags": [],
        "links": [],
    }
    base.update(kw)
    return base


def _activity(field="State", added="In Progress", removed="Open",
              author="Alice A", ts=NOW - 3600_000):
    return {
        "id": "a1",
        "timestamp": ts,
        "field": {"name": field},
        "author": {"name": author, "login": "alice_a"},
        "added": [{"name": added}] if added else [],
        "removed": [{"name": removed}] if removed else [],
    }


def _tools(route):
    """route(path) -> payload, so issues and activities differ."""
    client = MagicMock()
    client.get = AsyncMock(side_effect=lambda path, params=None, **kw: route(path))
    client.post = AsyncMock(return_value={})
    client.execute_command = AsyncMock(return_value={})
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    monitoring.register(mcp, resolver)
    return client, {n: t.fn for n, t in mcp._tool_manager._tools.items()}


class TestIssuesDigest:
    def _route(self, issues, activities):
        def route(path):
            if "/activities" in path:
                return activities
            return issues
        return route

    async def test_state_change_is_rendered(self):
        _, tools = _tools(self._route([_issue()], [_activity()]))
        out = await tools["get_issues_digest"](query="project: PROJ", since="7d")
        assert "PROJ-1" in out
        assert "Open" in out and "In Progress" in out

    async def test_quiet_period_says_so_rather_than_rendering_nothing(self):
        """An issue with no activity must be reported as unchanged, not
        silently dropped — the caller cannot tell those apart."""
        _, tools = _tools(self._route([_issue()], []))
        out = await tools["get_issues_digest"](query="project: PROJ", since="7d")
        assert "No changes" in out or "no changes" in out.lower()

    async def test_comment_activity_is_summarised(self):
        acts = [_activity(field="comments", added="a new comment", removed=None)]
        _, tools = _tools(self._route([_issue()], acts))
        out = await tools["get_issues_digest"](query="project: PROJ", since="7d")
        assert isinstance(out, str) and "PROJ-1" in out

    async def test_multiple_field_changes_all_appear(self):
        acts = [
            _activity(field="State", added="Closed", removed="In Progress"),
            _activity(field="Priority", added="Critical", removed="High"),
        ]
        _, tools = _tools(self._route([_issue()], acts))
        out = await tools["get_issues_digest"](query="project: PROJ", since="7d")
        assert "Closed" in out
        assert "Critical" in out or "Priority" in out

    async def test_empty_result_set_is_explained(self):
        _, tools = _tools(self._route([], []))
        out = await tools["get_issues_digest"](query="project: NONE", since="7d")
        assert out.strip()
        assert "PROJ-1" not in out


class TestCreationActivity:
    async def test_recent_creations_are_listed_newest_first(self):
        issues = [
            _issue("PROJ-1", "older", created=NOW - 5 * DAY),
            _issue("PROJ-2", "newer", created=NOW - 1 * DAY),
        ]
        _, tools = _tools(lambda path: issues)
        out = await tools["get_creation_activity"](project="PROJ", since="7d")
        assert out.index("PROJ-2") < out.index("PROJ-1"), "newest must lead"

    async def test_limit_is_respected(self):
        issues = [_issue(f"PROJ-{i}", f"s{i}", created=NOW - i * 3600_000)
                  for i in range(1, 12)]
        _, tools = _tools(lambda path: issues)
        out = await tools["get_creation_activity"](project="PROJ", since="7d", limit=3)
        listed = sum(1 for i in range(1, 12) if f"PROJ-{i}" in out)
        assert listed <= 3, f"limit ignored — {listed} issues rendered"

    async def test_nothing_created_is_stated(self):
        _, tools = _tools(lambda path: [])
        out = await tools["get_creation_activity"](project="PROJ", since="7d")
        assert out.strip()

    async def test_reporter_breakdown_appears(self):
        issues = [
            _issue("PROJ-1", "a", reporter={"name": "Alice A", "login": "alice_a"}),
            _issue("PROJ-2", "b", reporter={"name": "Bob B", "login": "bob_b"}),
        ]
        _, tools = _tools(lambda path: issues)
        out = await tools["get_creation_activity"](project="PROJ", since="7d")
        assert "Alice A" in out or "Bob B" in out
