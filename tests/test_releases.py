"""Tests for get_release_calendar (ADR-039)."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from mcp.server.fastmcp import FastMCP

from yt_mcp.tools import releases
from yt_mcp.tools.releases import _is_release_ticket


def _ms(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def _ticket(iid, summary, state="In Progress", resolved=None, deadline=None, assignee="Alice A"):
    cfs = [
        {"name": "State", "value": {"name": state}},
        {"name": "Assignee", "value": [{"name": assignee}]},
    ]
    if deadline:
        cfs.append({"name": "Deadline ☠️", "value": deadline})
    return {
        "idReadable": iid, "summary": summary, "resolved": resolved,
        "updated": _ms(2026, 7, 27), "customFields": cfs,
    }


def _setup(open_release, open_rc=None, shipped_release=None, shipped_rc=None):
    client = MagicMock()
    client.get = AsyncMock(side_effect=[
        open_release, shipped_release or [], open_rc or [], shipped_rc or [],
    ])
    resolver = MagicMock()
    resolver.resolve = MagicMock(return_value=client)
    mcp = FastMCP("test")
    releases.register(mcp, resolver)
    return client, mcp._tool_manager._tools["get_release_calendar"].fn


class TestReleaseShapeFilter:
    def test_release_shapes_accepted(self):
        for s in ("Release 8.5.0", "release-3.5.0", "Release_lite",
                  "[Alpha] Release 1.1.7", "RC-5.5.3", "Release 2.10.96 (2.10.98)"):
            assert _is_release_ticket(s), s

    def test_mention_only_summaries_rejected(self):
        for s in ("Validate payment flows before release",
                  "Check remaining information after release",
                  "Job trigger_release_dev = failed",
                  "Logs are displayed in the release build",
                  "Automate release loading to BigQuery",
                  ""):
            assert not _is_release_ticket(s), s


class TestCalendar:
    async def test_imminent_states_listed_first(self):
        client, fn = _setup([
            _ticket("PROJ-1", "Release 1.0.1", state="In Progress"),
            _ticket("PROJ-2", "Release 1.0.0", state="Ready for release"),
        ])
        out = await fn()
        assert out.index("PROJ-2") < out.index("PROJ-1")

    async def test_cadence_median_and_eta_when_no_deadlines(self):
        client, fn = _setup(
            [_ticket("PROJ-5", "Release 1.3.0", state="In Progress")],
            shipped_release=[
                _ticket("PROJ-4", "Release 1.2.0", state="Closed", resolved=_ms(2026, 7, 26)),
                _ticket("PROJ-3", "Release 1.1.0", state="Closed", resolved=_ms(2026, 7, 19)),
                _ticket("PROJ-2", "Release 1.0.0", state="Closed", resolved=_ms(2026, 7, 12)),
            ],
        )
        out = await fn()
        assert "cadence: ~7d" in out
        assert "next ETA ~2026-08-02" in out   # last ship Jul 26 + 7d

    async def test_no_eta_when_deadline_exists(self):
        client, fn = _setup(
            [_ticket("PROJ-5", "Release 1.3.0", deadline=_ms(2026, 8, 5))],
            shipped_release=[
                _ticket("PROJ-4", "Release 1.2.0", state="Closed", resolved=_ms(2026, 7, 26)),
                _ticket("PROJ-3", "Release 1.1.0", state="Closed", resolved=_ms(2026, 7, 12)),
            ],
        )
        out = await fn()
        assert "deadline 2026-08-05" in out
        assert "next ETA" not in out

    async def test_parked_queue_flagged(self):
        client, fn = _setup([
            _ticket("PROJ-1", "Release 1.0.0", state="Ready for Store"),
            _ticket("PROJ-2", "Release 1.0.1", state="Ready for Store"),
            _ticket("PROJ-3", "RC-1.0.2", state="Store Review"),
        ])
        out = await fn()
        assert "⚠️ Flags" in out and "parked" in out

    async def test_empty_result_explains_itself(self):
        client, fn = _setup([])
        out = await fn(projects="PROJ")
        assert "No release/RC-titled tickets" in out
        assert "PROJ" in out

    async def test_resolved_date_clause_comes_first(self):
        client, fn = _setup([])
        await fn(lookback_days=30)
        shipped_queries = [
            c.kwargs["params"]["query"] for c in client.get.call_args_list
            if "resolved date:" in c.kwargs["params"]["query"]
        ]
        assert shipped_queries, "expected shipped-window queries"
        # Parser quirk: the range must be the FIRST clause (ADR-039).
        assert all(q.startswith("resolved date:") for q in shipped_queries)

    async def test_projects_filter(self):
        client, fn = _setup([
            _ticket("PROJ-1", "Release 1.0.0"),
            _ticket("OPS-1", "Release 2.0.0"),
        ])
        out = await fn(projects="PROJ")
        assert "PROJ-1" in out and "OPS-1" not in out
